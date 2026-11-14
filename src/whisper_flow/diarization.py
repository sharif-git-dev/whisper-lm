"""Lightweight dual-channel speaker attribution and acoustic voiceprint clustering."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import math
import re

import numpy as np

_TURN_LINE_RE = re.compile(r"^\[(\d{1,2}):(\d{2})\]\s+([^:]+):\s*(.*)$", re.DOTALL)


@dataclass(frozen=True, slots=True)
class SpeakerEmbedding:
    """Normalized formant-spectrum + fundamental pitch representation of a voice segment."""

    vector: np.ndarray
    f0_hz: float
    voiced_frames: int


@dataclass(frozen=True, slots=True)
class ChannelTurn:
    """Contiguous active speech span isolated to either mic or loopback."""

    start_sample: int
    channel: str  # "mic" | "loopback"
    audio: np.ndarray


@lru_cache(maxsize=4)
def _mel_dct_matrices(sample_rate: int, n_fft: int = 512, n_mels: int = 26, n_mfcc: int = 14) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Build cached Mel filterbank, orthonormal DCT matrix, and sinusoidal cepstral lifter."""
    freqs = np.fft.rfftfreq(n_fft, d=1.0 / sample_rate)
    low_mel = 2595.0 * math.log10(1.0 + 80.0 / 700.0)
    high_mel = 2595.0 * math.log10(1.0 + min(7600.0, sample_rate * 0.48) / 700.0)
    mel_pts = np.linspace(low_mel, high_mel, n_mels + 2)
    hz_pts = 700.0 * (10.0 ** (mel_pts / 2595.0) - 1.0)
    bins = np.floor((n_fft + 1) * hz_pts / sample_rate).astype(int)

    fb = np.zeros((n_mels, len(freqs)), dtype=np.float32)
    for m in range(1, n_mels + 1):
        left, center, right = bins[m - 1], bins[m], bins[m + 1]
        if center > left:
            fb[m - 1, left:center] = (np.arange(left, center) - left) / float(center - left)
        if right > center:
            fb[m - 1, center:right] = (right - np.arange(center, right)) / float(right - center)

    k = np.arange(n_mfcc + 1)[:, None]
    n = np.arange(n_mels)[None, :]
    dct = (np.cos(math.pi / n_mels * (n + 0.5) * k) * math.sqrt(2.0 / n_mels)).astype(np.float32)
    lifter = (1.0 + 11.0 * np.sin(math.pi * np.arange(1, n_mfcc + 1) / 22.0)).astype(np.float32)
    return fb, dct[1:], lifter


def _estimate_f0_hz(voiced_frames: np.ndarray, sample_rate: int) -> float:
    """Estimate median fundamental frequency (Hz) across voiced frames via autocorrelation."""
    min_lag = max(1, int(sample_rate / 350.0))
    max_lag = max(min_lag + 2, int(sample_rate / 70.0))
    if voiced_frames.shape[0] == 0 or voiced_frames.shape[1] <= min_lag:
        return 0.0

    win = np.hanning(voiced_frames.shape[1]).astype(np.float32)
    windowed = voiced_frames * win
    n_fft = 1024
    ac = np.fft.irfft(np.abs(np.fft.rfft(windowed, n=n_fft)) ** 2, n=n_fft)[:, min_lag:max_lag]
    energy = np.sum(windowed * windowed, axis=1) + 1e-9
    norm_ac = ac / energy[:, None]
    best_idx = np.argmax(norm_ac, axis=1)
    best_val = np.max(norm_ac, axis=1)
    confident = best_val > 0.35
    if not np.any(confident):
        return 0.0
    lags = best_idx[confident] + min_lag
    return float(np.median(sample_rate / lags))


def extract_speaker_embedding(audio: np.ndarray, sample_rate: int = 16000) -> SpeakerEmbedding | None:
    """Extract liftered MFCC + median pitch representation from active speech frames."""
    samples = np.asarray(audio, dtype=np.float32)
    frame_len = max(64, int(round(0.025 * sample_rate)))
    hop = max(32, int(round(0.010 * sample_rate)))
    if samples.size < frame_len * 4:
        return None

    rms = float(np.sqrt(np.mean(samples * samples)))
    if rms < 0.008:
        return None

    frames = np.lib.stride_tricks.sliding_window_view(samples, frame_len)[::hop]
    energies = np.mean(frames * frames, axis=1)
    thresh = max(1e-5, 0.14 * float(np.percentile(energies, 90)))
    # Exclude low-energy silence and pure unvoiced high-frequency fricatives
    signs = frames >= 0.0
    zcr = np.mean(signs[:, 1:] != signs[:, :-1], axis=1)
    keep = (energies > thresh) & (zcr < 0.38)
    if int(np.sum(keep)) < 12:
        keep = energies > thresh
    voiced = frames[keep]
    if voiced.shape[0] < 10:
        return None

    fb, dct, lifter = _mel_dct_matrices(sample_rate)
    win = np.hanning(frame_len).astype(np.float32)
    spec = np.abs(np.fft.rfft(voiced * win, n=512)) ** 2
    mel_log = np.log(spec @ fb.T + 1e-6)
    mfcc = (mel_log @ dct.T) * lifter

    vec = mfcc.mean(axis=0)
    vec = vec - float(np.mean(vec))
    norm = float(np.linalg.norm(vec))
    if norm <= 1e-8:
        return None
    vec = (vec / norm).astype(np.float32, copy=False)
    f0 = _estimate_f0_hz(voiced, sample_rate)
    return SpeakerEmbedding(vector=vec, f0_hz=f0, voiced_frames=int(voiced.shape[0]))


def speaker_similarity(a: SpeakerEmbedding, b: SpeakerEmbedding) -> float:
    """Compute combined vocal timbre similarity minus sustained pitch separation penalty."""
    mfcc_sim = float(np.dot(a.vector, b.vector))
    if a.f0_hz > 0.0 and b.f0_hz > 0.0:
        semitones = abs(12.0 * math.log2(a.f0_hz / b.f0_hz))
        penalty = max(0.0, (semitones - 2.3) * 0.085)
        return mfcc_sim - penalty
    return mfcc_sim


@dataclass
class _SpeakerCentroid:
    label: str
    embedding: SpeakerEmbedding
    count: int = 1


class SpeakerTracker:
    """Assigns consistent session labels ('You', 'Speaker 1', 'Speaker 2', ...) across turns."""

    def __init__(self, similarity_threshold: float = 0.78, max_speakers: int = 6) -> None:
        self.similarity_threshold = similarity_threshold
        self.max_speakers = max_speakers
        self._seen_remote = False
        self._mic_speakers: list[_SpeakerCentroid] = []
        self._remote_speakers: list[_SpeakerCentroid] = []

    def _assign_to_pool(
        self,
        pool: list[_SpeakerCentroid],
        emb: SpeakerEmbedding | None,
        first_label: str,
        next_prefix: str,
        next_start_num: int,
    ) -> str:
        if not pool:
            if emb is not None:
                pool.append(_SpeakerCentroid(label=first_label, embedding=emb))
            return first_label

        if emb is None:
            return pool[-1].label

        best_idx = 0
        best_score = -2.0
        for idx, centroid in enumerate(pool):
            score = speaker_similarity(centroid.embedding, emb)
            if score > best_score:
                best_score = score
                best_idx = idx

        # Only split into a brand-new speaker on sufficiently long voiced speech
        can_spawn = emb.voiced_frames >= 38 and len(pool) < self.max_speakers
        if best_score < self.similarity_threshold and can_spawn:
            new_label = f"{next_prefix} {next_start_num + len(pool) - 1}"
            pool.append(_SpeakerCentroid(label=new_label, embedding=emb))
            return new_label

        matched = pool[best_idx]
        if emb.voiced_frames >= 25:
            blended_vec = 0.82 * matched.embedding.vector + 0.18 * emb.vector
            norm = float(np.linalg.norm(blended_vec))
            if norm > 1e-8:
                blended_vec = (blended_vec / norm).astype(np.float32, copy=False)
            blended_f0 = matched.embedding.f0_hz
            if matched.embedding.f0_hz > 0.0 and emb.f0_hz > 0.0:
                blended_f0 = 0.82 * matched.embedding.f0_hz + 0.18 * emb.f0_hz
            elif emb.f0_hz > 0.0:
                blended_f0 = emb.f0_hz
            matched.embedding = SpeakerEmbedding(
                vector=blended_vec,
                f0_hz=blended_f0,
                voiced_frames=matched.embedding.voiced_frames + emb.voiced_frames,
            )
            matched.count += 1
        return matched.label

    def assign(self, audio: np.ndarray, channel: str, sample_rate: int = 16000) -> str:
        """Identify speaker label for a single-channel speech segment."""
        emb = extract_speaker_embedding(audio, sample_rate=sample_rate)
        if channel == "loopback":
            self._seen_remote = True
            return self._assign_to_pool(
                self._remote_speakers,
                emb=emb,
                first_label="Speaker 1",
                next_prefix="Speaker",
                next_start_num=2,
            )

        # Mic channel: primary speaker is always "You"; if no remote call audio exists and another
        # acoustically distinct voice speaks into the mic (in-person room), track Speaker 2+ safely.
        if self._seen_remote:
            if not self._mic_speakers and emb is not None:
                self._mic_speakers.append(_SpeakerCentroid(label="You", embedding=emb))
            return "You"

        return self._assign_to_pool(
            self._mic_speakers,
            emb=emb,
            first_label="You",
            next_prefix="Speaker",
            next_start_num=2,
        )


def _group_channel_spans(mask: np.ndarray, step: int, bridge_gaps: int = 3, min_frames: int = 3) -> list[tuple[int, int]]:
    """Convert boolean active frame mask into contiguous sample ranges [start, end)."""
    if mask.size == 0 or not np.any(mask):
        return []
    bridged = mask.copy()
    gap = 0
    started = False
    for idx in range(bridged.size):
        if bridged[idx]:
            if started and 0 < gap <= bridge_gaps:
                bridged[idx - gap : idx] = True
            started = True
            gap = 0
        else:
            gap += 1

    spans: list[tuple[int, int]] = []
    start_idx: int | None = None
    for idx in range(bridged.size):
        if bridged[idx] and start_idx is None:
            start_idx = idx
        elif not bridged[idx] and start_idx is not None:
            if idx - start_idx >= min_frames:
                spans.append((start_idx * step, idx * step))
            start_idx = None
    if start_idx is not None and bridged.size - start_idx >= min_frames:
        spans.append((start_idx * step, bridged.size * step))
    return spans


def segment_channel_turns(
    mic: np.ndarray,
    loopback: np.ndarray,
    sample_rate: int = 16000,
    activity_rms: float = 0.011,
) -> list[ChannelTurn]:
    """Isolate non-bleeding chronological speech spans per audio channel."""
    total = max(mic.size, loopback.size)
    if total == 0:
        return []

    mic_pad = mic if mic.size == total else np.pad(mic, (0, total - mic.size))
    loop_pad = loopback if loopback.size == total else np.pad(loopback, (0, total - loopback.size))

    step = max(1, int(sample_rate * 0.10))
    n_frames = total // step
    if n_frames == 0:
        return []
    usable = n_frames * step

    mic_blocks = mic_pad[:usable].reshape(n_frames, step)
    loop_blocks = loop_pad[:usable].reshape(n_frames, step)
    mic_rms = np.sqrt(np.mean(mic_blocks * mic_blocks, axis=1))
    loop_rms = np.sqrt(np.mean(loop_blocks * loop_blocks, axis=1))

    loop_mask = loop_rms >= activity_rms
    # Suppress acoustic echo leaking from desktop speakers into the laptop mic
    mic_mask = (mic_rms >= activity_rms) & (~loop_mask | (mic_rms >= loop_rms * 0.55))

    turns: list[ChannelTurn] = []
    for s, e in _group_channel_spans(mic_mask, step):
        seg = mic_pad[s:min(total, e + step)]
        if seg.size > 0:
            turns.append(ChannelTurn(start_sample=s, channel="mic", audio=seg))

    for s, e in _group_channel_spans(loop_mask, step):
        seg = loop_pad[s:min(total, e + step)]
        if seg.size > 0:
            turns.append(ChannelTurn(start_sample=s, channel="loopback", audio=seg))

    turns.sort(key=lambda item: (item.start_sample, 0 if item.channel == "mic" else 1))
    return turns


def format_turn_timestamp(seconds: int) -> str:
    """Format elapsed meeting seconds cleanly as MM:SS."""
    secs = max(0, int(seconds))
    mins, rem = divmod(secs, 60)
    return f"{mins:02d}:{rem:02d}"


def append_speaker_turn(transcript: str, speaker: str, elapsed_seconds: int, text: str) -> str:
    """Append transcribed text, merging consecutive same-speaker turns within 45s."""
    cleaned_text = text.strip()
    if not cleaned_text:
        return transcript

    stamp = format_turn_timestamp(elapsed_seconds)
    existing = transcript.strip()
    if not existing:
        return f"[{stamp}] {speaker}: {cleaned_text}"

    blocks = existing.split("\n\n")
    last_match = _TURN_LINE_RE.match(blocks[-1])
    if last_match:
        prev_min, prev_sec, prev_speaker, prev_body = last_match.groups()
        prev_total = int(prev_min) * 60 + int(prev_sec)
        if prev_speaker.strip() == speaker and (max(0, int(elapsed_seconds)) - prev_total) <= 45:
            merged_body = f"{prev_body.rstrip()} {cleaned_text}".strip()
            blocks[-1] = f"[{prev_min}:{prev_sec}] {prev_speaker}: {merged_body}"
            return "\n\n".join(blocks)

    return f"{existing}\n\n[{stamp}] {speaker}: {cleaned_text}"
