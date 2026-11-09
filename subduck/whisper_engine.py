import os
import sys
import time
import ctypes
import tempfile
import subprocess
from pathlib import Path
from typing import List, Tuple, Optional, Dict, Any, Callable

from subduck.models import SubtitleEntry, srt_time_to_sec, sec_to_srt_time
from subduck.sub_parser import serialize_srt

_LOADED_MODELS: Dict[str, Any] = {}

def setup_cuda_libraries():
    """Ensure bundled nvidia-cublas and nvidia-cudnn wheels are in LD_LIBRARY_PATH and preloaded."""
    search_dirs = []
    for p in sys.path:
        cand = Path(p) / "nvidia"
        if cand.is_dir() and cand not in search_dirs:
            search_dirs.append(cand)

    venv_lib = Path(__file__).resolve().parent.parent / ".venv" / "lib"
    if venv_lib.is_dir():
        for py_dir in venv_lib.glob("python3.*"):
            sp = py_dir / "site-packages" / "nvidia"
            if sp.is_dir() and sp not in search_dirs:
                search_dirs.append(sp)

    current_ld = os.environ.get("LD_LIBRARY_PATH", "")
    additions = []

    for nv_dir in search_dirs:
        for pkg_name in ["cublas", "cudnn", "cuda_nvrtc"]:
            lib_dir = nv_dir / pkg_name / "lib"
            if lib_dir.is_dir():
                if str(lib_dir) not in current_ld and str(lib_dir) not in additions:
                    additions.append(str(lib_dir))
                for so_file in sorted(lib_dir.glob("*.so*")):
                    try:
                        ctypes.CDLL(str(so_file), mode=ctypes.RTLD_GLOBAL)
                    except Exception:
                        pass

    if additions:
        os.environ["LD_LIBRARY_PATH"] = ":".join(additions) + (f":{current_ld}" if current_ld else "")

setup_cuda_libraries()

def is_whisper_available() -> bool:
    """Check if faster-whisper is installed and importable."""
    try:
        import faster_whisper
        return True
    except ImportError:
        return False

def get_best_whisper_device() -> Tuple[str, str]:
    """
    Detect whether CUDA is usable with ctranslate2.
    Returns (device, compute_type): ('cuda', 'float16') or ('cpu', 'int8').
    """
    try:
        import ctranslate2
        cuda_types = ctranslate2.get_supported_compute_types("cuda")
        if cuda_types:
            if "float16" in cuda_types:
                return "cuda", "float16"
            elif "int8_float16" in cuda_types:
                return "cuda", "int8_float16"
            elif "float32" in cuda_types:
                return "cuda", "float32"
    except Exception:
        pass
    return "cpu", "int8"

def get_or_load_whisper_model(
    model_size: str = "base.en",
    device: str = "auto",
    log_callback=None
):
    """Retrieve cached WhisperModel or load a new instance."""
    from faster_whisper import WhisperModel

    if device == "auto":
        dev, compute = get_best_whisper_device()
    elif device == "cuda":
        dev = "cuda"
        compute = "float16"
    else:
        dev = "cpu"
        compute = "int8"

    cache_key = f"{model_size}_{dev}_{compute}"
    if cache_key in _LOADED_MODELS:
        return _LOADED_MODELS[cache_key], dev, compute

    if log_callback:
        dev_desc = f"NVIDIA GPU (CUDA {compute})" if dev == "cuda" else "CPU (int8 multi-core)"
        log_callback(f"Loading Whisper model '{model_size}' on {dev_desc}...")

    model = WhisperModel(model_size, device=dev, compute_type=compute)
    _LOADED_MODELS[cache_key] = model
    return model, dev, compute

def extract_audio_for_transcription(video_path: Path, log_callback=None) -> Optional[Path]:
    """
    Extract a fast 16kHz mono 16-bit PCM WAV from a video using FFmpeg.
    This guarantees high transcription speed and prevents network jitter on SMB shares.
    """
    temp_wav = Path(tempfile.gettempdir()) / f"subduck_audio_{os.getpid()}_{int(time.time()*1000)}.wav"
    cmd = [
        "ffmpeg", "-nostdin", "-hide_banner", "-y",
        "-i", str(video_path),
        "-vn",
        "-acodec", "pcm_s16le",
        "-ar", "16000",
        "-ac", "1",
        str(temp_wav)
    ]
    try:
        p = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, timeout=600)
        if p.returncode == 0 and temp_wav.is_file() and temp_wav.stat().st_size > 0:
            return temp_wav
        else:
            if log_callback:
                log_callback(f"Audio extraction notice: {p.stderr[-200:].strip() if p.stderr else 'Empty audio'}")
    except Exception as e:
        if log_callback:
            log_callback(f"Failed to extract audio for transcription: {e}")
    return None

def transcribe_video_to_subtitles(
    video_path: Path,
    model_size: str = "base.en",
    device: str = "auto",
    word_timestamps: bool = True,
    language: str = "en",
    progress_callback: Optional[Callable[[int, float, float], None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
    log_callback: Optional[Callable[[str], None]] = None
) -> Tuple[List[SubtitleEntry], str]:
    """
    Transcribe a video's audio using faster-whisper and return SubtitleEntry list and SRT text.
    Calls progress_callback(pct, current_sec, total_sec) as chunks are processed.
    """
    if not is_whisper_available():
        raise RuntimeError("faster-whisper is not installed in the environment.")

    t0 = time.time()
    if log_callback:
        log_callback(f"Starting Whisper AI transcription for: {video_path.name}")

    # Extract audio to 16kHz WAV
    wav_path = extract_audio_for_transcription(video_path, log_callback=log_callback)
    input_file = wav_path if (wav_path and wav_path.is_file()) else video_path

    try:
        model, dev, compute = get_or_load_whisper_model(model_size, device, log_callback=log_callback)

        if log_callback:
            log_callback(f"Transcribing audio stream ({dev.upper()} {compute})...")

        segments, info = model.transcribe(
            str(input_file),
            language=language if language != "auto" else None,
            word_timestamps=word_timestamps,
            vad_filter=True,
            vad_parameters=dict(min_silence_duration_ms=500)
        )

        total_dur = getattr(info, "duration", 0.0) or 0.0
        entries: List[SubtitleEntry] = []
        last_pct = -1

        for i, s in enumerate(segments, 1):
            if cancel_check and cancel_check():
                if log_callback:
                    log_callback("Whisper transcription cancelled by user.")
                break

            text = s.text.strip()
            if text:
                entries.append(SubtitleEntry(
                    index=i,
                    start_sec=float(s.start),
                    end_sec=float(s.end),
                    text=text
                ))

            if progress_callback and total_dur > 0:
                pct = min(99, int((s.end / total_dur) * 100))
                if pct != last_pct:
                    last_pct = pct
                    progress_callback(pct, float(s.end), float(total_dur))

        if progress_callback:
            progress_callback(100, float(total_dur), float(total_dur))

        srt_content = serialize_srt(entries)
        elapsed = time.time() - t0
        dur_str = f"{int(elapsed // 60)}m {int(elapsed % 60)}s" if elapsed >= 60 else f"{elapsed:.1f}s"

        if log_callback:
            log_callback(f"Whisper completed: {len(entries)} subtitle lines generated in {dur_str}.")

        return entries, srt_content

    finally:
        if wav_path and wav_path.is_file():
            try:
                wav_path.unlink()
            except Exception:
                pass
