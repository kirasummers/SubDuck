# SubDuck - Video Trigger Word & Audio Ducking Studio

SubDuck is a desktop application built with Python and PySide6 (Qt6) that scans video collections for sensitive trigger words in subtitles, ducks/mutes the audio during those exact timestamps, censors the subtitle text, and creates clean copies of your media files.

---

## 4 Output & Library Integration Modes

1. **Season Mirror Mode (Recommended for TV Shows & Jellyfin)**:
   - Creates a mirror directory (e.g. `Season 1 [Trigger-Clean]`).
   - Every file inside uses its **standard clean filename** (no ` trigger` suffix), ensuring Jellyfin autoplay and episode ordering work without interruptions.
   - Episodes with triggers are processed with ducked audio and censored subtitles.
   - Clean episodes (0 triggers) are automatically copied over so the season is 100% complete.
   - Thumbnails, artwork, and NFO metadata are copied over automatically.

2. **In-Place Replacement (Saves Disk Space)**:
   - Replaces the original video file in-place with the ducked/censored version.
   - Includes a safety option to back up the original video to an `_originals/` subfolder.

3. **Dual-Audio Track in Single File**:
   - Embeds both the Original Audio (Track 1) and the Trigger-Muted Audio (Track 2) into the same file.
   - Sets the ducked track as the `default` playback track and embeds the censored subtitle track.

4. **Save Alongside with Suffix (Best for Single Movies)**:
   - Saves `<Name> trigger.<ext>` and `<Name> trigger.srt` alongside the original file.

---

## Key Features

- **Intelligent Subtitle Extraction**: Automatically reads external `.srt`/`.vtt` files or extracts embedded English tracks from `.mkv` and `.mp4` containers.
- **Hyphen-Aware Whole-Word Matching**: Matches whole words strictly without false positives (e.g. `ass` will never match `class` or `passive`), while allowing hyphenated words (e.g. `cancer` matches `cancer-sign`).
- **Lossless Fast Video Passthrough**: Uses stream copy (`-c:v copy`) for video, avoiding re-encoding degradation and finishing an episode in seconds.
- **KDE & SMB Network Integration**: Native support for network shares via `kio-fuse`. Paste `smb://...` URLs directly, click **SMB / Network...**, or drag and drop folders from Dolphin.
- **Audio Safety Buffers**: Customizable lead-in and lead-out buffers (default: 200 ms).
- **Trigger Presets**: Save and load custom profiles (Medical & Illness, Profanity, Violence, Phobias).

---

## How to Launch

- **Terminal**: `subduck`
- **Application Launcher**: Search for **SubDuck** in your KDE Kickoff menu.
- **Direct Script**: `python3 main.py`
