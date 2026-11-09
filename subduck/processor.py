import os
import re
import shutil
import tempfile
import subprocess
from pathlib import Path
from typing import List, Tuple, Optional
from PySide6.QtCore import QObject, Signal

from subduck.models import VideoFileInfo, CensorConfig, SubtitleEntry
from subduck.matcher import calculate_merged_mute_intervals, censor_text_line, scan_subtitles_for_triggers
from subduck.sub_parser import serialize_srt
from subduck.whisper_engine import transcribe_video_to_subtitles, is_whisper_available

LOG_FILE = Path.home() / ".config" / "subduck" / "subduck.log"
ARTWORK_EXTS = {".jpg", ".jpeg", ".png", ".nfo", ".xml"}

def append_log(msg: str):
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:
        pass

def get_default_season_mirror_dir(source_dir: Path) -> Path:
    """Generate default mirror folder name: e.g. 'Season 1 [Trigger-Clean]'."""
    folder_name = source_dir.name
    return source_dir.parent / f"{folder_name} [Trigger-Clean]"

def build_output_paths(video_path: Path, config: CensorConfig) -> Tuple[Path, Path]:
    """Calculate the destination path for the video and subtitle files based on output mode."""
    mode = config.output_mode

    if mode == "season_mirror":
        dest_dir = Path(config.season_mirror_folder) if config.season_mirror_folder else get_default_season_mirror_dir(video_path.parent)
        dest_dir.mkdir(parents=True, exist_ok=True)
        # Inside the mirror folder, keep clean original filenames for seamless Jellyfin autoplay
        return dest_dir / video_path.name, dest_dir / f"{video_path.stem}.srt"

    elif mode == "inplace_replace":
        # Temporary output file before in-place replace
        temp_out = video_path.parent / f".tmp_{video_path.stem}{video_path.suffix}"
        return temp_out, video_path.parent / f"{video_path.stem}.srt"

    elif mode == "dual_audio":
        suffix = config.output_suffix or " dual"
        dest_dir = video_path.parent
        return dest_dir / f"{video_path.stem}{suffix}{video_path.suffix}", dest_dir / f"{video_path.stem}{suffix}.srt"

    else:  # separate_suffix
        dest_dir = video_path.parent
        if config.output_folder:
            p = Path(config.output_folder)
            if p.is_dir():
                dest_dir = p
        suffix = config.output_suffix or " trigger"
        return dest_dir / f"{video_path.stem}{suffix}{video_path.suffix}", dest_dir / f"{video_path.stem}{suffix}.srt"

def create_censored_subtitles(
    entries: List[SubtitleEntry],
    config: CensorConfig
) -> Tuple[List[SubtitleEntry], str]:
    """Generate modified subtitle entries with trigger words censored."""
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
            text=censored_text
        ))
    srt_str = serialize_srt(censored_entries)
    return censored_entries, srt_str

def get_container_subtitle_codec(ext: str) -> Optional[str]:
    """Determine the compatible subtitle codec for the target container format."""
    ext_clean = ext.lower().strip()
    if ext_clean in [".mp4", ".m4v", ".mov"]:
        return "mov_text"
    elif ext_clean in [".mkv"]:
        return "srt"
    elif ext_clean in [".webm"]:
        return "webvtt"
    return None

def safe_copy_file(src: Path, dst: Path):
    """
    Safely copy a file, working around FUSE/SMB limitations where
    copystat (e.g. utime, xattrs) raises Errno 25 (Inappropriate ioctl for device).
    """
    shutil.copyfile(src, dst)
    try:
        shutil.copystat(src, dst)
    except OSError:
        pass

def copy_artwork_and_metadata(source_dir: Path, target_dir: Path, log_fn=None):
    """Copy thumbnails, posters, and NFO metadata to target directory."""
    if not source_dir.is_dir() or not target_dir.is_dir():
        return
    try:
        for item in source_dir.iterdir():
            if item.is_file() and item.suffix.lower() in ARTWORK_EXTS:
                dest = target_dir / item.name
                if not dest.exists():
                    safe_copy_file(item, dest)
                    if log_fn:
                        log_fn(f"Copied artwork: {item.name}")
    except Exception as e:
        if log_fn:
            log_fn(f"Artwork copy notice: {e}")

def process_single_video(
    info: VideoFileInfo,
    config: CensorConfig,
    progress_callback=None,
    log_callback=None
) -> Tuple[bool, str]:
    """
    Process a single video according to the configured output mode.
    If subtitles are missing or bitmap-only, automatically transcribes via Whisper if enabled.
    """
    def log(msg: str):
        append_log(msg)
        if log_callback:
            log_callback(msg)

    # Automatic Whisper Subtitle Transcription if needed
    needs_whisper = (
        config.whisper_mode == "always" or
        (config.whisper_mode == "if_missing" and not info.has_subtitles)
    )

    if needs_whisper and is_whisper_available():
        log(f"Generating subtitles using Whisper AI ({config.whisper_model}) on {info.filename}...")
        try:
            whisper_entries, raw_srt = transcribe_video_to_subtitles(
                info.path,
                model_size=config.whisper_model,
                device=config.whisper_device,
                progress_callback=progress_callback,
                log_callback=log
            )
            info.subtitle_entries = whisper_entries
            info.has_subtitles = len(whisper_entries) > 0
            info.whisper_generated = True
            info.subtitle_source = f"Whisper AI ({config.whisper_model})"

            # Try saving raw generated SRT next to original if configured
            if config.whisper_save_to_source and raw_srt:
                try:
                    src_srt = info.path.parent / f"{info.path.stem}.srt"
                    if not src_srt.exists():
                        src_srt.write_text(raw_srt, encoding="utf-8")
                        log(f"Saved generated subtitle to source folder: {src_srt.name}")
                except Exception:
                    pass

            # Scan newly generated subtitles for triggers
            if config.trigger_words:
                info.matches = scan_subtitles_for_triggers(whisper_entries, config)
                if info.matches:
                    info.status = f"Flagged ({info.trigger_count})"
                    log(f"Whisper transcription detected {len(info.matches)} trigger instance(s).")
                else:
                    info.status = "Clean"
                    log("Whisper transcription clean: 0 trigger words detected.")
        except Exception as e:
            log(f"Whisper AI transcription error: {e}")
            if not info.has_subtitles:
                return False, f"Whisper AI transcription failed: {e}"

    # In season mirror mode, if this file has NO triggers, copy unmodified
    if not info.matches and config.output_mode == "season_mirror":
        out_video_path, out_sub_path = build_output_paths(info.path, config)
        if not out_video_path.exists() or out_video_path.stat().st_size != info.file_size:
            log(f"Copying clean episode to mirror folder: {info.filename}")
            try:
                safe_copy_file(info.path, out_video_path)
            except Exception as e:
                log(f"Error copying clean episode: {e}")
                return False, str(e)
        else:
            log(f"Clean episode already present in mirror folder: {info.filename}")

        # If Whisper generated subtitles for this clean episode, save them to the mirror folder
        if info.whisper_generated and info.subtitle_entries and config.whisper_save_srt:
            try:
                raw_srt = serialize_srt(info.subtitle_entries)
                out_sub_path.write_text(raw_srt, encoding="utf-8")
                log(f"Saved generated subtitle to mirror folder: {out_sub_path.name}")
            except Exception as e:
                log(f"Notice saving mirror subtitle: {e}")

        # Ensure all existing associated subtitle files are copied to the mirror folder
        try:
            has_exact_srt = out_sub_path.exists()
            for s_file in info.path.parent.iterdir():
                if s_file.is_file() and s_file.name.startswith(info.path.stem) and s_file.suffix.lower() in {".srt", ".vtt"}:
                    dest_sub = out_video_path.parent / s_file.name
                    if not dest_sub.exists():
                        safe_copy_file(s_file, dest_sub)
                        log(f"Copied subtitle: {s_file.name}")
                    if s_file.name == f"{out_video_path.stem}.srt":
                        has_exact_srt = True

            # If an English subtitle exists (e.g. .en.srt) but no exact .srt, copy as .srt for default player recognition
            if not has_exact_srt:
                en_sub = info.path.parent / f"{info.path.stem}.en.srt"
                std_sub = out_video_path.parent / f"{out_video_path.stem}.srt"
                if en_sub.is_file() and not std_sub.exists():
                    safe_copy_file(en_sub, std_sub)
                    log(f"Normalized English subtitle for player compatibility: {std_sub.name}")
        except Exception as e:
            log(f"Subtitle copy notice: {e}")

        info.output_path = out_video_path
        return True, "Clean episode copied to season mirror."

    if not info.matches:
        log(f"No triggers found in {info.filename}. Skipping.")
        return True, "No triggers found."

    out_video_path, out_sub_path = build_output_paths(info.path, config)
    log(f"Processing: {info.filename} -> {out_video_path.name}")

    # Generate censored subtitle
    censored_entries, srt_content = create_censored_subtitles(info.subtitle_entries, config)

    # Save external .srt file if enabled
    if config.save_srt or config.whisper_save_srt:
        try:
            out_sub_path.write_text(srt_content, encoding="utf-8")
            log(f"Saved external censored subtitle: {out_sub_path.name}")
        except Exception as e:
            log(f"Warning: Failed to write external subtitle: {e}")

    # Calculate audio mute intervals
    merged_intervals = calculate_merged_mute_intervals(
        info.matches,
        config.safety_before_ms,
        config.safety_after_ms
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

        def build_cmd(embed_sub: bool) -> List[str]:
            cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-y", "-i", str(info.path)]
            if embed_sub and temp_srt:
                cmd.extend(["-i", temp_srt])

            if config.output_mode == "dual_audio":
                # Dual Audio: Track 1 = Original, Track 2 = Ducked (Default)
                cmd.extend([
                    "-filter_complex", f"[0:a:0]{af_filter}[aduck]",
                    "-c:v", "copy",
                    "-map", "0:v:0",
                    "-map", "0:a:0", "-c:a:0", "copy", "-metadata:s:a:0", "title=Original Audio", "-disposition:a:0", "0",
                    "-map", "[aduck]", "-c:a:1", "aac", "-b:a", "256k", "-metadata:s:a:1", "title=Trigger-Muted", "-disposition:a:1", "default"
                ])
                if embed_sub and temp_srt:
                    cmd.extend([
                        "-map", "1:s:0", "-c:s", sub_codec,
                        "-metadata:s:s:0", "title=Censored English", "-disposition:s:0", "default"
                    ])
            else:
                # Standard Ducked Audio Output
                cmd.extend(["-c:v", "copy"])
                cmd.extend(["-af", af_filter, "-c:a", "aac", "-b:a", "256k"])
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

        # Try with embedded subtitles
        cmd = build_cmd(can_embed)
        log(f"Running FFmpeg: {' '.join(cmd)}")
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        # Fallback without subtitle embedding if container rejected codec
        if p.returncode != 0 and can_embed:
            log(f"Embedding subtitle rejected by container ({p.stderr[-150:].strip()}). Retrying without embedding...")
            if out_video_path.exists() and out_video_path.stat().st_size == 0:
                try:
                    out_video_path.unlink()
                except Exception:
                    pass
            cmd_fallback = build_cmd(False)
            p = subprocess.run(cmd_fallback, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        if p.returncode != 0:
            err_tail = p.stderr[-400:].strip()
            log(f"FFmpeg error:\n{err_tail}")
            if out_video_path.exists() and out_video_path.stat().st_size == 0:
                try:
                    out_video_path.unlink()
                except Exception:
                    pass
            return False, f"FFmpeg failed: {err_tail.splitlines()[-1] if err_tail else 'Unknown error'}"

        # Handle in-place replacement backup
        if config.output_mode == "inplace_replace":
            final_target = info.path
            if config.backup_originals:
                backup_dir = info.path.parent / "_originals"
                backup_dir.mkdir(parents=True, exist_ok=True)
                backup_file = backup_dir / info.path.name
                shutil.move(info.path, backup_file)
                log(f"Backed up original to: {backup_file}")
            else:
                info.path.unlink()
            shutil.move(out_video_path, final_target)
            info.output_path = final_target
        else:
            info.output_path = out_video_path

        log(f"SUCCESS: Completed {info.filename}")
        return True, "Success"

    except Exception as e:
        log(f"Exception during processing: {e}")
        if out_video_path.exists() and out_video_path.stat().st_size == 0:
            try:
                out_video_path.unlink()
            except Exception:
                pass
        return False, str(e)
    finally:
        if temp_srt and os.path.exists(temp_srt):
            try:
                os.remove(temp_srt)
            except Exception:
                pass

class BatchProcessWorker(QObject):
    file_started = Signal(str, int, int)
    file_finished = Signal(str, bool, str, int, int)
    all_finished = Signal(int, int, list)
    log_message = Signal(str)
    whisper_progress = Signal(int, float, float)

    def __init__(self, video_items: List[VideoFileInfo], all_scanned_items: List[VideoFileInfo], config: CensorConfig, source_dir: Path):
        super().__init__()
        self.video_items = video_items
        self.all_scanned_items = all_scanned_items
        self.config = config
        self.source_dir = source_dir
        self._is_cancelled = False

    def cancel(self):
        self._is_cancelled = True

    def run(self):
        success_count = 0
        fail_count = 0
        error_list = []

        # If Season Mirror Mode, ensure the mirror destination exists and copy artwork first
        if self.config.output_mode == "season_mirror":
            mirror_dir = Path(self.config.season_mirror_folder) if self.config.season_mirror_folder else get_default_season_mirror_dir(self.source_dir)
            mirror_dir.mkdir(parents=True, exist_ok=True)
            self.log_message.emit(f"Season Mirror Directory: {mirror_dir}")
            if self.config.copy_artwork:
                copy_artwork_and_metadata(self.source_dir, mirror_dir, log_fn=lambda m: self.log_message.emit(m))

            items_to_process = self.all_scanned_items
        else:
            items_to_process = self.video_items

        total_items = len(items_to_process)
        for idx, item in enumerate(items_to_process, 1):
            if self._is_cancelled:
                self.log_message.emit("Batch processing cancelled by user.")
                break

            self.file_started.emit(item.filename, idx, total_items)
            ok, msg = process_single_video(
                item,
                self.config,
                progress_callback=lambda pct, cur, tot: self.whisper_progress.emit(pct, cur, tot),
                log_callback=lambda m: self.log_message.emit(m)
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
