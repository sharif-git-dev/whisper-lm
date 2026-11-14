"""Microphone capture, resampling fallback, and WAV encoding helpers."""

from __future__ import annotations

import io
import math
import threading
import wave
from dataclasses import dataclass
from typing import Callable

import numpy as np
import sounddevice as sd


@dataclass(frozen=True)
class AudioDeviceInfo:
    """Descriptor for a host audio input device."""

    index: int
    name: str
    hostapi_name: str
    default_samplerate: float
    is_default: bool


def list_input_devices() -> list[AudioDeviceInfo]:
    """Return available audio input devices on the system."""
    devices = sd.query_devices()
    hostapis = sd.query_hostapis()
    try:
        default_in = sd.default.device[0]
    except Exception:
        default_in = None

    result: list[AudioDeviceInfo] = []
    for idx, dev in enumerate(devices):
        if int(dev.get("max_input_channels", 0)) > 0:
            api_idx = int(dev.get("hostapi", 0))
            api_name = str(hostapis[api_idx]["name"]) if 0 <= api_idx < len(hostapis) else ""
            result.append(
                AudioDeviceInfo(
                    index=idx,
                    name=str(dev["name"]).replace("\r", " ").replace("\n", " ").strip(),
                    hostapi_name=api_name,
                    default_samplerate=float(dev.get("default_samplerate", 16000.0)),
                    is_default=(idx == default_in),
                )
            )
    return result


def resample_mono(samples: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """Anti-aliased resampling of 1D float32 audio from *src_rate* to *dst_rate*."""
    arr = np.asarray(samples, dtype=np.float32).reshape(-1)
    if src_rate == dst_rate or arr.size == 0:
        return arr

    if src_rate > dst_rate:
        # Linear-phase windowed-sinc low-pass before decimation so sibilants never alias into voice band.
        cutoff_ratio = 0.45 * (dst_rate / float(src_rate))
        half = min(48, max(16, int(round(12.0 * src_rate / float(dst_rate)))))
        t = np.arange(-half, half + 1, dtype=np.float64)
        kernel = np.sinc(2.0 * cutoff_ratio * t) * np.blackman(2 * half + 1)
        kernel /= np.sum(kernel)
        padded = np.pad(arr.astype(np.float64), half, mode="edge")
        filtered = np.convolve(padded, kernel, mode="valid")
        arr = filtered.astype(np.float32, copy=False)

    dst_len = max(1, int(round(arr.size * (dst_rate / float(src_rate)))))
    x_old = np.linspace(0.0, 1.0, num=arr.size, endpoint=False)
    x_new = np.linspace(0.0, 1.0, num=dst_len, endpoint=False)
    return np.interp(x_new, x_old, arr).astype(np.float32)


def prepare_speech_for_asr(samples: np.ndarray, sample_rate: int = 16000) -> np.ndarray:
    """Strip sub-bass/DC mic drift and gently normalize quiet speech levels for clean ASR."""
    arr = np.asarray(samples, dtype=np.float32).reshape(-1)
    if arr.size == 0:
        return arr

    # Remove DC offset + slow < 30 Hz handling thumps via fast symmetric moving-average baseline subtraction.
    win = min(arr.size, max(16, int(sample_rate * 0.032)))
    if arr.size > win * 2:
        half = win // 2
        pad_right = win - 1 - half
        padded = np.pad(arr.astype(np.float64), (half, pad_right), mode="edge")
        csum = np.empty(padded.size + 1, dtype=np.float64)
        csum[0] = 0.0
        np.cumsum(padded, out=csum[1:])
        baseline = (csum[win:] - csum[:-win]) / float(win)
        arr = (arr - baseline[: arr.size]).astype(np.float32)
    else:
        arr = (arr - float(np.mean(arr))).astype(np.float32)

    # Check active 20ms frame RMS so pure silence / room noise is never amplified.
    frame = max(1, int(sample_rate * 0.02))
    n_frames = arr.size // frame
    active_rms = float(np.sqrt(np.mean(arr * arr)))
    if n_frames >= 3:
        blocks = arr[: n_frames * frame].reshape(n_frames, frame)
        rms_vals = np.sqrt(np.mean(blocks * blocks, axis=1))
        active_rms = float(np.percentile(rms_vals, 90))

    peak = float(np.max(np.abs(arr)))
    if active_rms > 0.0025 and 1e-4 < peak < 0.38:
        gain = min(10.0, 0.65 / peak)
        arr = np.clip(arr * gain, -0.95, 0.95).astype(np.float32)

    return arr


def float32_to_wav_bytes(samples: np.ndarray, sample_rate: int = 16000) -> bytes:
    """Convert mono float32 audio in [-1.0, 1.0] into 16-bit PCM WAV bytes."""
    if samples.ndim > 1:
        samples = samples.reshape(-1)
    clipped = np.clip(samples, -1.0, 1.0)
    pcm16 = (clipped * 32767.0).astype(np.int16)

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm16.tobytes())
    return buf.getvalue()


class AudioRecorder:
    """Thread-safe 16 kHz mono microphone recorder with sample-rate fallback."""

    def __init__(
        self,
        sample_rate: int = 16000,
        device: int | None = None,
        on_level: Callable[[float], None] | None = None,
    ) -> None:
        self.sample_rate = sample_rate
        self.device = device
        self.on_level = on_level
        self._lock = threading.Lock()
        self._chunks: list[np.ndarray] = []
        self._stream: sd.InputStream | None = None
        self._active_rate: int = sample_rate
        self._recording = False

    @property
    def is_recording(self) -> bool:
        with self._lock:
            return self._recording

    def start(self) -> None:
        """Open the input stream and begin collecting microphone frames."""
        with self._lock:
            if self._recording:
                return
            self._chunks = []
            self._recording = True

        def _callback(indata: np.ndarray, frames: int, time_info: object, status: sd.CallbackFlags) -> None:
            del frames, time_info, status
            mono = indata[:, 0].copy()
            with self._lock:
                if not self._recording:
                    return
                self._chunks.append(mono)
            if self.on_level is not None:
                rms = float(np.sqrt(np.mean(mono * mono))) if mono.size else 0.0
                db = 20.0 * math.log10(max(rms, 1e-6))
                # Map -58 dB (quiet room) .. -16 dB (loud speech) into [0.0, 1.0].
                norm = max(0.0, min(1.0, (db + 58.0) / 42.0))
                self.on_level(norm)

        open_rate = self.sample_rate
        stream: sd.InputStream | None = None
        try:
            try:
                stream = sd.InputStream(
                    samplerate=self.sample_rate,
                    channels=1,
                    dtype="float32",
                    blocksize=512,
                    device=self.device,
                    callback=_callback,
                )
            except Exception:
                dev_info = sd.query_devices(self.device, "input")
                open_rate = int(round(float(dev_info["default_samplerate"])))
                stream = sd.InputStream(
                    samplerate=open_rate,
                    channels=1,
                    dtype="float32",
                    blocksize=max(512, open_rate // 30),
                    device=self.device,
                    callback=_callback,
                )
            stream.start()
        except Exception:
            if stream is not None:
                stream.close()
            with self._lock:
                self._recording = False
                self._chunks = []
            raise

        with self._lock:
            self._stream = stream
            self._active_rate = open_rate

    def stop(self) -> np.ndarray:
        """Stop capturing and return mono float32 audio at self.sample_rate."""
        with self._lock:
            self._recording = False
            stream = self._stream
            self._stream = None
            chunks = self._chunks
            self._chunks = []
            active_rate = self._active_rate

        if stream is not None:
            try:
                stream.stop()
            finally:
                stream.close()

        if not chunks:
            return np.zeros(0, dtype=np.float32)

        audio = np.concatenate(chunks).astype(np.float32, copy=False)
        return resample_mono(audio, active_rate, self.sample_rate)
