"""Unit tests for multi-speaker channel segmentation and acoustic speaker tracking."""

from __future__ import annotations

import numpy as np

from whisper_lm.diarization import (
    SpeakerTracker,
    append_speaker_turn,
    extract_speaker_embedding,
    segment_channel_turns,
    speaker_similarity,
)


def _voiced_tone(f0: float, duration_s: float = 1.2, sr: int = 16000, amp: float = 0.25) -> np.ndarray:
    t = np.arange(int(sr * duration_s), dtype=np.float32) / float(sr)
    # Build harmonic stack shaped by different spectral envelopes per pitch family
    sig = 0.65 * np.sin(2.0 * np.pi * f0 * t)
    if f0 < 150.0:
        sig += 0.45 * np.sin(2.0 * np.pi * (2 * f0) * t) + 0.15 * np.sin(2.0 * np.pi * (3 * f0) * t)
    else:
        sig += 0.20 * np.sin(2.0 * np.pi * (3 * f0) * t) + 0.55 * np.sin(2.0 * np.pi * (5 * f0) * t)
    peak = float(np.max(np.abs(sig))) or 1.0
    return (sig / peak * amp).astype(np.float32)


def test_speaker_embedding_separates_distinct_voices_and_matches_same_voice() -> None:
    low_a = _voiced_tone(118.0)
    low_b = _voiced_tone(121.0)
    high = _voiced_tone(220.0)

    emb_a = extract_speaker_embedding(low_a)
    emb_b = extract_speaker_embedding(low_b)
    emb_h = extract_speaker_embedding(high)

    assert emb_a is not None
    assert emb_b is not None
    assert emb_h is not None
    assert speaker_similarity(emb_a, emb_b) >= 0.85
    assert speaker_similarity(emb_a, emb_h) < 0.72
    assert extract_speaker_embedding(np.zeros(16000, dtype=np.float32)) is None


def test_segment_channel_turns_suppresses_echo_and_orders_chronologically() -> None:
    sr = 16000
    mic = np.zeros(sr * 3, dtype=np.float32)
    loop = np.zeros(sr * 3, dtype=np.float32)

    remote_voice = _voiced_tone(215.0, duration_s=0.9, sr=sr, amp=0.35)
    local_voice = _voiced_tone(120.0, duration_s=0.9, sr=sr, amp=0.32)

    # 0.2s - 1.1s: remote speaker speaks + quiet speaker-to-mic bleed (20% amplitude)
    loop[int(0.2 * sr) : int(1.1 * sr)] = remote_voice
    mic[int(0.2 * sr) : int(1.1 * sr)] = remote_voice * 0.20

    # 1.7s - 2.6s: local user responds clearly on mic
    mic[int(1.7 * sr) : int(2.6 * sr)] = local_voice

    turns = segment_channel_turns(mic, loop, sample_rate=sr)
    assert [t.channel for t in turns] == ["loopback", "mic"]


def test_speaker_tracker_assigns_you_vs_remote_and_in_room_speakers() -> None:
    voice_low = _voiced_tone(118.0)
    voice_high = _voiced_tone(220.0)
    voice_mid = _voiced_tone(168.0)

    # 1) Dual-stream call: mic is You, distinct remote voices become Speaker 1 / Speaker 2
    call_tracker = SpeakerTracker()
    assert call_tracker.assign(voice_low, "mic") == "You"
    assert call_tracker.assign(voice_high, "loopback") == "Speaker 1"
    assert call_tracker.assign(voice_mid, "loopback") == "Speaker 2"
    assert call_tracker.assign(_voiced_tone(222.0), "loopback") == "Speaker 1"
    # Even if mic voice shifts slightly, remote call mode keeps mic pinned to You
    assert call_tracker.assign(voice_mid, "mic") == "You"

    # 2) Mic-only room recording: first voice is You, distinct second person becomes Speaker 2
    room_tracker = SpeakerTracker()
    assert room_tracker.assign(voice_low, "mic") == "You"
    assert room_tracker.assign(voice_high, "mic") == "Speaker 2"
    assert room_tracker.assign(_voiced_tone(120.0), "mic") == "You"


def test_append_speaker_turn_merges_close_same_speaker_blocks() -> None:
    t = ""
    t = append_speaker_turn(t, "You", 4, "Let's check the metrics.")
    assert t == "[00:04] You: Let's check the metrics."

    # Same speaker 10s later merges inline cleanly
    t = append_speaker_turn(t, "You", 14, "Latency looks great.")
    assert t == "[00:04] You: Let's check the metrics. Latency looks great."

    # Different speaker starts a labeled new turn
    t = append_speaker_turn(t, "Speaker 1", 20, "Agreed, ship it today.")
    assert t == (
        "[00:04] You: Let's check the metrics. Latency looks great.\n\n"
        "[00:20] Speaker 1: Agreed, ship it today."
    )

    # Same speaker after >= 45s gap starts a fresh timestamped paragraph
    t = append_speaker_turn(t, "Speaker 1", 72, "One final question on docs.")
    assert t.endswith("[01:12] Speaker 1: One final question on docs.")
