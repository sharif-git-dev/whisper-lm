"""Tests for Wispr-style hold-to-talk + quick double-tap hands-free state machine."""

from types import SimpleNamespace
import time

from PySide6.QtWidgets import QApplication
import numpy as np

from whisper_lm.app import WhisperLMApp
from whisper_lm.config import AppConfig


def _make_stub_app(monkeypatch, min_hold_ms: int = 120, double_tap_ms: int = 250) -> tuple[WhisperLMApp, dict[str, int]]:
    QApplication.instance() or QApplication([])
    monkeypatch.setattr("whisper_lm.app.play_activation_sound", lambda: None)

    cfg = AppConfig(
        hotkey_hold="ctrl+win",
        hotkey_toggle="ctrl+shift+space",
        min_hold_ms=min_hold_ms,
        double_tap_ms=double_tap_ms,
        vad_enabled=False,
        llm_cleanup_enabled=False,
        activation_sound_enabled=False,
    )
    app_ctrl = WhisperLMApp(cfg, QApplication.instance())  # type: ignore[arg-type]
    app_ctrl._state = "idle"

    counters = {"starts": 0, "cancels": 0, "finishes": 0}

    def fake_start() -> None:
        counters["starts"] += 1

    def fake_stop() -> np.ndarray:
        return np.zeros(1600, dtype=np.float32)

    app_ctrl.recorder = SimpleNamespace(start=fake_start, stop=fake_stop)  # type: ignore[assignment]
    monkeypatch.setattr("whisper_lm.app.get_foreground_context", lambda: None)

    orig_cancel = app_ctrl._cancel_recording

    def tracked_cancel() -> None:
        counters["cancels"] += 1
        orig_cancel()

    def tracked_finish() -> None:
        with app_ctrl._lock:
            if app_ctrl._state != "recording":
                return
            app_ctrl._cancel_tap_timer_locked()
            app_ctrl._state = "idle"
            app_ctrl._active_mode = None
        counters["finishes"] += 1

    monkeypatch.setattr(app_ctrl, "_cancel_recording", tracked_cancel)
    monkeypatch.setattr(app_ctrl, "_finish_and_process", tracked_finish)
    app_ctrl.hud.hide()
    app_ctrl.tray.hide()
    return app_ctrl, counters


def test_hold_starts_immediately_and_stops_on_release(monkeypatch) -> None:
    ctrl, counters = _make_stub_app(monkeypatch, min_hold_ms=40, double_tap_ms=120)
    ctrl._on_hold_down()
    assert ctrl._state == "recording"
    assert ctrl._active_mode == "hold"
    assert counters["starts"] == 1

    time.sleep(0.06)
    ctrl._on_hold_up()
    assert counters["finishes"] == 1
    assert counters["cancels"] == 0


def test_single_short_tap_cancels_after_double_tap_window(monkeypatch) -> None:
    ctrl, counters = _make_stub_app(monkeypatch, min_hold_ms=150, double_tap_ms=80)
    ctrl._on_hold_down()
    ctrl._on_hold_up()
    assert ctrl._active_mode == "tap_pending"

    time.sleep(0.16)
    assert ctrl._state == "idle"
    assert counters["cancels"] == 1
    assert counters["finishes"] == 0


def test_double_tap_locks_hands_free_until_pressed_again(monkeypatch) -> None:
    ctrl, counters = _make_stub_app(monkeypatch, min_hold_ms=150, double_tap_ms=250)

    # First tap down/up enters tap_pending
    ctrl._on_hold_down()
    ctrl._on_hold_up()
    assert ctrl._active_mode == "tap_pending"

    # Second tap within double_tap_ms promotes to hands-free toggle
    ctrl._on_hold_down()
    assert ctrl._state == "recording"
    assert ctrl._active_mode == "toggle"
    assert counters["starts"] == 1  # No mic restart

    # Releasing second tap keeps recording active!
    ctrl._on_hold_up()
    assert ctrl._state == "recording"
    assert ctrl._active_mode == "toggle"
    assert counters["finishes"] == 0

    # Pressing again stops hands-free recording
    ctrl._on_hold_down()
    assert counters["finishes"] == 1
    assert ctrl._state == "idle"


def test_meeting_start_plays_activation_chime(monkeypatch) -> None:
    app_ctrl, _counters = _make_stub_app(monkeypatch)
    chimes: list[str] = []
    monkeypatch.setattr("whisper_lm.app.play_activation_sound", lambda: chimes.append("chime"))
    monkeypatch.setattr(app_ctrl.meeting_recorder, "start", lambda **_kw: None)
    app_ctrl.config.activation_sound_enabled = True

    started = app_ctrl._start_meeting_capture(title="Sync", app_name="Google Meet", auto_detected=True)
    assert started is True
    assert chimes == ["chime"]
