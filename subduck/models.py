from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

@dataclass
class SubtitleEntry:
    index: int
    start_sec: float
    end_sec: float
    text: str
    original_block: str = ""

    @property
    def start_str(self) -> str:
        return sec_to_srt_time(self.start_sec)

    @property
    def end_str(self) -> str:
        return sec_to_srt_time(self.end_sec)

    @property
    def duration(self) -> float:
        return max(0.0, self.end_sec - self.start_sec)

@dataclass
class TriggerMatch:
    entry: SubtitleEntry
    matched_words: List[str]
    censored_text: str

@dataclass
class VideoFileInfo:
    path: Path
    filename: str
    file_size: int = 0
    duration_sec: float = 0.0
    resolution: str = ""
    has_subtitles: bool = False
    subtitle_source: str = "None"
    subtitle_entries: List[SubtitleEntry] = field(default_factory=list)
    matches: List[TriggerMatch] = field(default_factory=list)
    status: str = "Unscanned"  # Unscanned, Clean, Flagged, No Subtitles, Done, Error
    output_path: Optional[Path] = None
    error_msg: str = ""

    @property
    def trigger_count(self) -> int:
        return sum(len(m.matched_words) for m in self.matches)

    @property
    def duration_str(self) -> str:
        if self.duration_sec <= 0:
            return "--:--"
        m, s = divmod(int(self.duration_sec), 60)
        h, m = divmod(m, 60)
        if h > 0:
            return f"{h:02d}:{m:02d}:{s:02d}"
        return f"{m:02d}:{s:02d}"

    @property
    def size_str(self) -> str:
        if self.file_size <= 0:
            return "0 B"
        size = float(self.file_size)
        for unit in ["B", "KB", "MB", "GB", "TB"]:
            if size < 1024.0:
                return f"{size:.1f} {unit}"
            size /= 1024.0
        return f"{size:.1f} PB"

@dataclass
class CensorConfig:
    trigger_words: List[str] = field(default_factory=list)
    safety_before_ms: int = 200
    safety_after_ms: int = 200
    ducking_mode: str = "mute"  # mute, duck_20db, custom
    ducking_level: float = 0.0  # 0 = silence, 0.1 = -20dB
    censor_style: str = "asterisks"  # asterisks, inner_asterisks, tag, block, remove
    strict_matching: bool = True  # True = whole word boundary respecting hyphens

    # Output Modes:
    # "season_mirror": Complete Season Mirror to new folder (clean filenames, copies clean episodes + artwork)
    # "inplace_replace": Overwrite original in place (with optional backup)
    # "dual_audio": Add second ducked track as default, keep original audio
    # "separate_suffix": Save alongside as <Name> trigger.<ext>
    output_mode: str = "season_mirror"
    season_mirror_folder: str = ""
    backup_originals: bool = True
    copy_clean_files: bool = True
    copy_artwork: bool = True
    output_folder: str = ""
    output_suffix: str = " trigger"
    save_srt: bool = True
    embed_sub: bool = True
    subtitle_lang: str = "eng"

def sec_to_srt_time(sec: float) -> str:
    """Format seconds into SRT timestamp HH:MM:SS,mmm."""
    if sec < 0:
        sec = 0.0
    millis = int(round((sec - int(sec)) * 1000))
    if millis >= 1000:
        sec += 1
        millis = 0
    total_sec = int(sec)
    s = total_sec % 60
    total_min = total_sec // 60
    m = total_min % 60
    h = total_min // 60
    return f"{h:02d}:{m:02d}:{s:02d},{millis:03d}"

def srt_time_to_sec(time_str: str) -> float:
    """Parse SRT timestamp HH:MM:SS,mmm or HH:MM:SS.mmm into float seconds."""
    time_str = time_str.strip().replace(",", ".")
    parts = time_str.split(":")
    if len(parts) == 3:
        h = float(parts[0])
        m = float(parts[1])
        s = float(parts[2])
        return h * 3600.0 + m * 60.0 + s
    elif len(parts) == 2:
        m = float(parts[0])
        s = float(parts[1])
        return m * 60.0 + s
    return float(time_str)
