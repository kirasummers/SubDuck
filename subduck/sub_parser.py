import os
import re
import json
import subprocess
from pathlib import Path
from typing import List, Tuple, Optional
from subduck.models import SubtitleEntry, srt_time_to_sec, sec_to_srt_time

SRT_BLOCK_REGEX = re.compile(
    r"(?:^|\n\n+)(?P<idx>\d+)\r?\n"
    r"(?P<start>\d{1,2}:\d{2}:\d{2}[,\.]\d{1,3})\s*-->\s*"
    r"(?P<end>\d{1,2}:\d{2}:\d{2}[,\.]\d{1,3})"
    r"(?:[^\r\n]*)\r?\n"
    r"(?P<text>[\s\S]*?)(?=(?:\r?\n\r?\n\d+\r?\n|\Z))",
    re.MULTILINE
)

def parse_srt_content(content: str) -> List[SubtitleEntry]:
    """Parse SRT formatted text into SubtitleEntry list."""
    entries = []
    # Strip WebVTT header if present
    clean_content = re.sub(r"^WEBVTT[^\n]*\n+", "", content.strip())
    
    for match in SRT_BLOCK_REGEX.finditer(clean_content):
        idx_str = match.group("idx")
        start_str = match.group("start")
        end_str = match.group("end")
        text = match.group("text").strip()
        
        # Clean HTML/formatting tags from subtitle text (like <i>, </b>, <font...>)
        clean_text = re.sub(r"<[^>]+>", "", text)
        
        try:
            start_sec = srt_time_to_sec(start_str)
            end_sec = srt_time_to_sec(end_str)
            entries.append(SubtitleEntry(
                index=int(idx_str),
                start_sec=start_sec,
                end_sec=end_sec,
                text=clean_text,
                original_block=match.group(0).strip()
            ))
        except Exception:
            continue

    return entries

def serialize_srt(entries: List[SubtitleEntry]) -> str:
    """Serialize SubtitleEntry list into standard SRT formatted string."""
    blocks = []
    for i, entry in enumerate(entries, 1):
        start_ts = sec_to_srt_time(entry.start_sec)
        end_ts = sec_to_srt_time(entry.end_sec)
        blocks.append(f"{i}\n{start_ts} --> {end_ts}\n{entry.text}\n")
    return "\n".join(blocks)

def probe_video(video_path: Path) -> dict:
    """Use ffprobe to retrieve duration, resolution, and stream metadata."""
    cmd = [
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_format", "-show_streams", str(video_path)
    ]
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10)
        if p.returncode == 0:
            return json.loads(p.stdout)
    except Exception:
        pass
    return {}

def extract_subtitles_from_video(video_path: Path, preferred_lang: str = "eng") -> Tuple[List[SubtitleEntry], str]:
    """
    Find and extract subtitles for a video file.
    Checks external .srt files first, then embedded subtitle tracks in container.
    """
    # Check external subtitle files (.srt, .vtt)
    stem = video_path.stem
    parent = video_path.parent
    
    # Priority external candidates: exact match, .en.srt, .eng.srt
    candidates = [
        parent / f"{stem}.srt",
        parent / f"{stem}.en.srt",
        parent / f"{stem}.eng.srt",
        parent / f"{stem}.English.srt",
        parent / f"{stem}.vtt",
    ]
    for ext_path in candidates:
        if ext_path.is_file():
            try:
                content = ext_path.read_text(encoding="utf-8", errors="replace")
                entries = parse_srt_content(content)
                if entries:
                    return entries, f"External ({ext_path.name})"
            except Exception:
                pass

    # Check embedded subtitle tracks
    meta = probe_video(video_path)
    streams = meta.get("streams", [])
    sub_streams = [s for s in streams if s.get("codec_type") == "subtitle"]

    if not sub_streams:
        return [], "None"

    # Score subtitle streams: prioritize English text subtitles (subrip, srt, ass, mov_text)
    best_stream = None
    best_score = -1

    for s in sub_streams:
        idx = s.get("index")
        codec = s.get("codec_name", "").lower()
        tags = s.get("tags", {})
        lang = tags.get("language", "").lower()
        title = tags.get("title", "").lower()

        score = 0
        if codec in ["subrip", "srt", "mov_text", "ass"]:
            score += 10
        elif codec in ["dvb_subtitle", "hdmv_pgs_subtitle"]:
            # Image based subtitles cannot be text extracted via srt filter directly
            continue

        if lang in [preferred_lang.lower(), "en", "english"]:
            score += 20
        elif lang in ["und", ""]:
            score += 5

        if "sdh" in title or "cc" in title:
            score += 2

        if score > best_score:
            best_score = score
            best_stream = s

    if not best_stream:
        return [], "None (Bitmap / Incompatible)"

    stream_idx = best_stream.get("index")
    stream_lang = best_stream.get("tags", {}).get("language", "und")
    stream_codec = best_stream.get("codec_name", "sub")

    # Extract embedded text subtitle via ffmpeg
    cmd = [
        "ffmpeg", "-nostdin", "-hide_banner", "-v", "quiet",
        "-i", str(video_path),
        "-map", f"0:{stream_idx}",
        "-f", "srt", "-"
    ]
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=20)
        if p.returncode == 0 and p.stdout.strip():
            entries = parse_srt_content(p.stdout)
            if entries:
                return entries, f"Embedded [Track {stream_idx}: {stream_lang.upper()} ({stream_codec})]"
    except Exception:
        pass

    return [], "None"
