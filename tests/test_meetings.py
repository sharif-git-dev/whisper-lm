"""Unit tests for meeting detection, timed dual-stream mixing, storage, and controller."""

from pathlib import Path
from types import SimpleNamespace

import numpy as np

from whisper_lm.config import AppConfig
from whisper_lm.meetings import (
    DetectedMeeting,
    MeetingDetector,
    MeetingRecord,
    MeetingRecorderController,
    MeetingStore,
    TimedMixBuffer,
    classify_active_meeting,
)


def test_classify_active_meeting_browser_titles_and_ignores_self() -> None:
    # Self / YouTube / text editor should never trigger meeting detection
    idle = classify_active_meeting(
        windows=[
            ("chrome.exe", "Lo-fi hip hop radio - YouTube - Google Chrome"),
            ("python.exe", "WhisperLM"),
        ],
        active_mic_apps={"python.exe"},
    )
    assert idle is None

    # Unambiguous Google Meet code tab triggers even while muted (no active mic)
    meet = classify_active_meeting(
        windows=[("chrome.exe", "Meet - abc-defg-hij - Google Chrome")],
        active_mic_apps=set(),
    )
    assert meet is not None
    assert meet.app_name == "Google Meet"
    assert meet.reason == "web-title"


def test_classify_active_meeting_requires_call_signal_for_chat_apps() -> None:
    # Texting on Discord / WhatsApp should not trigger meeting recording
    texting = classify_active_meeting(
        windows=[("discord.exe", "General - Discord"), ("whatsapp.root.exe", "WhatsApp")],
        active_mic_apps=set(),
    )
    assert texting is None

    # Active mic on WhatsApp packaged app triggers meeting detection
    call = classify_active_meeting(
        windows=[("whatsapp.root.exe", "WhatsApp")],
        active_mic_apps={"whatsappdesktop"},
    )
    assert call is not None
    assert call.app_name == "WhatsApp Call"
    assert call.reason == "app+mic"

    # Minimized Zoom holding mic still triggers even without a top-level Zoom window
    minimized_zoom = classify_active_meeting(windows=[], active_mic_apps={"zoom.exe"})
    assert minimized_zoom is not None
    assert minimized_zoom.app_name == "Zoom"
    assert minimized_zoom.reason == "minimized+mic"


def test_meeting_detector_debounce_and_manual_stop_suppression() -> None:
    started: list[DetectedMeeting] = []
    ended: list[DetectedMeeting] = []
    detector = MeetingDetector(
        on_meeting_started=started.append,
        on_meeting_ended=ended.append,
        start_debounce_seconds=3.0,
        stop_debounce_seconds=6.0,
    )

    from whisper_lm import meetings as m_mod

    orig_win = m_mod.list_visible_windows
    orig_mic = m_mod.get_active_microphone_apps
    state = {"in_call": True}
    try:
        m_mod.list_visible_windows = lambda: [("zoom.exe", "Zoom Meeting")] if state["in_call"] else []
        m_mod.get_active_microphone_apps = lambda: {"zoom.exe"} if state["in_call"] else set()

        detector._poll_once(now=0.0)
        assert started == []
        detector._poll_once(now=3.5)
        assert len(started) == 1

        # Manual stop suppresses re-triggering while Zoom window remains open
        detector.mark_manually_stopped()
        detector._poll_once(now=5.0)
        detector._poll_once(now=10.0)
        assert len(started) == 1

        # Closing window clears suppression, so a subsequent meeting starts again
        state["in_call"] = False
        detector._poll_once(now=12.0)
        state["in_call"] = True
        detector._poll_once(now=13.0)
        detector._poll_once(now=17.0)
        assert len(started) == 2
    finally:
        m_mod.list_visible_windows = orig_win
        m_mod.get_active_microphone_apps = orig_mic


def test_timed_mix_buffer_aligns_intermittent_loopback() -> None:
    buf = TimedMixBuffer(target_rate=100)
    buf.reset(now=10.0)

    # Continuous mic for 1.0s at 0.25 amplitude
    buf.push_mic(np.full(100, 0.25, dtype=np.float32), timestamp=11.0)
    # Loopback only plays during the second half [10.5s .. 11.0s] at 0.30 amplitude
    buf.push_loopback(np.full(50, 0.30, dtype=np.float32), timestamp=11.0)

    mixed = buf.drain(now=11.0)
    assert mixed.shape == (100,)
    assert np.allclose(mixed[:50], 0.25, atol=1e-4)
    assert np.allclose(mixed[50:], 0.55, atol=1e-4)


def test_timed_mix_buffer_separates_multiple_loopback_tracks() -> None:
    from whisper_lm.meetings import _apply_loopback_gain

    buf = TimedMixBuffer(target_rate=100)
    buf.reset(now=10.0)

    buf.push_mic(np.full(100, 0.20, dtype=np.float32), timestamp=11.0)
    # Two different playback devices emit non-overlapping loopback chunks without colliding cursors
    buf.push_loopback(np.full(40, 0.30, dtype=np.float32), timestamp=10.4, track_id="20")
    buf.push_loopback(np.full(40, 0.40, dtype=np.float32), timestamp=11.0, track_id="21")

    mixed = buf.drain(now=11.0)
    assert mixed.shape == (100,)
    assert np.allclose(mixed[:40], 0.50, atol=1e-4)
    assert np.allclose(mixed[40:60], 0.20, atol=1e-4)
    assert np.allclose(mixed[60:], 0.60, atol=1e-4)

    # Low-volume remote speaker audio is boosted without boosting near-zero floor
    env = [0.0]
    quiet_speech = np.full(64, 0.04, dtype=np.float32)
    boosted = _apply_loopback_gain(quiet_speech, env)
    assert float(np.max(boosted)) > 0.10
    noise_floor = np.full(64, 0.0005, dtype=np.float32)
    assert np.allclose(_apply_loopback_gain(noise_floor, [0.0]), noise_floor)


def test_meeting_store_roundtrip_and_delete(tmp_path: Path) -> None:
    store = MeetingStore(tmp_path / "meetings")
    rec = MeetingRecord(
        id="20261114_070000_meet",
        title="Weekly Sync",
        app_name="Google Meet",
        started_at="2026-11-14 07:00",
        ended_at="2026-11-14 07:10",
        duration_seconds=600,
        raw_transcript="um hello team let's start",
        cleaned_transcript="Hello team, let's start.",
        status="completed",
        auto_detected=True,
    )
    store.save(rec)
    loaded = store.list_all()
    assert len(loaded) == 1
    assert loaded[0].display_transcript == "Hello team, let's start."
    assert (tmp_path / "meetings" / f"{rec.id}.md").exists()

    store.delete(rec.id)
    assert store.list_all() == []


def test_meeting_recorder_controller_chunk_and_cleanup(tmp_path: Path, monkeypatch) -> None:
    class FakeCapture:
        def __init__(self, **_kwargs) -> None:
            self.calls = 0

        def start(self) -> None:
            pass

        def drain_samples(self) -> np.ndarray:
            return np.full(8000, 0.2, dtype=np.float32)

        def stop(self) -> np.ndarray:
            return np.full(8000, 0.2, dtype=np.float32)

    monkeypatch.setattr("whisper_lm.meetings.DualStreamAudioCapture", FakeCapture)

    class FakeAsr:
        def transcribe(self, _wav_bytes: bytes):
            return SimpleNamespace(text="we are discussing deployment", detected_language="en")

    class FakeCleanup:
        def clean_meeting_transcript(self, raw_text: str, app_name: str = "") -> str:
            assert app_name == "Zoom"
            return f"CLEANED: {raw_text}"

    updates: list[str] = []
    cfg = AppConfig(vad_enabled=False, meeting_cleanup_enabled=True, meeting_chunk_seconds=1)
    store = MeetingStore(tmp_path / "meetings")
    ctrl = MeetingRecorderController(
        config=cfg,
        asr=FakeAsr(),  # type: ignore[arg-type]
        cleanup=FakeCleanup(),  # type: ignore[arg-type]
        vad=None,
        store=store,
        is_llm_available=lambda: True,
        on_record_updated=lambda rec: updates.append(rec.status),
    )

    rec = ctrl.start(title="Zoom Meeting", app_name="Zoom", auto_detected=True)
    assert rec is not None
    assert ctrl.is_recording
    ctrl.stop()
    ctrl.shutdown()

    saved = store.list_all()[0]
    assert saved.status == "completed"
    assert "we are discussing deployment" in saved.raw_transcript
    assert saved.cleaned_transcript.startswith("CLEANED:")
    assert "cleaning" in updates and "completed" in updates


def test_meeting_live_chunk_flushes_on_pause_before_stop(tmp_path: Path, monkeypatch) -> None:
    import time

    sr = 16000
    scripted = [
        np.full(int(sr * 1.1), 0.25, dtype=np.float32),  # Active speech (~1.1s)
        np.zeros(int(sr * 0.7), dtype=np.float32),       # Natural pause (~0.7s -> tail_rms < 0.011)
    ]

    class FakeCapture:
        def __init__(self, **_kwargs) -> None:
            self.idx = 0

        def start(self) -> None:
            pass

        def drain_samples(self) -> np.ndarray:
            if self.idx < len(scripted):
                out = scripted[self.idx]
                self.idx += 1
                return out
            return np.zeros(0, dtype=np.float32)

        def stop(self) -> np.ndarray:
            return np.zeros(0, dtype=np.float32)

    monkeypatch.setattr("whisper_lm.meetings.DualStreamAudioCapture", FakeCapture)

    class FakeAsr:
        def transcribe(self, _wav_bytes: bytes):
            return SimpleNamespace(text="Live update arrived.", detected_language="en")

    class FakeCleanup:
        def clean_meeting_transcript(self, raw_text: str, app_name: str = "") -> str:
            return raw_text

    seen_live: list[str] = []
    cfg = AppConfig(vad_enabled=False, meeting_cleanup_enabled=False, sample_rate=sr)
    ctrl = MeetingRecorderController(
        config=cfg,
        asr=FakeAsr(),  # type: ignore[arg-type]
        cleanup=FakeCleanup(),  # type: ignore[arg-type]
        vad=None,
        store=MeetingStore(tmp_path / "meetings"),
        is_llm_available=lambda: False,
        on_record_updated=lambda rec: seen_live.append(f"{rec.status}:{rec.raw_transcript}"),
    )

    ctrl.start(title="Live Test", app_name="Manual")
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and not any("Live update arrived." in s for s in seen_live):
        time.sleep(0.05)

    assert any(s.startswith("recording:") and "Live update arrived." in s for s in seen_live)
    ctrl.shutdown()


def test_dual_channel_diarization_cleanup_and_summary(tmp_path: Path, monkeypatch) -> None:
    import time
    import numpy as np
    from whisper_lm.asr import TranscriptionResult
    from whisper_lm.config import AppConfig
    from whisper_lm.meetings import DrainedChannels, MeetingRecord, MeetingRecorderController, MeetingStore

    cfg = AppConfig(
        meetings_dir=str(tmp_path / "meetings"),
        vad_enabled=False,
        meeting_chunk_seconds=2.0,
        meeting_cleanup_enabled=True,
        meeting_summary_enabled=True,
    )
    sr = cfg.sample_rate
    t = np.arange(int(sr * 1.5), dtype=np.float32) / float(sr)
    mic_voice = (0.3 * np.sin(2.0 * np.pi * 120.0 * t)).astype(np.float32)
    loop_voice = (0.35 * np.sin(2.0 * np.pi * 220.0 * t)).astype(np.float32)

    class DualFakeCapture:
        def __init__(self, **_kwargs) -> None:
            self.calls = 0

        def start(self) -> None:
            pass

        def drain_channels(self) -> DrainedChannels:
            self.calls += 1
            if self.calls == 1:
                mic = np.concatenate([mic_voice, np.zeros(int(sr * 0.6), dtype=np.float32)])
                loop = np.zeros_like(mic)
                return DrainedChannels(mixed=mic, mic=mic, loopback=loop)
            if self.calls == 2:
                loop = np.concatenate([loop_voice, np.zeros(int(sr * 0.6), dtype=np.float32)])
                mic = np.zeros_like(loop)
                return DrainedChannels(mixed=loop, mic=mic, loopback=loop)
            empty = np.empty(0, dtype=np.float32)
            return DrainedChannels(mixed=empty, mic=empty, loopback=empty)

        def drain_samples(self) -> np.ndarray:
            return self.drain_channels().mixed

        def stop_channels(self) -> DrainedChannels:
            empty = np.empty(0, dtype=np.float32)
            return DrainedChannels(mixed=empty, mic=empty, loopback=empty)

        def stop(self) -> np.ndarray:
            return np.empty(0, dtype=np.float32)

    monkeypatch.setattr("whisper_lm.meetings.DualStreamAudioCapture", DualFakeCapture)

    class DualFakeAsr:
        def __init__(self) -> None:
            self.idx = 0

        def transcribe(self, _wav_bytes: bytes) -> TranscriptionResult:
            self.idx += 1
            if self.idx == 1:
                return TranscriptionResult(text="Let's review the release plan.", detected_language="en")
            return TranscriptionResult(text="Everything looks green on my side.", detected_language="en")

    class FakeLlm:
        def clean_meeting_transcript(self, text: str, **_kwargs) -> str:
            return text.replace("Let's", "Let us")

        def summarize_meeting_transcript(self, text: str, **_kwargs) -> str:
            return f"## Overview\nRelease approved ({len(text.splitlines())} turns)."

    store = MeetingStore(Path(cfg.meetings_dir))
    updates: list[str] = []
    controller = MeetingRecorderController(
        config=cfg,
        asr=DualFakeAsr(),
        cleanup=FakeLlm(),
        vad=None,
        store=store,
        is_llm_available=lambda: True,
        on_record_updated=lambda rec: updates.append(rec.status),
    )

    rec = controller.start(title="Release Sync", app_name="Meet")
    assert rec is not None
    time.sleep(1.3)
    controller.shutdown()

    saved = store.list_all()[0]
    assert saved.status == "completed"
    assert "] You: Let's review the release plan." in saved.raw_transcript
    assert "] Speaker 1: Everything looks green on my side." in saved.raw_transcript
    assert "] You: Let us review the release plan." in saved.cleaned_transcript
    assert "## Overview" in saved.summary
    assert "summarizing" in updates

    md_content = (Path(cfg.meetings_dir) / f"{saved.id}.md").read_text(encoding="utf-8")
    assert "## Summary" in md_content

    # Re-summarizing an existing stored record updates and persists cleanly
    class FakeResum:
        def clean_meeting_transcript(self, text: str, **_kwargs) -> str:
            return text

        def summarize_meeting_transcript(self, _text: str, **_kwargs) -> str:
            return "## Overview\nUpdated summary."

    controller.cleanup = FakeResum()
    resummarized = controller.summarize_record(saved.id)
    assert resummarized is not None
    assert resummarized.summary == "## Overview\nUpdated summary."
    assert store.list_all()[0].summary == "## Overview\nUpdated summary."
