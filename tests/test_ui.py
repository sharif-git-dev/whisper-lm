"""Tests for Wispr-style HUD waveform pill, Hub navigation/meetings UI, tray, and single-instance mutex."""

from PySide6.QtWidgets import QApplication, QPushButton

from whisper_lm.app import (
    _acquire_single_instance_mutex,
    _signal_existing_instance,
    _start_ipc_server,
)
from whisper_lm.config import AppConfig
from whisper_lm.meetings import MeetingRecord
from whisper_lm.ui import (
    FlowHubWindow,
    FlowPillWidget,
    HistoryEntry,
    MeetingPillWidget,
    TrayController,
    render_brand_pixmap,
)


def test_hud_animation_moves_bars_when_listening() -> None:
    app = QApplication.instance() or QApplication([])
    del app

    hud = FlowPillWidget()
    hud.set_state("listening", "Listening...", 0)
    assert hud._anim_timer.isActive()
    assert hud._waveform.isVisibleTo(hud)
    assert not hud._label.isVisibleTo(hud)

    before = list(hud._waveform._bars)
    hud.level_received.emit(0.95)
    hud._on_anim_tick()
    hud._on_anim_tick()
    after = list(hud._waveform._bars)
    assert before != after
    assert max(after) > 0.35

    hud.set_state("transcribing", "Transcribing...", 0)
    assert hud._waveform.isVisibleTo(hud)
    assert not hud._label.isVisibleTo(hud)

    hud.set_state("idle", "", 0)
    assert not hud._anim_timer.isActive()
    assert hud.isVisible()
    assert (hud.width(), hud.height()) == FlowPillWidget._IDLE_SIZE
    hud.hide()


def test_hud_always_on_click_drag_and_toggle() -> None:
    from PySide6.QtCore import QEvent, QPoint, Qt
    from PySide6.QtGui import QMouseEvent

    app = QApplication.instance() or QApplication([])
    del app

    toggles: list[str] = []
    hud = FlowPillWidget(always_on=True, on_toggle_dictate=lambda: toggles.append("toggle"))
    assert hud.isVisible()
    assert (hud.width(), hud.height()) == FlowPillWidget._IDLE_SIZE

    # Simulated click (press + release with <6px movement) triggers dictation toggle
    local_pos = hud.rect().center()
    global_pos = hud.mapToGlobal(local_pos)
    press = QMouseEvent(
        QEvent.Type.MouseButtonPress,
        local_pos.toPointF(),
        global_pos.toPointF(),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    release = QMouseEvent(
        QEvent.Type.MouseButtonRelease,
        local_pos.toPointF(),
        global_pos.toPointF(),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    hud.mousePressEvent(press)
    hud.mouseReleaseEvent(release)
    assert toggles == ["toggle"]

    # After fade_out in always-on mode, shrinks smoothly back to idle icon rather than hiding
    hud.set_state("listening", "", 0)
    assert hud._morph_anim.endValue() == 1.0
    hud._morph_anim.setCurrentTime(hud._morph_anim.duration())
    hud._set_morph_progress(1.0)
    assert (hud.width(), hud.height()) == FlowPillWidget._ACTIVE_SIZE
    assert hud._waveform._reveal == 1.0

    hud.set_state("fade_out", "", 0)
    assert hud.isVisible()
    assert hud._morph_anim.endValue() == 1.0
    hud._morph_anim.setCurrentTime(hud._morph_anim.duration() // 2)
    mid_val = float(hud._morph_anim.currentValue())
    hud._set_morph_progress(mid_val)
    assert FlowPillWidget._IDLE_SIZE[0] < hud.width() < FlowPillWidget._ACTIVE_SIZE[0]

    hud._morph_anim.stop()
    hud._set_morph_progress(1.0)
    hud._on_morph_finished()
    assert (hud.width(), hud.height()) == FlowPillWidget._IDLE_SIZE

    # Disabling always-on hides idle icon; re-enabling shows it again
    hud.set_always_on(False)
    assert not hud.isVisible()
    hud.set_always_on(True)
    assert hud.isVisible()
    hud.hide()


def test_hud_hover_expands_record_button() -> None:
    app = QApplication.instance() or QApplication([])
    del app

    dictates: list[str] = []
    recordings: list[str] = []
    hud = FlowPillWidget(
        always_on=True,
        on_toggle_dictate=lambda: dictates.append("dictate"),
        on_toggle_recording=lambda: recordings.append("record"),
    )
    assert (hud.width(), hud.height()) == FlowPillWidget._IDLE_SIZE
    assert not hud._record_btn.isVisibleTo(hud)

    # Hovering idle bar expands smoothly to reveal the small Record button
    hud._set_hovered(True)
    hud._morph_anim.stop()
    hud._set_morph_progress(1.0)
    hud._on_morph_finished()
    assert (hud.width(), hud.height()) == FlowPillWidget._HOVER_SIZE
    assert hud._record_btn.isVisibleTo(hud)

    # Clicking the small Record button starts/stops general recording without triggering dictation
    hud._record_btn.click()
    assert recordings == ["record"]
    assert dictates == []

    hud.recording_state_changed.emit(True)
    assert hud._record_btn._recording
    assert "Stop" in hud._record_btn.toolTip()

    # Starting dictation hides Record button and keeps clean active waveform pill
    hud.set_state("listening", "", 0)
    assert not hud._record_btn.isVisibleTo(hud)
    hud._morph_anim.stop()
    hud._set_morph_progress(1.0)
    assert (hud.width(), hud.height()) == FlowPillWidget._ACTIVE_SIZE

    # Finishing dictation without hover returns smoothly to resting idle size
    hud._hovered = False
    hud.set_state("fade_out", "", 0)
    hud._morph_anim.stop()
    hud._set_morph_progress(1.0)
    hud._on_morph_finished()
    assert (hud.width(), hud.height()) == FlowPillWidget._IDLE_SIZE
    hud.hide()


def test_hub_navigation_and_meetings_view() -> None:
    app = QApplication.instance() or QApplication([])
    del app

    saved_configs: list[AppConfig] = []
    cfg = AppConfig(hotkey_hold="ctrl+win", custom_dictionary=["Wispr"])
    hub = FlowHubWindow(cfg, saved_configs.append, lambda _t: None)
    assert hub._stack.currentIndex() == 0

    hub.open_page("meetings")
    assert hub._stack.currentIndex() == 1

    hub.open_page("dictionary")
    assert hub._stack.currentIndex() == 2
    hub._dict_input.setText("OpenAI")
    hub._dict_add_btn.click()
    assert "OpenAI" in saved_configs[-1].custom_dictionary
    hub._remove_dictionary_word("OpenAI")
    assert "OpenAI" not in saved_configs[-1].custom_dictionary

    hub.open_page("settings")
    assert hub._stack.currentIndex() == 3
    hub.open_page("meetings")

    rec = MeetingRecord(
        id="20261114_080000_zoom",
        title="Product Sync",
        app_name="Zoom",
        started_at="2026-11-14 08:00",
        duration_seconds=95,
        raw_transcript="um lets ship v2",
        cleaned_transcript="Let's ship v2.",
        status="completed",
    )
    hub._set_meetings([rec])
    assert hub._meeting_list.count() == 1
    assert hub._detail_body.toPlainText() == "Let's ship v2."

    hub._switch_meeting_view("raw")
    assert hub._detail_body.toPlainText() == "um lets ship v2"

    hub._append_history(
        HistoryEntry("08:05:00", "Code.exe", "um test", "Test.", "en")
    )
    assert hub._tile_words.value_lbl.text() == "1"

    # Loading month-old history groups by date/time bucket and supports search + filter
    month_old = HistoryEntry(
        "14:20:00",
        "Notion",
        "quarterly budget",
        "Quarterly budget review.",
        "en",
        date="2026-10-10",
    )
    today_entry = HistoryEntry("08:05:00", "Code.exe", "um test", "Test.", "en")
    hub._set_history([today_entry, month_old])
    assert hub._tile_words.value_lbl.text() == "4"
    assert hub._tile_sessions.value_lbl.text() == "2"
    assert hub._tile_days.value_lbl.text() == "2"
    hub._history_search.setText("quarterly")
    assert "Matching (1 of 2)" in hub._history_section_lbl.text()
    hub._history_search.clear()

    # Starting a new live recording switches selection away from previously viewed old meeting
    hub._set_meetings([rec])
    assert hub._selected_meeting_id == rec.id
    live_rec = MeetingRecord(
        id="20261114_081500_live",
        title="Live Sync",
        app_name="Manual",
        started_at="2026-11-14 08:15",
        duration_seconds=3,
        raw_transcript="Live sentence.",
        status="recording",
    )
    hub._set_meetings([live_rec, rec])
    assert hub._selected_meeting_id == live_rec.id
    assert "Live sentence." in hub._detail_body.toPlainText()
    hub.close()


def test_meeting_pill_states() -> None:
    app = QApplication.instance() or QApplication([])
    del app

    pill = MeetingPillWidget(on_stop_clicked=lambda: None, on_open_hub=lambda: None)
    pill._apply_state("recording", "Google Meet")
    assert pill._tick_timer.isActive()
    assert "Google Meet" in pill._text.text()

    pill._apply_state("cleaning", "Google Meet")
    assert not pill._tick_timer.isActive()
    assert "Polishing" in pill._text.text()

    pill._apply_state("idle", "")
    assert not pill.isVisible()


def test_tray_shortcut_preset_includes_ctrl_win() -> None:
    app = QApplication.instance() or QApplication([])
    del app

    cfg = AppConfig(hotkey_hold="ctrl+win")
    chosen: list[str] = []
    win = FlowHubWindow(cfg, lambda _c: None, lambda _t: None)
    tray = TrayController(
        config=cfg,
        window=win,
        on_toggle_cleanup=lambda _b: None,
        on_change_hold_hotkey=chosen.append,
        on_quit=lambda: None,
    )
    assert "ctrl+win" in tray._hold_actions
    assert tray._hold_actions["ctrl+win"].isChecked()
    tray._hold_actions["ctrl+win"].trigger()
    assert chosen == ["ctrl+win"]
    tray.hide()


def test_single_instance_mutex_blocks_duplicate() -> None:
    import ctypes
    import uuid

    test_name = f"Local\\WhisperLMTest_{uuid.uuid4().hex}"
    first = _acquire_single_instance_mutex(test_name)
    try:
        assert first is not None
        second = _acquire_single_instance_mutex(test_name)
        assert second is None
    finally:
        if first is not None:
            ctypes.WinDLL("kernel32").CloseHandle(first)


def test_hub_meeting_summary_tab_trigger_and_settings() -> None:
    app = QApplication.instance() or QApplication([])
    from whisper_lm.config import AppConfig
    from whisper_lm.meetings import MeetingRecord
    from whisper_lm.ui import FlowHubWindow, MeetingPillWidget

    saved_cfgs: list[AppConfig] = []
    summarized_ids: list[str] = []

    cfg = AppConfig(meeting_summary_enabled=True)
    hub = FlowHubWindow(
        config=cfg,
        on_save_config=saved_cfgs.append,
        on_copy_text=lambda _t: None,
        on_summarize_meeting=summarized_ids.append,
    )

    rec_with_summary = MeetingRecord(
        id="20261114_120000_standup",
        title="Weekly Standup",
        app_name="Zoom",
        started_at="2026-11-14 12:00",
        duration_seconds=180,
        raw_transcript="[00:01] You: uh let's deploy today",
        cleaned_transcript="[00:01] You: Let's deploy today.",
        summary="## Overview\nAgreed to deploy today.",
        status="completed",
    )
    hub._set_meetings([rec_with_summary])
    app.processEvents()

    assert hub._meeting_view_mode == "summary"
    assert "Agreed to deploy today." in hub._detail_body.toPlainText()
    assert hub._btn_summarize_meeting.text() == "Regenerate"

    hub._switch_meeting_view("cleaned")
    assert hub._detail_body.toPlainText() == "[00:01] You: Let's deploy today."

    hub._switch_meeting_view("raw")
    assert hub._detail_body.toPlainText() == "[00:01] You: uh let's deploy today"

    hub._summarize_selected_meeting()
    assert summarized_ids == ["20261114_120000_standup"]
    assert hub._btn_summarize_meeting.text() == "Working..."
    assert not hub._btn_summarize_meeting.isEnabled()

    assert hub._meeting_summary_switch.isChecked() is True
    hub._meeting_summary_switch.setChecked(False)
    hub._save_settings()
    assert saved_cfgs[-1].meeting_summary_enabled is False

    pill = MeetingPillWidget(on_stop_clicked=lambda: None, on_open_hub=lambda: None)
    pill._apply_state("summarizing", "Zoom")
    assert pill._text.text() == "Summarizing meeting..."
    pill._apply_state("idle", "")

    hub.close()
    pill.close()


def test_arabic_toggle_in_settings_and_tray() -> None:
    app = QApplication.instance() or QApplication([])
    from whisper_lm.config import AppConfig
    from whisper_lm.ui import FlowHubWindow, TrayController

    saved_cfgs: list[AppConfig] = []
    tray_toggles: list[bool] = []
    hub = FlowHubWindow(
        config=AppConfig(whisper_language="en"),
        on_save_config=saved_cfgs.append,
        on_copy_text=lambda _t: None,
    )
    assert hub._arabic_switch.isChecked() is False

    hub._arabic_switch.setChecked(True)
    hub._save_settings()
    assert saved_cfgs[-1].whisper_language == "auto"

    hub.sync_from_config(AppConfig(whisper_language="en"))
    assert hub._arabic_switch.isChecked() is False

    tray = TrayController(
        config=AppConfig(whisper_language="en"),
        window=hub,
        on_toggle_cleanup=lambda _b: None,
        on_change_hold_hotkey=lambda _k: None,
        on_toggle_arabic_mode=tray_toggles.append,
    )
    assert tray._arabic_action.isChecked() is False
    tray._arabic_action.trigger()
    assert tray_toggles == [True]

    app.processEvents()
    tray.hide()
    hub.close()


def test_whisperlm_brand_icon_setup_banner_and_ipc_activate() -> None:
    import time
    import uuid

    app = QApplication.instance() or QApplication([])
    pix = render_brand_pixmap(64)
    assert not pix.isNull()
    assert pix.width() == 64 and pix.height() == 64

    hub = FlowHubWindow(
        config=AppConfig(),
        on_save_config=lambda _c: None,
        on_copy_text=lambda _t: None,
    )
    assert hub.windowTitle() == "WhisperLM"

    hub.runtime_status_changed.emit("Downloading Whisper speech model... 42%", 42)
    app.processEvents()
    assert hub._setup_banner.isVisibleTo(hub)
    assert hub._setup_progress.value() == 42
    assert hub._setup_pct_lbl.text() == "42%"

    hub.runtime_status_changed.emit("Ready", -1)
    app.processEvents()
    assert not hub._setup_banner.isVisibleTo(hub)

    activated: list[str] = []
    bus_name = f"WhisperLM_Test_{uuid.uuid4().hex}"
    server = _start_ipc_server(lambda: activated.append("up"), server_name=bus_name)
    try:
        assert _signal_existing_instance(server_name=bus_name) is True
        deadline = time.monotonic() + 1.0
        while not activated and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        assert activated == ["up"]
    finally:
        server.close()
        hub.close()


def test_launch_at_startup_setting_and_tray_sync() -> None:
    app = QApplication.instance() or QApplication([])
    saved_cfgs: list[AppConfig] = []
    tray_toggles: list[bool] = []
    hub = FlowHubWindow(
        config=AppConfig(launch_at_startup=True),
        on_save_config=saved_cfgs.append,
        on_copy_text=lambda _t: None,
    )
    assert hub._startup_switch.isChecked() is True

    hub._startup_switch.setChecked(False)
    hub._save_settings()
    assert saved_cfgs[-1].launch_at_startup is False

    hub.sync_from_config(AppConfig(launch_at_startup=True))
    assert hub._startup_switch.isChecked() is True

    tray = TrayController(
        config=AppConfig(launch_at_startup=False),
        window=hub,
        on_toggle_cleanup=lambda _b: None,
        on_change_hold_hotkey=lambda _k: None,
        on_toggle_startup=tray_toggles.append,
    )
    assert tray._startup_action.isChecked() is False
    tray._startup_action.trigger()
    assert tray_toggles == [True]

    app.processEvents()
    tray.hide()
    hub.close()


def test_hub_update_banner_and_cloud_backup_settings() -> None:
    from whisper_lm.updater import UpdateInfo

    app = QApplication.instance() or QApplication([])
    saved_cfgs: list[AppConfig] = []
    installed_updates: list[UpdateInfo] = []
    hub = FlowHubWindow(
        config=AppConfig(cloud_backup_mode="auto"),
        on_save_config=saved_cfgs.append,
        on_copy_text=lambda _t: None,
        on_install_update=installed_updates.append,
    )

    assert not hub._update_banner.isVisibleTo(hub)
    assert not hub._cloud_folder_row.isVisibleTo(hub)
    assert not hub._cloud_firebase_row.isVisibleTo(hub)
    assert not hub._cloud_github_row.isVisibleTo(hub)

    info = UpdateInfo(
        version="0.9.0",
        tag_name="v0.9.0",
        release_notes="Better sync",
        asset_name="WhisperLM-Setup.exe",
        download_url="https://example.invalid/WhisperLM-Setup.exe",
        size_bytes=80_000_000,
    )
    hub.update_available.emit(info, False)
    app.processEvents()
    assert hub._update_banner.isVisibleTo(hub)
    assert "0.9.0" in hub._update_banner_lbl.text()
    hub._update_now_btn.click()
    assert installed_updates == [info]

    # Switching dropdown reveals only the matching BYO input row
    hub._set_combo_data(hub._cloud_combo, "firebase")
    assert not hub._cloud_firebase_row.isHidden()
    assert hub._cloud_github_row.isHidden()
    hub._firebase_url_input.setText("https://demo-db.firebaseio.com")
    hub._save_settings()
    assert saved_cfgs[-1].cloud_backup_mode == "firebase"
    assert saved_cfgs[-1].cloud_firebase_url == "https://demo-db.firebaseio.com"

    hub.close()


def test_home_hides_clear_button_and_settings_collapses_clear_history() -> None:
    app = QApplication.instance() or QApplication([])
    cleared: list[bool] = []
    hub = FlowHubWindow(
        config=AppConfig(),
        on_save_config=lambda _cfg: None,
        on_copy_text=lambda _t: None,
        on_clear_history=lambda: cleared.append(True),
    )
    hub._set_history(
        [
            HistoryEntry(
                "10:00:00",
                "Notes",
                "hello world",
                "Hello world.",
                "en",
                date="2026-11-14",
            )
        ]
    )

    home_page = hub._stack.widget(0)
    home_btn_texts = [btn.text().strip() for btn in home_page.findChildren(QPushButton)]
    assert "Clear" not in home_btn_texts

    hub._switch_page(3)
    assert hub._data_panel.isHidden()
    assert hub._clear_history_btn.text() == "Clear All Dictations"

    hub._data_toggle_btn.click()
    assert not hub._data_panel.isHidden()
    assert hub._data_toggle_btn.text().endswith("▾")

    hub._tile_words.pulse()
    hub._brand_mark.set_pulsing(True)
    hub._brand_mark.set_pulsing(False)

    hub._clear_history()
    assert cleared == [True]
    assert hub._tile_sessions.value_lbl.text() == "0"

    hub._data_toggle_btn.click()
    assert hub._data_panel.isHidden()
    assert hub._data_toggle_btn.text().endswith("▸")
    app.processEvents()
    hub.close()


def test_meeting_audio_bar_visibility_playback_and_setting(tmp_path, monkeypatch) -> None:
    import wave
    import numpy as np

    app = QApplication.instance() or QApplication([])
    meetings_dir = tmp_path / "meetings"
    meetings_dir.mkdir(parents=True)

    rec_with_audio = MeetingRecord(
        id="20261114_200000_audio",
        title="Design Review",
        app_name="Google Meet",
        started_at="2026-11-14 20:00",
        duration_seconds=2,
        raw_transcript="[00:00] You: Let's review the spec.",
        cleaned_transcript="[00:00] You: Let's review the spec.",
        audio_file="20261114_200000_audio.wav",
        status="completed",
    )
    wav_path = meetings_dir / rec_with_audio.audio_file
    with wave.open(str(wav_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(np.zeros(20000, dtype=np.int16).tobytes())

    rec_without_audio = MeetingRecord(
        id="20261114_190000_textonly",
        title="Older Call",
        app_name="Zoom",
        started_at="2026-11-14 19:00",
        duration_seconds=30,
        cleaned_transcript="Transcript only.",
        status="completed",
    )

    class FakeOutputStream:
        def __init__(self, **_kwargs) -> None:
            self.started = False
            self.stopped = False

        def start(self) -> None:
            self.started = True

        def stop(self) -> None:
            self.stopped = True

        def close(self) -> None:
            pass

    monkeypatch.setattr("whisper_lm.ui.sd.OutputStream", FakeOutputStream)

    saved_cfgs: list[AppConfig] = []
    deleted_ids: list[str] = []
    hub = FlowHubWindow(
        config=AppConfig(),
        on_save_config=saved_cfgs.append,
        on_copy_text=lambda _t: None,
        on_delete_meeting=deleted_ids.append,
        meetings_dir=meetings_dir,
    )

    # Default setting enables meeting reference audio and persists toggles cleanly
    assert hub._meeting_save_audio_switch.isChecked() is True
    hub._meeting_save_audio_switch.setChecked(False)
    hub._save_settings()
    assert saved_cfgs[-1].meeting_save_audio is False

    # Selecting meeting with saved WAV shows compact player bar and controls playback/seek cleanly
    hub._set_meetings([rec_with_audio, rec_without_audio])
    assert not hub._audio_bar.isHidden()
    assert hub._audio_bar._slider.maximum() == 20000
    assert hub._audio_bar._time_lbl.text() == "00:00 / 00:01"

    hub._audio_bar.toggle_play()
    assert hub._audio_bar._playing is True
    assert hub._audio_bar._play_btn.text() == "Pause"

    hub._audio_bar.seek(16000)
    assert hub._audio_bar._pos_frames == 16000
    assert hub._audio_bar._time_lbl.text() == "00:01 / 00:01"

    hub._audio_bar.toggle_play()
    assert hub._audio_bar._playing is False
    assert hub._audio_bar._play_btn.text() == "Play"

    # Switching to text-only meeting hides player bar and releases file handles
    hub._meeting_list.setCurrentRow(1)
    assert hub._audio_bar.isHidden()

    # Switching back and deleting releases file handles cleanly before deleting
    hub._meeting_list.setCurrentRow(0)
    assert not hub._audio_bar.isHidden()
    hub._delete_selected_meeting()
    assert deleted_ids == [rec_with_audio.id]
    assert hub._audio_bar.isHidden()
    wav_path.unlink()

    app.processEvents()
    hub.close()
