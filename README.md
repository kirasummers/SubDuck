# SubDuck

A Desktop tool for scanning video files against custom trigger word lists, muting or ducking the audio around matched dialogue and redacting the subtitle track without re-encoding the video stream.

<p align="center">
  <img src="docs/screenshot.png" alt="SubDuck Interface">
</p>

<p>If a video doesn't have text subtitles or only has bitmap based subtitles, SubDuck can extract a temporary 16 kHz mono WAV and transcribe it locally with `faster-whisper`. Matched trigger words are cleanly muted or ducked in the audio stream and redacted from the subtitle track.</p>

<p>Here is the subtitle app in action:</p>

<p align="center">
  <img src="docs/subtitles.png" alt="SubDuck Subtitle Redaction Application showing various fields for entering a path, viewig files that have matched the trigger and buttons to set parameters and start processing.">
</p>

## Requirements

- Python 3.10+
- `ffmpeg` and `ffprobe` in `PATH` (`sudo pacman -S ffmpeg` or `sudo apt install ffmpeg`)
- Optional: NVIDIA GPU with CUDA 12 libraries for `faster-whisper` (`float16`). If `nvidia-cublas-cu12` and `nvidia-cudnn-cu12` are installed inside `.venv`, SubDuck preloads the `.so` files at startup so you don't need to export `LD_LIBRARY_PATH` manually.

## Setup

```bash
git clone https://github.com/kirasummers/SubDuck.git
cd SubDuck
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

Run with `subduck` or `python3 main.py`.

## How it works

1. Point the app at a folder or drag a folder in from the file browser.
2. Pick a trigger profile or edit the word list via 'Edit Trigger Words...' (words stay hidden on the main window unless revealed - so you only see them if you ask to).
3. Choose an output mode:
   - 'Complete Folder Mirror' writes a parallel folder (`<Series> [Trigger-Clean]/Season X`) with original filenames so Jellyfin/Plex don't merge filtered and unfiltered episodes into one entry. Clean episodes, posters, and `.nfo` files are copied across.
   - 'In-Place Replace' moves the original file into `_originals/` and writes the filtered file in place.
   - 'Dual-Audio Track' keeps the original audio track alongside the filtered track (set as default) in the same container.
   - 'Save Alongside with Suffix' outputs `<Name> trigger.<ext>` next to the source video.

## Settings & Configuration

Fine-tune safety buffers, ducking volume, Whisper models, and hardware acceleration in the 'Settings' dialogue:

<p align="center">
  <img src="docs/settings.png" alt="SubDuck Settings">
</p>

## License

MIT
