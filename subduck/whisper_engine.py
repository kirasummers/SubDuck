import ctypes
import importlib.util
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from subduck.models import SubtitleEntry
from subduck.sub_parser import serialize_srt

_LOADED_MODELS = {}


def setup_cuda_libraries():
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
        for pkg_name in ("cublas", "cudnn", "cuda_nvrtc"):
            lib_dir = nv_dir / pkg_name / "lib"
            if not lib_dir.is_dir():
                continue
            if str(lib_dir) not in current_ld and str(lib_dir) not in additions:
                additions.append(str(lib_dir))
            for so_file in sorted(lib_dir.glob("*.so*")):
                try:
                    ctypes.CDLL(str(so_file), mode=ctypes.RTLD_GLOBAL)
                except OSError:
                    continue

    if additions:
        os.environ["LD_LIBRARY_PATH"] = ":".join(additions) + (f":{current_ld}" if current_ld else "")


setup_cuda_libraries()


def is_whisper_available() -> bool:
    return importlib.util.find_spec("faster_whisper") is not None


def get_best_whisper_device() -> tuple[str, str]:
    if importlib.util.find_spec("ctranslate2") is not None:
        import ctranslate2
        if ctranslate2.get_cuda_device_count() > 0:
            cuda_types = ctranslate2.get_supported_compute_types("cuda")
            for preferred in ("float16", "int8_float16", "float32"):
                if preferred in cuda_types:
                    return "cuda", preferred
    return "cpu", "int8"


def get_or_load_whisper_model(model_size: str = "base.en", device: str = "auto", log_callback=None):
    from faster_whisper import WhisperModel

    if device == "auto":
        dev, compute = get_best_whisper_device()
    elif device == "cuda":
        dev, compute = "cuda", "float16"
    else:
        dev, compute = "cpu", "int8"

    cache_key = f"{model_size}_{dev}_{compute}"
    if cache_key in _LOADED_MODELS:
        return _LOADED_MODELS[cache_key], dev, compute

    if log_callback:
        dev_desc = f"NVIDIA GPU (CUDA {compute})" if dev == "cuda" else "CPU (int8)"
        log_callback(f"Loading Whisper model '{model_size}' on {dev_desc}...")

    model = WhisperModel(model_size, device=dev, compute_type=compute)
    _LOADED_MODELS[cache_key] = model
    return model, dev, compute


def extract_audio_for_transcription(video_path: Path, log_callback=None) -> Path | None:
    temp_wav = Path(tempfile.gettempdir()) / f"subduck_audio_{os.getpid()}_{int(time.time() * 1000)}.wav"
    cmd = [
        "ffmpeg", "-nostdin", "-hide_banner", "-y",
        "-i", str(video_path),
        "-vn",
        "-acodec", "pcm_s16le",
        "-ar", "16000",
        "-ac", "1",
        str(temp_wav)
    ]
    res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, timeout=600)
    if res.returncode == 0 and temp_wav.is_file() and temp_wav.stat().st_size > 0:
        return temp_wav
    if log_callback:
        err_msg = res.stderr[-200:].strip() if res.stderr else "Empty audio stream"
        log_callback(f"Audio extraction notice: {err_msg}")
    return None


def transcribe_video_to_subtitles(
    video_path: Path,
    model_size: str = "base.en",
    device: str = "auto",
    word_timestamps: bool = True,
    language: str = "en",
    progress_callback=None,
    cancel_check=None,
    log_callback=None
) -> tuple[list[SubtitleEntry], str]:
    if not is_whisper_available():
        raise RuntimeError("faster-whisper is not installed in the environment.")

    t0 = time.time()
    if log_callback:
        log_callback(f"Starting Whisper transcription for: {video_path.name}")

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
            vad_parameters={"min_silence_duration_ms": 500}
        )

        total_dur = float(getattr(info, "duration", 0.0) or 0.0)
        entries = []
        last_pct = -1

        for i, s in enumerate(segments, 1):
            if cancel_check and cancel_check():
                if log_callback:
                    log_callback("Whisper transcription cancelled by user.")
                break

            text = s.text.strip()
            if text:
                word_list = []
                if word_timestamps and getattr(s, "words", None):
                    for w in s.words:
                        w_text = getattr(w, "word", "").strip()
                        if w_text:
                            word_list.append((w_text, float(w.start), float(w.end)))

                entries.append(SubtitleEntry(
                    index=i,
                    start_sec=float(s.start),
                    end_sec=float(s.end),
                    text=text,
                    word_timings=word_list
                ))

            if progress_callback and total_dur > 0:
                pct = min(99, int((s.end / total_dur) * 100))
                if pct != last_pct:
                    last_pct = pct
                    progress_callback(pct, float(s.end), total_dur)

        if progress_callback:
            progress_callback(100, total_dur, total_dur)

        srt_content = serialize_srt(entries)
        elapsed = time.time() - t0
        dur_str = f"{int(elapsed // 60)}m {int(elapsed % 60)}s" if elapsed >= 60 else f"{elapsed:.1f}s"

        if log_callback:
            log_callback(f"Whisper completed: {len(entries)} subtitle lines generated in {dur_str}.")

        return entries, srt_content

    finally:
        if wav_path and wav_path.exists():
            wav_path.unlink(missing_ok=True)
