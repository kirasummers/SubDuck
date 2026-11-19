import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from PySide6.QtCore import QObject, Signal

from subduck.models import VideoFileInfo, CensorConfig, SubtitleEntry
from subduck.matcher import calculate_merged_mute_intervals, censor_text_line, scan_subtitles_for_triggers
from subduck.sub_parser import serialize_srt
from subduck.whisper_engine import transcribe_video_to_subtitles, is_whisper_available

LOG_FILE = Path.home() / ".config" / "subduck" / "subduck.log"
ARTWORK_EXTS = {".jpg", ".jpeg", ".png", ".nfo", ".xml"}


def append_log(msg: str):
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(msg + "\n")


def _cleanup_partial_file(path: Path, only_if_empty: bool = False):
    if not path.exists():
        return
    if only_if_empty and path.stat().st_size > 0:
        return
    path.unlink(missing_ok=True)


def get_default_season_mirror_dir(source_dir: Path) -> Path:
    # Keep parallel show folder at top level so Jellyfin doesn't merge seasons
    folder_name = source_dir.name
    if re.search(r"^(season|series|s)\s*\d+", folder_name, re.I):
        show_dir = source_dir.parent
        lib_dir = show_dir.parent
        if lib_dir and lib_dir != show_dir:
            return lib_dir / f"{show_dir.name} [Trigger-Clean]" / folder_name
    return source_dir.parent / f"{folder_name} [Trigger-Clean]"


def build_output_paths(video_path: Path, config: CensorConfig) -> tuple[Path, Path]:
    mode = config.output_mode

    if mode == "season_mirror":
        dest_dir = (
            Path(config.season_mirror_folder)
            if config.season_mirror_folder
            else get_default_season_mirror_dir(video_path.parent)
        )
        dest_dir.mkdir(parents=True, exist_ok=True)
        return dest_dir / video_path.name, dest_dir / f"{video_path.stem}.srt"

    if mode == "inplace_replace":
        temp_out = video_path.parent / f".tmp_{video_path.stem}{video_path.suffix}"
        return temp_out, video_path.parent / f"{video_path.stem}.srt"

    if mode == "dual_audio":
        suffix = config.output_suffix or " dual"
        dest_dir = video_path.parent
        return (
            dest_dir / f"{video_path.stem}{suffix}{video_path.suffix}",
            dest_dir / f"{video_path.stem}{suffix}.srt"
        )

    dest_dir = video_path.parent
    if config.output_folder:
        p = Path(config.output_folder)
        if p.is_dir():
            dest_dir = p
    suffix = config.output_suffix or " trigger"
    return (
        dest_dir / f"{video_path.stem}{suffix}{video_path.suffix}",
        dest_dir / f"{video_path.stem}{suffix}.srt"
    )


def create_censored_subtitles(entries: list[SubtitleEntry], config: CensorConfig):
    censored_entries = []
    for entry in entries:
        censored_text, _ = censor_text_line(
            entry.text,
            config.trigger_words,
            config.censor_style,
            config.strict_matching
        )
        censored_entries.append(SubtitleEntry(
            index=entry.index,
            start_sec=entry.start_sec,
            end_sec=entry.end_sec,
            text=censored_text,
            word_timings=list(entry.word_timings)
        ))
    return censored_entries, serialize_srt(censored_entries)


def get_container_subtitle_codec(ext: str) -> str | None:
    ext_clean = ext.lower().strip()
    if ext_clean in {".mp4", ".m4v", ".mov"}:
        return "mov_text"
    if ext_clean == ".mkv":
        return "srt"
    if ext_clean == ".webm":
        return "webvtt"
    return None


def safe_copy_file(
    src: Path,
    dst: Path,
    progress_callback=None,
    cancel_check=None,
    chunk_size: int = 4 * 1024 * 1024
) -> bool:
    total_size = src.stat().st_size if src.exists() else 0

    if not progress_callback and not cancel_check:
        shutil.copyfile(src, dst)
    else:
        copied = 0
        last_pct = -1
        with open(src, "rb") as fsrc, open(dst, "wb") as fdst:
            while True:
                if cancel_check and cancel_check():
                    fdst.close()
                    _cleanup_partial_file(dst)
                    return False
                chunk = fsrc.read(chunk_size)
                if not chunk:
                    break
                fdst.write(chunk)
                copied += len(chunk)
                if total_size > 0 and progress_callback:
                    pct = int((copied / total_size) * 100)
                    if pct != last_pct:
                        last_pct = pct
                        progress_callback(copied, total_size)

        if progress_callback:
            progress_callback(copied, total_size)

    # copystat throws ENOTTY (errno 25) on kio-fuse smb mounts
    try:
        shutil.copystat(src, dst)
    except OSError:
        pass
    return True


def copy_artwork_and_metadata(source_dir: Path, target_dir: Path, log_fn=None):
    if not source_dir.is_dir() or not target_dir.is_dir():
        return
    if re.search(r"^(season|series|s)\s*\d+", source_dir.name, re.I):
        show_src = source_dir.parent
        show_dest = target_dir.parent
        if show_src.is_dir() and show_dest.is_dir():
            for item in show_src.iterdir():
                if item.is_file() and item.suffix.lower() in ARTWORK_EXTS:
                    dest = show_dest / item.name
                    if not dest.exists():
                        safe_copy_file(item, dest)
                        if log_fn:
                            log_fn(f"Copied series artwork: {item.name}")

    for item in source_dir.iterdir():
        if item.is_file() and item.suffix.lower() in ARTWORK_EXTS:
            dest = target_dir / item.name
            if not dest.exists():
                safe_copy_file(item, dest)
                if log_fn:
                    log_fn(f"Copied artwork: {item.name}")


def process_single_video(
    info: VideoFileInfo,
    config: CensorConfig,
    progress_callback=None,
    copy_progress_callback=None,
    cancel_check=None,
    worker=None,
    log_callback=None
) -> tuple[bool, str]:
    def log(msg: str):
        append_log(msg)
        if log_callback:
            log_callback(msg)

    needs_whisper = (
        config.whisper_mode == "always"
        or (config.whisper_mode == "if_missing" and not info.has_subtitles)
    )

    if needs_whisper and is_whisper_available():
        log(f"Generating subtitles using Whisper ({config.whisper_model}) on {info.filename}...")
        try:
            whisper_entries, raw_srt = transcribe_video_to_subtitles(
                info.path,
                model_size=config.whisper_model,
                device=config.whisper_device,
                progress_callback=progress_callback,
                cancel_check=cancel_check,
                log_callback=log
            )
            if cancel_check and cancel_check():
                return False, "Cancelled by user"

            info.subtitle_entries = whisper_entries
            info.has_subtitles = len(whisper_entries) > 0
            info.whisper_generated = True
            info.subtitle_source = f"Whisper ({config.whisper_model})"

            if config.whisper_save_to_source and raw_srt:
                src_srt = info.path.parent / f"{info.path.stem}.srt"
                if not src_srt.exists():
                    src_srt.write_text(raw_srt, encoding="utf-8")
                    log(f"Saved generated subtitle to source folder: {src_srt.name}")

            if config.trigger_words:
                info.matches = scan_subtitles_for_triggers(whisper_entries, config)
                if info.matches:
                    info.status = f"Flagged ({info.trigger_count})"
                    log(f"Whisper transcription detected {len(info.matches)} trigger instance(s).")
                else:
                    info.status = "Clean"
                    log("Whisper transcription clean: 0 trigger words detected.")
        except RuntimeError as e:
            log(f"Whisper transcription error: {e}")
            if not info.has_subtitles:
                return False, f"Whisper transcription failed: {e}"

    if not info.matches and config.output_mode == "season_mirror":
        out_video_path, out_sub_path = build_output_paths(info.path, config)
        if not out_video_path.exists() or out_video_path.stat().st_size != info.file_size:
            log(f"Copying clean episode to mirror folder: {info.filename}")

            def on_copy_chunk(fname, copied, total):
                if copy_progress_callback:
                    copy_progress_callback(fname, copied, total)

            ok_copy = safe_copy_file(
                info.path,
                out_video_path,
                progress_callback=lambda c, t: on_copy_chunk(info.filename, c, t),
                cancel_check=cancel_check
            )
            if not ok_copy or (cancel_check and cancel_check()):
                return False, "Cancelled by user"
        else:
            log(f"Clean episode already present in mirror folder: {info.filename}")

        if info.whisper_generated and info.subtitle_entries and config.whisper_save_srt:
            raw_srt = serialize_srt(info.subtitle_entries)
            out_sub_path.write_text(raw_srt, encoding="utf-8")
            log(f"Saved generated subtitle to mirror folder: {out_sub_path.name}")

        has_exact_srt = out_sub_path.exists()
        for s_file in info.path.parent.iterdir():
            if s_file.is_file() and s_file.name.startswith(info.path.stem) and s_file.suffix.lower() in {".srt", ".vtt"}:
                dest_sub = out_video_path.parent / s_file.name
                if not dest_sub.exists():
                    safe_copy_file(s_file, dest_sub)
                    log(f"Copied subtitle: {s_file.name}")
                if s_file.name == f"{out_video_path.stem}.srt":
                    has_exact_srt = True

        if not has_exact_srt:
            en_sub = info.path.parent / f"{info.path.stem}.en.srt"
            std_sub = out_video_path.parent / f"{out_video_path.stem}.srt"
            if en_sub.is_file() and not std_sub.exists():
                safe_copy_file(en_sub, std_sub)
                log(f"Normalized English subtitle for player compatibility: {std_sub.name}")

        info.output_path = out_video_path
        return True, "Clean episode copied to season mirror."

    if not info.matches:
        log(f"No triggers found in {info.filename}. Skipping.")
        return True, "No triggers found."

    out_video_path, out_sub_path = build_output_paths(info.path, config)
    log(f"Processing: {info.filename} -> {out_video_path.name}")

    _, srt_content = create_censored_subtitles(info.subtitle_entries, config)

    if config.save_srt or config.whisper_save_srt:
        out_sub_path.write_text(srt_content, encoding="utf-8")
        log(f"Saved external censored subtitle: {out_sub_path.name}")

    merged_intervals = calculate_merged_mute_intervals(
        info.matches,
        config.safety_before_ms,
        config.safety_after_ms,
        strict=config.strict_matching
    )
    log(f"Calculated {len(merged_intervals)} audio mute interval(s).")

    duck_val = config.ducking_level
    between_expr = "+".join(f"between(t,{s:.3f},{e:.3f})" for s, e in merged_intervals)
    af_filter = f"volume=enable='{between_expr}':volume={duck_val}:eval=frame"

    temp_srt = None
    sub_codec = get_container_subtitle_codec(out_video_path.suffix)
    can_embed = config.embed_sub and (sub_codec is not None)

    try:
        with tempfile.NamedTemporaryFile("w", suffix=".srt", delete=False, encoding="utf-8") as tf:
            tf.write(srt_content)
            temp_srt = tf.name

        def build_cmd(embed_sub: bool) -> list[str]:
            cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-y", "-i", str(info.path)]
            if embed_sub and temp_srt:
                cmd.extend(["-i", temp_srt])

            if config.output_mode == "dual_audio":
                cmd.extend([
                    "-filter_complex", f"[0:a:0]{af_filter}[aduck]",
                    "-c:v", "copy",
                    "-map", "0:v:0",
                    "-map", "0:a:0", "-c:a:0", "copy",
                    "-metadata:s:a:0", "title=Original Audio", "-disposition:a:0", "0",
                    "-map", "[aduck]", "-c:a:1", "aac", "-b:a", "256k",
                    "-metadata:s:a:1", "title=Trigger-Muted", "-disposition:a:1", "default"
                ])
                if embed_sub and temp_srt:
                    cmd.extend([
                        "-map", "1:s:0", "-c:s", sub_codec,
                        "-metadata:s:s:0", "title=Censored English", "-disposition:s:0", "default"
                    ])
            else:
                cmd.extend(["-c:v", "copy", "-af", af_filter, "-c:a", "aac", "-b:a", "256k"])
                if embed_sub and temp_srt:
                    cmd.extend([
                        "-map", "0:v:0",
                        "-map", "0:a:0",
                        "-map", "1:s:0",
                        "-c:s", sub_codec,
                        "-metadata:s:s:0", "title=Censored English",
                        "-metadata:s:s:0", "language=eng"
                    ])
                else:
                    cmd.extend(["-map", "0:v:0", "-map", "0:a:0"])

            cmd.extend(["-map_chapters", "0", "-map_metadata", "0", str(out_video_path)])
            return cmd

        def run_proc(command: list[str]):
            p = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if worker:
                worker._active_proc = p
            stdout, stderr = p.communicate()
            if worker:
                worker._active_proc = None
            return p.returncode, stdout, stderr

        cmd = build_cmd(can_embed)
        log(f"Running FFmpeg: {' '.join(cmd)}")
        rc, _, err = run_proc(cmd)

        if cancel_check and cancel_check():
            _cleanup_partial_file(out_video_path)
            return False, "Cancelled by user"

        if rc != 0 and can_embed:
            log(f"Embedding subtitle rejected by container ({err[-150:].strip()}). Retrying without embedding...")
            _cleanup_partial_file(out_video_path, only_if_empty=True)
            rc, _, err = run_proc(build_cmd(False))

            if cancel_check and cancel_check():
                _cleanup_partial_file(out_video_path)
                return False, "Cancelled by user"

        if rc != 0:
            err_tail = err[-400:].strip()
            log(f"FFmpeg error:\n{err_tail}")
            _cleanup_partial_file(out_video_path, only_if_empty=True)
            return False, f"FFmpeg failed: {err_tail.splitlines()[-1] if err_tail else 'Unknown error'}"

        if config.output_mode == "inplace_replace":
            final_target = info.path
            if config.backup_originals:
                backup_dir = info.path.parent / "_originals"
                backup_dir.mkdir(parents=True, exist_ok=True)
                backup_file = backup_dir / info.path.name
                shutil.move(info.path, backup_file)
                log(f"Backed up original to: {backup_file}")
            else:
                info.path.unlink(missing_ok=True)
            shutil.move(out_video_path, final_target)
            info.output_path = final_target
        else:
            info.output_path = out_video_path

        log(f"Completed {info.filename}")
        return True, "Success"

    finally:
        if temp_srt:
            Path(temp_srt).unlink(missing_ok=True)


class BatchProcessWorker(QObject):
    file_started = Signal(str, int, int)
    file_finished = Signal(str, bool, str, int, int)
    all_finished = Signal(int, int, list)
    log_message = Signal(str)
    whisper_progress = Signal(int, float, float)
    copy_progress = Signal(str, int, int, int)
    status_update = Signal(str)

    def __init__(self, video_items, all_scanned_items, config: CensorConfig, source_dir: Path, process_all: bool = False):
        super().__init__()
        self.video_items = video_items
        self.all_scanned_items = all_scanned_items
        self.config = config
        self.source_dir = source_dir
        self.process_all = process_all
        self._is_cancelled = False
        self._active_proc = None

    def cancel(self):
        self._is_cancelled = True
        if self._active_proc is not None:
            self._active_proc.terminate()

    def run(self):
        success_count = 0
        fail_count = 0
        error_list = []

        if self.config.output_mode == "season_mirror":
            self.status_update.emit("Preparing mirror directory...")
            mirror_dir = (
                Path(self.config.season_mirror_folder)
                if self.config.season_mirror_folder
                else get_default_season_mirror_dir(self.source_dir)
            )
            mirror_dir.mkdir(parents=True, exist_ok=True)
            self.log_message.emit(f"Mirror directory: {mirror_dir}")
            if self.config.copy_artwork and self.process_all:
                self.status_update.emit("Copying posters, artwork, and metadata...")
                copy_artwork_and_metadata(self.source_dir, mirror_dir, log_fn=self.log_message.emit)

        items_to_process = self.all_scanned_items if self.process_all else self.video_items
        total_items = len(items_to_process)

        for idx, item in enumerate(items_to_process, 1):
            if self._is_cancelled:
                self.log_message.emit("Batch processing cancelled by user.")
                break

            self.file_started.emit(item.filename, idx, total_items)

            def on_copy(fname: str, copied: int, tot: int):
                pct = int((copied / tot) * 100) if tot > 0 else 0
                self.copy_progress.emit(fname, copied, tot, pct)

            ok, msg = process_single_video(
                item,
                self.config,
                progress_callback=self.whisper_progress.emit,
                copy_progress_callback=on_copy,
                cancel_check=lambda: self._is_cancelled,
                worker=self,
                log_callback=self.log_message.emit
            )

            if ok:
                success_count += 1
                item.status = "Done"
            else:
                fail_count += 1
                item.status = "Error"
                item.error_msg = msg
                error_list.append(f"{item.filename}: {msg}")

            self.file_finished.emit(item.filename, ok, msg, idx, total_items)

        self.all_finished.emit(success_count, fail_count, error_list)
