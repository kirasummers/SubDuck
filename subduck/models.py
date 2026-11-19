from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class SubtitleEntry:
    index: int
    start_sec: float
    end_sec: float
    text: str
    original_block: str = ""
    word_timings: list[tuple[str, float, float]] = field(default_factory=list)

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
    matched_words: list[str]
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
    has_bitmap_subtitles: bool = False
    bitmap_codec: str = ""
    whisper_generated: bool = False
    subtitle_entries: list[SubtitleEntry] = field(default_factory=list)
    matches: list[TriggerMatch] = field(default_factory=list)
    status: str = "Unscanned"
    output_path: Path | None = None
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
        if h:
            return f"{h:02d}:{m:02d}:{s:02d}"
        return f"{m:02d}:{s:02d}"

    @property
    def size_str(self) -> str:
        if self.file_size <= 0:
            return "0 B"
        size = float(self.file_size)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if size < 1024.0:
                return f"{size:.1f} {unit}"
            size /= 1024.0
        return f"{size:.1f} PB"


@dataclass
class CensorConfig:
    trigger_words: list[str] = field(default_factory=list)
    safety_before_ms: int = 200
    safety_after_ms: int = 200
    ducking_mode: str = "mute"
    ducking_level: float = 0.0
    censor_style: str = "asterisks"
    strict_matching: bool = True

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

    whisper_mode: str = "if_missing"
    whisper_model: str = "base.en"
    whisper_device: str = "auto"
    whisper_save_srt: bool = True
    whisper_save_to_source: bool = True

    inhibit_sleep: bool = True


def sec_to_srt_time(sec: float) -> str:
    if sec < 0:
        sec = 0.0
    millis = int(round((sec - int(sec)) * 1000))
    if millis >= 1000:
        sec += 1
        millis = 0
    total_sec = int(sec)
    s = total_sec % 60
    total_min = total_sec // 60
    return f"{total_min // 60:02d}:{total_min % 60:02d}:{s:02d},{millis:03d}"


def srt_time_to_sec(time_str: str) -> float:
    parts = time_str.strip().replace(",", ".").split(":")
    if len(parts) == 3:
        return float(parts[0]) * 3600.0 + float(parts[1]) * 60.0 + float(parts[2])
    if len(parts) == 2:
        return float(parts[0]) * 60.0 + float(parts[1])
    return float(time_str)
