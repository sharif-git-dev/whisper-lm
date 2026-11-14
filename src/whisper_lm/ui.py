"""Minimalist monochrome dark desktop Hub, floating dictation pill, and tray UI."""

from __future__ import annotations

import ctypes
from dataclasses import replace
from datetime import datetime
import math
from pathlib import Path
import subprocess
import sys
import threading
from typing import Any, Callable
import wave

import numpy as np
import sounddevice as sd
from PySide6.QtCore import (
    Property,
    QEasingCurve,
    QPoint,
    QPropertyAnimation,
    QRectF,
    Qt,
    QTimer,
    QUrl,
    QVariantAnimation,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QBrush,
    QColor,
    QCursor,
    QDesktopServices,
    QFont,
    QIcon,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QRadialGradient,
)
from PySide6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QComboBox,
    QFileDialog,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QStackedWidget,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from whisper_lm import __version__
from whisper_lm.audio import AudioRecorder, list_input_devices
from whisper_lm.config import AppConfig
from whisper_lm.history import (
    HistoryEntry,
    date_category_label,
    format_entry_when,
    matches_time_filter,
)
from whisper_lm.hotkeys import HOLD_HOTKEY_PRESETS, TOGGLE_HOTKEY_PRESETS
from whisper_lm.meetings import MeetingRecord, format_duration
from whisper_lm.sync import describe_cloud_target, detect_cloud_drives, resolve_auto_backup_drive
from whisper_lm.updater import UpdateInfo

try:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    dwmapi = ctypes.WinDLL("dwmapi", use_last_error=False)
except Exception:
    user32 = None
    dwmapi = None

_GWL_EXSTYLE = -20
_WS_EX_NOACTIVATE = 0x08000000
_WS_EX_TOOLWINDOW = 0x00000080
_DWMWA_USE_IMMUTABLE_DARK_MODE = 20

# WhisperLM deep obsidian + electric indigo accent palette
_BG_ROOT = "#040406"
_BG_SIDEBAR = "#07070A"
_BG_CARD = "#0B0B0F"
_BG_CARD_HOVER = "#101017"
_BG_INPUT = "#060609"
_BORDER = "#171720"
_BORDER_SUBTLE = "#121219"
_TEXT_PRIMARY = "#F4F4F8"
_TEXT_SECONDARY = "#8A8A98"
_TEXT_MUTED = "#50505E"
_ACCENT_INDIGO = "#7B8BFF"
_ACCENT_INDIGO_DEEP = "#5868F2"
_ACCENT_GREEN = "#34D399"
_ACCENT_RED = "#F87171"

_DARK_MENU_QSS = (
    f"QMenu {{ background-color: {_BG_CARD}; color: {_TEXT_PRIMARY}; border: 1px solid #1E1E2A; padding: 6px; border-radius: 10px; }}"
    "QMenu::item { padding: 6px 18px; border-radius: 6px; }"
    "QMenu::item:selected { background-color: #161622; }"
)


def _lerp_color(c1: QColor, c2: QColor, t: float) -> QColor:
    clamped = max(0.0, min(1.0, float(t)))
    return QColor(
        int(round(c1.red() + (c2.red() - c1.red()) * clamped)),
        int(round(c1.green() + (c2.green() - c1.green()) * clamped)),
        int(round(c1.blue() + (c2.blue() - c1.blue()) * clamped)),
        int(round(c1.alpha() + (c2.alpha() - c1.alpha()) * clamped)),
    )


def _fade_in_widget(widget: QWidget, duration: int = 165) -> None:
    """Apply a crisp non-blocking fade-in transition without mutating widget visibility."""
    prev = getattr(widget, "_active_fade_anim", None)
    if isinstance(prev, QPropertyAnimation):
        prev.stop()
    effect = widget.graphicsEffect()
    if not isinstance(effect, QGraphicsOpacityEffect):
        effect = QGraphicsOpacityEffect(widget)
        widget.setGraphicsEffect(effect)
    effect.setOpacity(0.0)
    anim = QPropertyAnimation(effect, b"opacity", widget)
    anim.setDuration(duration)
    anim.setEasingCurve(QEasingCurve.Type.OutCubic)
    anim.setStartValue(0.0)
    anim.setEndValue(1.0)

    def _cleanup() -> None:
        if getattr(widget, "_active_fade_anim", None) is anim:
            widget._active_fade_anim = None
            widget.setGraphicsEffect(None)

    anim.finished.connect(_cleanup)
    widget._active_fade_anim = anim
    anim.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)


def _apply_noactivate_style(widget: QWidget) -> None:
    if user32 is None:
        return
    try:
        hwnd = int(widget.winId())
        ex_style = user32.GetWindowLongW(hwnd, _GWL_EXSTYLE)
        user32.SetWindowLongW(hwnd, _GWL_EXSTYLE, ex_style | _WS_EX_NOACTIVATE | _WS_EX_TOOLWINDOW)
    except Exception:
        pass


def _apply_dark_titlebar(widget: QWidget) -> None:
    if dwmapi is None:
        return
    try:
        hwnd = int(widget.winId())
        enabled = ctypes.c_int(1)
        dwmapi.DwmSetWindowAttribute(
            hwnd,
            _DWMWA_USE_IMMUTABLE_DARK_MODE,
            ctypes.byref(enabled),
            ctypes.sizeof(enabled),
        )
    except Exception:
        pass


class _ToggleSwitch(QAbstractButton):
    """Minimal monochrome iOS-style toggle switch with animated thumb."""

    def __init__(self, checked: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(40, 22)
        self._thumb_x = 21.0 if checked else 3.0
        self._anim = QPropertyAnimation(self, b"thumbPos", self)
        self._anim.setDuration(145)
        self._anim.setEasingCurve(QEasingCurve.Type.OutBack)
        self.setChecked(checked)
        self.toggled.connect(self._animate_thumb)

    def _get_thumb_pos(self) -> float:
        return self._thumb_x

    def _set_thumb_pos(self, pos: float) -> None:
        self._thumb_x = pos
        self.update()

    thumbPos = Property(float, _get_thumb_pos, _set_thumb_pos)

    def _animate_thumb(self, checked: bool) -> None:
        self._anim.stop()
        self._anim.setStartValue(self._thumb_x)
        self._anim.setEndValue(21.0 if checked else 3.0)
        self._anim.start()

    def setChecked(self, checked: bool) -> None:
        super().setChecked(checked)
        if not self._anim.state():
            self._thumb_x = 21.0 if checked else 3.0
            self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        progress = max(0.0, min(1.0, (self._thumb_x - 3.0) / 18.0))

        track_color = _lerp_color(QColor("#15151E"), QColor(_ACCENT_INDIGO), progress)
        painter.setPen(QPen(_lerp_color(QColor("#232330"), QColor(123, 139, 255, 160), progress), 1.0))
        painter.setBrush(QBrush(track_color))
        painter.drawRoundedRect(QRectF(0.5, 0.5, self.width() - 1.0, self.height() - 1.0), 10.5, 10.5)

        painter.setPen(Qt.PenStyle.NoPen)
        thumb_color = _lerp_color(QColor(_TEXT_SECONDARY), QColor("#FFFFFF"), progress)
        painter.setBrush(QBrush(thumb_color))
        painter.drawEllipse(QRectF(self._thumb_x, 3.0, 16.0, 16.0))


def _draw_spark_star(
    painter: QPainter,
    cx: float,
    cy: float,
    radius: float,
    color: QColor,
) -> None:
    """Paint a crisp 4-point AI spark star centered at (cx, cy)."""
    inner = radius * 0.28
    path = QPainterPath()
    path.moveTo(cx, cy - radius)
    path.quadTo(cx + inner, cy - inner, cx + radius, cy)
    path.quadTo(cx + inner, cy + inner, cx, cy + radius)
    path.quadTo(cx - inner, cy + inner, cx - radius, cy)
    path.quadTo(cx - inner, cy - inner, cx, cy - radius)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(color))
    painter.drawPath(path)


def _draw_whisperlm_glyph(
    painter: QPainter,
    rect: QRectF,
    bar_color: QColor,
    spark_color: QColor | None = None,
    heights: tuple[float, ...] = (0.48, 0.94, 0.62),
) -> None:
    """Paint the WhisperLM emblem: 3 crisp voice bars paired with an indigo LM star spark."""
    w = rect.width()
    h = rect.height()
    bars_area_w = w * 0.62
    count = len(heights)
    bar_w = max(1.6, bars_area_w * 0.20)
    gap = max(1.0, (bars_area_w - count * bar_w) / max(1, count - 1))

    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(bar_color))
    for idx, ratio in enumerate(heights):
        bh = max(2.2, h * ratio)
        x = rect.x() + idx * (bar_w + gap)
        y = rect.y() + (h - bh) / 2.0
        painter.drawRoundedRect(QRectF(x, y, bar_w, bh), bar_w / 2.0, bar_w / 2.0)

    spark_r = max(2.2, min(w * 0.21, h * 0.34))
    spark_cx = rect.right() - spark_r
    spark_cy = rect.y() + h * 0.36
    sc = spark_color if spark_color is not None else QColor(123, 139, 255, bar_color.alpha())
    _draw_spark_star(painter, spark_cx, spark_cy, spark_r, sc)


class _BrandMark(QWidget):
    """Crisp WhisperLM soundwave + indigo spark icon with gentle hover & status breath."""

    def __init__(self, size: int = 20, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(size, size)
        self.setMouseTracking(True)
        self._phase = 0.0
        self._hovered = False
        self._pulsing = False
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)

    def _tick(self) -> None:
        self._phase += 0.16
        self.update()

    def set_pulsing(self, active: bool) -> None:
        self._pulsing = bool(active)
        if self._pulsing and not self._timer.isActive():
            self._timer.start()
        elif not self._pulsing and not self._hovered:
            self._timer.stop()
            self._phase = 0.0
            self.update()

    def enterEvent(self, event) -> None:
        self._hovered = True
        if not self._timer.isActive():
            self._timer.start()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hovered = False
        if not self._pulsing:
            self._timer.stop()
            self._phase = 0.0
            self.update()
        super().leaveEvent(event)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        base = (0.48, 0.94, 0.62)
        if self._timer.isActive():
            amp = 0.16
            heights = tuple(
                max(0.28, min(1.0, h + amp * math.sin(self._phase - idx * 0.9)))
                for idx, h in enumerate(base)
            )
        else:
            heights = base
        _draw_whisperlm_glyph(
            painter,
            QRectF(0.5, 1.0, float(self.width()) - 1.0, float(self.height()) - 2.0),
            QColor(_TEXT_PRIMARY),
            QColor(_ACCENT_INDIGO),
            heights=heights,
        )


def _draw_pill_shell(
    painter: QPainter,
    rect: QRectF,
    radius: float,
    border_alpha: int,
    fill_alpha: int = 250,
    glow_color: QColor | None = None,
) -> None:
    """Paint a deep obsidian capsule with ambient shadow and optional animated accent rim."""
    for expand, alpha in ((4.5, 18), (2.4, 34), (1.0, 56)):
        shadow_rect = rect.adjusted(-expand, -expand + 1.0, expand, expand + 2.0)
        shadow_path = QPainterPath()
        shadow_path.addRoundedRect(shadow_rect, radius + expand, radius + expand)
        painter.fillPath(shadow_path, QColor(0, 0, 0, alpha))

    if glow_color is not None and glow_color.alpha() > 0:
        aura_rect = rect.adjusted(-2.0, -2.0, 2.0, 2.0)
        aura_path = QPainterPath()
        aura_path.addRoundedRect(aura_rect, radius + 2.0, radius + 2.0)
        painter.setPen(QPen(glow_color, 1.6))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(aura_path)

    shell_path = QPainterPath()
    shell_path.addRoundedRect(rect, radius, radius)
    grad = QLinearGradient(rect.topLeft(), rect.bottomLeft())
    grad.setColorAt(0.0, QColor(14, 14, 20, fill_alpha))
    grad.setColorAt(1.0, QColor(5, 5, 8, min(255, fill_alpha + 4)))
    painter.fillPath(shell_path, QBrush(grad))
    border_pen_color = glow_color if (glow_color is not None and glow_color.alpha() > border_alpha) else QColor(255, 255, 255, border_alpha)
    painter.setPen(QPen(border_pen_color, 1.0))
    painter.drawPath(shell_path)


class _WaveformWidget(QWidget):
    """Fluid symmetrical monochrome waveform centered inside the floating dictation pill."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(0)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._bars: list[float] = [0.18] * 11
        self._mode = "idle"
        self._phase = 0.0
        self._reveal = 1.0
        self._center_weights = [
            0.30, 0.44, 0.62, 0.80, 0.94, 1.0, 0.94, 0.80, 0.62, 0.44, 0.30,
        ]

    def set_reveal(self, value: float) -> None:
        self._reveal = max(0.0, min(1.0, float(value)))
        self.update()

    def set_mode(self, mode: str) -> None:
        self._mode = mode
        if mode == "idle":
            self._bars = [0.18] * len(self._bars)
        self.update()

    def step_animation(self, level: float, phase: float) -> None:
        self._phase = phase
        n = len(self._bars)
        if self._mode == "listening":
            floor = 0.16
            effective = max(0.09, level)
            for idx in range(n):
                wave = 0.72 + 0.28 * math.sin(phase * 1.45 - idx * 0.62)
                harmonic = 0.14 * math.cos(phase * 2.15 + idx * 0.95)
                target = floor + max(0.0, (effective * wave + harmonic)) * self._center_weights[idx] * 0.92
                self._bars[idx] = self._bars[idx] * 0.48 + min(1.0, target) * 0.52
        elif self._mode in {"loading", "transcribing", "cleaning"}:
            for idx in range(n):
                travel = 0.5 + 0.5 * math.sin(phase * 1.15 - idx * 0.68)
                target = 0.16 + 0.42 * travel * self._center_weights[idx]
                self._bars[idx] = self._bars[idx] * 0.52 + target * 0.48
        self.update()

    def paintEvent(self, _event) -> None:
        if self._reveal < 0.03:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        n = len(self._bars)
        bar_w = 2.2
        gap = 2.5
        total_w = n * bar_w + (n - 1) * gap
        start_x = (self.width() - total_w) / 2.0
        cy = self.height() / 2.0
        max_h = max(4.0, min(16.0, self.height() - 4.0))

        painter.setPen(Qt.PenStyle.NoPen)
        alpha = int(round((242 if self._mode == "listening" else 195) * self._reveal))
        wave_color = (
            QColor(123, 139, 255, alpha)
            if self._mode in {"cleaning", "loading"}
            else QColor(250, 250, 252, alpha)
        )
        painter.setBrush(QBrush(wave_color))
        scale_h = 0.4 + 0.6 * self._reveal
        for idx, val in enumerate(self._bars):
            bh = max(2.4, min(max_h, val * max_h * scale_h))
            x = start_x + idx * (bar_w + gap)
            y = cy - bh / 2.0
            painter.drawRoundedRect(QRectF(x, y, bar_w, bh), 1.1, 1.1)


class _FlowRecordButton(QPushButton):
    """Compact circular record/stop control shown when hovering the resting Flow Bar."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._recording = False
        self._hovered = False
        self.setFixedSize(20, 20)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMouseTracking(True)
        self.setToolTip("Start Recording")

    def set_recording(self, active: bool) -> None:
        self._recording = active
        self.setToolTip("Stop Recording" if active else "Start Recording")
        self.update()

    def enterEvent(self, event) -> None:
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)

        bg_alpha = 40 if self._recording else (28 if self._hovered else 14)
        bg = QColor(239, 68, 68, bg_alpha) if self._recording else QColor(255, 255, 255, bg_alpha)
        painter.setBrush(bg)
        painter.drawEllipse(QRectF(1.0, 1.0, 18.0, 18.0))

        cx, cy = self.width() / 2.0, self.height() / 2.0
        painter.setBrush(QColor(_ACCENT_RED))
        if self._recording:
            painter.drawRoundedRect(QRectF(cx - 3.25, cy - 3.25, 6.5, 6.5), 1.5, 1.5)
        else:
            painter.drawEllipse(QRectF(cx - 3.5, cy - 3.5, 7.0, 7.0))


class FlowPillWidget(QWidget):
    """Minimal Wispr Flow-style floating bar: tiny resting icon, hover Record control, active waves."""

    state_requested = Signal(str, str, int)
    level_received = Signal(float)
    recording_state_changed = Signal(bool)

    _IDLE_SIZE = (42, 22)
    _HOVER_SIZE = (88, 34)
    _ACTIVE_SIZE = (104, 40)

    # (inset_x, inset_y, radius, glyph_alpha, glyph_shift_x, wave_reveal, label_alpha, rec_alpha)
    _MODE_SPECS: dict[str, tuple[float, float, float, float, float, float, float, float]] = {
        "idle": (5.0, 4.0, 7.0, 1.0, 0.0, 0.0, 0.0, 0.0),
        "hover": (6.0, 5.0, 12.0, 1.0, -16.0, 0.0, 0.0, 1.0),
        "active": (8.0, 6.0, 14.0, 0.0, 0.0, 1.0, 0.0, 0.0),
        "text": (8.0, 6.0, 14.0, 0.0, 0.0, 0.0, 1.0, 0.0),
    }

    def __init__(
        self,
        always_on: bool = True,
        on_toggle_dictate: Callable[[], None] | None = None,
        on_toggle_recording: Callable[[], None] | None = None,
        on_open_hub: Callable[[], None] | None = None,
        on_set_always_on: Callable[[bool], None] | None = None,
    ) -> None:
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        self._always_on = always_on
        self._on_toggle_dictate = on_toggle_dictate
        self._on_toggle_recording = on_toggle_recording
        self._on_open_hub = on_open_hub
        self._on_set_always_on = on_set_always_on

        self._state = "idle"
        self._session_recording = False
        self._phase = 0.0
        self._target_level = 0.0
        self._display_level = 0.0
        self._hovered = False
        self._press_global: QPoint | None = None
        self._drag_offset: QPoint | None = None
        self._dragging = False
        self._custom_bottom_center: QPoint | None = None

        self._visual_mode = "idle" if always_on else "active"
        self._from_visuals = self._MODE_SPECS[self._visual_mode]
        self._from_size = self._IDLE_SIZE if always_on else self._ACTIVE_SIZE
        self._morph_target_size = self._from_size
        self._morph_progress = 1.0

        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(12, 6, 12, 6)
        self._layout.setSpacing(0)

        self._waveform = _WaveformWidget()
        self._label = QLabel()
        self._label.setMinimumWidth(0)
        self._label.setFont(QFont("Segoe UI", 9, QFont.Weight.Medium))
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._label.setStyleSheet(f"color: {_TEXT_PRIMARY}; background: transparent;")
        self._label.hide()

        self._layout.addWidget(self._waveform, 1)
        self._layout.addWidget(self._label, 1)

        self._record_btn = _FlowRecordButton(self)
        self._record_btn.hide()
        if self._on_toggle_recording is not None:
            self._record_btn.clicked.connect(self._on_toggle_recording)

        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(lambda: self.set_state("fade_out", "", 0))

        self._hover_collapse_timer = QTimer(self)
        self._hover_collapse_timer.setSingleShot(True)
        self._hover_collapse_timer.setInterval(140)
        self._hover_collapse_timer.timeout.connect(self._on_hover_collapse_timeout)

        self._anim_timer = QTimer(self)
        self._anim_timer.setInterval(16)
        self._anim_timer.timeout.connect(self._on_anim_tick)

        self.state_requested.connect(self.set_state)
        self.level_received.connect(self.set_audio_level)
        self.recording_state_changed.connect(self._set_recording_active)

        self._fade_anim = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade_anim.setDuration(110)
        self._fade_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fade_anim.finished.connect(self._on_fade_finished)
        self._hiding_after_fade = False

        self._morph_anim = QPropertyAnimation(self, b"morphProgress", self)
        self._morph_anim.setDuration(180)
        self._morph_anim.finished.connect(self._on_morph_finished)

        if self._always_on:
            self.set_state("idle", "", 0)
        else:
            self.setFixedSize(*self._ACTIVE_SIZE)

    def _set_recording_active(self, active: bool) -> None:
        self._session_recording = bool(active)
        self._record_btn.set_recording(self._session_recording)
        self.update()

    def _should_hover_expand(self) -> bool:
        return (
            self._always_on
            and self._state == "idle"
            and self._hovered
            and not self._dragging
            and self._on_toggle_recording is not None
        )

    def _position_record_btn(self, w: int | None = None, h: int | None = None) -> None:
        pw = self.width() if w is None else w
        ph = self.height() if h is None else h
        bx = max(4, pw - 33)
        by = max(2, (ph - self._record_btn.height()) // 2)
        self._record_btn.move(bx, by)

    def _current_visuals(self) -> tuple[float, float, float, float, float, float, float, float]:
        t = max(0.0, min(1.0, self._morph_progress))
        target = self._MODE_SPECS[self._visual_mode]
        a0, a1, a2, a3, a4, a5, a6, a7 = self._from_visuals
        b0, b1, b2, b3, b4, b5, b6, b7 = target
        return (
            a0 + (b0 - a0) * t,
            a1 + (b1 - a1) * t,
            a2 + (b2 - a2) * t,
            a3 + (b3 - a3) * t,
            a4 + (b4 - a4) * t,
            a5 + (b5 - a5) * t,
            a6 + (b6 - a6) * t,
            a7 + (b7 - a7) * t,
        )

    def _begin_morph(
        self,
        to_mode: str,
        to_size: tuple[int, int],
        duration: int,
        curve: QEasingCurve.Type,
    ) -> None:
        self._from_visuals = self._current_visuals()
        self._from_size = (self.width(), self.height())
        self._visual_mode = to_mode
        self._morph_target_size = to_size
        self._morph_anim.stop()
        self._morph_anim.setDuration(duration)
        self._morph_anim.setEasingCurve(curve)
        self._morph_anim.setStartValue(0.0)
        self._morph_anim.setEndValue(1.0)
        self._set_morph_progress(0.0)
        self._morph_anim.start()

    def _get_morph_progress(self) -> float:
        return self._morph_progress

    def _set_morph_progress(self, value: float) -> None:
        self._morph_progress = value
        clamped = max(0.0, value)
        fw, fh = self._from_size
        tw, th = self._morph_target_size
        w = max(self._IDLE_SIZE[0], int(round(fw + (tw - fw) * clamped)))
        h = max(self._IDLE_SIZE[1], int(round(fh + (th - fh) * clamped)))

        _, _, _, _, _, wave_rev, label_a, rec_a = self._current_visuals()
        margin_frac = max(wave_rev, label_a)
        mx = int(round(12 * margin_frac))
        my = int(round(6 * margin_frac))
        self._layout.setContentsMargins(mx, my, mx, my)
        self._waveform.set_reveal(wave_rev)
        if self._state in {"loading", "done", "error"}:
            self._label.setVisible(label_a >= 0.65)
        show_rec = self._always_on and self._state == "idle" and rec_a >= 0.72 and self._on_toggle_recording is not None
        if show_rec:
            self._position_record_btn(w, h)
            self._record_btn.show()
        else:
            self._record_btn.hide()
        self.setFixedSize(w, h)
        self._reposition()
        self.update()

    morphProgress = Property(float, _get_morph_progress, _set_morph_progress)

    def _on_morph_finished(self) -> None:
        if self._state == "idle" and self._always_on:
            self._waveform.set_mode("idle")
            self._waveform.hide()
            self._label.hide()
            self._layout.setContentsMargins(0, 0, 0, 0)
            if self._visual_mode == "hover":
                self.setFixedSize(*self._HOVER_SIZE)
                self._reposition()
                if self._on_toggle_recording is not None:
                    self._position_record_btn()
                    self._record_btn.show()
            else:
                self._record_btn.hide()
                self.setFixedSize(*self._IDLE_SIZE)
                self._reposition()
            self.update()

    def set_always_on(self, enabled: bool) -> None:
        self._always_on = enabled
        self._hover_collapse_timer.stop()
        if self._state == "idle":
            self._morph_anim.stop()
            self._hovered = False
            self._visual_mode = "idle" if enabled else "active"
            self._from_visuals = self._MODE_SPECS[self._visual_mode]
            self._morph_progress = 1.0
            self.set_state("idle", "", 0)

    def _on_fade_finished(self) -> None:
        if self._hiding_after_fade:
            self._hiding_after_fade = False
            self.hide()

    def _fade_and_hide(self) -> None:
        self._anim_timer.stop()
        self._waveform.set_mode("idle")
        if not self.isVisible():
            return
        self._hiding_after_fade = True
        self._fade_anim.stop()
        self._fade_anim.setStartValue(self.windowOpacity())
        self._fade_anim.setEndValue(0.0)
        self._fade_anim.start()

    def _on_anim_tick(self) -> None:
        self._phase += 0.22
        self._display_level = self._display_level * 0.42 + self._target_level * 0.58
        self._target_level *= 0.89
        self._waveform.step_animation(self._display_level, self._phase)

    def _on_hover_collapse_timeout(self) -> None:
        if self._press_global is not None:
            return
        if self.isVisible() and self.rect().contains(self.mapFromGlobal(QCursor.pos())):
            return
        self._set_hovered(False)

    def _set_hovered(self, hovered: bool) -> None:
        if self._hovered == hovered:
            return
        self._hovered = hovered
        if self._always_on and self._state == "idle" and self.isVisible():
            if hovered and self._should_hover_expand():
                self._begin_morph("hover", self._HOVER_SIZE, 175, QEasingCurve.Type.OutBack)
            elif not hovered and self._visual_mode != "idle":
                self._begin_morph("idle", self._IDLE_SIZE, 165, QEasingCurve.Type.OutCubic)
                return
        self.update()

    def enterEvent(self, event) -> None:
        self._hover_collapse_timer.stop()
        self._set_hovered(True)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hover_collapse_timer.start()
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._hover_collapse_timer.stop()
            pos = event.globalPosition().toPoint()
            self._press_global = pos
            self._drag_offset = pos - self.frameGeometry().topLeft()
            self._dragging = False
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._press_global is not None and self._drag_offset is not None and (event.buttons() & Qt.MouseButton.LeftButton):
            pos = event.globalPosition().toPoint()
            if (pos - self._press_global).manhattanLength() >= 6:
                self._dragging = True
                target = pos - self._drag_offset
                screen = QApplication.primaryScreen()
                if screen:
                    avail = screen.availableGeometry()
                    nx = max(avail.x() + 4, min(avail.x() + avail.width() - self.width() - 4, target.x()))
                    ny = max(avail.y() + 4, min(avail.y() + avail.height() - self.height() - 4, target.y()))
                    self.move(nx, ny)
                    self._custom_bottom_center = QPoint(nx + self.width() // 2, ny + self.height())
                else:
                    self.move(target)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._press_global is not None:
            was_drag = self._dragging
            self._press_global = None
            self._drag_offset = None
            self._dragging = False
            if not was_drag:
                if self._state == "loading" and self._on_open_hub is not None:
                    self._on_open_hub()
                elif self._on_toggle_dictate is not None:
                    self._on_toggle_dictate()
            elif not self.rect().contains(self.mapFromGlobal(QCursor.pos())):
                self._hover_collapse_timer.start()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event) -> None:
        menu = QMenu(self)
        menu.setStyleSheet(_DARK_MENU_QSS)
        if self._on_toggle_dictate is not None:
            label = "Stop Dictation" if self._state == "listening" else "Start Dictation"
            dictate_act = menu.addAction(label)
            dictate_act.triggered.connect(self._on_toggle_dictate)
        if self._on_toggle_recording is not None:
            rec_label = "Stop Recording" if self._session_recording else "Start Recording"
            rec_act = menu.addAction(rec_label)
            rec_act.triggered.connect(self._on_toggle_recording)
        if self._on_open_hub is not None:
            open_act = menu.addAction("Open WhisperLM")
            open_act.triggered.connect(self._on_open_hub)
        if self._custom_bottom_center is not None:
            reset_act = menu.addAction("Reset Icon Position")
            reset_act.triggered.connect(self._reset_position)
        menu.addSeparator()
        hide_act = menu.addAction("Hide Floating Bar")
        hide_act.triggered.connect(lambda: (self._on_set_always_on(False) if self._on_set_always_on else self.set_always_on(False)))
        menu.popup(event.globalPos())
        event.accept()

    def _reset_position(self) -> None:
        self._custom_bottom_center = None
        self._reposition()

    def _pill_glow_color(self) -> QColor | None:
        if self._state == "listening":
            pulse = 0.55 + 0.45 * math.sin(self._phase * 1.15)
            return QColor(123, 139, 255, int(round(52 + 68 * pulse)))
        if self._state in {"cleaning", "loading", "transcribing"}:
            pulse = 0.5 + 0.5 * math.sin(self._phase * 0.9)
            return QColor(123, 139, 255, int(round(36 + 44 * pulse)))
        if self._state == "error":
            return QColor(248, 113, 113, 96)
        if self._session_recording:
            return QColor(239, 68, 68, 72)
        if self._hovered:
            return QColor(123, 139, 255, 65)
        return None

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        glow = self._pill_glow_color()
        if not self._always_on:
            rect = QRectF(8.0, 6.0, self.width() - 16.0, self.height() - 12.0)
            alpha = 46 if self._state == "listening" else (38 if self._state == "error" else 30)
            _draw_pill_shell(painter, rect, radius=14.0, border_alpha=alpha, glow_color=glow)
            return

        inset_x, inset_y, radius, glyph_a, glyph_shift, _, _, rec_a = self._current_visuals()
        rect = QRectF(inset_x, inset_y, self.width() - 2.0 * inset_x, self.height() - 2.0 * inset_y)
        is_idle_like = self._state == "idle"
        base_border = 58 if self._hovered else 34
        target_border = 46 if self._state == "listening" else (38 if self._state == "error" else 30)
        border_alpha = int(round(base_border * glyph_a + target_border * (1.0 - glyph_a)))
        fill_alpha = int(round((248 if self._hovered else 234) * glyph_a + 250 * (1.0 - glyph_a)))
        _draw_pill_shell(painter, rect, radius=radius, border_alpha=border_alpha, fill_alpha=fill_alpha, glow_color=glow)

        if glyph_a > 0.02:
            glyph_alpha = int(round((242 if self._hovered else 182) * glyph_a))
            spark_alpha = int(round((245 if self._hovered else 210) * glyph_a))
            cx = rect.center().x() + glyph_shift
            cy = rect.center().y()
            _draw_whisperlm_glyph(
                painter,
                QRectF(cx - 6.5, cy - 4.5, 13.0, 9.0),
                QColor(250, 250, 252, glyph_alpha),
                QColor(123, 139, 255, spark_alpha),
            )
            if self._session_recording and is_idle_like and rec_a < 0.35:
                dot_alpha = int(round(235 * glyph_a * (1.0 - rec_a / 0.35)))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(239, 68, 68, dot_alpha))
                painter.drawEllipse(QRectF(cx + 8.0, cy - 5.0, 4.5, 4.5))

        if rec_a > 0.08:
            div_x = rect.center().x() + 2.0
            painter.setPen(QPen(QColor(255, 255, 255, int(round(32 * rec_a))), 1.0))
            painter.drawLine(QPoint(int(round(div_x)), int(round(rect.top() + 5))), QPoint(int(round(div_x)), int(round(rect.bottom() - 5))))

    def set_audio_level(self, level: float) -> None:
        self._target_level = max(self._target_level * 0.35, max(0.0, min(1.0, float(level))))

    def set_state(self, state: str, message: str = "", auto_hide_ms: int = 0) -> None:
        self._hide_timer.stop()
        self._hiding_after_fade = False

        if state == "fade_out":
            if self._always_on:
                state = "idle"
            else:
                self._state = state
                self._fade_and_hide()
                return

        self._state = state

        if state == "idle":
            self._anim_timer.stop()
            self._fade_anim.stop()
            if not self._always_on:
                self._waveform.set_mode("idle")
                self._waveform.hide()
                self._label.hide()
                self._record_btn.hide()
                self.hide()
                return
            self.setWindowOpacity(1.0)
            target_mode = "hover" if self._should_hover_expand() else "idle"
            target_size = self._HOVER_SIZE if target_mode == "hover" else self._IDLE_SIZE
            if not self.isVisible():
                self._morph_anim.stop()
                self._visual_mode = target_mode
                self._from_visuals = self._MODE_SPECS[target_mode]
                self._from_size = target_size
                self._morph_target_size = target_size
                self._morph_progress = 1.0
                self._waveform.set_mode("idle")
                self._waveform.set_reveal(0.0)
                self._waveform.hide()
                self._label.hide()
                self._layout.setContentsMargins(0, 0, 0, 0)
                self.setFixedSize(*target_size)
                if target_mode == "hover" and self._on_toggle_recording is not None:
                    self._position_record_btn()
                    self._record_btn.show()
                else:
                    self._record_btn.hide()
                self._reposition()
                self.show()
                _apply_noactivate_style(self)
                self.update()
            elif (self.width(), self.height()) == target_size and target_mode == "idle":
                self._morph_anim.stop()
                self._visual_mode = "idle"
                self._from_visuals = self._MODE_SPECS["idle"]
                self._from_size = target_size
                self._morph_target_size = target_size
                self._morph_progress = 1.0
                self._waveform.set_mode("idle")
                self._waveform.set_reveal(0.0)
                self._waveform.hide()
                self._label.hide()
                self._record_btn.hide()
                self._layout.setContentsMargins(0, 0, 0, 0)
                self.setFixedSize(*target_size)
                self._reposition()
                self.update()
            else:
                self._label.hide()
                self._record_btn.hide()
                self._begin_morph(target_mode, target_size, 185, QEasingCurve.Type.OutCubic)
            return

        self._hover_collapse_timer.stop()
        self._record_btn.hide()
        if state in {"listening", "transcribing", "cleaning"}:
            target_mode = "active"
            target_size = self._ACTIVE_SIZE
            self._label.hide()
            self._waveform.show()
            self._waveform.set_mode(state)
            if not self._anim_timer.isActive():
                self._anim_timer.start()
        else:
            # Startup / ready / error compact text capsule
            target_mode = "text"
            self._anim_timer.stop()
            self._waveform.hide()
            preview = message if len(message) <= 34 else message[:31] + "..."
            text_color = _ACCENT_RED if state == "error" else _TEXT_PRIMARY
            self._label.setStyleSheet(f"color: {text_color}; background: transparent;")
            self._label.setText(preview or ("Done" if state == "done" else "Error"))
            w = max(118, min(276, self._label.sizeHint().width() + 40))
            target_size = (w, 40)

        self._fade_anim.stop()
        if not self.isVisible():
            self._morph_anim.stop()
            self._visual_mode = target_mode
            self._from_visuals = self._MODE_SPECS[target_mode]
            self._from_size = target_size
            self._morph_target_size = target_size
            self._morph_progress = 1.0
            self._layout.setContentsMargins(12, 6, 12, 6)
            self._waveform.set_reveal(1.0)
            if target_mode == "text":
                self._label.show()
            self.setFixedSize(*target_size)
            self._reposition()
            self.setWindowOpacity(0.0)
            self.show()
            _apply_noactivate_style(self)
            self._fade_anim.setStartValue(0.0)
            self._fade_anim.setEndValue(1.0)
            self._fade_anim.start()
        elif self._always_on:
            self.setWindowOpacity(1.0)
            self._begin_morph(target_mode, target_size, 210, QEasingCurve.Type.OutBack)
        else:
            self._visual_mode = target_mode
            self._from_visuals = self._MODE_SPECS[target_mode]
            self._from_size = target_size
            self._morph_target_size = target_size
            self._morph_progress = 1.0
            self._layout.setContentsMargins(12, 6, 12, 6)
            self._waveform.set_reveal(1.0)
            if target_mode == "text":
                self._label.show()
            self.setFixedSize(*target_size)
            self._reposition()
            self.setWindowOpacity(1.0)
        self.update()

        if auto_hide_ms > 0:
            self._hide_timer.start(auto_hide_ms)

    def _reposition(self) -> None:
        screen = QApplication.primaryScreen()
        if not screen:
            return
        avail = screen.availableGeometry()
        expand_t = max(
            0.0,
            min(1.0, (self.height() - self._IDLE_SIZE[1]) / max(1, self._ACTIVE_SIZE[1] - self._IDLE_SIZE[1])),
        )
        if self._custom_bottom_center is not None:
            cx = self._custom_bottom_center.x()
            bottom_y = self._custom_bottom_center.y()
        else:
            cx = avail.x() + avail.width() // 2
            offset_bottom = 6.0 + 6.0 * expand_t if self._always_on else 12.0
            bottom_y = avail.y() + avail.height() - int(round(offset_bottom))
        x = max(avail.x() + 4, min(avail.x() + avail.width() - self.width() - 4, cx - self.width() // 2))
        y = max(avail.y() + 4, min(avail.y() + avail.height() - self.height() - 4, bottom_y - self.height()))
        self.move(x, y)


FloatingHudWidget = FlowPillWidget


class MeetingPillWidget(QWidget):
    """Sleek draggable top-right capsule shown while a meeting is actively recording."""

    state_changed = Signal(str, str)  # ("recording" | "cleaning" | "summarizing" | "idle", app_title)

    def __init__(self, on_stop_clicked: Callable[[], None], on_open_hub: Callable[[], None]) -> None:
        super().__init__()
        self._on_stop_clicked = on_stop_clicked
        self._on_open_hub = on_open_hub
        self._drag_offset: QPoint | None = None
        self._positioned = False
        self._started_at = 0.0
        self._state = "idle"
        self._pulse_on = True
        self._pulse_phase = 0.0
        self._app_label = "Meeting"

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFixedHeight(42)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(20, 8, 14, 8)
        layout.setSpacing(8)

        # Reserve 8px on the left for the anti-aliased recording circle painted in paintEvent
        self._dot_spacer = QWidget()
        self._dot_spacer.setFixedSize(8, 8)

        self._text = QLabel("Meeting · 00:00")
        self._text.setFont(QFont("Segoe UI", 9, QFont.Weight.DemiBold))
        self._text.setStyleSheet(f"color: {_TEXT_PRIMARY}; background: transparent;")

        self._stop_btn = QPushButton("Stop")
        self._stop_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._stop_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._stop_btn.setFixedHeight(20)
        self._stop_btn.setStyleSheet(
            f"QPushButton {{ background: #14141D; color: {_TEXT_SECONDARY}; border: 1px solid #242433; "
            f"border-radius: 10px; padding: 0 9px; font-size: 11px; font-weight: 600; outline: none; }}"
            "QPushButton:hover { background: #33161B; border-color: #6B2A35; color: #FCA5A5; }"
        )
        self._stop_btn.clicked.connect(self._on_stop_clicked)

        layout.addWidget(self._dot_spacer)
        layout.addWidget(self._text)
        layout.addWidget(self._stop_btn)

        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(1000)
        self._tick_timer.timeout.connect(self._update_elapsed)

        self._pulse_timer = QTimer(self)
        self._pulse_timer.setInterval(33)
        self._pulse_timer.timeout.connect(self._step_pulse)

        self._fade_anim = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade_anim.setDuration(175)
        self._fade_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.state_changed.connect(self._apply_state)

    def _step_pulse(self) -> None:
        self._pulse_phase += 0.14
        self.update()

    def _apply_state(self, state: str, label: str) -> None:
        was_visible = self.isVisible()
        self._state = state
        self._app_label = (label or "Meeting")[:14]
        if state == "idle":
            self._tick_timer.stop()
            self._pulse_timer.stop()
            self.hide()
            return
        if state == "recording":
            import time

            self._started_at = time.monotonic()
            self._pulse_on = True
            self._stop_btn.show()
            self._update_elapsed()
            self._tick_timer.start()
            self._pulse_timer.start()
        elif state in {"cleaning", "summarizing"}:
            self._tick_timer.stop()
            self._pulse_timer.start()
            self._text.setText("Summarizing meeting..." if state == "summarizing" else "Polishing meeting...")
            self._stop_btn.hide()

        self.adjustSize()
        if not self._positioned:
            self._place_default()
            self._positioned = True
        if not was_visible:
            self.setWindowOpacity(0.0)
            self.show()
            _apply_noactivate_style(self)
            self._fade_anim.stop()
            self._fade_anim.setStartValue(0.0)
            self._fade_anim.setEndValue(1.0)
            self._fade_anim.start()
        else:
            self.show()
            _apply_noactivate_style(self)
        self.update()

    def _update_elapsed(self) -> None:
        import time

        elapsed = int(time.monotonic() - self._started_at)
        self._pulse_on = not self._pulse_on
        self._text.setText(f"{self._app_label} · {format_duration(elapsed)}")
        self.adjustSize()
        self.update()

    def _place_default(self) -> None:
        screen = QApplication.primaryScreen()
        if not screen:
            return
        avail = screen.availableGeometry()
        self.move(avail.x() + avail.width() - self.width() - 18, avail.y() + 14)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event) -> None:
        if self._drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()

    def mouseDoubleClickEvent(self, _event) -> None:
        self._on_open_hub()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(8.0, 6.0, self.width() - 16.0, self.height() - 12.0)
        breath = 0.5 + 0.5 * math.sin(self._pulse_phase)
        if self._state == "recording":
            glow = QColor(248, 113, 113, int(round(42 + 64 * breath)))
            dot_color = _lerp_color(QColor("#992929"), QColor(_ACCENT_RED), breath)
        else:
            glow = QColor(123, 139, 255, int(round(36 + 52 * breath)))
            dot_color = _lerp_color(QColor(_TEXT_SECONDARY), QColor(_ACCENT_INDIGO), breath)
        _draw_pill_shell(painter, rect, radius=15.0, border_alpha=32, glow_color=glow)

        painter.setPen(Qt.PenStyle.NoPen)
        cy = rect.center().y()
        if self._state == "recording":
            halo_r = 5.5 + 2.5 * breath
            painter.setBrush(QBrush(QColor(248, 113, 113, int(round(75 * (1.0 - 0.5 * breath))))))
            painter.drawEllipse(QRectF(23.5 - halo_r, cy - halo_r, halo_r * 2.0, halo_r * 2.0))
        painter.setBrush(QBrush(dot_color))
        painter.drawEllipse(QRectF(20.0, cy - 3.5, 7.0, 7.0))


class _HoverCardMixin:
    """Shared smooth hover progress animation for dark interactive surfaces."""

    def _init_hover_anim(self, duration: int = 150) -> None:
        self._hover_t = 0.0
        self._hover_anim = QVariantAnimation(self)
        self._hover_anim.setDuration(duration)
        self._hover_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._hover_anim.valueChanged.connect(self._on_hover_val)

    def _on_hover_val(self, val: Any) -> None:
        self._hover_t = float(val)
        self.update()

    def _start_hover_anim(self, hovered: bool) -> None:
        self._hover_anim.stop()
        self._hover_anim.setStartValue(self._hover_t)
        self._hover_anim.setEndValue(1.0 if hovered else 0.0)
        self._hover_anim.start()


class _MetricTile(_HoverCardMixin, QFrame):
    """Deep obsidian metric card with animated hover border glow and update shimmer."""

    def __init__(self, label: str, value: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("MetricTile")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        self.setMouseTracking(True)
        self._init_hover_anim(150)

        self._pulse_t = 0.0
        self._pulse_anim = QVariantAnimation(self)
        self._pulse_anim.setDuration(420)
        self._pulse_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._pulse_anim.valueChanged.connect(self._on_pulse_val)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 13, 16, 13)
        layout.setSpacing(4)

        self.value_lbl = QLabel(value)
        self.value_lbl.setObjectName("MetricValue")
        self.label_lbl = QLabel(label)
        self.label_lbl.setObjectName("MetricLabel")

        layout.addWidget(self.value_lbl)
        layout.addWidget(self.label_lbl)

    def _on_pulse_val(self, val: Any) -> None:
        self._pulse_t = float(val)
        self.update()

    def pulse(self) -> None:
        self._pulse_anim.stop()
        self._pulse_anim.setStartValue(1.0)
        self._pulse_anim.setEndValue(0.0)
        self._pulse_anim.start()

    def enterEvent(self, event) -> None:
        self._start_hover_anim(True)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._start_hover_anim(False)
        super().leaveEvent(event)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(0.5, 0.5, self.width() - 1.0, self.height() - 1.0)
        fill = _lerp_color(QColor(_BG_CARD), QColor(_BG_CARD_HOVER), self._hover_t)
        border = _lerp_color(QColor(_BORDER), QColor(123, 139, 255, 110), max(self._hover_t, self._pulse_t * 0.9))
        painter.setPen(QPen(border, 1.0))
        painter.setBrush(QBrush(fill))
        painter.drawRoundedRect(rect, 12.0, 12.0)

        glow_alpha = int(round(max(self._hover_t * 34.0, self._pulse_t * 58.0)))
        if glow_alpha > 1:
            grad = QRadialGradient(rect.width() * 0.25, 0.0, rect.width() * 0.85)
            grad.setColorAt(0.0, QColor(123, 139, 255, glow_alpha))
            grad.setColorAt(1.0, QColor(123, 139, 255, 0))
            clip = QPainterPath()
            clip.addRoundedRect(rect, 12.0, 12.0)
            painter.fillPath(clip, QBrush(grad))


def _bidi_ltr_display(text: str) -> str:
    """Keep mixed English+Arabic sentences left-to-right when Latin text dominates."""
    latin = sum(1 for ch in text if ("a" <= ch.lower() <= "z"))
    arabic = sum(1 for ch in text if "\u0600" <= ch <= "\u06ff")
    if latin > 0 and arabic > 0 and latin >= arabic:
        return f"\u200e{text}"
    return text


def _hairline_divider() -> QFrame:
    """Create a subtle 1px horizontal divider inside Hub cards."""
    line = QFrame()
    line.setFixedHeight(1)
    line.setStyleSheet(f"background-color: {_BORDER_SUBTLE};")
    return line


class _DictationRow(_HoverCardMixin, QWidget):
    """Single transcript row with smooth hover highlight inside a grouped day card."""

    def __init__(self, entry: HistoryEntry, on_copy: Callable[[str], None], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._on_copy = on_copy
        self._text = entry.cleaned_text
        self.setMouseTracking(True)
        self._init_hover_anim(135)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 13, 16, 13)
        layout.setSpacing(6)

        top = QHBoxLayout()
        meta_parts = [format_entry_when(entry)]
        if entry.app_name:
            meta_parts.append(entry.app_name)
        meta = QLabel("  ·  ".join(meta_parts))
        meta.setObjectName("MutedMeta")
        top.addWidget(meta)
        top.addStretch(1)

        self._copy_btn = QPushButton("Copy")
        self._copy_btn.setObjectName("GhostPillBtn")
        self._copy_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._copy_btn.clicked.connect(self._handle_copy)
        top.addWidget(self._copy_btn)
        layout.addLayout(top)

        body = QLabel(_bidi_ltr_display(entry.cleaned_text))
        body.setObjectName("BodyText")
        body.setWordWrap(True)
        body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(body)

    def enterEvent(self, event) -> None:
        self._start_hover_anim(True)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._start_hover_anim(False)
        super().leaveEvent(event)

    def paintEvent(self, _event) -> None:
        if self._hover_t <= 0.01:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(QColor(123, 139, 255, int(round(12 * self._hover_t)))))
        painter.drawRoundedRect(QRectF(4.0, 2.0, self.width() - 8.0, self.height() - 4.0), 8.0, 8.0)
        painter.setBrush(QBrush(QColor(123, 139, 255, int(round(165 * self._hover_t)))))
        painter.drawRoundedRect(QRectF(4.0, 10.0, 2.5, max(8.0, self.height() - 20.0)), 1.25, 1.25)

    def _handle_copy(self) -> None:
        self._on_copy(self._text)
        self._copy_btn.setText("Copied")
        QTimer.singleShot(1100, lambda: self._copy_btn.setText("Copy"))


class _DictationDayGroup(QFrame):
    """Grouped rounded card holding all dictations for a single date bucket."""

    def __init__(self, entries: list[HistoryEntry], on_copy: Callable[[str], None], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Card")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        for idx, entry in enumerate(entries):
            if idx > 0:
                layout.addWidget(_hairline_divider())
            layout.addWidget(_DictationRow(entry, on_copy=on_copy))


class _MeetingAudioBar(QFrame):
    """Compact obsidian audio playback bar for saved meeting reference WAV files."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("AudioBar")
        self._lock = threading.Lock()
        self._path: Path | None = None
        self._wf: wave.Wave_read | None = None
        self._stream: sd.OutputStream | None = None
        self._framerate = 16000
        self._channels = 1
        self._sampwidth = 2
        self._total_frames = 0
        self._pos_frames = 0
        self._playing = False
        self._scrubbing = False

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(10)

        self._play_btn = QPushButton("Play")
        self._play_btn.setObjectName("PrimaryPillBtn")
        self._play_btn.setFixedWidth(74)
        self._play_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._play_btn.clicked.connect(self.toggle_play)

        self._time_lbl = QLabel("00:00 / 00:00")
        self._time_lbl.setObjectName("MutedMeta")
        self._time_lbl.setMinimumWidth(88)

        self._slider = QSlider(Qt.Orientation.Horizontal)
        self._slider.setObjectName("AudioSlider")
        self._slider.setRange(0, 0)
        self._slider.setCursor(Qt.CursorShape.PointingHandCursor)
        self._slider.sliderPressed.connect(self._on_scrub_start)
        self._slider.sliderMoved.connect(self._on_scrub_move)
        self._slider.sliderReleased.connect(self._on_scrub_end)

        self._open_btn = QPushButton("Open Audio")
        self._open_btn.setObjectName("GhostPillBtn")
        self._open_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._open_btn.setToolTip("Open the recorded meeting WAV file in Explorer")
        self._open_btn.clicked.connect(self._open_in_folder)

        layout.addWidget(self._play_btn)
        layout.addWidget(self._slider, 1)
        layout.addWidget(self._time_lbl)
        layout.addWidget(self._open_btn)

        self._timer = QTimer(self)
        self._timer.setInterval(80)
        self._timer.timeout.connect(self._sync_progress)
        self.hide()

    def load_file(self, path: Path | None, *, animate: bool = False) -> None:
        resolved: Path | None = None
        if path is not None:
            try:
                resolved = path.resolve()
            except OSError:
                resolved = path
        if resolved is None or not resolved.exists() or not resolved.is_file():
            self.unload()
            return
        if self._path == resolved and self._total_frames > 0:
            if self.isHidden():
                self.show()
                if animate:
                    _fade_in_widget(self, duration=150)
            return

        self.unload()
        try:
            with wave.open(str(resolved), "rb") as wf:
                channels = int(wf.getnchannels())
                sampwidth = int(wf.getsampwidth())
                framerate = int(wf.getframerate())
                nframes = int(wf.getnframes())
        except Exception:
            self.hide()
            return

        if nframes <= 0 or framerate <= 0 or sampwidth != 2 or channels <= 0:
            self.hide()
            return

        self._path = resolved
        self._channels = channels
        self._sampwidth = sampwidth
        self._framerate = framerate
        self._total_frames = nframes
        self._pos_frames = 0

        self._slider.blockSignals(True)
        self._slider.setRange(0, nframes)
        self._slider.setValue(0)
        self._slider.blockSignals(False)
        self._play_btn.setEnabled(True)
        self._play_btn.setText("Play")
        self._play_btn.setToolTip("")
        self._update_time_label(0)

        was_hidden = self.isHidden()
        self.show()
        if animate and was_hidden:
            _fade_in_widget(self, duration=150)

    def unload(self) -> None:
        self._close_audio_output()
        self._path = None
        self._total_frames = 0
        self._pos_frames = 0
        self._slider.blockSignals(True)
        self._slider.setRange(0, 0)
        self._slider.setValue(0)
        self._slider.blockSignals(False)
        self._play_btn.setText("Play")
        self._time_lbl.setText("00:00 / 00:00")
        self.hide()

    def pause(self) -> None:
        self._close_audio_output()
        self._play_btn.setText("Play")

    def toggle_play(self) -> None:
        if self._playing:
            self.pause()
        else:
            self.play()

    def play(self) -> None:
        if self._path is None or not self._path.exists() or self._total_frames <= 0:
            return
        self._close_audio_output()
        with self._lock:
            if self._pos_frames >= self._total_frames:
                self._pos_frames = 0
            start_pos = self._pos_frames
        try:
            wf = wave.open(str(self._path), "rb")
            wf.setpos(max(0, min(start_pos, self._total_frames - 1)))
            stream = sd.OutputStream(
                samplerate=self._framerate,
                channels=self._channels,
                dtype="float32",
                callback=self._audio_callback,
            )
            with self._lock:
                self._wf = wf
                self._stream = stream
                self._playing = True
            stream.start()
        except Exception:
            self._close_audio_output()
            self._play_btn.setText("Play")
            self._play_btn.setToolTip("Audio playback unavailable on the active output device")
            return
        self._play_btn.setText("Pause")
        self._timer.start()

    def _close_audio_output(self) -> None:
        self._timer.stop()
        with self._lock:
            self._playing = False
            stream = self._stream
            wf = self._wf
            self._stream = None
            self._wf = None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass
        if wf is not None:
            try:
                wf.close()
            except Exception:
                pass

    def _audio_callback(self, outdata: np.ndarray, frames: int, _time_info: Any, _status: Any) -> None:
        with self._lock:
            wf = self._wf
            if not self._playing or wf is None:
                outdata.fill(0.0)
                return
            try:
                raw = wf.readframes(frames)
            except Exception:
                outdata.fill(0.0)
                self._playing = False
                return
            pcm = np.frombuffer(raw, dtype=np.int16)
            actual = pcm.size // max(1, self._channels)
            if actual > 0:
                usable = actual * self._channels
                floats = (pcm[:usable].astype(np.float32) / 32768.0).reshape(actual, self._channels)
                outdata[:actual, :] = floats
            if actual < frames:
                outdata[actual:, :] = 0.0
                self._pos_frames = self._total_frames
                self._playing = False
            else:
                self._pos_frames += actual

    def _on_scrub_start(self) -> None:
        self._scrubbing = True

    def _on_scrub_move(self, value: int) -> None:
        self._update_time_label(value)

    def _on_scrub_end(self) -> None:
        self._scrubbing = False
        self.seek(self._slider.value())

    def seek(self, frame_pos: int) -> None:
        target = max(0, min(int(frame_pos), self._total_frames))
        with self._lock:
            self._pos_frames = target
            if self._wf is not None:
                try:
                    self._wf.setpos(min(target, max(0, self._total_frames - 1)))
                except Exception:
                    pass
        self._update_time_label(target)

    def _sync_progress(self) -> None:
        with self._lock:
            playing = self._playing
            pos = self._pos_frames
        if not self._scrubbing:
            self._slider.blockSignals(True)
            self._slider.setValue(min(pos, self._total_frames))
            self._slider.blockSignals(False)
            self._update_time_label(pos)
        if not playing:
            self._close_audio_output()
            if pos >= self._total_frames:
                with self._lock:
                    self._pos_frames = 0
                self._slider.setValue(0)
                self._update_time_label(0)
            self._play_btn.setText("Play")

    def _update_time_label(self, frame_pos: int) -> None:
        cur_secs = int(max(0, frame_pos) // max(1, self._framerate))
        tot_secs = int(max(0, self._total_frames) // max(1, self._framerate))
        self._time_lbl.setText(f"{format_duration(cur_secs)} / {format_duration(tot_secs)}")

    def _open_in_folder(self) -> None:
        path = self._path
        if path is None or not path.exists():
            return
        if sys.platform == "win32":
            try:
                subprocess.Popen(["explorer", "/select,", str(path)])
                return
            except Exception:
                pass
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent)))


class FlowHubWindow(QMainWindow):
    """WhisperLM desktop Hub window: Home, Meetings, Dictionary, Settings."""

    history_added = Signal(object)
    history_loaded = Signal(list)
    meetings_updated = Signal(list)
    meeting_state_changed = Signal(str, str)  # ("recording" | "cleaning" | "summarizing" | "idle", app_name)
    runtime_status_changed = Signal(str, int)  # (status_message, progress_percent; -1 hides setup bar)
    open_page_requested = Signal(str)
    update_available = Signal(object, bool)  # (UpdateInfo, prompt_user)
    cloud_status_changed = Signal(str)
    quit_requested = Signal()

    def __init__(
        self,
        config: AppConfig,
        on_save_config: Callable[[AppConfig], None],
        on_copy_text: Callable[[str], None],
        on_start_meeting: Callable[[], None] | None = None,
        on_stop_meeting: Callable[[], None] | None = None,
        on_delete_meeting: Callable[[str], None] | None = None,
        on_toggle_meeting_auto: Callable[[bool], None] | None = None,
        on_summarize_meeting: Callable[[str], None] | None = None,
        on_clear_history: Callable[[], None] | None = None,
        on_check_updates: Callable[[], None] | None = None,
        on_install_update: Callable[[Any], None] | None = None,
        on_skip_update: Callable[[str], None] | None = None,
        meetings_dir: Path | None = None,
    ) -> None:
        super().__init__()
        self.config = config
        self._meetings_dir = meetings_dir
        self._on_save_config = on_save_config
        self._on_copy_text = on_copy_text
        self._on_start_meeting = on_start_meeting or (lambda: None)
        self._on_stop_meeting = on_stop_meeting or (lambda: None)
        self._on_delete_meeting = on_delete_meeting or (lambda _mid: None)
        self._on_toggle_meeting_auto = on_toggle_meeting_auto or (lambda _b: None)
        self._on_summarize_meeting = on_summarize_meeting or (lambda _mid: None)
        self._on_clear_history = on_clear_history or (lambda: None)
        self._on_check_updates = on_check_updates or (lambda: None)
        self._on_install_update = on_install_update or (lambda _u: None)
        self._on_skip_update = on_skip_update or (lambda _v: None)

        self._history: list[HistoryEntry] = []
        self._history_time_filter = "all"
        self._history_query = ""
        self._history_render_limit = 150
        self._meetings: list[MeetingRecord] = []
        self._selected_meeting_id: str | None = None
        self._active_live_meeting_id: str | None = None
        self._meeting_query = ""
        self._meeting_view_mode = "cleaned"  # "summary" | "cleaned" | "raw"
        self._meeting_view_explicit: str | None = None
        self._is_meeting_recording = False
        self._pending_update: UpdateInfo | None = None
        self._update_prompt_shown = False

        self._test_recorder: AudioRecorder | None = None
        self._test_timer: QTimer | None = None

        self.setWindowTitle("WhisperLM")
        self.setWindowIcon(_build_tray_icon(_TEXT_PRIMARY))
        self.resize(960, 650)
        self.setMinimumSize(820, 560)
        self._apply_stylesheet()
        self._build_ui()

        self.history_added.connect(self._append_history)
        self.history_loaded.connect(self._set_history)
        self.meetings_updated.connect(self._set_meetings)
        self.meeting_state_changed.connect(self._set_meeting_status)
        self.runtime_status_changed.connect(self._set_runtime_status)
        self.open_page_requested.connect(self.open_page)
        self.update_available.connect(self._on_update_available)
        self.cloud_status_changed.connect(self._set_cloud_status)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        _apply_dark_titlebar(self)

    def stop_audio_playback(self) -> None:
        if getattr(self, "_audio_bar", None) is not None:
            self._audio_bar.unload()

    def closeEvent(self, event) -> None:
        self.stop_audio_playback()
        super().closeEvent(event)

    def _apply_stylesheet(self) -> None:
        self.setStyleSheet(
            f"""
            QMainWindow, QWidget#CentralRoot {{
                background-color: {_BG_ROOT};
                color: {_TEXT_PRIMARY};
                font-family: "Segoe UI Variable Text", "Segoe UI", sans-serif;
            }}
            QWidget#Sidebar {{
                background-color: {_BG_SIDEBAR};
                border-right: 1px solid {_BORDER_SUBTLE};
            }}
            QPushButton#NavBtn {{
                background-color: transparent;
                color: {_TEXT_SECONDARY};
                border: 1px solid transparent;
                border-radius: 8px;
                padding: 9px 12px;
                text-align: left;
                font-size: 13px;
                font-weight: 500;
            }}
            QPushButton#NavBtn:hover {{
                background-color: #101016;
                color: {_TEXT_PRIMARY};
            }}
            QPushButton#NavBtn[active="true"] {{
                background-color: #12121E;
                border-color: rgba(123, 139, 255, 0.38);
                color: {_TEXT_PRIMARY};
                font-weight: 600;
            }}
            QLabel#PageTitle {{
                font-size: 22px;
                font-weight: 600;
                color: {_TEXT_PRIMARY};
            }}
            QLabel#PageSubtitle {{
                font-size: 13px;
                color: {_TEXT_SECONDARY};
            }}
            QLabel#SectionTitle {{
                font-size: 13px;
                font-weight: 600;
                color: {_TEXT_SECONDARY};
                margin-top: 4px;
            }}
            QFrame#Card, QFrame#MetricTile {{
                background-color: {_BG_CARD};
                border: 1px solid {_BORDER};
                border-radius: 12px;
            }}
            QLabel#MetricValue {{
                font-size: 20px;
                font-weight: 600;
                color: {_TEXT_PRIMARY};
            }}
            QLabel#MetricLabel {{
                font-size: 12px;
                color: {_TEXT_SECONDARY};
            }}
            QLabel#MutedMeta {{
                font-size: 11.5px;
                color: {_TEXT_MUTED};
            }}
            QLabel#BodyText {{
                font-size: 14px;
                line-height: 1.45;
                color: {_TEXT_PRIMARY};
            }}
            QLabel#RawFootnote {{
                font-size: 11.5px;
                color: {_TEXT_MUTED};
                padding-top: 2px;
            }}
            QLabel#SettingTitle {{
                font-size: 13.5px;
                font-weight: 500;
                color: {_TEXT_PRIMARY};
            }}
            QLabel#SettingDesc {{
                font-size: 12px;
                color: {_TEXT_SECONDARY};
            }}
            QPushButton {{
                outline: none;
            }}
            QPushButton#PrimaryPillBtn {{
                background-color: {_TEXT_PRIMARY};
                color: {_BG_ROOT};
                border: none;
                border-radius: 16px;
                padding: 8px 16px;
                font-size: 12.5px;
                font-weight: 600;
            }}
            QPushButton#PrimaryPillBtn:hover {{
                background-color: #DFDFE8;
            }}
            QPushButton#DangerPillBtn {{
                background-color: #1D0D11;
                color: #FCA5A5;
                border: 1px solid #4E1D27;
                border-radius: 16px;
                padding: 8px 16px;
                font-size: 12.5px;
                font-weight: 600;
            }}
            QPushButton#DangerPillBtn:hover {{
                background-color: #2C1217;
                border-color: #7A2B3A;
            }}
            QPushButton#GhostPillBtn {{
                background-color: #0F0F15;
                color: {_TEXT_SECONDARY};
                border: 1px solid #1C1C26;
                border-radius: 12px;
                padding: 4px 11px;
                font-size: 11.5px;
                font-weight: 500;
            }}
            QPushButton#GhostPillBtn:hover {{
                background-color: #161621;
                border-color: rgba(123, 139, 255, 0.42);
                color: {_TEXT_PRIMARY};
            }}
            QPushButton#DiscreteLinkBtn {{
                background-color: transparent;
                color: {_TEXT_MUTED};
                border: none;
                padding: 4px 2px;
                text-align: left;
                font-size: 12px;
                font-weight: 500;
            }}
            QPushButton#DiscreteLinkBtn:hover {{
                color: {_TEXT_SECONDARY};
            }}
            QPushButton#SegBtn {{
                background-color: transparent;
                color: {_TEXT_SECONDARY};
                border: 1px solid transparent;
                border-radius: 10px;
                padding: 4px 10px;
                font-size: 11.5px;
                font-weight: 500;
            }}
            QPushButton#SegBtn[active="true"] {{
                background-color: #14141F;
                color: {_TEXT_PRIMARY};
                border-color: rgba(123, 139, 255, 0.35);
            }}
            QComboBox, QPlainTextEdit, QLineEdit {{
                background-color: {_BG_INPUT};
                color: {_TEXT_PRIMARY};
                border: 1px solid #1B1B25;
                border-radius: 8px;
                padding: 6px 10px;
                font-size: 12.5px;
            }}
            QLineEdit:focus, QPlainTextEdit:focus {{
                border-color: {_ACCENT_INDIGO};
            }}
            QComboBox:hover {{
                border-color: rgba(123, 139, 255, 0.55);
            }}
            QComboBox QAbstractItemView {{
                background-color: {_BG_CARD};
                color: {_TEXT_PRIMARY};
                selection-background-color: #171724;
                border: 1px solid {_BORDER};
            }}
            QListWidget#MeetingList {{
                background-color: {_BG_CARD};
                border: 1px solid {_BORDER};
                border-radius: 12px;
                padding: 6px;
                outline: none;
            }}
            QListWidget#MeetingList::item {{
                border-radius: 8px;
                padding: 10px 12px;
                margin: 2px 0px;
                color: {_TEXT_PRIMARY};
            }}
            QListWidget#MeetingList::item:selected {{
                background-color: #141420;
                border: 1px solid rgba(123, 139, 255, 0.32);
            }}
            QListWidget#MeetingList::item:hover:!selected {{
                background-color: #101017;
            }}
            QMessageBox, QDialog {{
                background-color: {_BG_CARD};
                color: {_TEXT_PRIMARY};
            }}
            QMessageBox QLabel {{
                color: {_TEXT_PRIMARY};
                font-size: 13px;
            }}
            QMessageBox QPushButton {{
                background-color: #12121A;
                color: {_TEXT_PRIMARY};
                border: 1px solid #222230;
                border-radius: 10px;
                padding: 6px 14px;
                min-width: 74px;
                font-size: 12.5px;
            }}
            QMessageBox QPushButton:hover {{
                background-color: #1A1A26;
                border-color: rgba(123, 139, 255, 0.48);
            }}
            QToolTip {{
                background-color: #0D0D14;
                color: {_TEXT_PRIMARY};
                border: 1px solid #232333;
                padding: 4px 8px;
                border-radius: 6px;
            }}
            QScrollArea {{
                border: none;
                background: transparent;
            }}
            QScrollBar:vertical {{
                background: transparent;
                width: 6px;
                margin: 4px 0 4px 0;
            }}
            QScrollBar::handle:vertical {{
                background: #1B1B24;
                border-radius: 3px;
                min-height: 24px;
            }}
            QScrollBar::handle:vertical:hover {{
                background: #2C2C3A;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
                background: transparent;
                height: 0px;
            }}
            QProgressBar {{
                background-color: #0F0F16;
                border: none;
                border-radius: 3px;
                max-height: 6px;
            }}
            QProgressBar::chunk {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {_ACCENT_INDIGO_DEEP}, stop:1 {_ACCENT_INDIGO});
                border-radius: 3px;
            }}
            QFrame#AudioBar {{
                background-color: {_BG_INPUT};
                border: 1px solid {_BORDER};
                border-radius: 10px;
            }}
            QSlider#AudioSlider::groove:horizontal {{
                background-color: #171722;
                height: 4px;
                border-radius: 2px;
            }}
            QSlider#AudioSlider::sub-page:horizontal {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {_ACCENT_INDIGO_DEEP}, stop:1 {_ACCENT_INDIGO});
                height: 4px;
                border-radius: 2px;
            }}
            QSlider#AudioSlider::handle:horizontal {{
                background-color: {_TEXT_PRIMARY};
                border: 2px solid {_ACCENT_INDIGO};
                width: 12px;
                height: 12px;
                margin: -5px 0px;
                border-radius: 7px;
            }}
            QSlider#AudioSlider::handle:horizontal:hover {{
                background-color: #FFFFFF;
                border-color: #A2AEFF;
            }}
            """
        )

    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("CentralRoot")
        self.setCentralWidget(root)

        shell = QHBoxLayout(root)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)

        shell.addWidget(self._build_sidebar())

        self._stack = QStackedWidget()
        self._stack.addWidget(self._build_home_page())
        self._stack.addWidget(self._build_meetings_page())
        self._stack.addWidget(self._build_dictionary_page())
        self._stack.addWidget(self._build_settings_page())
        shell.addWidget(self._stack, 1)

        self._switch_page(0)
        self._page_ready = True
        self.sync_from_config(self.config)

    def _build_sidebar(self) -> QWidget:
        sidebar = QWidget()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(216)

        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(14, 18, 14, 16)
        layout.setSpacing(6)

        brand_row = QHBoxLayout()
        brand_row.setContentsMargins(6, 4, 6, 14)
        brand_row.setSpacing(10)
        self._brand_mark = _BrandMark(20)
        brand_row.addWidget(self._brand_mark)

        title = QLabel("WhisperLM")
        title.setFont(QFont("Segoe UI", 11.5, QFont.Weight.DemiBold))
        title.setStyleSheet(f"color: {_TEXT_PRIMARY};")
        brand_row.addWidget(title)
        brand_row.addStretch(1)
        layout.addLayout(brand_row)

        self._nav_buttons: list[QPushButton] = []
        for idx, label in enumerate(("Home", "Meetings", "Dictionary", "Settings")):
            btn = QPushButton(f"  {label}")
            btn.setObjectName("NavBtn")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _c=False, i=idx: self._switch_page(i))
            self._nav_buttons.append(btn)
            layout.addWidget(btn)

        layout.addStretch(1)

        footer_card = QFrame()
        footer_card.setObjectName("Card")
        fl = QVBoxLayout(footer_card)
        fl.setContentsMargins(12, 10, 12, 10)
        fl.setSpacing(4)

        self._sidebar_hotkey_lbl = QLabel()
        self._sidebar_hotkey_lbl.setStyleSheet(f"color: {_TEXT_PRIMARY}; font-size: 12px; font-weight: 600;")
        self._sidebar_status_lbl = QLabel("Ready")
        self._sidebar_status_lbl.setStyleSheet(f"color: {_TEXT_MUTED}; font-size: 11px;")
        fl.addWidget(self._sidebar_hotkey_lbl)
        fl.addWidget(self._sidebar_status_lbl)
        layout.addWidget(footer_card)

        return sidebar

    def _switch_page(self, index: int) -> None:
        changed = self._stack.currentIndex() != index
        self._stack.setCurrentIndex(index)
        for idx, btn in enumerate(self._nav_buttons):
            btn.setProperty("active", "true" if idx == index else "false")
            btn.style().unpolish(btn)
            btn.style().polish(btn)
        if getattr(self, "_page_ready", False) and changed and self.isVisible():
            target = self._stack.currentWidget()
            if target is not None:
                _fade_in_widget(target, duration=155)

    def open_page(self, page: str) -> None:
        mapping = {"home": 0, "meetings": 1, "dictionary": 2, "settings": 3}
        self._switch_page(mapping.get(page, 0))
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _build_home_page(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(28, 24, 28, 22)
        outer.setSpacing(16)

        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title_box.setSpacing(3)
        title = QLabel("Home")
        title.setObjectName("PageTitle")
        self._home_sub = QLabel()
        self._home_sub.setObjectName("PageSubtitle")
        title_box.addWidget(title)
        title_box.addWidget(self._home_sub)
        header.addLayout(title_box)
        header.addStretch(1)
        outer.addLayout(header)

        # Runtime setup / download status banner (hidden once Ready)
        self._setup_banner = QFrame()
        self._setup_banner.setObjectName("Card")
        self._setup_banner.setStyleSheet(
            f"QFrame#Card {{ background-color: #0B0B14; border: 1px solid rgba(123, 139, 255, 0.36); border-radius: 12px; }}"
        )
        sb = QVBoxLayout(self._setup_banner)
        sb.setContentsMargins(16, 12, 16, 12)
        sb.setSpacing(8)
        sb_top = QHBoxLayout()
        sb_dot = QLabel("✦")
        sb_dot.setStyleSheet(f"color: {_ACCENT_INDIGO}; font-size: 13px;")
        self._setup_status_lbl = QLabel("Preparing local AI models...")
        self._setup_status_lbl.setStyleSheet(f"color: {_TEXT_PRIMARY}; font-size: 12.5px; font-weight: 500;")
        self._setup_pct_lbl = QLabel("")
        self._setup_pct_lbl.setStyleSheet(f"color: {_ACCENT_INDIGO}; font-size: 12px; font-weight: 600;")
        sb_top.addWidget(sb_dot)
        sb_top.addWidget(self._setup_status_lbl, 1)
        sb_top.addWidget(self._setup_pct_lbl)
        self._setup_progress = QProgressBar()
        self._setup_progress.setTextVisible(False)
        self._setup_progress.setRange(0, 100)
        self._setup_progress.setValue(0)
        sb.addLayout(sb_top)
        sb.addWidget(self._setup_progress)
        self._setup_banner.hide()
        outer.addWidget(self._setup_banner)

        # In-app update available card (hidden unless a newer release is found)
        self._update_banner = QFrame()
        self._update_banner.setObjectName("Card")
        self._update_banner.setStyleSheet(
            f"QFrame#Card {{ background-color: #0B0B14; border: 1px solid rgba(123, 139, 255, 0.42); border-radius: 12px; }}"
        )
        ub = QHBoxLayout(self._update_banner)
        ub.setContentsMargins(16, 12, 16, 12)
        ub.setSpacing(10)
        ub_dot = QLabel("✦")
        ub_dot.setStyleSheet(f"color: {_ACCENT_INDIGO}; font-size: 13px;")
        self._update_banner_lbl = QLabel("A newer version of WhisperLM is available.")
        self._update_banner_lbl.setStyleSheet(f"color: {_TEXT_PRIMARY}; font-size: 12.5px; font-weight: 500;")
        self._update_now_btn = QPushButton("Update Now")
        self._update_now_btn.setObjectName("PrimaryPillBtn")
        self._update_now_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._update_now_btn.clicked.connect(self._trigger_pending_update)
        later_btn = QPushButton("Later")
        later_btn.setObjectName("GhostPillBtn")
        later_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        later_btn.clicked.connect(self._update_banner.hide)
        ub.addWidget(ub_dot)
        ub.addWidget(self._update_banner_lbl, 1)
        ub.addWidget(later_btn)
        ub.addWidget(self._update_now_btn)
        self._update_banner.hide()
        outer.addWidget(self._update_banner)

        metrics = QHBoxLayout()
        metrics.setSpacing(12)
        self._tile_words = _MetricTile("Words dictated", "0")
        self._tile_sessions = _MetricTile("Dictations", "0")
        self._tile_days = _MetricTile("Active days", "0")
        for tile in (self._tile_words, self._tile_sessions, self._tile_days):
            tile.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            metrics.addWidget(tile)
        outer.addLayout(metrics)

        filter_row = QHBoxLayout()
        filter_row.setSpacing(8)
        self._history_search = QLineEdit()
        self._history_search.setPlaceholderText("Search dictations by text, app, or date...")
        self._history_search.setClearButtonEnabled(True)
        self._history_search.textChanged.connect(self._on_history_search)
        filter_row.addWidget(self._history_search, 1)

        self._history_filter_btns: dict[str, QPushButton] = {}
        for key, label in (
            ("all", "All"),
            ("today", "Today"),
            ("week", "7d"),
            ("month", "30d"),
            ("older", "Older"),
        ):
            btn = QPushButton(label)
            btn.setObjectName("SegBtn")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _c=False, k=key: self._set_history_time_filter(k))
            self._history_filter_btns[key] = btn
            filter_row.addWidget(btn)
        outer.addLayout(filter_row)

        self._history_section_lbl = QLabel("History")
        self._history_section_lbl.setObjectName("SectionTitle")
        outer.addWidget(self._history_section_lbl)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll_inner = QWidget()
        scroll_inner.setStyleSheet("background: transparent;")
        self._history_layout = QVBoxLayout(scroll_inner)
        self._history_layout.setContentsMargins(0, 0, 6, 0)
        self._history_layout.setSpacing(10)

        self._home_empty = QFrame()
        self._home_empty.setObjectName("Card")
        el = QVBoxLayout(self._home_empty)
        el.setContentsMargins(20, 22, 20, 22)
        el.setSpacing(6)
        self._home_empty_title = QLabel("Ready when you are")
        self._home_empty_title.setFont(QFont("Segoe UI", 11, QFont.Weight.DemiBold))
        self._home_empty_body = QLabel(
            "Click any text field and hold your shortcut to start dictating."
        )
        self._home_empty_body.setObjectName("PageSubtitle")
        self._home_empty_body.setWordWrap(True)
        el.addWidget(self._home_empty_title)
        el.addWidget(self._home_empty_body)

        self._history_layout.addWidget(self._home_empty)
        self._history_layout.addStretch(1)
        scroll.setWidget(scroll_inner)
        outer.addWidget(scroll, 1)
        self._refresh_history_filter_buttons()
        return page

    def _build_meetings_page(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(28, 24, 28, 22)
        outer.setSpacing(14)

        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title_box.setSpacing(3)
        title = QLabel("Meetings")
        title.setObjectName("PageTitle")
        sub = QLabel("Record calls and browse full meeting transcripts")
        sub.setObjectName("PageSubtitle")
        title_box.addWidget(title)
        title_box.addWidget(sub)
        header.addLayout(title_box)
        header.addStretch(1)

        auto_lbl = QLabel("Auto-detect")
        auto_lbl.setStyleSheet(f"color: {_TEXT_SECONDARY}; font-size: 12.5px; font-weight: 500;")
        self._meeting_auto_header_switch = _ToggleSwitch(self.config.meeting_auto_detect)
        self._meeting_auto_header_switch.toggled.connect(self._on_header_auto_toggle)

        self._meeting_action_btn = QPushButton("Record Meeting")
        self._meeting_action_btn.setObjectName("PrimaryPillBtn")
        self._meeting_action_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._meeting_action_btn.clicked.connect(self._toggle_manual_meeting)

        header.addWidget(auto_lbl)
        header.addWidget(self._meeting_auto_header_switch)
        header.addSpacing(8)
        header.addWidget(self._meeting_action_btn)
        outer.addLayout(header)

        # Live status banner (hidden when idle)
        self._meeting_banner = QFrame()
        self._meeting_banner.setObjectName("Card")
        bl = QHBoxLayout(self._meeting_banner)
        bl.setContentsMargins(14, 10, 14, 10)
        self._banner_dot = QLabel("●")
        self._banner_dot.setStyleSheet(f"color: {_ACCENT_RED}; font-size: 12px;")
        self._banner_text = QLabel("Recording meeting...")
        self._banner_text.setStyleSheet(f"color: {_TEXT_PRIMARY}; font-size: 13px; font-weight: 500;")
        bl.addWidget(self._banner_dot)
        bl.addWidget(self._banner_text)
        bl.addStretch(1)
        self._meeting_banner.hide()
        outer.addWidget(self._meeting_banner)

        # Master-detail split
        split = QHBoxLayout()
        split.setSpacing(14)

        left_col = QVBoxLayout()
        left_col.setSpacing(8)
        self._meeting_search = QLineEdit()
        self._meeting_search.setPlaceholderText("Search meetings...")
        self._meeting_search.setClearButtonEnabled(True)
        self._meeting_search.setFixedWidth(224)
        self._meeting_search.textChanged.connect(self._on_meeting_search)
        left_col.addWidget(self._meeting_search)

        self._meeting_list = QListWidget()
        self._meeting_list.setObjectName("MeetingList")
        self._meeting_list.setFixedWidth(224)
        self._meeting_list.currentItemChanged.connect(self._on_meeting_selected)
        left_col.addWidget(self._meeting_list, 1)
        split.addLayout(left_col)

        detail_card = QFrame()
        detail_card.setObjectName("Card")
        dl = QVBoxLayout(detail_card)
        dl.setContentsMargins(18, 16, 18, 16)
        dl.setSpacing(8)

        self._detail_title = QLabel("No meeting selected")
        self._detail_title.setFont(QFont("Segoe UI", 13, QFont.Weight.DemiBold))
        self._detail_title.setWordWrap(True)
        dl.addWidget(self._detail_title)

        self._detail_meta = QLabel("Meetings are recorded and transcribed automatically.")
        self._detail_meta.setObjectName("MutedMeta")
        dl.addWidget(self._detail_meta)

        detail_bar = QHBoxLayout()
        detail_bar.setSpacing(6)

        self._btn_view_summary = QPushButton("Summary")
        self._btn_view_summary.setObjectName("SegBtn")
        self._btn_view_summary.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_view_summary.clicked.connect(lambda: self._switch_meeting_view("summary"))

        self._btn_view_cleaned = QPushButton("Cleaned")
        self._btn_view_cleaned.setObjectName("SegBtn")
        self._btn_view_cleaned.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_view_cleaned.clicked.connect(lambda: self._switch_meeting_view("cleaned"))

        self._btn_view_raw = QPushButton("Original")
        self._btn_view_raw.setObjectName("SegBtn")
        self._btn_view_raw.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_view_raw.clicked.connect(lambda: self._switch_meeting_view("raw"))

        self._btn_summarize_meeting = QPushButton("Summarize")
        self._btn_summarize_meeting.setObjectName("GhostPillBtn")
        self._btn_summarize_meeting.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_summarize_meeting.clicked.connect(self._summarize_selected_meeting)
        self._btn_summarize_meeting.setEnabled(False)

        self._btn_copy_meeting = QPushButton("Copy")
        self._btn_copy_meeting.setObjectName("GhostPillBtn")
        self._btn_copy_meeting.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_copy_meeting.clicked.connect(self._copy_selected_meeting)

        self._btn_delete_meeting = QPushButton("Delete")
        self._btn_delete_meeting.setObjectName("GhostPillBtn")
        self._btn_delete_meeting.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_delete_meeting.clicked.connect(self._delete_selected_meeting)

        detail_bar.addWidget(self._btn_view_summary)
        detail_bar.addWidget(self._btn_view_cleaned)
        detail_bar.addWidget(self._btn_view_raw)
        detail_bar.addStretch(1)
        detail_bar.addWidget(self._btn_summarize_meeting)
        detail_bar.addWidget(self._btn_copy_meeting)
        detail_bar.addWidget(self._btn_delete_meeting)
        dl.addLayout(detail_bar)
        dl.addWidget(self._divider())

        self._audio_bar = _MeetingAudioBar()
        dl.addWidget(self._audio_bar)

        self._detail_body = QPlainTextEdit()
        self._detail_body.setReadOnly(True)
        self._detail_body.setStyleSheet(
            f"QPlainTextEdit {{ background-color: transparent; border: none; padding: 4px 0px; "
            f"color: {_TEXT_PRIMARY}; font-size: 13.5px; }}"
        )
        self._detail_body.setPlaceholderText("Meeting transcript will appear here as you talk...")
        dl.addWidget(self._detail_body, 1)

        split.addWidget(detail_card, 1)
        outer.addLayout(split, 1)
        self._refresh_view_toggle_buttons()
        return page

    def _setting_row(self, title: str, subtitle: str, control: QWidget) -> QWidget:
        row = QWidget()
        rl = QHBoxLayout(row)
        rl.setContentsMargins(16, 12, 16, 12)
        rl.setSpacing(14)

        txt = QVBoxLayout()
        txt.setSpacing(2)
        tl = QLabel(title)
        tl.setObjectName("SettingTitle")
        sl = QLabel(subtitle)
        sl.setObjectName("SettingDesc")
        sl.setWordWrap(True)
        txt.addWidget(tl)
        txt.addWidget(sl)
        rl.addLayout(txt, 1)
        rl.addWidget(control, 0, Qt.AlignmentFlag.AlignVCenter)
        return row

    def _divider(self) -> QFrame:
        return _hairline_divider()

    def _build_dictionary_page(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(28, 24, 28, 22)
        outer.setSpacing(14)

        title_box = QVBoxLayout()
        title_box.setSpacing(3)
        title = QLabel("Dictionary")
        title.setObjectName("PageTitle")
        sub = QLabel("Add names, terms, and phrases so WhisperLM always spells them your way")
        sub.setObjectName("PageSubtitle")
        title_box.addWidget(title)
        title_box.addWidget(sub)
        outer.addLayout(title_box)

        add_row = QHBoxLayout()
        add_row.setSpacing(8)
        self._dict_input = QLineEdit()
        self._dict_input.setPlaceholderText("Add a word, name, or phrase...")
        self._dict_input.returnPressed.connect(self._add_dictionary_word)
        self._dict_add_btn = QPushButton("Add word")
        self._dict_add_btn.setObjectName("PrimaryPillBtn")
        self._dict_add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._dict_add_btn.clicked.connect(self._add_dictionary_word)
        add_row.addWidget(self._dict_input, 1)
        add_row.addWidget(self._dict_add_btn)
        outer.addLayout(add_row)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        inner.setStyleSheet("background: transparent;")
        wrap = QVBoxLayout(inner)
        wrap.setContentsMargins(0, 0, 6, 8)
        wrap.setSpacing(10)

        self._dict_card = QFrame()
        self._dict_card.setObjectName("Card")
        self._dict_list_layout = QVBoxLayout(self._dict_card)
        self._dict_list_layout.setContentsMargins(0, 0, 0, 0)
        self._dict_list_layout.setSpacing(0)
        wrap.addWidget(self._dict_card)
        wrap.addStretch(1)
        scroll.setWidget(inner)
        outer.addWidget(scroll, 1)
        return page

    def _build_settings_page(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(28, 24, 28, 22)
        outer.setSpacing(14)

        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title_box.setSpacing(3)
        title = QLabel("Settings")
        title.setObjectName("PageTitle")
        sub = QLabel("Shortcuts, microphone, behavior, and meetings")
        sub.setObjectName("PageSubtitle")
        title_box.addWidget(title)
        title_box.addWidget(sub)
        header.addLayout(title_box)
        header.addStretch(1)

        self._save_btn = QPushButton("Save Changes")
        self._save_btn.setObjectName("PrimaryPillBtn")
        self._save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._save_btn.clicked.connect(self._save_settings)
        header.addWidget(self._save_btn)
        outer.addLayout(header)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        inner.setStyleSheet("background: transparent;")
        cl = QVBoxLayout(inner)
        cl.setContentsMargins(0, 0, 8, 8)
        cl.setSpacing(14)

        # 1. Shortcuts
        sec1 = QLabel("Shortcuts")
        sec1.setObjectName("SectionTitle")
        cl.addWidget(sec1)

        card1 = QFrame()
        card1.setObjectName("Card")
        c1 = QVBoxLayout(card1)
        c1.setContentsMargins(0, 0, 0, 0)
        c1.setSpacing(0)

        self._hold_combo = QComboBox()
        self._hold_combo.setFixedWidth(252)
        self._hold_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        for key, label in HOLD_HOTKEY_PRESETS:
            self._hold_combo.addItem(label, key)
        self._toggle_combo = QComboBox()
        self._toggle_combo.setFixedWidth(252)
        self._toggle_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        for key, label in TOGGLE_HOTKEY_PRESETS:
            self._toggle_combo.addItem(label, key)

        self._always_on_switch = _ToggleSwitch(self.config.always_on_pill)
        self._startup_switch = _ToggleSwitch(self.config.launch_at_startup)

        c1.addWidget(self._setting_row("Hold to Dictate", "Hold to talk, or double-tap to stay hands-free", self._hold_combo))
        c1.addWidget(self._divider())
        c1.addWidget(self._setting_row("Hands-Free Shortcut", "Press once to start listening, press again to stop", self._toggle_combo))
        c1.addWidget(self._divider())
        c1.addWidget(self._setting_row("Floating Flow Bar", "Keep a compact Flow bar at the bottom of your screen", self._always_on_switch))
        c1.addWidget(self._divider())
        c1.addWidget(self._setting_row("Launch at Startup", "Start WhisperLM automatically when you sign in to Windows", self._startup_switch))
        cl.addWidget(card1)

        # 2. Audio
        sec2 = QLabel("Audio")
        sec2.setObjectName("SectionTitle")
        cl.addWidget(sec2)

        card2 = QFrame()
        card2.setObjectName("Card")
        c2 = QVBoxLayout(card2)
        c2.setContentsMargins(0, 0, 0, 0)
        c2.setSpacing(0)

        self._mic_combo = QComboBox()
        self._mic_combo.setFixedWidth(252)
        self._mic_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self._mic_combo.addItem("System Default Microphone", None)
        for dev in list_input_devices():
            self._mic_combo.addItem(dev.name, dev.index)

        mic_test_wrap = QWidget()
        mt = QHBoxLayout(mic_test_wrap)
        mt.setContentsMargins(0, 0, 0, 0)
        mt.setSpacing(8)
        self._mic_test_btn = QPushButton("Test Mic")
        self._mic_test_btn.setObjectName("GhostPillBtn")
        self._mic_test_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._mic_test_btn.clicked.connect(self._toggle_mic_test)
        self._mic_meter = QProgressBar()
        self._mic_meter.setRange(0, 100)
        self._mic_meter.setTextVisible(False)
        self._mic_meter.setFixedWidth(120)
        mt.addWidget(self._mic_meter)
        mt.addWidget(self._mic_test_btn)

        self._vad_switch = _ToggleSwitch(self.config.vad_enabled)
        self._sound_switch = _ToggleSwitch(self.config.activation_sound_enabled)

        c2.addWidget(self._setting_row("Microphone", "Choose the microphone used for dictation and meetings", self._mic_combo))
        c2.addWidget(self._divider())
        c2.addWidget(self._setting_row("Mic Check", "Test your microphone input level", mic_test_wrap))
        c2.addWidget(self._divider())
        c2.addWidget(self._setting_row("Silence Filter", "Skip background pauses before transcribing", self._vad_switch))
        c2.addWidget(self._divider())
        c2.addWidget(self._setting_row("Sound Effects", "Play a gentle pop sound when dictation starts", self._sound_switch))
        cl.addWidget(card2)

        # 3. Dictation
        sec3 = QLabel("Dictation")
        sec3.setObjectName("SectionTitle")
        cl.addWidget(sec3)

        card3 = QFrame()
        card3.setObjectName("Card")
        c3 = QVBoxLayout(card3)
        c3.setContentsMargins(0, 0, 0, 0)
        c3.setSpacing(0)

        self._arabic_switch = _ToggleSwitch(self.config.arabic_enabled)
        self._cleanup_switch = _ToggleSwitch(self.config.llm_cleanup_enabled)
        self._restore_clip_switch = _ToggleSwitch(self.config.restore_clipboard)

        c3.addWidget(self._setting_row("Arabic + English", "Detect spoken Arabic alongside English (keep off for pure English)", self._arabic_switch))
        c3.addWidget(self._divider())
        c3.addWidget(self._setting_row("Smart Formatting", "Automatically remove filler words and clean punctuation", self._cleanup_switch))
        c3.addWidget(self._divider())
        c3.addWidget(self._setting_row("Restore Clipboard", "Put previous clipboard content back after pasting", self._restore_clip_switch))
        cl.addWidget(card3)

        # 4. Meetings
        sec4 = QLabel("Meetings")
        sec4.setObjectName("SectionTitle")
        cl.addWidget(sec4)

        card4 = QFrame()
        card4.setObjectName("Card")
        c4 = QVBoxLayout(card4)
        c4.setContentsMargins(0, 0, 0, 0)
        c4.setSpacing(0)

        self._meeting_auto_switch = _ToggleSwitch(self.config.meeting_auto_detect)
        self._meeting_sys_audio_switch = _ToggleSwitch(self.config.meeting_capture_system_audio)
        self._meeting_save_audio_switch = _ToggleSwitch(self.config.meeting_save_audio)
        self._meeting_cleanup_switch = _ToggleSwitch(self.config.meeting_cleanup_enabled)
        self._meeting_summary_switch = _ToggleSwitch(self.config.meeting_summary_enabled)

        c4.addWidget(self._setting_row("Auto-Detect Meetings", "Automatically record Meet, Zoom, Teams, Discord, and WhatsApp calls", self._meeting_auto_switch))
        c4.addWidget(self._divider())
        c4.addWidget(self._setting_row("Record Meeting Audio", "Capture both what others say and your microphone", self._meeting_sys_audio_switch))
        c4.addWidget(self._divider())
        c4.addWidget(self._setting_row("Save Meeting Audio", "Record full meeting audio locally as a WAV reference file", self._meeting_save_audio_switch))
        c4.addWidget(self._divider())
        c4.addWidget(self._setting_row("Polish Transcripts", "Clean up wording and punctuation after a meeting ends", self._meeting_cleanup_switch))
        c4.addWidget(self._divider())
        c4.addWidget(self._setting_row("Meeting Summaries", "Summarize key points, decisions, and action items after each meeting", self._meeting_summary_switch))
        cl.addWidget(card4)

        # 5. Backup & Updates
        sec5 = QLabel("Backup & Updates")
        sec5.setObjectName("SectionTitle")
        cl.addWidget(sec5)

        card5 = QFrame()
        card5.setObjectName("Card")
        c5 = QVBoxLayout(card5)
        c5.setContentsMargins(0, 0, 0, 0)
        c5.setSpacing(0)

        self._cloud_combo = QComboBox()
        self._cloud_combo.setFixedWidth(252)
        self._cloud_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self._populate_cloud_combo()
        self._cloud_combo.currentIndexChanged.connect(self._on_cloud_selection_changed)

        self._cloud_row = self._setting_row(
            "Cloud Backup",
            "Automatically backs up dictations, meetings, and dictionary cleanly across installs",
            self._cloud_combo,
        )
        self._cloud_desc_lbl = self._cloud_row.findChild(QLabel, "SettingDesc")
        c5.addWidget(self._cloud_row)

        # Conditional 1-line destination / token inputs (hidden unless Custom Folder / Firebase / GitHub)
        folder_wrap = QWidget()
        fw = QHBoxLayout(folder_wrap)
        fw.setContentsMargins(0, 0, 0, 0)
        fw.setSpacing(8)
        self._cloud_folder_input = QLineEdit(self.config.cloud_backup_folder)
        self._cloud_folder_input.setPlaceholderText("Select any synced folder...")
        self._cloud_folder_input.setFixedWidth(210)
        browse_btn = QPushButton("Browse")
        browse_btn.setObjectName("GhostPillBtn")
        browse_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        browse_btn.clicked.connect(self._browse_cloud_folder)
        fw.addWidget(self._cloud_folder_input)
        fw.addWidget(browse_btn)
        self._cloud_folder_row = self._setting_row("Backup Folder", "Any folder synced by Google Drive, OneDrive, or Dropbox", folder_wrap)
        c5.addWidget(self._cloud_folder_row)

        fb_wrap = QWidget()
        fbw = QVBoxLayout(fb_wrap)
        fbw.setContentsMargins(0, 0, 0, 0)
        fbw.setSpacing(6)
        self._firebase_url_input = QLineEdit(self.config.cloud_firebase_url)
        self._firebase_url_input.setPlaceholderText("https://your-project-default-rtdb.firebaseio.com")
        self._firebase_url_input.setFixedWidth(260)
        self._firebase_token_input = QLineEdit(self.config.cloud_firebase_token)
        self._firebase_token_input.setPlaceholderText("Optional auth secret / token")
        self._firebase_token_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._firebase_token_input.setFixedWidth(260)
        fbw.addWidget(self._firebase_url_input)
        fbw.addWidget(self._firebase_token_input)
        self._cloud_firebase_row = self._setting_row("Firebase Database", "Syncs JSON snapshots directly to your private Firebase RTDB", fb_wrap)
        c5.addWidget(self._cloud_firebase_row)

        self._github_token_input = QLineEdit(self.config.cloud_github_token)
        self._github_token_input.setPlaceholderText("GitHub token (gist scope)")
        self._github_token_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._github_token_input.setFixedWidth(252)
        self._cloud_github_row = self._setting_row("GitHub Token", "Stores encrypted-free private Gist backups in your own GitHub account", self._github_token_input)
        c5.addWidget(self._cloud_github_row)

        c5.addWidget(self._divider())
        self._auto_update_switch = _ToggleSwitch(self.config.auto_check_updates)
        c5.addWidget(self._setting_row("Automatic Update Checks", "Ask before installing newer WhisperLM releases when available", self._auto_update_switch))

        c5.addWidget(self._divider())
        self._update_btn = QPushButton("Check for Updates")
        self._update_btn.setObjectName("GhostPillBtn")
        self._update_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._update_btn.clicked.connect(self._on_update_button_clicked)
        self._update_row = self._setting_row(
            "App Updates",
            f"Installed version v{__version__}",
            self._update_btn,
        )
        self._update_desc_lbl = self._update_row.findChild(QLabel, "SettingDesc")
        c5.addWidget(self._update_row)

        cl.addWidget(card5)

        # Hidden Local Data panel inside Settings so clearing dictations is safely tucked away
        self._data_toggle_btn = QPushButton("Manage local data ▸")
        self._data_toggle_btn.setObjectName("DiscreteLinkBtn")
        self._data_toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._data_toggle_btn.clicked.connect(self._toggle_data_panel)
        cl.addWidget(self._data_toggle_btn)

        self._data_panel = QFrame()
        self._data_panel.setObjectName("Card")
        dp = QVBoxLayout(self._data_panel)
        dp.setContentsMargins(0, 0, 0, 0)
        dp.setSpacing(0)
        self._clear_history_btn = QPushButton("Clear All Dictations")
        self._clear_history_btn.setObjectName("DangerPillBtn")
        self._clear_history_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._clear_history_btn.clicked.connect(self._confirm_and_clear_history)
        dp.addWidget(
            self._setting_row(
                "Clear Dictation History",
                "Permanently delete saved dictations from local storage and synced backups",
                self._clear_history_btn,
            )
        )
        self._data_panel.hide()
        cl.addWidget(self._data_panel)

        cl.addStretch(1)
        scroll.setWidget(inner)
        outer.addWidget(scroll, 1)
        return page

    def _toggle_data_panel(self) -> None:
        opening = self._data_panel.isHidden()
        self._data_toggle_btn.setText("Manage local data ▾" if opening else "Manage local data ▸")
        if opening:
            self._data_panel.setMaximumHeight(0)
            self._data_panel.show()
            target_h = max(68, self._data_panel.sizeHint().height())
            anim = QPropertyAnimation(self._data_panel, b"maximumHeight", self)
            anim.setDuration(190)
            anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            anim.setStartValue(0)
            anim.setEndValue(target_h)
            anim.finished.connect(lambda: self._data_panel.setMaximumHeight(16777215))
            anim.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)
            _fade_in_widget(self._data_panel, duration=180)
        else:
            self._data_panel.hide()

    # --- State & Sync Helpers ---

    @staticmethod
    def _set_combo_data(combo: QComboBox, value: object) -> None:
        idx = combo.findData(value)
        if idx >= 0:
            combo.setCurrentIndex(idx)

    @staticmethod
    def _format_hotkey_label(hotkey: str) -> str:
        return hotkey.replace("+", " + ").replace("_", " ").title()

    def sync_from_config(self, cfg: AppConfig) -> None:
        self.config = cfg
        hold_lbl = self._format_hotkey_label(cfg.hotkey_hold)
        self._home_sub.setText(f"Hold {hold_lbl} to dictate, double-tap for hands-free")
        self._sidebar_hotkey_lbl.setText(f"Hold: {hold_lbl}")

        self._set_combo_data(self._hold_combo, cfg.hotkey_hold)
        self._set_combo_data(self._toggle_combo, cfg.hotkey_toggle)
        self._always_on_switch.setChecked(cfg.always_on_pill)
        self._startup_switch.setChecked(cfg.launch_at_startup)
        self._set_combo_data(self._mic_combo, cfg.input_device)
        self._vad_switch.setChecked(cfg.vad_enabled)
        self._sound_switch.setChecked(cfg.activation_sound_enabled)
        self._arabic_switch.setChecked(cfg.arabic_enabled)
        self._cleanup_switch.setChecked(cfg.llm_cleanup_enabled)
        self._restore_clip_switch.setChecked(cfg.restore_clipboard)

        self._meeting_auto_header_switch.blockSignals(True)
        self._meeting_auto_header_switch.setChecked(cfg.meeting_auto_detect)
        self._meeting_auto_header_switch.blockSignals(False)

        self._meeting_auto_switch.setChecked(cfg.meeting_auto_detect)
        self._meeting_sys_audio_switch.setChecked(cfg.meeting_capture_system_audio)
        self._meeting_save_audio_switch.setChecked(cfg.meeting_save_audio)
        self._meeting_cleanup_switch.setChecked(cfg.meeting_cleanup_enabled)
        self._meeting_summary_switch.setChecked(cfg.meeting_summary_enabled)

        self._auto_update_switch.setChecked(cfg.auto_check_updates)
        self._cloud_folder_input.setText(cfg.cloud_backup_folder)
        self._firebase_url_input.setText(cfg.cloud_firebase_url)
        self._firebase_token_input.setText(cfg.cloud_firebase_token)
        self._github_token_input.setText(cfg.cloud_github_token)
        self._sync_cloud_combo_selection(cfg)
        self._update_cloud_detail_visibility()
        self._set_cloud_status(describe_cloud_target(cfg))
        self._rebuild_dictionary_list()

    def _populate_cloud_combo(self) -> None:
        self._cloud_combo.blockSignals(True)
        self._cloud_combo.clear()
        drives = detect_cloud_drives()
        auto_drive = resolve_auto_backup_drive(drives)
        auto_lbl = f"Auto ({auto_drive.display_name})" if auto_drive else "Auto (Local only)"
        self._cloud_combo.addItem(auto_lbl, "auto")
        for drive in drives:
            self._cloud_combo.addItem(drive.display_name, f"folder::{drive.backup_path}")
        self._cloud_combo.addItem("Choose Custom Folder...", "folder_custom")
        self._cloud_combo.addItem("Own Firebase Database", "firebase")
        self._cloud_combo.addItem("GitHub Private Gist", "github")
        self._cloud_combo.addItem("Off (This PC only)", "off")
        self._cloud_combo.blockSignals(False)

    def _sync_cloud_combo_selection(self, cfg: AppConfig) -> None:
        mode = (cfg.cloud_backup_mode or "auto").lower()
        target: str = mode
        if mode == "folder":
            folder = cfg.cloud_backup_folder.strip()
            candidate = f"folder::{folder}"
            target = candidate if self._cloud_combo.findData(candidate) >= 0 else "folder_custom"
        self._cloud_combo.blockSignals(True)
        self._set_combo_data(self._cloud_combo, target)
        self._cloud_combo.blockSignals(False)

    def _update_cloud_detail_visibility(self) -> None:
        choice = str(self._cloud_combo.currentData() or "auto")
        animate = getattr(self, "_page_ready", False) and self.isVisible()
        for row, visible in (
            (self._cloud_folder_row, choice == "folder_custom"),
            (self._cloud_firebase_row, choice == "firebase"),
            (self._cloud_github_row, choice == "github"),
        ):
            was_hidden = row.isHidden()
            row.setVisible(visible)
            if animate and was_hidden and visible:
                _fade_in_widget(row, duration=150)

    def _on_cloud_selection_changed(self, _index: int) -> None:
        choice = str(self._cloud_combo.currentData() or "auto")
        self._update_cloud_detail_visibility()
        if choice == "folder_custom" and not self._cloud_folder_input.text().strip():
            self._browse_cloud_folder()

    def _browse_cloud_folder(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Select Cloud Sync Folder")
        if chosen:
            self._cloud_folder_input.setText(chosen)

    def _set_cloud_status(self, message: str) -> None:
        if getattr(self, "_cloud_desc_lbl", None) is not None and message:
            self._cloud_desc_lbl.setText(f"{message} · Keeps dictations, meetings, and dictionary backed up cleanly")

    def _on_update_available(self, info: UpdateInfo, prompt_user: bool) -> None:
        self._pending_update = info
        self._update_banner_lbl.setText(f"WhisperLM v{info.version} is available — Would you like to update?")
        was_hidden = self._update_banner.isHidden()
        self._update_banner.show()
        if was_hidden and getattr(self, "_page_ready", False) and self.isVisible():
            _fade_in_widget(self._update_banner, duration=180)
        self._update_btn.setText(f"Update to v{info.version}")
        self._update_btn.setObjectName("PrimaryPillBtn")
        self._update_btn.style().unpolish(self._update_btn)
        self._update_btn.style().polish(self._update_btn)
        if self._update_desc_lbl is not None:
            self._update_desc_lbl.setText(f"v{info.version} ready (installed: v{__version__})")

        if not prompt_user or self._update_prompt_shown:
            return
        self._update_prompt_shown = True

        box = QMessageBox(self)
        box.setWindowTitle("WhisperLM Update Available")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(f"WhisperLM v{info.version} is available (installed: v{__version__}).\n\nWould you like to update now?")
        if info.release_notes:
            box.setInformativeText(info.release_notes[:320])
        update_btn = box.addButton("Update Now", QMessageBox.ButtonRole.AcceptRole)
        later_btn = box.addButton("Later", QMessageBox.ButtonRole.RejectRole)
        skip_btn = box.addButton("Skip Version", QMessageBox.ButtonRole.DestructiveRole)
        box.setDefaultButton(update_btn)
        box.exec()

        clicked = box.clickedButton()
        if clicked == update_btn:
            self._trigger_pending_update()
        elif clicked == skip_btn:
            self._update_banner.hide()
            self._on_skip_update(info.version)
        else:
            del later_btn

    def _trigger_pending_update(self) -> None:
        if self._pending_update is not None:
            self._on_install_update(self._pending_update)

    def _on_update_button_clicked(self) -> None:
        if self._pending_update is not None:
            self._trigger_pending_update()
        else:
            if self._update_desc_lbl is not None:
                self._update_desc_lbl.setText(f"Checking for updates... (installed: v{__version__})")
            self._on_check_updates()

    def _set_runtime_status(self, message: str, percent: int) -> None:
        self._sidebar_status_lbl.setText(message or "Ready")
        if getattr(self, "_brand_mark", None) is not None:
            self._brand_mark.set_pulsing(percent >= 0 or getattr(self, "_is_meeting_recording", False))
        if percent < 0:
            self._setup_banner.hide()
            return
        self._setup_status_lbl.setText(message)
        self._setup_progress.setValue(max(0, min(100, percent)))
        self._setup_pct_lbl.setText(f"{percent}%" if percent > 0 else "")
        was_hidden = self._setup_banner.isHidden()
        self._setup_banner.show()
        if was_hidden and getattr(self, "_page_ready", False) and self.isVisible():
            _fade_in_widget(self._setup_banner, duration=180)

    # --- Custom Dictionary ---

    def _rebuild_dictionary_list(self) -> None:
        while self._dict_list_layout.count():
            item = self._dict_list_layout.takeAt(0)
            if item and item.widget():
                item.widget().deleteLater()

        terms = list(self.config.custom_dictionary)
        if not terms:
            empty = QLabel("No custom words yet. Add names, acronyms, or spellings above.")
            empty.setObjectName("SettingDesc")
            empty.setContentsMargins(16, 16, 16, 16)
            self._dict_list_layout.addWidget(empty)
            return

        for idx, term in enumerate(terms):
            if idx > 0:
                self._dict_list_layout.addWidget(_hairline_divider())
            row = QWidget()
            rl = QHBoxLayout(row)
            rl.setContentsMargins(16, 11, 16, 11)
            rl.setSpacing(12)
            lbl = QLabel(_bidi_ltr_display(term))
            lbl.setObjectName("SettingTitle")
            rm_btn = QPushButton("Remove")
            rm_btn.setObjectName("GhostPillBtn")
            rm_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            rm_btn.clicked.connect(lambda _c=False, t=term: self._remove_dictionary_word(t))
            rl.addWidget(lbl, 1)
            rl.addWidget(rm_btn)
            self._dict_list_layout.addWidget(row)

        if getattr(self, "_page_ready", False) and self.isVisible():
            _fade_in_widget(self._dict_card, duration=150)

    def _add_dictionary_word(self) -> None:
        term = self._dict_input.text().strip()
        if not term:
            return
        terms = list(self.config.custom_dictionary)
        if term not in terms:
            terms.append(term)
            self._save_custom_dictionary(terms)
        self._dict_input.clear()

    def _remove_dictionary_word(self, term: str) -> None:
        terms = [t for t in self.config.custom_dictionary if t != term]
        self._save_custom_dictionary(terms)

    def _save_custom_dictionary(self, terms: list[str]) -> None:
        updated = replace(self.config, custom_dictionary=terms)
        self.config = updated
        self._on_save_config(updated)
        self._rebuild_dictionary_list()

    # --- Dictation History ---

    def _refresh_history_filter_buttons(self) -> None:
        for key, btn in self._history_filter_btns.items():
            btn.setProperty("active", "true" if self._history_time_filter == key else "false")
            btn.style().unpolish(btn)
            btn.style().polish(btn)

    def _set_history_time_filter(self, key: str) -> None:
        self._history_time_filter = key
        self._refresh_history_filter_buttons()
        self._rebuild_history_feed()

    def _on_history_search(self, text: str) -> None:
        self._history_query = text.strip().lower()
        self._rebuild_history_feed()

    def _set_history(self, entries: list[HistoryEntry]) -> None:
        self._history = list(entries)
        self._rebuild_history_feed()

    def _append_history(self, entry: HistoryEntry) -> None:
        self._history.insert(0, entry)
        self._rebuild_history_feed()

    def _rebuild_history_feed(self) -> None:
        total_words = sum(len(item.cleaned_text.split()) for item in self._history)
        active_days = len({item.date for item in self._history if item.date})
        animate = getattr(self, "_page_ready", False) and self.isVisible()
        for tile, val in (
            (self._tile_words, str(total_words)),
            (self._tile_sessions, str(len(self._history))),
            (self._tile_days, str(active_days)),
        ):
            changed = tile.value_lbl.text() != val
            tile.value_lbl.setText(val)
            if animate and changed:
                tile.pulse()

        while self._history_layout.count() > 2:
            item = self._history_layout.takeAt(0)
            if item and item.widget() and item.widget() is not self._home_empty:
                item.widget().deleteLater()

        query = self._history_query
        tf = self._history_time_filter
        matched: list[HistoryEntry] = []
        for entry in self._history:
            if not matches_time_filter(entry.date, tf):
                continue
            if query:
                bucket = date_category_label(entry.date)
                haystack = f"{entry.cleaned_text} {entry.raw_text} {entry.app_name} {entry.date} {bucket}".lower()
                if query not in haystack:
                    continue
            matched.append(entry)

        if not matched:
            if self._history:
                self._home_empty_title.setText("No matching dictations")
                self._home_empty_body.setText("Try another search term or switch the time range filter.")
            else:
                self._home_empty_title.setText("Ready when you are")
                self._home_empty_body.setText(
                    "Click any text field and hold your shortcut to start dictating."
                )
            self._home_empty.show()
            self._history_section_lbl.setText("History")
            return

        self._home_empty.hide()
        shown = matched[: self._history_render_limit]
        self._history_section_lbl.setText(
            f"History ({len(matched)} saved)" if len(matched) == len(self._history) else f"Matching ({len(matched)} of {len(self._history)})"
        )

        grouped: list[tuple[str, list[HistoryEntry]]] = []
        for entry in shown:
            bucket = date_category_label(entry.date)
            if not grouped or grouped[-1][0] != bucket:
                grouped.append((bucket, [entry]))
            else:
                grouped[-1][1].append(entry)

        insert_pos = 0
        for bucket_idx, (bucket, bucket_entries) in enumerate(grouped):
            lbl = QLabel(bucket)
            lbl.setObjectName("SectionTitle")
            self._history_layout.insertWidget(insert_pos, lbl)
            insert_pos += 1
            group_card = _DictationDayGroup(bucket_entries, on_copy=self._on_copy_text)
            self._history_layout.insertWidget(insert_pos, group_card)
            if animate and bucket_idx == 0:
                _fade_in_widget(group_card, duration=165)
            insert_pos += 1

        if len(matched) > len(shown):
            more_btn = QPushButton(f"Show all {len(matched)} entries")
            more_btn.setObjectName("GhostPillBtn")
            more_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            more_btn.clicked.connect(lambda: self._expand_history_limit(len(matched)))
            self._history_layout.insertWidget(insert_pos, more_btn)

    def _expand_history_limit(self, target: int) -> None:
        self._history_render_limit = max(self._history_render_limit + 150, target)
        self._rebuild_history_feed()

    def _confirm_and_clear_history(self) -> None:
        if not self._history:
            return
        reply = QMessageBox.question(
            self,
            "Clear Local History",
            f"Delete all {len(self._history)} saved dictations from local storage?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._clear_history()

    def _clear_history(self) -> None:
        self._history.clear()
        self._on_clear_history()
        self._rebuild_history_feed()

    # --- Meetings UI ---

    def _on_header_auto_toggle(self, enabled: bool) -> None:
        self._meeting_auto_switch.setChecked(enabled)
        self._on_toggle_meeting_auto(enabled)

    def _toggle_manual_meeting(self) -> None:
        if self._is_meeting_recording:
            self._on_stop_meeting()
        else:
            self._on_start_meeting()

    def _set_meeting_status(self, state: str, label: str) -> None:
        self._is_meeting_recording = state == "recording"
        if getattr(self, "_brand_mark", None) is not None:
            self._brand_mark.set_pulsing(state in {"recording", "cleaning", "summarizing"})
        animate = getattr(self, "_page_ready", False) and self.isVisible()
        if state == "recording":
            if not hasattr(self, "_hub_meeting_timer"):
                self._hub_meeting_timer = QTimer(self)
                self._hub_meeting_timer.setInterval(1000)
                self._hub_meeting_timer.timeout.connect(self._tick_meeting_banner)
            if not self._hub_meeting_timer.isActive():
                import time

                self._hub_meeting_started_at = time.monotonic()
                self._hub_meeting_label = label or "Meeting"
                self._hub_meeting_timer.start()
            self._meeting_action_btn.setText("Stop Recording")
            self._meeting_action_btn.setObjectName("DangerPillBtn")
            self._banner_dot.setStyleSheet(f"color: {_ACCENT_RED}; font-size: 12px;")
            self._tick_meeting_banner()
            was_hidden = self._meeting_banner.isHidden()
            self._meeting_banner.show()
            if animate and was_hidden:
                _fade_in_widget(self._meeting_banner, duration=175)
            self._nav_buttons[1].setText("  Meetings  ●")
            self._sidebar_status_lbl.setText(f"Recording: {label[:16]}")
        elif state in {"cleaning", "summarizing"}:
            if hasattr(self, "_hub_meeting_timer"):
                self._hub_meeting_timer.stop()
            self._meeting_action_btn.setText("Record Meeting")
            self._meeting_action_btn.setObjectName("PrimaryPillBtn")
            self._banner_dot.setStyleSheet(f"color: {_TEXT_SECONDARY}; font-size: 12px;")
            is_sum = state == "summarizing"
            self._banner_text.setText("Summarizing meeting..." if is_sum else "Polishing meeting transcript...")
            was_hidden = self._meeting_banner.isHidden()
            self._meeting_banner.show()
            if animate and was_hidden:
                _fade_in_widget(self._meeting_banner, duration=175)
            self._nav_buttons[1].setText("  Meetings")
            self._sidebar_status_lbl.setText("Summarizing" if is_sum else "Polishing transcript")
        else:
            if hasattr(self, "_hub_meeting_timer"):
                self._hub_meeting_timer.stop()
            self._meeting_action_btn.setText("Record Meeting")
            self._meeting_action_btn.setObjectName("PrimaryPillBtn")
            self._meeting_banner.hide()
            self._nav_buttons[1].setText("  Meetings")
            self._sidebar_status_lbl.setText("Ready")

        self._meeting_action_btn.style().unpolish(self._meeting_action_btn)
        self._meeting_action_btn.style().polish(self._meeting_action_btn)

    def _tick_meeting_banner(self) -> None:
        import time

        elapsed = int(time.monotonic() - getattr(self, "_hub_meeting_started_at", time.monotonic()))
        lbl = getattr(self, "_hub_meeting_label", "Meeting")
        self._banner_text.setText(f"Recording {lbl} · {format_duration(elapsed)}")

    def _on_meeting_search(self, text: str) -> None:
        self._meeting_query = text.strip().lower()
        self._refresh_meeting_list_ui()

    def _set_meetings(self, records: list[MeetingRecord]) -> None:
        self._meetings = list(records)
        live_rec = next((r for r in self._meetings if r.status == "recording"), None)
        force_live = live_rec is not None and live_rec.id != self._active_live_meeting_id
        self._active_live_meeting_id = live_rec.id if live_rec is not None else None
        if force_live and live_rec is not None:
            self._selected_meeting_id = live_rec.id
        self._refresh_meeting_list_ui()

    def _refresh_meeting_list_ui(self) -> None:
        q = self._meeting_query
        visible: list[MeetingRecord] = []
        for rec in self._meetings:
            if q:
                haystack = (
                    f"{rec.title} {rec.app_name} {rec.started_at} {rec.summary} "
                    f"{rec.raw_transcript} {rec.cleaned_transcript}"
                ).lower()
                if q not in haystack:
                    continue
            visible.append(rec)

        prev_id = self._selected_meeting_id
        self._meeting_list.blockSignals(True)
        self._meeting_list.clear()

        target_row = 0 if visible else -1
        for idx, rec in enumerate(visible):
            prefix = "● " if rec.status == "recording" else ""
            sub = f"{rec.started_at} · {format_duration(rec.duration_seconds)}"
            item = QListWidgetItem(f"{prefix}{rec.title}\n{sub}")
            item.setData(Qt.ItemDataRole.UserRole, rec.id)
            self._meeting_list.addItem(item)
            if rec.id == prev_id:
                target_row = idx

        if target_row >= 0:
            self._meeting_list.setCurrentRow(target_row)
            self._selected_meeting_id = visible[target_row].id
            self._render_selected_meeting(visible[target_row])
        else:
            self._selected_meeting_id = None
            self._audio_bar.unload()
            self._btn_summarize_meeting.setText("Summarize")
            self._btn_summarize_meeting.setEnabled(False)
            self._detail_title.setText("No matching meetings" if self._meetings else "No meetings yet")
            self._detail_meta.setText(
                "Clear the search filter to view saved meetings."
                if self._meetings
                else "Start a call on Meet, Zoom, Teams, or click Record Meeting."
            )
            self._detail_body.clear()
        self._meeting_list.blockSignals(False)

    def _on_meeting_selected(self, current: QListWidgetItem | None, _previous: QListWidgetItem | None) -> None:
        if current is None:
            return
        mid = current.data(Qt.ItemDataRole.UserRole)
        if mid != self._selected_meeting_id:
            self._meeting_view_explicit = None
        self._selected_meeting_id = mid
        for rec in self._meetings:
            if rec.id == mid:
                self._render_selected_meeting(rec)
                break

    def _switch_meeting_view(self, mode: str) -> None:
        self._meeting_view_explicit = mode
        self._meeting_view_mode = mode
        self._refresh_view_toggle_buttons()
        for rec in self._meetings:
            if rec.id == self._selected_meeting_id:
                self._render_selected_meeting(rec)
                break

    def _refresh_view_toggle_buttons(self) -> None:
        for btn, key in (
            (self._btn_view_summary, "summary"),
            (self._btn_view_cleaned, "cleaned"),
            (self._btn_view_raw, "raw"),
        ):
            btn.setProperty("active", "true" if self._meeting_view_mode == key else "false")
            btn.style().unpolish(btn)
            btn.style().polish(btn)

    def _resolve_meeting_audio_path(self, rec: MeetingRecord) -> Path | None:
        if rec.status == "recording":
            return None
        base = self._meetings_dir or self.config.resolve_data_path(self.config.meetings_dir)
        filename = Path(rec.audio_file).name if rec.audio_file else f"{rec.id}.wav"
        candidate = base / filename
        try:
            if candidate.exists() and candidate.is_file() and candidate.stat().st_size > 44:
                return candidate
        except OSError:
            pass
        return None

    def _render_selected_meeting(self, rec: MeetingRecord) -> None:
        self._detail_title.setText(rec.title)
        state_lbl = {
            "recording": "Recording live",
            "cleaning": "Polishing...",
            "summarizing": "Summarizing...",
            "completed": format_duration(rec.duration_seconds),
        }.get(rec.status, "")
        self._detail_meta.setText(f"{rec.app_name or 'Manual'}  ·  {rec.started_at}  ·  {state_lbl}")

        audio_path = self._resolve_meeting_audio_path(rec)
        self._audio_bar.load_file(audio_path, animate=getattr(self, "_page_ready", False) and self.isVisible())

        if self._meeting_view_explicit is None:
            preferred = "summary" if (rec.status == "completed" and rec.summary.strip()) else "cleaned"
            if preferred != self._meeting_view_mode:
                self._meeting_view_mode = preferred
                self._refresh_view_toggle_buttons()

        if rec.status == "recording":
            self._btn_summarize_meeting.setText("Summarize")
            self._btn_summarize_meeting.setEnabled(False)
        elif rec.status in {"cleaning", "summarizing"}:
            self._btn_summarize_meeting.setText("Working...")
            self._btn_summarize_meeting.setEnabled(False)
        else:
            self._btn_summarize_meeting.setText("Regenerate" if rec.summary.strip() else "Summarize")
            self._btn_summarize_meeting.setEnabled(bool(rec.display_transcript))

        if self._meeting_view_mode == "summary":
            if rec.summary.strip():
                text = rec.summary.strip()
            elif rec.status == "summarizing":
                text = "Generating summary..."
            else:
                text = "No summary yet. Click Summarize to create one."
        elif self._meeting_view_mode == "raw":
            text = rec.raw_transcript.strip()
        else:
            text = rec.display_transcript

        display_text = "\n".join(_bidi_ltr_display(line) for line in text.splitlines())
        if self._detail_body.toPlainText() != display_text:
            scroll_bar = self._detail_body.verticalScrollBar()
            at_bottom = scroll_bar.value() >= max(0, scroll_bar.maximum() - 10)
            self._detail_body.setPlainText(display_text)
            if rec.status == "recording" and at_bottom:
                scroll_bar.setValue(scroll_bar.maximum())

    def _summarize_selected_meeting(self) -> None:
        if not self._selected_meeting_id:
            return
        self._meeting_view_explicit = "summary"
        self._meeting_view_mode = "summary"
        self._refresh_view_toggle_buttons()
        self._btn_summarize_meeting.setText("Working...")
        self._btn_summarize_meeting.setEnabled(False)
        self._on_summarize_meeting(self._selected_meeting_id)

    def _copy_selected_meeting(self) -> None:
        text = self._detail_body.toPlainText().replace("\u200e", "").strip()
        if not text:
            return
        self._on_copy_text(text)
        self._btn_copy_meeting.setText("Copied")
        QTimer.singleShot(1100, lambda: self._btn_copy_meeting.setText("Copy"))

    def _delete_selected_meeting(self) -> None:
        if self._selected_meeting_id:
            self._audio_bar.unload()
            self._on_delete_meeting(self._selected_meeting_id)

    # --- Settings Actions ---

    def _toggle_mic_test(self) -> None:
        if self._test_recorder is not None:
            if self._test_timer:
                self._test_timer.stop()
            self._test_recorder.stop()
            self._test_recorder = None
            self._mic_meter.setValue(0)
            self._mic_test_btn.setText("Test Mic")
            return

        levels = [0.0]
        rec = AudioRecorder(
            sample_rate=self.config.sample_rate,
            device=self._mic_combo.currentData(),
            on_level=lambda lvl: levels.__setitem__(0, lvl),
        )
        try:
            rec.start()
        except Exception:
            return
        self._test_recorder = rec
        self._mic_test_btn.setText("Stop")
        self._test_timer = QTimer(self)
        self._test_timer.setInterval(40)
        self._test_timer.timeout.connect(lambda: self._mic_meter.setValue(int(levels[0] * 100)))
        self._test_timer.start()

    def _save_settings(self) -> None:
        cloud_choice = str(self._cloud_combo.currentData() or "auto")
        if cloud_choice.startswith("folder::"):
            cloud_mode = "folder"
            cloud_folder = cloud_choice.split("::", 1)[1]
        elif cloud_choice == "folder_custom":
            cloud_mode = "folder"
            cloud_folder = self._cloud_folder_input.text().strip()
        elif cloud_choice in {"auto", "firebase", "github", "off"}:
            cloud_mode = cloud_choice
            cloud_folder = self._cloud_folder_input.text().strip()
        else:
            cloud_mode = "auto"
            cloud_folder = ""

        updated = replace(
            self.config,
            hotkey_hold=str(self._hold_combo.currentData()),
            hotkey_toggle=str(self._toggle_combo.currentData()),
            input_device=self._mic_combo.currentData(),
            vad_enabled=self._vad_switch.isChecked(),
            whisper_language="auto" if self._arabic_switch.isChecked() else "en",
            llm_cleanup_enabled=self._cleanup_switch.isChecked(),
            restore_clipboard=self._restore_clip_switch.isChecked(),
            always_on_pill=self._always_on_switch.isChecked(),
            launch_at_startup=self._startup_switch.isChecked(),
            activation_sound_enabled=self._sound_switch.isChecked(),
            meeting_auto_detect=self._meeting_auto_switch.isChecked(),
            meeting_capture_system_audio=self._meeting_sys_audio_switch.isChecked(),
            meeting_save_audio=self._meeting_save_audio_switch.isChecked(),
            meeting_cleanup_enabled=self._meeting_cleanup_switch.isChecked(),
            meeting_summary_enabled=self._meeting_summary_switch.isChecked(),
            cloud_backup_mode=cloud_mode,
            cloud_backup_folder=cloud_folder,
            cloud_firebase_url=self._firebase_url_input.text().strip(),
            cloud_firebase_token=self._firebase_token_input.text().strip(),
            cloud_github_token=self._github_token_input.text().strip(),
            auto_check_updates=self._auto_update_switch.isChecked(),
        )
        self._on_save_config(updated)
        self.sync_from_config(updated)
        self._save_btn.setText("Saved")
        QTimer.singleShot(1100, lambda: self._save_btn.setText("Save Changes"))


ControlCenterWindow = FlowHubWindow


def render_brand_pixmap(size: int = 64, dot_color: str = _TEXT_PRIMARY) -> QPixmap:
    """Render the crisp WhisperLM rounded obsidian squircle emblem at any size."""
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    m = size * (4.0 / 64.0)
    box = QRectF(m, m, size - 2.0 * m, size - 2.0 * m)
    radius = size * (16.0 / 64.0)
    bg = QLinearGradient(box.topLeft(), box.bottomRight())
    bg.setColorAt(0.0, QColor(20, 20, 28))
    bg.setColorAt(1.0, QColor(11, 11, 15))
    painter.setBrush(QBrush(bg))
    painter.setPen(QPen(QColor(123, 139, 255, 110), max(1.2, size * (2.2 / 64.0))))
    painter.drawRoundedRect(box, radius, radius)

    spark_hex = _ACCENT_INDIGO if dot_color == _TEXT_PRIMARY else dot_color
    pad = size * (15.0 / 64.0)
    glyph_rect = QRectF(pad, pad, size - 2.0 * pad, size - 2.0 * pad)
    _draw_whisperlm_glyph(painter, glyph_rect, QColor(dot_color), QColor(spark_hex))
    painter.end()
    return pix


def _build_tray_icon(dot_color: str) -> QIcon:
    return QIcon(render_brand_pixmap(64, dot_color))


class TrayController(QWidget):
    """Windows system tray icon synced with Hub, shortcuts, and Meeting recording."""

    status_requested = Signal(str, str)
    meeting_status_changed = Signal(bool)

    def __init__(
        self,
        config: AppConfig,
        window: FlowHubWindow,
        on_toggle_cleanup: Callable[[bool], None],
        on_change_hold_hotkey: Callable[[str], None],
        on_toggle_meeting: Callable[[], None] | None = None,
        on_toggle_meeting_auto: Callable[[bool], None] | None = None,
        on_toggle_always_on: Callable[[bool], None] | None = None,
        on_quit: Callable[[], None] = lambda: None,
        on_toggle_arabic_mode: Callable[[bool], None] | None = None,
        on_toggle_startup: Callable[[bool], None] | None = None,
    ) -> None:
        super().__init__()
        self._window = window
        self._on_toggle_cleanup = on_toggle_cleanup
        self._on_change_hold_hotkey = on_change_hold_hotkey
        self._on_toggle_meeting = on_toggle_meeting or (lambda: None)
        self._on_toggle_meeting_auto = on_toggle_meeting_auto or (lambda _b: None)
        self._on_toggle_always_on = on_toggle_always_on or (lambda _b: None)
        self._on_toggle_arabic_mode = on_toggle_arabic_mode or (lambda _b: None)
        self._on_toggle_startup = on_toggle_startup or (lambda _b: None)
        self._on_quit = on_quit

        self._tray = QSystemTrayIcon(_build_tray_icon(_TEXT_PRIMARY), self)
        self._menu = QMenu()
        self._menu.setStyleSheet(_DARK_MENU_QSS)

        self._status_action = QAction("Starting WhisperLM...", self._menu)
        self._status_action.setEnabled(False)
        self._menu.addAction(self._status_action)
        self._menu.addSeparator()

        open_action = QAction("Open WhisperLM", self._menu)
        open_action.triggered.connect(lambda: self._window.open_page("home"))
        self._menu.addAction(open_action)

        self._meeting_toggle_action = QAction("Record Meeting", self._menu)
        self._meeting_toggle_action.triggered.connect(self._on_toggle_meeting)
        self._menu.addAction(self._meeting_toggle_action)

        self._meeting_auto_action = QAction("Auto-Detect Meetings", self._menu)
        self._meeting_auto_action.setCheckable(True)
        self._meeting_auto_action.toggled.connect(self._on_toggle_meeting_auto)
        self._menu.addAction(self._meeting_auto_action)

        self._menu.addSeparator()

        self._hold_menu = self._menu.addMenu("Hold Dictate Shortcut")
        self._hold_group = QActionGroup(self._hold_menu)
        self._hold_group.setExclusive(True)
        self._hold_actions: dict[str, QAction] = {}
        for key, label in HOLD_HOTKEY_PRESETS:
            act = QAction(label, self._hold_menu)
            act.setCheckable(True)
            act.triggered.connect(lambda _checked=False, k=key: self._on_change_hold_hotkey(k))
            self._hold_group.addAction(act)
            self._hold_menu.addAction(act)
            self._hold_actions[key] = act

        self._always_on_action = QAction("Floating Flow Bar", self._menu)
        self._always_on_action.setCheckable(True)
        self._always_on_action.toggled.connect(self._on_toggle_always_on)
        self._menu.addAction(self._always_on_action)

        self._startup_action = QAction("Launch at Startup", self._menu)
        self._startup_action.setCheckable(True)
        self._startup_action.toggled.connect(self._on_toggle_startup)
        self._menu.addAction(self._startup_action)

        self._cleanup_action = QAction("Smart Formatting", self._menu)
        self._cleanup_action.setCheckable(True)
        self._cleanup_action.toggled.connect(self._on_toggle_cleanup)
        self._menu.addAction(self._cleanup_action)

        self._arabic_action = QAction("Arabic + English", self._menu)
        self._arabic_action.setCheckable(True)
        self._arabic_action.toggled.connect(self._on_toggle_arabic_mode)
        self._menu.addAction(self._arabic_action)

        self._menu.addSeparator()
        quit_action = QAction("Quit", self._menu)
        quit_action.triggered.connect(self._on_quit)
        self._menu.addAction(quit_action)

        self._tray.setContextMenu(self._menu)
        self._tray.activated.connect(self._on_activated)
        self._tray.messageClicked.connect(lambda: self._window.open_page("home"))
        self._tray.show()

        self.status_requested.connect(self.set_status)
        self.meeting_status_changed.connect(self._set_meeting_recording)
        self.sync_config(config)

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self._window.open_page("home")

    def _set_meeting_recording(self, active: bool) -> None:
        self._meeting_toggle_action.setText("Stop Meeting Recording" if active else "Record Meeting")

    @staticmethod
    def _set_action_checked(action: QAction, checked: bool) -> None:
        action.blockSignals(True)
        action.setChecked(checked)
        action.blockSignals(False)

    def sync_config(self, config: AppConfig) -> None:
        self._set_action_checked(self._always_on_action, config.always_on_pill)
        self._set_action_checked(self._startup_action, config.launch_at_startup)
        self._set_action_checked(self._cleanup_action, config.llm_cleanup_enabled)
        self._set_action_checked(self._arabic_action, config.arabic_enabled)
        self._set_action_checked(self._meeting_auto_action, config.meeting_auto_detect)

        for key, act in self._hold_actions.items():
            self._set_action_checked(act, key == config.hotkey_hold)

    def set_status(self, status_text: str, dot_color: str = _TEXT_PRIMARY) -> None:
        self._status_action.setText(status_text)
        self._tray.setIcon(_build_tray_icon(dot_color))
        self._tray.setToolTip(f"WhisperLM — {status_text}")

    def notify(self, title: str, message: str) -> None:
        """Display a native Windows tray balloon notification."""
        self._tray.showMessage(title, message, QSystemTrayIcon.MessageIcon.Information, 6000)

    def hide(self) -> None:
        self._tray.hide()


def current_timestamp() -> str:
    """Return short local time string for history items."""
    return datetime.now().strftime("%H:%M:%S")
