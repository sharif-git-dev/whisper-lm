"""Tests for audio WAV encoding, resampling, and Silero VAD silence trimming."""

from pathlib import Path
import io
import wave

import numpy as np
import pytest

from whisper_lm.audio import float32_to_wav_bytes, resample_mono
from whisper_lm.sound import generate_activation_chime_wav, play_activation_sound
from whisper_lm.vad import SileroVAD


def test_float32_to_wav_bytes_roundtrip() -> None:
    samples = np.linspace(-0.5, 0.5, 1600, dtype=np.float32)
    wav_bytes = float32_to_wav_bytes(samples, sample_rate=16000)

    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2
        assert wf.getframerate() == 16000
        assert wf.getnframes() == 1600


def test_resample_mono_ratio() -> None:
    src = np.ones(48000, dtype=np.float32)
    dst = resample_mono(src, 48000, 16000)
    assert dst.shape == (16000,)
    assert np.allclose(dst, 1.0, atol=1e-4)


def test_silero_vad_drops_pure_silence() -> None:
    vad_path = Path("models/silero_vad.onnx")
    if not vad_path.exists():
        pytest.skip("models/silero_vad.onnx not present")

    vad = SileroVAD(vad_path)
    silence = np.zeros(16000, dtype=np.float32)
    speech = vad.extract_speech(silence, sample_rate=16000)
    assert speech.size == 0


def test_activation_chime_wav_is_valid_and_gentle() -> None:
    wav_bytes = generate_activation_chime_wav()
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2
        assert wf.getframerate() == 44100
        frames = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
    peak = float(np.max(np.abs(frames))) / 32767.0
    assert 0.05 < peak <= 0.25
    play_activation_sound()


def test_resample_attenuates_aliasing_and_prepare_speech_boosts_quiet_voice(monkeypatch) -> None:
    from whisper_lm.audio import prepare_speech_for_asr

    # 15 kHz tone at 48 kHz exceeds 8 kHz Nyquist of 16 kHz target -> anti-aliasing filter must crush it
    t48 = np.arange(48000, dtype=np.float32) / 48000.0
    high_alias = np.sin(2.0 * np.pi * 15000.0 * t48).astype(np.float32)
    down = resample_mono(high_alias, 48000, 16000)
    assert float(np.sqrt(np.mean(down * down))) < 0.05

    # Quiet speech with DC offset gets centered and boosted, while pure digital silence stays quiet
    t16 = np.arange(16000, dtype=np.float32) / 16000.0
    quiet_voice = (0.04 + 0.03 * np.sin(2.0 * np.pi * 180.0 * t16)).astype(np.float32)
    prepared = prepare_speech_for_asr(quiet_voice, sample_rate=16000)
    assert abs(float(np.mean(prepared))) < 0.005
    assert float(np.max(np.abs(prepared))) > 0.25

    zero_out = prepare_speech_for_asr(np.zeros(16000, dtype=np.float32), sample_rate=16000)
    assert float(np.max(np.abs(zero_out))) == 0.0

    # Verify play_activation_sound never triggers winsound SND_MEMORY | SND_ASYNC error
    import winsound
    calls: list[tuple[str, int]] = []
    monkeypatch.setattr(winsound, "PlaySound", lambda arg, flags: calls.append((str(arg), int(flags))))
    play_activation_sound()
    assert len(calls) == 1
    assert calls[0][0].endswith(".wav")
    assert calls[0][1] & winsound.SND_FILENAME
    assert calls[0][1] & winsound.SND_ASYNC
