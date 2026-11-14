"""Automatic Windows meeting detection, dual-stream capture, and continuous transcription."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import asdict, dataclass
from datetime import datetime
import json
from pathlib import Path
import re
import threading
import time
from typing import Any, Callable
import winreg

import numpy as np

from whisper_flow.asr import WhisperServerClient
from whisper_flow.audio import float32_to_wav_bytes, prepare_speech_for_asr, resample_mono
from whisper_flow.cleanup import LlmCleanupClient
from whisper_flow.config import AppConfig
from whisper_flow.diarization import (
    ChannelTurn,
    SpeakerTracker,
    append_speaker_turn,
    segment_channel_turns,
)
from whisper_flow.vad import SileroVAD

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

_ENUM_WINDOWS_PROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumWindows.argtypes = [_ENUM_WINDOWS_PROC, wintypes.LPARAM]
user32.EnumWindows.restype = wintypes.BOOL
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD

kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD),
]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_DEFAULT_IGNORE_APPS = frozenset({
    "python.exe",
    "pythonw.exe",
    "whisper-flow.exe",
    "whisper-server.exe",
    "llama-server.exe",
})

_BROWSER_APPS = frozenset({
    "chrome.exe",
    "msedge.exe",
    "firefox.exe",
    "brave.exe",
    "opera.exe",
    "vivaldi.exe",
    "arc.exe",
})

_CALL_APP_LABELS: dict[str, str] = {
    "zoom.exe": "Zoom",
    "teams.exe": "Microsoft Teams",
    "ms-teams.exe": "Microsoft Teams",
    "ciscocollabhost.exe": "Webex",
    "webex.exe": "Webex",
    "webexmta.exe": "Webex",
    "slack.exe": "Slack Huddle",
    "whatsapp.exe": "WhatsApp Call",
    "whatsapp.root.exe": "WhatsApp Call",
    "skype.exe": "Skype",
}

# Unambiguous web meeting tab titles (detected even when the user starts muted)
_WEB_MEETING_TITLE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bmeet\s*[-–—]\s*[a-z]{3}-[a-z]{4}-[a-z]{3}\b", re.IGNORECASE), "Google Meet"),
    (re.compile(r"\bzoom meeting\b|\bzoom webinar\b", re.IGNORECASE), "Zoom Meeting"),
    (re.compile(r"\b(?:meeting|call|huddle)\b.*\bmicrosoft teams\b|\bmicrosoft teams\b.*\b(?:meeting|call)\b", re.IGNORECASE), "Microsoft Teams"),
    (re.compile(r"\bhuddle\b.*\bslack\b|\bslack\b.*\bhuddle\b", re.IGNORECASE), "Slack Huddle"),
    (re.compile(r"\bjitsi meet\b|\bwhereby\b", re.IGNORECASE), "Web Meeting"),
)

# Looser browser title check used ONLY when the browser currently holds an active microphone handle
_BROWSER_ACTIVE_MIC_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bgoogle meet\b|\bmeet\.", re.IGNORECASE), "Google Meet"),
    (re.compile(r"\bzoom\b", re.IGNORECASE), "Zoom"),
    (re.compile(r"\bmicrosoft teams\b|\bteams\.microsoft", re.IGNORECASE), "Microsoft Teams"),
    (re.compile(r"\bdiscord\b", re.IGNORECASE), "Discord"),
    (re.compile(r"\bslack\b", re.IGNORECASE), "Slack Huddle"),
    (re.compile(r"\bwebex\b", re.IGNORECASE), "Webex"),
    (re.compile(r"\bchime\b|\bskype\b|\bwhatsapp\b", re.IGNORECASE), "Web Call"),
)

_DESKTOP_ACTIVE_CALL_TITLE_RE = re.compile(
    r"\b(zoom meeting|zoom webinar|meeting compact view|whatsapp (?:voice|video) call|huddle)\b",
    re.IGNORECASE,
)


@dataclass(slots=True, frozen=True)
class DetectedMeeting:
    """Represents an active meeting window/process detected on Windows."""

    app_name: str
    title: str
    reason: str

    @property
    def key(self) -> str:
        return f"{self.app_name}:{self.title}".lower()


def _query_process_name(pid: int) -> str:
    if pid <= 0:
        return ""
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(512)
        size = wintypes.DWORD(len(buf))
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return Path(buf.value).name.lower()
        return ""
    finally:
        kernel32.CloseHandle(handle)


def list_visible_windows() -> list[tuple[str, str]]:
    """Return `(process_name_lower, window_title)` for visible top-level windows."""
    windows: list[tuple[str, str]] = []
    enum_proc_t = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _callback(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value.strip()
        if not title:
            return True
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        proc = _query_process_name(int(pid.value))
        if proc:
            windows.append((proc, title))
        return True

    user32.EnumWindows(enum_proc_t(_callback), 0)
    return windows


def _extract_exe_name(subkey_name: str) -> str:
    # NonPackaged registry keys store paths using `#` instead of `\`
    cleaned = subkey_name.replace("#", "\\")
    return Path(cleaned).name.lower()


def _scan_consent_subkey(key: winreg.HKEYType, packaged: bool, active: set[str]) -> None:
    try:
        sub_count, _, _ = winreg.QueryInfoKey(key)
    except OSError:
        return

    for idx in range(sub_count):
        try:
            name = winreg.EnumKey(key, idx)
        except OSError:
            continue
        if name == "NonPackaged":
            try:
                with winreg.OpenKey(key, name) as non_pkg:
                    _scan_consent_subkey(non_pkg, packaged=False, active=active)
            except OSError:
                pass
            continue
        try:
            with winreg.OpenKey(key, name) as entry:
                start_val, _ = winreg.QueryValueEx(entry, "LastUsedTimeStart")
                stop_val, _ = winreg.QueryValueEx(entry, "LastUsedTimeStop")
                if int(start_val) > 0 and int(stop_val) == 0:
                    if packaged:
                        # Example: "5319275A.WhatsAppDesktop_cv1g1gvanyjgm" -> "whatsappdesktop"
                        pkg_prefix = name.split("_", 1)[0].split(".")[-1].lower()
                        if pkg_prefix:
                            active.add(pkg_prefix)
                    else:
                        exe = _extract_exe_name(name)
                        if exe:
                            active.add(exe)
        except OSError:
            continue


def get_active_microphone_apps() -> set[str]:
    """Read Windows ConsentStore to find apps currently holding an active mic stream."""
    active: set[str] = set()
    reg_path = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, reg_path) as root:
            _scan_consent_subkey(root, packaged=True, active=active)
    except OSError:
        pass
    return active


def _matches_active_mic(proc_name: str, active_mic_apps: set[str]) -> bool:
    if proc_name in active_mic_apps:
        return True
    stem = proc_name.removesuffix(".exe")
    # Handle Store/UWP names such as WhatsApp.Root.exe <-> whatsappdesktop
    if "whatsapp" in stem and any("whatsapp" in item for item in active_mic_apps):
        return True
    return stem in active_mic_apps


def _clean_meeting_title(raw_title: str, fallback: str) -> str:
    title = re.sub(r"\s*[-–|]\s*(Google Chrome|Microsoft Edge|Mozilla Firefox|Brave|Opera)$", "", raw_title, flags=re.IGNORECASE).strip()
    if len(title) > 54:
        return title[:51].rstrip() + "..."
    return title or fallback


def classify_active_meeting(
    windows: list[tuple[str, str]],
    active_mic_apps: set[str],
    ignore_apps: frozenset[str] = _DEFAULT_IGNORE_APPS,
) -> DetectedMeeting | None:
    """Infer whether an active meeting/call is happening from visible windows and mic usage."""
    filtered_windows = [(proc.lower(), title) for proc, title in windows if proc.lower() not in ignore_apps]

    # 1. Explicit desktop call windows OR dedicated meeting apps currently using the mic
    for proc, title in filtered_windows:
        if proc in _CALL_APP_LABELS:
            label = _CALL_APP_LABELS[proc]
            if _matches_active_mic(proc, active_mic_apps):
                return DetectedMeeting(app_name=label, title=_clean_meeting_title(title, label), reason="app+mic")
            if _DESKTOP_ACTIVE_CALL_TITLE_RE.search(title):
                return DetectedMeeting(app_name=label, title=_clean_meeting_title(title, label), reason="call-title")

    # 2. Explicit browser web-meeting titles (works even if muted on entry)
    for proc, title in filtered_windows:
        if proc not in _BROWSER_APPS:
            continue
        for pattern, label in _WEB_MEETING_TITLE_PATTERNS:
            if pattern.search(title):
                return DetectedMeeting(app_name=label, title=_clean_meeting_title(title, label), reason="web-title")

    # 3. Browser currently holding an active mic with a recognizable meeting/call tab title
    for proc, title in filtered_windows:
        if proc not in _BROWSER_APPS or proc not in active_mic_apps:
            continue
        for pattern, label in _BROWSER_ACTIVE_MIC_PATTERNS:
            if pattern.search(title):
                return DetectedMeeting(app_name=label, title=_clean_meeting_title(title, label), reason="browser+mic")

    # 4. Minimized dedicated call/meeting apps currently holding the microphone
    for proc, label in _CALL_APP_LABELS.items():
        if proc in ignore_apps:
            continue
        if _matches_active_mic(proc, active_mic_apps):
            return DetectedMeeting(app_name=label, title=label, reason="minimized+mic")

    return None


class MeetingDetector:
    """Polls Windows foreground/visible apps & mic usage to detect meeting start and end."""

    def __init__(
        self,
        on_meeting_started: Callable[[DetectedMeeting], None],
        on_meeting_ended: Callable[[DetectedMeeting], None],
        poll_interval_seconds: float = 2.0,
        start_debounce_seconds: float = 3.0,
        stop_debounce_seconds: float = 8.0,
    ) -> None:
        self._on_started = on_meeting_started
        self._on_ended = on_meeting_ended
        self._poll_interval = poll_interval_seconds
        self._start_debounce = start_debounce_seconds
        self._stop_debounce = stop_debounce_seconds

        self._lock = threading.Lock()
        self._enabled = True
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

        self._candidate: DetectedMeeting | None = None
        self._candidate_since: float = 0.0
        self._active: DetectedMeeting | None = None
        self._absent_since: float | None = None
        self._suppressed_until_clear = False

    def set_enabled(self, enabled: bool) -> None:
        with self._lock:
            self._enabled = enabled
            if not enabled:
                self._candidate = None
                self._absent_since = None

    def mark_manually_stopped(self) -> None:
        """Suppress auto-retriggering while the current meeting window remains open."""
        with self._lock:
            self._active = None
            self._candidate = None
            self._absent_since = None
            self._suppressed_until_clear = True

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, name="meeting-detector", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._poll_once(now=time.monotonic())
            except Exception:
                pass
            self._stop_event.wait(self._poll_interval)

    def _poll_once(self, now: float) -> None:
        detected = classify_active_meeting(list_visible_windows(), get_active_microphone_apps())
        started_evt: DetectedMeeting | None = None
        ended_evt: DetectedMeeting | None = None

        with self._lock:
            if not self._enabled:
                return

            if self._suppressed_until_clear:
                if detected is None:
                    self._suppressed_until_clear = False
                return

            if self._active is None:
                if detected is None:
                    self._candidate = None
                    return
                if self._candidate is None or self._candidate.app_name != detected.app_name:
                    self._candidate = detected
                    self._candidate_since = now
                    return
                if (now - self._candidate_since) >= self._start_debounce:
                    self._active = detected
                    self._candidate = None
                    self._absent_since = None
                    started_evt = detected
            else:
                if detected is not None:
                    self._absent_since = None
                else:
                    if self._absent_since is None:
                        self._absent_since = now
                    elif (now - self._absent_since) >= self._stop_debounce:
                        ended_evt = self._active
                        self._active = None
                        self._absent_since = None

        if started_evt is not None:
            self._on_started(started_evt)
        elif ended_evt is not None:
            self._on_ended(ended_evt)


@dataclass(slots=True)
class _MixTrack:
    chunks: list[tuple[float, np.ndarray]]
    cursor: int = 0


@dataclass(slots=True)
class DrainedChannels:
    """Aligned mixed, microphone, and remote loopback buffers from one drain window."""

    mixed: np.ndarray
    mic: np.ndarray
    loopback: np.ndarray


class TimedMixBuffer:
    """Wall-clock-aligned multi-source float32 mixer handling intermittent WASAPI loopbacks."""

    def __init__(self, target_rate: int = 16000) -> None:
        self.target_rate = target_rate
        self._lock = threading.Lock()
        self._tracks: dict[str, _MixTrack] = {}
        self._window_start = time.monotonic()
        self._last_rms = 0.0

    def reset(self, now: float | None = None) -> None:
        t = time.monotonic() if now is None else now
        with self._lock:
            self._tracks.clear()
            self._window_start = t
            self._last_rms = 0.0

    def _push_track(self, track_id: str, mono_16k: np.ndarray, timestamp: float | None = None) -> None:
        if mono_16k.size == 0:
            return
        ts = time.monotonic() if timestamp is None else timestamp
        rms = float(np.sqrt(np.mean(np.square(mono_16k))))
        with self._lock:
            track = self._tracks.get(track_id)
            if track is None:
                track = _MixTrack(chunks=[])
                self._tracks[track_id] = track
            track.chunks.append((ts, mono_16k))
            self._last_rms = max(rms, self._last_rms * 0.65)

    def push_mic(self, mono_16k: np.ndarray, timestamp: float | None = None) -> None:
        self._push_track("mic", mono_16k, timestamp)

    def push_loopback(
        self,
        mono_16k: np.ndarray,
        timestamp: float | None = None,
        track_id: str = "default",
    ) -> None:
        self._push_track(f"loopback:{track_id}", mono_16k, timestamp)

    def rms_level(self) -> float:
        with self._lock:
            return min(1.0, self._last_rms * 7.5)

    def _paint_stream(self, mix: np.ndarray, track: _MixTrack, window_start: float) -> None:
        total = mix.shape[0]
        cursor = track.cursor
        max_jitter = int(0.12 * self.target_rate)

        for ts_end, samples in track.chunks:
            n = samples.shape[0]
            wall_end = int(round((ts_end - window_start) * self.target_rate))
            wall_start = wall_end - n

            # Follow continuous callbacks sequentially to avoid micro-gaps, unless silence paused the stream
            if abs(cursor - wall_start) <= max_jitter:
                start_pos = cursor
            else:
                start_pos = max(0, wall_start)

            end_pos = start_pos + n
            cursor = end_pos

            dst_start = max(0, start_pos)
            dst_end = min(total, end_pos)
            if dst_end <= dst_start:
                continue
            src_start = dst_start - start_pos
            src_end = src_start + (dst_end - dst_start)
            mix[dst_start:dst_end] += samples[src_start:src_end]

        track.cursor = max(0, cursor - total)

    def drain_channels(self, now: float | None = None) -> DrainedChannels:
        """Mix and clear accumulated audio while returning separate aligned mic & loopback tracks."""
        t_end = time.monotonic() if now is None else now
        with self._lock:
            has_chunks = any(track.chunks for track in self._tracks.values())
            max_cursor = max((track.cursor for track in self._tracks.values()), default=0)
            n_samples = max(int(round((t_end - self._window_start) * self.target_rate)), max_cursor)
            if n_samples <= 0 or not has_chunks:
                self._window_start = t_end
                for track in self._tracks.values():
                    track.cursor = 0
                empty = np.empty(0, dtype=np.float32)
                return DrainedChannels(mixed=empty, mic=empty, loopback=empty)

            mic_mix = np.zeros(n_samples, dtype=np.float32)
            loop_mix = np.zeros(n_samples, dtype=np.float32)
            for name, track in self._tracks.items():
                if not track.chunks:
                    track.cursor = max(0, track.cursor - n_samples)
                    continue
                target = mic_mix if name == "mic" else loop_mix
                self._paint_stream(target, track, self._window_start)
                track.chunks.clear()

            mic_clipped = np.clip(mic_mix, -1.0, 1.0)
            loop_clipped = np.clip(loop_mix, -1.0, 1.0)
            mixed = np.clip(mic_mix + loop_mix, -1.0, 1.0)
            self._window_start = t_end
            return DrainedChannels(mixed=mixed, mic=mic_clipped, loopback=loop_clipped)

    def drain(self, now: float | None = None) -> np.ndarray:
        """Mix and clear accumulated audio from all active sources aligned to wall clock."""
        return self.drain_channels(now).mixed


def _to_mono_float32(raw_bytes: bytes, channels: int) -> np.ndarray:
    arr = np.frombuffer(raw_bytes, dtype=np.float32)
    if channels > 1:
        usable = (arr.size // channels) * channels
        if usable == 0:
            return np.empty(0, dtype=np.float32)
        arr = arr[:usable].reshape(-1, channels).mean(axis=1)
    return arr.astype(np.float32, copy=False)


def _apply_loopback_gain(mono: np.ndarray, env_ref: list[float]) -> np.ndarray:
    """Gently lift low-volume Windows speaker loopback without amplifying silence."""
    peak = float(np.max(np.abs(mono))) if mono.size else 0.0
    if peak <= 1e-5:
        return mono
    env_ref[0] = max(peak, env_ref[0] * 0.992)
    if env_ref[0] > 0.004:
        gain = min(3.5, 0.28 / env_ref[0])
        if gain > 1.05:
            return np.clip(mono * gain, -1.0, 1.0)
    return mono


class DualStreamAudioCapture:
    """Captures microphone + all active Windows WASAPI speaker loopbacks via PyAudioWPatch."""

    def __init__(
        self,
        sample_rate: int = 16000,
        input_device_index: int | None = None,
        capture_system_audio: bool = True,
    ) -> None:
        self.sample_rate = sample_rate
        self.input_device_index = input_device_index
        self.capture_system_audio = capture_system_audio
        self._buffer = TimedMixBuffer(target_rate=sample_rate)
        self._pa: Any = None
        self._mic_stream: Any = None
        self._loopback_streams: list[Any] = []
        self._active = False
        self.has_loopback = False

    def _resolve_mic_device(self, pa: Any) -> dict[str, Any] | None:
        if self.input_device_index is not None:
            try:
                import sounddevice as sd

                sd_info = sd.query_devices(self.input_device_index)
                target_name = str(sd_info.get("name", "")).lower()
                for idx in range(pa.get_device_count()):
                    dev = pa.get_device_info_by_index(idx)
                    if dev.get("maxInputChannels", 0) > 0 and not dev.get("isLoopbackDevice", False):
                        if target_name and target_name in str(dev.get("name", "")).lower():
                            return dev
            except Exception:
                pass
        try:
            return pa.get_default_input_device_info()
        except Exception:
            return None

    def _resolve_loopback_devices(self, pa: Any) -> list[dict[str, Any]]:
        """Return active WASAPI loopback devices, default output first so calls on headsets are never missed."""
        loopbacks: list[dict[str, Any]] = []
        seen_indices: set[int] = set()
        default_name = ""
        try:
            wasapi_info = pa.get_host_api_info_by_type(pa.paWASAPI if hasattr(pa, "paWASAPI") else 13)
            default_out = pa.get_device_info_by_index(wasapi_info["defaultOutputDevice"])
            default_name = str(default_out.get("name", ""))
        except Exception:
            pass

        try:
            all_loopbacks = list(pa.get_loopback_device_info_generator())
        except Exception:
            all_loopbacks = []

        # Put the Windows default output loopback first, then all remaining active outputs (headsets/monitors)
        prioritized = sorted(
            all_loopbacks,
            key=lambda dev: 0 if (default_name and default_name in str(dev.get("name", ""))) else 1,
        )
        for dev in prioritized:
            if not dev.get("isLoopbackDevice", False) or int(dev.get("maxInputChannels", 0)) <= 0:
                continue
            idx = int(dev["index"])
            if idx not in seen_indices:
                seen_indices.add(idx)
                loopbacks.append(dev)
        return loopbacks

    def start(self) -> None:
        if self._active:
            return
        import pyaudiowpatch as pyaudio

        pa = pyaudio.PyAudio()
        self._pa = pa
        self._buffer.reset()
        self._loopback_streams = []
        self.has_loopback = False

        try:
            mic_dev = self._resolve_mic_device(pa)
            if mic_dev is not None:
                mic_rate = int(mic_dev["defaultSampleRate"])
                mic_ch = max(1, min(2, int(mic_dev.get("maxInputChannels", 1))))

                def _mic_cb(in_data: bytes | None, _frames: int, _time_info: Any, _status: int) -> tuple[None, int]:
                    if in_data:
                        mono = _to_mono_float32(in_data, mic_ch)
                        if mono.size > 0:
                            resampled = resample_mono(mono, mic_rate, self.sample_rate)
                            self._buffer.push_mic(resampled)
                    return (None, pyaudio.paContinue)

                self._mic_stream = pa.open(
                    format=pyaudio.paFloat32,
                    channels=mic_ch,
                    rate=mic_rate,
                    input=True,
                    input_device_index=int(mic_dev["index"]),
                    frames_per_buffer=1024,
                    stream_callback=_mic_cb,
                )

            if self.capture_system_audio:
                for loop_dev in self._resolve_loopback_devices(pa):
                    dev_idx = int(loop_dev["index"])
                    loop_rate = int(loop_dev["defaultSampleRate"])
                    loop_ch = max(1, int(loop_dev.get("maxInputChannels", 2)))
                    env_ref = [0.0]

                    def _make_loop_cb(
                        track_id: str,
                        rate: int,
                        channels: int,
                        env: list[float],
                    ) -> Callable[[bytes | None, int, Any, int], tuple[None, int]]:
                        def _loop_cb(
                            in_data: bytes | None,
                            _frames: int,
                            _time_info: Any,
                            _status: int,
                        ) -> tuple[None, int]:
                            if in_data:
                                mono = _to_mono_float32(in_data, channels)
                                if mono.size > 0 and float(np.max(np.abs(mono))) > 1e-5:
                                    leveled = _apply_loopback_gain(mono, env)
                                    resampled = resample_mono(leveled, rate, self.sample_rate)
                                    self._buffer.push_loopback(resampled, track_id=track_id)
                            return (None, pyaudio.paContinue)

                        return _loop_cb

                    try:
                        stream = pa.open(
                            format=pyaudio.paFloat32,
                            channels=loop_ch,
                            rate=loop_rate,
                            input=True,
                            input_device_index=dev_idx,
                            frames_per_buffer=1024,
                            stream_callback=_make_loop_cb(str(dev_idx), loop_rate, loop_ch, env_ref),
                        )
                        self._loopback_streams.append(stream)
                    except Exception:
                        continue
                self.has_loopback = bool(self._loopback_streams)

            self._active = True
        except Exception:
            self.stop()
            raise

    def drain_channels(self) -> DrainedChannels:
        return self._buffer.drain_channels()

    def drain_samples(self) -> np.ndarray:
        return self._buffer.drain()

    def rms_level(self) -> float:
        return self._buffer.rms_level()

    def stop_channels(self) -> DrainedChannels:
        streams = list(self._loopback_streams)
        if self._mic_stream is not None:
            streams.insert(0, self._mic_stream)
        self._mic_stream = None
        self._loopback_streams = []
        for stream in streams:
            try:
                stream.stop_stream()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass
        if self._pa is not None:
            try:
                self._pa.terminate()
            except Exception:
                pass
            self._pa = None
        self._active = False
        return self._buffer.drain_channels()

    def stop(self) -> np.ndarray:
        return self.stop_channels().mixed


@dataclass(slots=True)
class MeetingRecord:
    """Persisted meeting transcript record."""

    id: str
    title: str
    app_name: str
    started_at: str
    ended_at: str = ""
    duration_seconds: int = 0
    raw_transcript: str = ""
    cleaned_transcript: str = ""
    summary: str = ""
    status: str = "recording"  # "recording" | "cleaning" | "summarizing" | "completed"
    auto_detected: bool = False

    @property
    def display_transcript(self) -> str:
        return self.cleaned_transcript.strip() or self.raw_transcript.strip()


class MeetingStore:
    """Reads and writes meeting transcript files under the configured meetings directory."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)

    def save(self, record: MeetingRecord) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        json_path = self.directory / f"{record.id}.json"
        json_path.write_text(json.dumps(asdict(record), indent=2, ensure_ascii=False), encoding="utf-8")

        md_path = self.directory / f"{record.id}.md"
        body = record.display_transcript or "_No speech captured._"
        summary_block = f"## Summary\n\n{record.summary.strip()}\n\n" if record.summary.strip() else ""
        md_text = (
            f"# {record.title}\n\n"
            f"- App: {record.app_name or 'Manual'}\n"
            f"- Started: {record.started_at}\n"
            f"- Duration: {format_duration(record.duration_seconds)}\n\n"
            f"---\n\n{summary_block}{body}\n"
        )
        md_path.write_text(md_text, encoding="utf-8")

    def list_all(self) -> list[MeetingRecord]:
        if not self.directory.exists():
            return []
        records: list[MeetingRecord] = []
        valid_fields = set(MeetingRecord.__dataclass_fields__)
        for path in sorted(self.directory.glob("*.json"), reverse=True):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                filtered = {k: v for k, v in raw.items() if k in valid_fields}
                rec = MeetingRecord(**filtered)
                # Recover gracefully if the app terminated mid-recording in an earlier session
                if rec.status in {"recording", "cleaning", "summarizing"}:
                    rec.status = "completed"
                records.append(rec)
            except Exception:
                continue
        return records

    def delete(self, meeting_id: str) -> None:
        for ext in (".json", ".md"):
            target = self.directory / f"{meeting_id}{ext}"
            if target.exists():
                target.unlink(missing_ok=True)


def format_duration(seconds: int) -> str:
    mins, secs = divmod(max(0, int(seconds)), 60)
    hours, mins = divmod(mins, 60)
    if hours > 0:
        return f"{hours}:{mins:02d}:{secs:02d}"
    return f"{mins:02d}:{secs:02d}"


def _make_meeting_id(title: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:28] or "meeting"
    return f"{stamp}_{slug}"


class MeetingRecorderController:
    """Orchestrates live chunked transcription and optional post-meeting cleanup."""

    def __init__(
        self,
        config: AppConfig,
        asr: WhisperServerClient,
        cleanup: LlmCleanupClient,
        vad: SileroVAD | None,
        store: MeetingStore,
        is_llm_available: Callable[[], bool],
        on_record_updated: Callable[[MeetingRecord], None],
    ) -> None:
        self.config = config
        self.asr = asr
        self.cleanup = cleanup
        self.vad = vad
        self.store = store
        self._is_llm_available = is_llm_available
        self._on_record_updated = on_record_updated

        self._lock = threading.Lock()
        self._capture: DualStreamAudioCapture | None = None
        self._active_record: MeetingRecord | None = None
        self._started_monotonic: float = 0.0
        self._stop_event = threading.Event()
        self._worker_thread: threading.Thread | None = None

    @property
    def is_recording(self) -> bool:
        with self._lock:
            return self._active_record is not None and self._active_record.status == "recording"

    @property
    def current_record(self) -> MeetingRecord | None:
        with self._lock:
            return self._active_record

    def start(self, title: str, app_name: str = "Manual", auto_detected: bool = False) -> MeetingRecord | None:
        with self._lock:
            if self._active_record is not None:
                return self._active_record

            capture = DualStreamAudioCapture(
                sample_rate=self.config.sample_rate,
                input_device_index=self.config.input_device,
                capture_system_audio=self.config.meeting_capture_system_audio,
            )
            capture.start()

            record = MeetingRecord(
                id=_make_meeting_id(title or app_name),
                title=title or app_name or "Meeting",
                app_name=app_name,
                started_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
                status="recording",
                auto_detected=auto_detected,
            )
            self._capture = capture
            self._active_record = record
            self._started_monotonic = time.monotonic()
            self._stop_event.clear()

        self.store.save(record)
        self._on_record_updated(record)

        self._worker_thread = threading.Thread(
            target=self._chunk_loop,
            name="meeting-transcriber",
            daemon=True,
        )
        self._worker_thread.start()
        return record

    def stop(self) -> None:
        with self._lock:
            if self._active_record is None or self._active_record.status != "recording":
                return
            self._stop_event.set()

    def shutdown(self) -> None:
        self.stop()
        thread = self._worker_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=15.0)

    def _transcribe_chunk(self, samples: np.ndarray) -> str:
        if samples.size < int(self.config.sample_rate * 0.35):
            return ""
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
                return ""
        wav_bytes = float32_to_wav_bytes(speech, sample_rate=self.config.sample_rate)
        result = self.asr.transcribe(wav_bytes)
        return result.text.strip()

    @staticmethod
    def _pad_to_size(samples: np.ndarray, size: int) -> np.ndarray:
        if samples.size == size:
            return samples
        if samples.size > size:
            return samples[:size]
        return np.pad(samples, (0, size - samples.size))

    @classmethod
    def _read_capture_channels(cls, capture: Any, stop: bool = False) -> DrainedChannels:
        fn_name = "stop_channels" if stop else "drain_channels"
        channel_fn = getattr(capture, fn_name, None)
        if callable(channel_fn):
            drained = channel_fn()
            size = drained.mixed.size
            if size == 0:
                return drained
            return DrainedChannels(
                mixed=drained.mixed,
                mic=cls._pad_to_size(drained.mic, size),
                loopback=cls._pad_to_size(drained.loopback, size),
            )
        arr = capture.stop() if stop else capture.drain_samples()
        empty = np.empty(0, dtype=np.float32) if arr.size == 0 else np.zeros(arr.size, dtype=np.float32)
        return DrainedChannels(mixed=arr, mic=arr, loopback=empty)

    @staticmethod
    def _find_quiet_split_idx(audio: np.ndarray, sample_rate: int) -> int:
        """Pick the lowest-energy 120ms valley near the end of *audio* to avoid cutting words."""
        win = max(1, int(sample_rate * 0.12))
        search_span = min(audio.size // 2, int(sample_rate * 1.6))
        start = max(0, audio.size - search_span)
        if audio.size - start <= win:
            return audio.size
        best_idx = audio.size
        best_energy = float("inf")
        step = max(1, win // 2)
        for idx in range(start, audio.size - win + 1, step):
            seg = audio[idx : idx + win]
            energy = float(np.mean(seg * seg))
            if energy < best_energy:
                best_energy = energy
                best_idx = idx + win // 2
        return best_idx

    def _flush_channels(
        self,
        record: MeetingRecord,
        mic: np.ndarray,
        loopback: np.ndarray,
        mixed: np.ndarray,
        tracker: SpeakerTracker,
        chunk_start_secs: int,
    ) -> None:
        if mixed.size == 0:
            return
        sr = self.config.sample_rate
        turns = segment_channel_turns(mic, loopback, sample_rate=sr)
        if not turns and mixed.size >= int(sr * 0.35):
            mic_rms = float(np.sqrt(np.mean(mic * mic))) if mic.size > 0 else 0.0
            loop_rms = float(np.sqrt(np.mean(loopback * loopback))) if loopback.size > 0 else 0.0
            if loop_rms > mic_rms * 1.35 and loop_rms >= 0.003:
                turns = [ChannelTurn(start_sample=0, channel="loopback", audio=loopback)]
            else:
                turns = [ChannelTurn(start_sample=0, channel="mic", audio=mic if mic.size > 0 else mixed)]

        updated = False
        for turn in turns:
            try:
                text = self._transcribe_chunk(turn.audio)
            except Exception:
                continue
            if not text:
                continue
            speaker = tracker.assign(turn.audio, turn.channel, sample_rate=sr)
            turn_elapsed = max(0, chunk_start_secs + int(turn.start_sample // sr))
            with self._lock:
                record.raw_transcript = append_speaker_turn(
                    record.raw_transcript,
                    speaker=speaker,
                    elapsed_seconds=turn_elapsed,
                    text=text,
                )
                record.duration_seconds = int(time.monotonic() - self._started_monotonic)
            updated = True

        if updated:
            self.store.save(record)
            self._on_record_updated(record)

    def _generate_summary_text(self, record: MeetingRecord) -> str:
        summarizer = getattr(self.cleanup, "summarize_meeting_transcript", None)
        source = record.display_transcript
        if not source or not callable(summarizer):
            return ""
        return summarizer(source, title=record.title, app_name=record.app_name).strip()

    def summarize_record(self, meeting_id: str) -> MeetingRecord | None:
        """Generate and persist a local-LLM summary for an existing completed meeting."""
        with self._lock:
            active = self._active_record
            if active is not None and active.id == meeting_id and active.status == "recording":
                return None

        target = next((r for r in self.store.list_all() if r.id == meeting_id), None)
        if target is None or not target.display_transcript:
            return None

        previous_status = target.status
        target.status = "summarizing"
        self.store.save(target)
        self._on_record_updated(target)
        try:
            summary = self._generate_summary_text(target)
            if summary:
                target.summary = summary
        finally:
            target.status = previous_status if previous_status != "summarizing" else "completed"
            self.store.save(target)
            self._on_record_updated(target)
        return target

    def _chunk_loop(self) -> None:
        sr = self.config.sample_rate
        max_chunk_secs = min(max(3.5, float(self.config.meeting_chunk_seconds)), 6.0)
        min_live_samples = int(sr * 1.4)
        max_live_samples = int(sr * max_chunk_secs)
        tail_samples = int(sr * 0.55)
        preroll_samples = int(sr * 0.25)

        record = self._active_record
        capture = self._capture
        if record is None or capture is None:
            return

        tracker = SpeakerTracker()
        pending_mixed: list[np.ndarray] = []
        pending_mic: list[np.ndarray] = []
        pending_loop: list[np.ndarray] = []
        pending_count = 0
        preroll_mixed = np.zeros(0, dtype=np.float32)
        preroll_mic = np.zeros(0, dtype=np.float32)
        preroll_loop = np.zeros(0, dtype=np.float32)

        while not self._stop_event.wait(0.45):
            drained = self._read_capture_channels(capture, stop=False)
            if drained.mixed.size > 0:
                if pending_count == 0 and float(np.max(np.abs(drained.mixed))) < 0.006:
                    preroll_mixed = drained.mixed[-preroll_samples:]
                    preroll_mic = drained.mic[-preroll_samples:]
                    preroll_loop = drained.loopback[-preroll_samples:]
                    continue
                if pending_count == 0 and preroll_mixed.size > 0:
                    pending_mixed.append(preroll_mixed)
                    pending_mic.append(preroll_mic)
                    pending_loop.append(preroll_loop)
                    pending_count += preroll_mixed.size
                    preroll_mixed = np.zeros(0, dtype=np.float32)
                    preroll_mic = np.zeros(0, dtype=np.float32)
                    preroll_loop = np.zeros(0, dtype=np.float32)
                pending_mixed.append(drained.mixed)
                pending_mic.append(drained.mic)
                pending_loop.append(drained.loopback)
                pending_count += drained.mixed.size

            if pending_count < min_live_samples:
                continue

            combined_mixed = np.concatenate(pending_mixed) if len(pending_mixed) > 1 else pending_mixed[0]
            tail = combined_mixed[-min(tail_samples, combined_mixed.size) :]
            tail_rms = float(np.sqrt(np.mean(tail * tail)))

            if tail_rms < 0.011 or combined_mixed.size >= max_live_samples:
                combined_mic = np.concatenate(pending_mic) if len(pending_mic) > 1 else pending_mic[0]
                combined_loop = np.concatenate(pending_loop) if len(pending_loop) > 1 else pending_loop[0]
                if tail_rms < 0.011:
                    flush_mixed, flush_mic, flush_loop = combined_mixed, combined_mic, combined_loop
                    pending_mixed.clear()
                    pending_mic.clear()
                    pending_loop.clear()
                    pending_count = 0
                else:
                    split_idx = self._find_quiet_split_idx(combined_mixed, sr)
                    flush_mixed = combined_mixed[:split_idx]
                    flush_mic = combined_mic[:split_idx]
                    flush_loop = combined_loop[:split_idx]
                    rem_mixed = combined_mixed[split_idx:]
                    rem_mic = combined_mic[split_idx:]
                    rem_loop = combined_loop[split_idx:]
                    pending_mixed = [rem_mixed] if rem_mixed.size > 0 else []
                    pending_mic = [rem_mic] if rem_mic.size > 0 else []
                    pending_loop = [rem_loop] if rem_loop.size > 0 else []
                    pending_count = rem_mixed.size

                chunk_start_secs = max(0, int((time.monotonic() - self._started_monotonic) - (combined_mixed.size / sr)))
                self._flush_channels(record, flush_mic, flush_loop, flush_mixed, tracker, chunk_start_secs)

        # Flush any buffered speech + trailing capture audio cleanly on stop
        trailing = self._read_capture_channels(capture, stop=True)
        if trailing.mixed.size > 0:
            pending_mixed.append(trailing.mixed)
            pending_mic.append(trailing.mic)
            pending_loop.append(trailing.loopback)
        if pending_mixed:
            final_mixed = np.concatenate(pending_mixed) if len(pending_mixed) > 1 else pending_mixed[0]
            final_mic = np.concatenate(pending_mic) if len(pending_mic) > 1 else pending_mic[0]
            final_loop = np.concatenate(pending_loop) if len(pending_loop) > 1 else pending_loop[0]
            chunk_start_secs = max(0, int((time.monotonic() - self._started_monotonic) - (final_mixed.size / sr)))
            self._flush_channels(record, final_mic, final_loop, final_mixed, tracker, chunk_start_secs)

        with self._lock:
            record.ended_at = datetime.now().strftime("%Y-%m-%d %H:%M")
            record.duration_seconds = int(time.monotonic() - self._started_monotonic)

        if self.config.meeting_cleanup_enabled and record.raw_transcript.strip() and self._is_llm_available():
            with self._lock:
                record.status = "cleaning"
            self.store.save(record)
            self._on_record_updated(record)
            try:
                cleaned = self.cleanup.clean_meeting_transcript(
                    record.raw_transcript,
                    app_name=record.app_name,
                )
                with self._lock:
                    record.cleaned_transcript = cleaned
            except Exception:
                pass

        if self.config.meeting_summary_enabled and record.display_transcript and self._is_llm_available():
            with self._lock:
                record.status = "summarizing"
            self.store.save(record)
            self._on_record_updated(record)
            try:
                summary = self._generate_summary_text(record)
                if summary:
                    with self._lock:
                        record.summary = summary
            except Exception:
                pass

        with self._lock:
            record.status = "completed"
            if self._active_record is record:
                self._capture = None
                self._active_record = None
        self.store.save(record)
        self._on_record_updated(record)
