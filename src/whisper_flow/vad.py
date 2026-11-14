"""Silero VAD v5 ONNX voice activity detector and silence trimmer."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import onnxruntime as ort

WINDOW_SAMPLES_16K = 512
CONTEXT_SAMPLES_16K = 64


class SileroVAD:
    """Lightweight CPU ONNX Silero VAD for 16 kHz mono audio."""

    def __init__(self, model_path: Path) -> None:
        if not model_path.exists():
            raise FileNotFoundError(f"Silero VAD model not found: {model_path}")
        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        self._session = ort.InferenceSession(
            str(model_path),
            sess_options=opts,
            providers=["CPUExecutionProvider"],
        )

    def extract_speech(
        self,
        samples: np.ndarray,
        sample_rate: int = 16000,
        threshold: float = 0.28,
        min_speech_ms: int = 110,
        padding_ms: int = 320,
        bridge_gap_ms: int = 1100,
    ) -> np.ndarray:
        """Return speech-only segments (bridging short natural pauses) or empty array on silence."""
        if sample_rate != 16000:
            raise ValueError("SileroVAD only supports 16000 Hz audio.")
        if samples.size < WINDOW_SAMPLES_16K:
            return np.zeros(0, dtype=np.float32)

        if samples.ndim > 1:
            samples = samples.reshape(-1)
        samples = samples.astype(np.float32, copy=False)

        state = np.zeros((2, 1, 128), dtype=np.float32)
        sr_tensor = np.array(sample_rate, dtype=np.int64)
        context = np.zeros((1, CONTEXT_SAMPLES_16K), dtype=np.float32)

        probs: list[float] = []
        num_windows = samples.size // WINDOW_SAMPLES_16K
        for i in range(num_windows):
            start = i * WINDOW_SAMPLES_16K
            chunk = samples[start : start + WINDOW_SAMPLES_16K].reshape(1, WINDOW_SAMPLES_16K)
            x = np.concatenate([context, chunk], axis=1)
            out, state = self._session.run(None, {"input": x, "state": state, "sr": sr_tensor})
            probs.append(float(out.item()))
            context = chunk[:, -CONTEXT_SAMPLES_16K:]

        # Process any trailing partial window with zero-padding so end of sentence isn't cut off.
        rem = samples.size - num_windows * WINDOW_SAMPLES_16K
        if rem > 0:
            tail = np.zeros((1, WINDOW_SAMPLES_16K), dtype=np.float32)
            tail[0, :rem] = samples[num_windows * WINDOW_SAMPLES_16K :]
            x = np.concatenate([context, tail], axis=1)
            out, _ = self._session.run(None, {"input": x, "state": state, "sr": sr_tensor})
            probs.append(float(out.item()))

        speech_flags = [p >= threshold for p in probs]
        if not any(speech_flags):
            return np.zeros(0, dtype=np.float32)

        min_windows = max(1, int(round((min_speech_ms / 1000.0) * sample_rate / WINDOW_SAMPLES_16K)))
        pad_windows = max(1, int(round((padding_ms / 1000.0) * sample_rate / WINDOW_SAMPLES_16K)))
        bridge_windows = max(0, int(round((bridge_gap_ms / 1000.0) * sample_rate / WINDOW_SAMPLES_16K)))

        # Merge raw active windows into padded ranges and drop tiny transient clicks.
        segments: list[tuple[int, int]] = []
        seg_start: int | None = None
        for idx, active in enumerate(speech_flags):
            if active and seg_start is None:
                seg_start = idx
            elif not active and seg_start is not None:
                if idx - seg_start >= min_windows:
                    segments.append((seg_start, idx))
                seg_start = None
        if seg_start is not None and (len(speech_flags) - seg_start >= min_windows):
            segments.append((seg_start, len(speech_flags)))

        if not segments:
            return np.zeros(0, dtype=np.float32)

        merged: list[tuple[int, int]] = []
        for s_idx, e_idx in segments:
            ps = max(0, s_idx - pad_windows)
            pe = min(len(speech_flags), e_idx + pad_windows)
            if merged and ps <= merged[-1][1] + bridge_windows:
                merged[-1] = (merged[-1][0], max(merged[-1][1], pe))
            else:
                merged.append((ps, pe))

        pieces = [
            samples[s * WINDOW_SAMPLES_16K : min(samples.size, e * WINDOW_SAMPLES_16K)]
            for s, e in merged
        ]
        return np.concatenate(pieces).astype(np.float32, copy=False)
