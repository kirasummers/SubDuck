import json
import re
import shutil
import subprocess
from pathlib import Path
from subduck.models import SubtitleEntry, srt_time_to_sec, sec_to_srt_time

SRT_BLOCK_RE = re.compile(
    r"(?:^|\n\n+)(?P<idx>\d+)\r?\n"
    r"(?P<start>\d{1,2}:\d{2}:\d{2}[,\.]\d{1,3})\s*-->\s*"
    r"(?P<end>\d{1,2}:\d{2}:\d{2}[,\.]\d{1,3})"
    r"(?:[^\r\n]*)\r?\n"
    r"(?P<text>[\s\S]*?)(?=(?:\r?\n\r?\n\d+\r?\n|\Z))",
    re.MULTILINE
)

BITMAP_CODECS = {"hdmv_pgs_subtitle", "dvb_subtitle", "dvd_subtitle", "pgssub"}
TEXT_CODECS = {"subrip", "srt", "mov_text", "ass", "webvtt", "text"}


def parse_srt_content(content: str) -> list[SubtitleEntry]:
    entries = []
    body = re.sub(r"^WEBVTT[^\n]*\n+", "", content.strip())

    for m in SRT_BLOCK_RE.finditer(body):
        raw_text = m.group("text").strip()
        # TODO: preserve basic italic tags when burning or remuxing ASS tracks
        clean_text = re.sub(r"<[^>]+>", "", raw_text)
        try:
            start_sec = srt_time_to_sec(m.group("start"))
            end_sec = srt_time_to_sec(m.group("end"))
        except ValueError:
            continue

        entries.append(SubtitleEntry(
            index=int(m.group("idx")),
            start_sec=start_sec,
            end_sec=end_sec,
            text=clean_text,
            original_block=m.group(0).strip()
        ))

    return entries


def serialize_srt(entries: list[SubtitleEntry]) -> str:
    out = []
    for i, e in enumerate(entries, 1):
        out.append(f"{i}\n{sec_to_srt_time(e.start_sec)} --> {sec_to_srt_time(e.end_sec)}\n{e.text}\n")
    return "\n".join(out)


def probe_video(video_path: Path) -> dict:
    if not shutil.which("ffprobe"):
        return {}
    cmd = [
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_format", "-show_streams", str(video_path)
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    if res.returncode == 0 and res.stdout:
        return json.loads(res.stdout)
    return {}


def _format_bitmap_label(codec_name: str) -> str:
    low = codec_name.lower()
    if "pgs" in low:
        return "PGS (Blu-ray)"
    if "dvb" in low:
        return "DVB"
    if "dvd" in low:
        return "VobSub"
    return codec_name.upper()


def extract_subtitles_from_video(video_path: Path, preferred_lang: str = "eng"):
    stem = video_path.stem
    parent = video_path.parent

    for suffix in (".srt", ".en.srt", ".eng.srt", ".English.srt", ".vtt"):
        sidecar = parent / f"{stem}{suffix}"
        if sidecar.is_file():
            content = sidecar.read_text(encoding="utf-8", errors="replace")
            entries = parse_srt_content(content)
            if entries:
                return entries, f"External ({sidecar.name})", False, ""

    meta = probe_video(video_path)
    sub_streams = [s for s in meta.get("streams", []) if s.get("codec_type") == "subtitle"]
    if not sub_streams:
        return [], "None", False, ""

    bitmaps = [s for s in sub_streams if s.get("codec_name", "").lower() in BITMAP_CODECS]
    has_bitmap = bool(bitmaps)
    bitmap_codec = _format_bitmap_label(bitmaps[0].get("codec_name", "")) if has_bitmap else ""

    best_stream = None
    best_score = -1

    for s in sub_streams:
        codec = s.get("codec_name", "").lower()
        if codec in BITMAP_CODECS:
            continue

        tags = s.get("tags", {})
        lang = tags.get("language", "").lower()
        title = tags.get("title", "").lower()

        score = 10 if codec in TEXT_CODECS else 0
        if lang in {preferred_lang.lower(), "en", "english"}:
            score += 20
        elif lang in {"und", ""}:
            score += 5

        if "sdh" in title or "cc" in title:
            score += 2

        if score > best_score:
            best_score = score
            best_stream = s

    if not best_stream:
        if has_bitmap:
            return [], f"Embedded [Bitmap: {bitmap_codec}]", True, bitmap_codec
        return [], "None", False, ""

    stream_idx = best_stream.get("index")
    stream_lang = best_stream.get("tags", {}).get("language", "und").upper()
    stream_codec = best_stream.get("codec_name", "sub")

    if shutil.which("ffmpeg"):
        cmd = [
            "ffmpeg", "-nostdin", "-hide_banner", "-v", "quiet",
            "-i", str(video_path),
            "-map", f"0:{stream_idx}",
            "-f", "srt", "-"
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if res.returncode == 0 and res.stdout.strip():
            entries = parse_srt_content(res.stdout)
            if entries:
                return entries, f"Embedded [Track {stream_idx}: {stream_lang} ({stream_codec})]", has_bitmap, bitmap_codec

    if has_bitmap:
        return [], f"Embedded [Bitmap: {bitmap_codec}]", True, bitmap_codec
    return [], "None", False, ""
