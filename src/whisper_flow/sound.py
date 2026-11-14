"""Subtle synthesized Windows activation 'pop' sound cue (zero external asset files)."""

from __future__ import annotations

import io
import math
from pathlib import Path
import tempfile
import threading
import wave

import numpy as np

try:
    import winsound
except ImportError:
    winsound = None  # type: ignore[assignment]


def generate_activation_chime_wav(sample_rate: int = 44100) -> bytes:
    """Synthesize a crisp, warm Wispr-style 'pop' indicator sound as 16-bit PCM WAV bytes."""
    duration_s = 0.140
    pre_wake_s = 0.025
    n = int(sample_rate * duration_s)
    t = np.arange(n, dtype=np.float64) / sample_rate

    # Subtle sub-threshold pre-wake frame so sleeping DisplayPort/HDMI audio links unmute cleanly
    wake_ramp = np.where(t < pre_wake_s, 0.0025 * np.sin(math.pi * t / pre_wake_s), 0.0)

    # Organic upward pitch-glide bubble pop (250 Hz -> 650 Hz) with smooth 3.5ms attack & soft decay
    t2 = np.maximum(0.0, t - pre_wake_s)
    active = t >= pre_wake_s
    # Analytic integral of f(u) = f_end - (f_end - f_start) * exp(-u / tau)
    f_start, f_end, tau = 250.0, 650.0, 0.018
    phase = 2.0 * math.pi * (f_end * t2 - (f_end - f_start) * tau * (1.0 - np.exp(-t2 / tau)))

    attack = np.clip(t2 / 0.0035, 0.0, 1.0)
    decay = np.where(active, np.exp(-t2 * 34.0), 0.0)
    tail_fade = np.clip((duration_s - t) / 0.010, 0.0, 1.0)
    env = attack * decay * tail_fade

    thump = np.sin(2.0 * math.pi * 155.0 * t2) * np.exp(-t2 * 90.0) * attack
    pop_wave = (np.sin(phase) + 0.22 * np.sin(2.0 * phase) + 0.35 * thump) * env

    raw = pop_wave + wake_ramp
    max_val = float(np.max(np.abs(raw))) or 1.0
    signal = np.clip((raw / max_val) * 0.245, -0.95, 0.95)
    pcm = (signal * 32767.0).astype("<i2")

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm.tobytes())
    return buf.getvalue()


_ACTIVATION_WAV_BYTES: bytes = generate_activation_chime_wav()
_WAV_CACHE_PATH: Path = Path(tempfile.gettempdir()) / "whisper_flow_activation_pop_v2.wav"


def _ensure_cached_wav_file() -> Path | None:
    try:
        if not _WAV_CACHE_PATH.exists() or _WAV_CACHE_PATH.stat().st_size != len(_ACTIVATION_WAV_BYTES):
            _WAV_CACHE_PATH.write_bytes(_ACTIVATION_WAV_BYTES)
        return _WAV_CACHE_PATH
    except Exception:
        return None


def play_activation_sound() -> None:
    """Play the subtle activation pop non-blockingly on Windows."""
    if winsound is None:
        return
    # CPython's winsound forbids SND_MEMORY | SND_ASYNC; play from cached temp WAV instead.
    wav_path = _ensure_cached_wav_file()
    if wav_path is not None:
        try:
            winsound.PlaySound(
                str(wav_path),
                winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT,
            )
            return
        except Exception:
            pass

    def _sync_fallback() -> None:
        try:
            winsound.PlaySound(
                _ACTIVATION_WAV_BYTES,
                winsound.SND_MEMORY | winsound.SND_NODEFAULT,
            )
        except Exception:
            pass

    threading.Thread(target=_sync_fallback, daemon=True).start()
