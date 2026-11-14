"""Main coordinator connecting global hotkeys, audio, VAD, Vulkan servers, and UI."""

from __future__ import annotations

import ctypes
import os
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

import numpy as np
from PySide6.QtWidgets import QApplication

from whisper_flow.asr import WhisperServerClient
from whisper_flow.audio import AudioRecorder, float32_to_wav_bytes, prepare_speech_for_asr
from whisper_flow.bootstrap import ensure_assets_downloaded, missing_assets
from whisper_flow.cleanup import LlmCleanupClient
from whisper_flow.config import AppConfig
from whisper_flow.history import DictationStore
from whisper_flow.hotkeys import GlobalHotkeyListener
from whisper_flow.injector import (
    WindowContext,
    get_foreground_context,
    paste_text_at_cursor,
    set_clipboard_text,
)
from whisper_flow.meetings import (
    DetectedMeeting,
    MeetingDetector,
    MeetingRecord,
    MeetingRecorderController,
    MeetingStore,
)
from whisper_flow.servers import ServerManager
from whisper_flow.sound import play_activation_sound
from whisper_flow.ui import (
    FlowHubWindow,
    FlowPillWidget,
    HistoryEntry,
    MeetingPillWidget,
    TrayController,
    current_timestamp,
)
from whisper_flow.vad import SileroVAD


class WhisperFlowApp:
    """Orchestrates local dictation and automatic meeting recording."""

    def __init__(self, config: AppConfig, qt_app: QApplication) -> None:
        self.config = config
        self.qt_app = qt_app

        self._lock = threading.Lock()
        self._summary_lock = threading.Lock()
        self._summarizing_ids: set[str] = set()
        self._state = "loading"  # loading | idle | recording | processing
        self._record_started_at = 0.0
        self._active_mode: str | None = None  # "hold" | "tap_pending" | "toggle"
        self._tap_seq = 0
        self._tap_timer: threading.Timer | None = None
        self._target_context: WindowContext | None = None

        self.history_store = DictationStore(self.config.resolve_path(self.config.history_dir))
        self.hud = FlowPillWidget(
            always_on=self.config.always_on_pill,
            on_toggle_dictate=self._on_toggle_press,
            on_toggle_recording=self._toggle_manual_meeting,
            on_open_hub=lambda: self.window.open_page("home"),
            on_set_always_on=self._toggle_always_on_pill,
        )
        self.window = FlowHubWindow(
            config=self.config,
            on_save_config=self._apply_new_config,
            on_copy_text=self._copy_to_clipboard,
            on_start_meeting=self._manual_start_meeting,
            on_stop_meeting=self._manual_stop_meeting,
            on_delete_meeting=self._delete_meeting,
            on_toggle_meeting_auto=self._toggle_meeting_auto,
            on_summarize_meeting=self._summarize_meeting,
            on_clear_history=self.history_store.clear,
        )
        self.meeting_pill = MeetingPillWidget(
            on_stop_clicked=self._manual_stop_meeting,
            on_open_hub=lambda: self.window.open_page("meetings"),
        )
        self.tray = TrayController(
            config=self.config,
            window=self.window,
            on_toggle_cleanup=self._toggle_cleanup,
            on_change_hold_hotkey=self._change_hold_hotkey,
            on_toggle_meeting=self._toggle_manual_meeting,
            on_toggle_meeting_auto=self._toggle_meeting_auto,
            on_toggle_always_on=self._toggle_always_on_pill,
            on_toggle_arabic_mode=self._toggle_arabic_mode,
            on_quit=self.shutdown,
        )

        self.servers = ServerManager(self.config)
        self.asr = WhisperServerClient(self.config)
        self.cleanup = LlmCleanupClient(self.config)
        self.vad = self._load_vad()
        self.recorder = self._make_recorder()
        self.hotkeys = GlobalHotkeyListener(
            hold_hotkey=self.config.hotkey_hold,
            toggle_hotkey=self.config.hotkey_toggle,
            on_hold_down=self._on_hold_down,
            on_hold_up=self._on_hold_up,
            on_toggle=self._on_toggle_press,
        )

        self.meeting_store = MeetingStore(self.config.resolve_path(self.config.meetings_dir))
        self.meeting_recorder = MeetingRecorderController(
            config=self.config,
            asr=self.asr,
            cleanup=self.cleanup,
            vad=self.vad,
            store=self.meeting_store,
            is_llm_available=lambda: self.servers.llama_running,
            on_record_updated=self._on_meeting_record_updated,
        )
        self.meeting_detector = MeetingDetector(
            on_meeting_started=self._on_meeting_detected,
            on_meeting_ended=self._on_meeting_ended,
        )
        self.meeting_detector.set_enabled(self.config.meeting_auto_detect)
        self.window.history_loaded.emit(self.history_store.list_all())
        self.window.meetings_updated.emit(self.meeting_store.list_all())

    def _load_vad(self) -> SileroVAD | None:
        vad_path = self.config.resolve_path("models/silero_vad.onnx")
        if not vad_path.exists():
            return None
        return SileroVAD(vad_path)

    def _make_recorder(self) -> AudioRecorder:
        return AudioRecorder(
            sample_rate=self.config.sample_rate,
            device=self.config.input_device,
            on_level=self.hud.level_received.emit,
        )

    def start(self) -> None:
        """Boot background inference engines and enable global hotkeys."""
        self.hud.state_requested.emit("loading", "Starting Whisper Flow...", 0)
        threading.Thread(target=self._boot_worker, name="boot-servers", daemon=True).start()

    def _boot_worker(self) -> None:
        try:
            if missing_assets(self.config):
                def _on_dl_progress(msg: str) -> None:
                    self.tray.status_requested.emit(msg, "#F5F5F7")
                    self.hud.state_requested.emit("loading", msg, 0)

                ensure_assets_downloaded(self.config, on_progress=_on_dl_progress)
                if self.vad is None:
                    self.vad = self._load_vad()
                    self.meeting_recorder.vad = self.vad
                self.hud.state_requested.emit("loading", "Starting Whisper Flow...", 0)

            self.servers.start(wait_timeout_s=90.0)
            self.hotkeys.start()
            self.meeting_detector.start()
            with self._lock:
                self._state = "idle"
            self.tray.status_requested.emit("Ready", "#F5F5F7")
            label = self.config.hotkey_hold.replace("+", " + ").replace("_", " ").title()
            self.hud.state_requested.emit("done", f"Ready ({label})", 1800)
        except Exception as exc:
            with self._lock:
                self._state = "error"
            self.tray.status_requested.emit(f"Startup failed: {exc}", "#f87171")
            self.hud.state_requested.emit("error", "Startup failed (check logs)", 4000)

    def _cancel_tap_timer_locked(self) -> None:
        self._tap_seq += 1
        if self._tap_timer is not None:
            self._tap_timer.cancel()
            self._tap_timer = None

    def _on_hold_down(self) -> None:
        action = "none"
        with self._lock:
            if self._state == "idle":
                action = "start_hold"
            elif self._state == "recording":
                if self._active_mode == "tap_pending":
                    # Quick double-tap promotes to hands-free continuous recording.
                    self._cancel_tap_timer_locked()
                    self._active_mode = "toggle"
                    action = "lock_hands_free"
                elif self._active_mode == "toggle":
                    action = "finish"

        if action == "start_hold":
            self._begin_recording(mode="hold")
        elif action == "lock_hands_free":
            self.tray.status_requested.emit("Listening (Hands-Free)...", "#34d399")
        elif action == "finish":
            self._finish_and_process()

    def _on_hold_up(self) -> None:
        finish = False
        timer: threading.Timer | None = None
        with self._lock:
            if self._state != "recording" or self._active_mode != "hold":
                return
            held_ms = (time.monotonic() - self._record_started_at) * 1000.0
            if held_ms < self.config.min_hold_ms:
                # Hold briefly in tap_pending so a quick 2nd tap locks hands-free mode cleanly.
                self._active_mode = "tap_pending"
                self._cancel_tap_timer_locked()
                wait_s = max(0.05, self.config.double_tap_ms / 1000.0)
                timer = threading.Timer(wait_s, self._expire_tap_pending, args=(self._tap_seq,))
                timer.daemon = True
                self._tap_timer = timer
            else:
                finish = True

        if finish:
            self._finish_and_process()
        elif timer is not None:
            timer.start()

    def _expire_tap_pending(self, seq: int) -> None:
        with self._lock:
            if self._state != "recording" or self._active_mode != "tap_pending" or self._tap_seq != seq:
                return
        self._cancel_recording()

    def _on_toggle_press(self) -> None:
        with self._lock:
            state = self._state
            mode = self._active_mode
        if state == "idle":
            self._begin_recording(mode="toggle")
        elif state == "recording" and mode in {"toggle", "tap_pending"}:
            self._finish_and_process()

    def _begin_recording(self, mode: str) -> None:
        with self._lock:
            if self._state != "idle":
                return
            self._state = "recording"
            self._active_mode = mode
            self._record_started_at = time.monotonic()
            self._target_context = get_foreground_context()

        try:
            if self.config.activation_sound_enabled:
                play_activation_sound()
            self.recorder.start()
            self.tray.status_requested.emit("Listening...", "#34d399")
            self.hud.state_requested.emit("listening", "", 0)
        except Exception:
            with self._lock:
                self._state = "idle"
                self._active_mode = None
            self.tray.status_requested.emit("Mic error", "#f87171")
            self.hud.state_requested.emit("error", "Microphone error", 2000)

    def _cancel_recording(self) -> None:
        self.recorder.stop()
        with self._lock:
            self._cancel_tap_timer_locked()
            self._state = "idle"
            self._active_mode = None
        self.tray.status_requested.emit("Ready", "#F5F5F7")
        self.hud.state_requested.emit("idle", "", 0)

    def _finish_and_process(self) -> None:
        with self._lock:
            if self._state != "recording":
                return
            self._cancel_tap_timer_locked()
            self._state = "processing"
            self._active_mode = None
            context = self._target_context

        # Capture the last ~60ms PortAudio buffer frame so trailing consonants aren't clipped.
        time.sleep(0.06)
        samples = self.recorder.stop()
        threading.Thread(
            target=self._process_audio_worker,
            args=(samples, context),
            name="process-dictation",
            daemon=True,
        ).start()

    def _process_audio_worker(self, samples: np.ndarray, context: WindowContext | None) -> None:
        try:
            if samples.size < int(self.config.sample_rate * 0.15):
                self.hud.state_requested.emit("idle", "", 0)
                return

            speech = prepare_speech_for_asr(samples, sample_rate=self.config.sample_rate)
            if self.config.vad_enabled and self.vad is not None:
                speech = self.vad.extract_speech(
                    speech,
                    sample_rate=self.config.sample_rate,
                    threshold=self.config.vad_threshold,
                    min_speech_ms=self.config.vad_min_speech_ms,
                    padding_ms=self.config.vad_padding_ms,
                )
                if speech.size == 0:
                    self.hud.state_requested.emit("error", "No speech detected", 1100)
                    return

            self.tray.status_requested.emit("Transcribing...", "#A1A1AA")
            self.hud.state_requested.emit("transcribing", "Transcribing...", 0)
            wav_bytes = float32_to_wav_bytes(speech, sample_rate=self.config.sample_rate)
            asr_res = self.asr.transcribe(wav_bytes)
            raw_text = asr_res.text.strip()
            if not raw_text:
                self.hud.state_requested.emit("error", "No speech detected", 1100)
                return

            final_text = raw_text
            if self.config.llm_cleanup_enabled and self.servers.llama_running:
                self.tray.status_requested.emit("Polishing...", "#A1A1AA")
                self.hud.state_requested.emit("cleaning", "Polishing...", 0)
                final_text = self.cleanup.clean(raw_text, context=context)

            if not final_text:
                self.hud.state_requested.emit("idle", "", 0)
                return

            paste_text_at_cursor(
                final_text,
                restore_clipboard=self.config.restore_clipboard,
                restore_delay_ms=self.config.clipboard_restore_delay_ms,
            )

            self.hud.state_requested.emit("fade_out", "", 0)

            entry = HistoryEntry(
                timestamp=current_timestamp(),
                app_name=context.app_name if context else "",
                raw_text=raw_text,
                cleaned_text=final_text,
                detected_language=asr_res.detected_language,
            )
            self.history_store.append(entry)
            self.window.history_added.emit(entry)
        except Exception:
            self.hud.state_requested.emit("error", "Transcription error", 2000)
        finally:
            with self._lock:
                self._state = "idle"
            self.tray.status_requested.emit("Ready", "#F5F5F7")

    def _apply_new_config(self, new_cfg: AppConfig) -> None:
        new_cfg.save()
        self.config = new_cfg
        self.recorder = self._make_recorder()
        self.meeting_recorder.config = new_cfg
        self.meeting_detector.set_enabled(new_cfg.meeting_auto_detect)
        self.hotkeys.update_hotkeys(new_cfg.hotkey_hold, new_cfg.hotkey_toggle)
        self.hud.set_always_on(new_cfg.always_on_pill)
        self.tray.sync_config(new_cfg)
        if (
            new_cfg.llm_cleanup_enabled or new_cfg.meeting_cleanup_enabled or new_cfg.meeting_summary_enabled
        ) and not self.servers.llama_running:
            threading.Thread(target=self.servers.ensure_llama_started, daemon=True).start()

    def _start_meeting_capture(self, title: str, app_name: str, auto_detected: bool) -> bool:
        if self.meeting_recorder.is_recording:
            return False
        self.meeting_recorder.start(
            title=title,
            app_name=app_name,
            auto_detected=auto_detected,
        )
        if self.config.activation_sound_enabled:
            play_activation_sound()
        return True

    def _on_meeting_detected(self, meeting: DetectedMeeting) -> None:
        try:
            self._start_meeting_capture(
                title=meeting.title,
                app_name=meeting.app_name,
                auto_detected=True,
            )
        except Exception:
            self.tray.status_requested.emit("Meeting capture error", "#F87171")

    def _on_meeting_ended(self, _meeting: DetectedMeeting) -> None:
        self.meeting_recorder.stop()

    def _manual_start_meeting(self) -> None:
        try:
            self._start_meeting_capture(
                title="Recording",
                app_name="Manual",
                auto_detected=False,
            )
        except Exception:
            self.hud.state_requested.emit("error", "Mic capture error", 2000)

    def _manual_stop_meeting(self) -> None:
        self.meeting_detector.mark_manually_stopped()
        self.meeting_recorder.stop()

    def _toggle_manual_meeting(self) -> None:
        if self.meeting_recorder.is_recording:
            self._manual_stop_meeting()
        else:
            self._manual_start_meeting()

    def _toggle_meeting_auto(self, enabled: bool) -> None:
        self.config.meeting_auto_detect = enabled
        self.config.save()
        self.meeting_detector.set_enabled(enabled)
        self.tray.sync_config(self.config)
        self.window.sync_from_config(self.config)

    def _delete_meeting(self, meeting_id: str) -> None:
        active = self.meeting_recorder.current_record
        if active is not None and active.id == meeting_id:
            return
        self.meeting_store.delete(meeting_id)
        self.window.meetings_updated.emit(self.meeting_store.list_all())

    def _summarize_meeting(self, meeting_id: str) -> None:
        if not meeting_id:
            return
        with self._summary_lock:
            if meeting_id in self._summarizing_ids:
                return
            self._summarizing_ids.add(meeting_id)

        def _worker() -> None:
            try:
                self.window.meeting_state_changed.emit("summarizing", "Meeting")
                if not self.servers.ensure_llama_started():
                    self.tray.status_requested.emit("Summary model unavailable", "#F87171")
                    self.window.meetings_updated.emit(self.meeting_store.list_all())
                    return
                updated = self.meeting_recorder.summarize_record(meeting_id)
                if updated is None:
                    self.window.meetings_updated.emit(self.meeting_store.list_all())
            except Exception:
                self.tray.status_requested.emit("Summary failed", "#F87171")
                self.window.meetings_updated.emit(self.meeting_store.list_all())
            finally:
                with self._summary_lock:
                    self._summarizing_ids.discard(meeting_id)
                if self.meeting_recorder.is_recording:
                    active = self.meeting_recorder.current_record
                    lbl = (active.app_name or active.title) if active else "Meeting"
                    self.window.meeting_state_changed.emit("recording", lbl)
                else:
                    self.window.meeting_state_changed.emit("idle", "")

        threading.Thread(target=_worker, name="meeting-summary", daemon=True).start()

    def _on_meeting_record_updated(self, record: MeetingRecord) -> None:
        records = self.meeting_store.list_all()
        # Keep the live in-memory status visible in the UI list while saving on disk
        for idx, item in enumerate(records):
            if item.id == record.id:
                records[idx] = record
                break
        self.window.meetings_updated.emit(records)
        ui_state = record.status if record.status in {"recording", "cleaning", "summarizing"} else "idle"
        label = record.app_name or record.title
        self.window.meeting_state_changed.emit(ui_state, label)
        self.meeting_pill.state_changed.emit(ui_state, label)
        self.tray.meeting_status_changed.emit(record.status == "recording")
        self.hud.recording_state_changed.emit(record.status == "recording")

    def _toggle_always_on_pill(self, enabled: bool) -> None:
        self.config.always_on_pill = enabled
        self.config.save()
        self.hud.set_always_on(enabled)
        self.tray.sync_config(self.config)
        self.window.sync_from_config(self.config)

    def _toggle_cleanup(self, enabled: bool) -> None:
        self.config.llm_cleanup_enabled = enabled
        self.config.save()
        self.tray.sync_config(self.config)
        self.window.sync_from_config(self.config)
        if enabled and not self.servers.llama_running:
            threading.Thread(target=self.servers.ensure_llama_started, daemon=True).start()

    def _toggle_arabic_mode(self, enabled: bool) -> None:
        self.config.whisper_language = "auto" if enabled else "en"
        self.config.save()
        self.tray.sync_config(self.config)
        self.window.sync_from_config(self.config)
        label = "Arabic + English" if enabled else "English Only"
        self.hud.state_requested.emit("done", label, 1400)

    def _change_hold_hotkey(self, hold_key: str) -> None:
        self.config.hotkey_hold = hold_key
        self.config.save()
        self.hotkeys.update_hotkeys(self.config.hotkey_hold, self.config.hotkey_toggle)
        self.tray.sync_config(self.config)
        self.window.sync_from_config(self.config)
        label = hold_key.replace("+", " + ").replace("_", " ").title()
        self.hud.state_requested.emit("done", f"Hold: {label}", 1500)

    @staticmethod
    def _copy_to_clipboard(text: str) -> None:
        set_clipboard_text(text)

    def shutdown(self) -> None:
        """Stop hooks, meeting capture, HTTP clients, local servers, and exit."""
        with self._lock:
            self._cancel_tap_timer_locked()
        self.hotkeys.stop()
        self.meeting_detector.stop()
        self.meeting_recorder.shutdown()
        self.asr.close()
        self.cleanup.close()
        self.servers.stop()
        self.hud.hide()
        self.meeting_pill.hide()
        self.tray.hide()
        self.qt_app.quit()


def _acquire_single_instance_mutex(name: str = "Local\\WhisperFlowSingleInstanceMutex") -> int | None:
    """Ensure only one Whisper Flow instance runs at a time."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    handle = kernel32.CreateMutexW(None, False, name)
    if not handle:
        return None
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(handle)
        return None
    return int(handle)


def main() -> int:
    """Launch the Whisper Flow Windows tray application."""
    mutex = _acquire_single_instance_mutex()
    if mutex is None:
        return 0

    # Ensure relative paths work regardless of current working directory.
    project_root = Path(__file__).resolve().parents[2]
    os.chdir(project_root)

    qt_app = QApplication(sys.argv)
    qt_app.setQuitOnLastWindowClosed(False)

    config = AppConfig.load(project_root / "config.json")
    controller = WhisperFlowApp(config=config, qt_app=qt_app)
    controller.start()
    return qt_app.exec()
