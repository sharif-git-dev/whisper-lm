"""Global Win32 low-level keyboard hook for hold-to-talk and toggle hotkeys."""

from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes
from typing import Callable

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
WM_QUIT = 0x0012
LLKHF_INJECTED = 0x00000010

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
VK_MASK_NONAME = 0xE8  # Unassigned VK used to mask standalone Win key Start Menu popup

ULONG_PTR = ctypes.c_uint64 if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_uint32
LRESULT = ctypes.c_int64 if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_long
HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

WIN_VKS = {0x5B, 0x5C}
CTRL_VKS = {0x11, 0xA2, 0xA3}
MODIFIER_VKS = {
    0x10, 0xA0, 0xA1,  # Shift
    0x11, 0xA2, 0xA3,  # Ctrl
    0x12, 0xA4, 0xA5,  # Alt
    0x5B, 0x5C,        # Win
}


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [
        ("mi", _MOUSEINPUT),
        ("ki", _KEYBDINPUT),
        ("hi", _HARDWAREINPUT),
    ]


class _INPUT(ctypes.Structure):
    _fields_ = [
        ("type", wintypes.DWORD),
        ("union", _INPUT_UNION),
    ]


user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
user32.SetWindowsHookExW.restype = wintypes.HHOOK
user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
user32.UnhookWindowsHookEx.restype = wintypes.BOOL
user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
user32.CallNextHookEx.restype = LRESULT
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.GetMessageW.restype = wintypes.BOOL
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.TranslateMessage.restype = wintypes.BOOL
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.restype = LRESULT
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PostThreadMessageW.restype = wintypes.BOOL
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]
user32.SendInput.restype = wintypes.UINT
kernel32.GetCurrentThreadId.restype = wintypes.DWORD


KEY_MAP: dict[str, set[int]] = {
    "ctrl": {0x11, 0xA2, 0xA3},
    "lctrl": {0xA2},
    "left_ctrl": {0xA2},
    "rctrl": {0xA3},
    "right_ctrl": {0xA3},
    "alt": {0x12, 0xA4, 0xA5},
    "lalt": {0xA4},
    "left_alt": {0xA4},
    "ralt": {0xA5},
    "right_alt": {0xA5},
    "shift": {0x10, 0xA0, 0xA1},
    "lshift": {0xA0},
    "left_shift": {0xA0},
    "rshift": {0xA1},
    "right_shift": {0xA1},
    "win": {0x5B, 0x5C},
    "lwin": {0x5B},
    "left_win": {0x5B},
    "rwin": {0x5C},
    "right_win": {0x5C},
    "space": {0x20},
    "caps_lock": {0x14},
    "pause": {0x13},
    "scroll_lock": {0x91},
    "insert": {0x2D},
    "f8": {0x77},
    "f9": {0x78},
    "f10": {0x79},
    "f11": {0x7A},
    "f12": {0x7B},
}

HOLD_HOTKEY_PRESETS: list[tuple[str, str]] = [
    ("ctrl+win", "Ctrl + Win (Wispr Flow style)"),
    ("right_ctrl", "Right Ctrl"),
    ("f9", "F9"),
    ("f8", "F8"),
    ("pause", "Pause / Break"),
    ("right_alt", "Right Alt (note: AltGr on Arabic layout)"),
]

TOGGLE_HOTKEY_PRESETS: list[tuple[str, str]] = [
    ("ctrl+shift+space", "Ctrl + Shift + Space"),
    ("ctrl+alt+space", "Ctrl + Alt + Space"),
    ("f10", "F10"),
    ("", "Disabled"),
]


def parse_hotkey(spec: str) -> tuple[list[set[int]], set[int]] | None:
    """Parse a hotkey string into (modifier_groups, trigger_vks)."""
    cleaned = spec.strip().lower()
    if not cleaned:
        return None
    parts = [p.strip() for p in cleaned.split("+") if p.strip()]
    if not parts:
        return None

    groups: list[set[int]] = []
    for part in parts:
        if part in KEY_MAP:
            groups.append(KEY_MAP[part])
        elif len(part) == 1 and part.isalnum():
            groups.append({ord(part.upper())})
        else:
            raise ValueError(f"Unsupported key token in hotkey: {part!r}")

    return groups[:-1], groups[-1]


def is_modifier_only_spec(spec: tuple[list[set[int]], set[int]] | None) -> bool:
    """Return True if every key in the hotkey spec is a modifier (e.g. ctrl+win or right_ctrl)."""
    if spec is None:
        return False
    mod_groups, trigger_vks = spec
    return all(group <= MODIFIER_VKS for group in [*mod_groups, trigger_vks])


def matches_hotkey_press(
    spec: tuple[list[set[int]], set[int]] | None,
    pressed_vks: set[int],
    newly_pressed_vk: int,
) -> bool:
    """Check whether pressed_vks + newly_pressed_vk activates spec."""
    if spec is None:
        return False
    mod_groups, trigger_vks = spec
    all_groups = [*mod_groups, trigger_vks]

    if is_modifier_only_spec(spec):
        # Order-independent matching so Ctrl+Win works whether Ctrl or Win lands first.
        if not any(newly_pressed_vk in group for group in all_groups):
            return False
        return all(any(vk in pressed_vks for vk in group) for group in all_groups)

    if newly_pressed_vk not in trigger_vks:
        return False
    return all(any(vk in pressed_vks for vk in group) for group in mod_groups)


def spec_is_still_held(
    spec: tuple[list[set[int]], set[int]] | None,
    pressed_vks: set[int],
    released_vk: int,
) -> bool:
    """Return True if a hold hotkey remains satisfied after released_vk is removed."""
    if spec is None:
        return False
    mod_groups, trigger_vks = spec
    if is_modifier_only_spec(spec):
        all_groups = [*mod_groups, trigger_vks]
        return all(any(vk in pressed_vks for vk in group) for group in all_groups)

    if released_vk in trigger_vks:
        return False
    return all(any(vk in pressed_vks for vk in group) for group in mod_groups)


def _send_win_menu_mask_key() -> None:
    """Send an unassigned key down/up so Windows aborts Start Menu activation on Win keyup."""
    seq = (_INPUT * 2)(
        _INPUT(
            type=INPUT_KEYBOARD,
            union=_INPUT_UNION(
                ki=_KEYBDINPUT(wVk=VK_MASK_NONAME, wScan=0, dwFlags=0, time=0, dwExtraInfo=0)
            ),
        ),
        _INPUT(
            type=INPUT_KEYBOARD,
            union=_INPUT_UNION(
                ki=_KEYBDINPUT(
                    wVk=VK_MASK_NONAME,
                    wScan=0,
                    dwFlags=KEYEVENTF_KEYUP,
                    time=0,
                    dwExtraInfo=0,
                )
            ),
        ),
    )
    user32.SendInput(2, seq, ctypes.sizeof(_INPUT))


class GlobalHotkeyListener:
    """Win32 low-level global keyboard listener supporting hold-to-talk and toggle shortcuts."""

    def __init__(
        self,
        hold_hotkey: str,
        toggle_hotkey: str,
        on_hold_down: Callable[[], None],
        on_hold_up: Callable[[], None],
        on_toggle: Callable[[], None],
    ) -> None:
        self._hold_spec = parse_hotkey(hold_hotkey)
        self._toggle_spec = parse_hotkey(toggle_hotkey)
        self._on_hold_down = on_hold_down
        self._on_hold_up = on_hold_up
        self._on_toggle = on_toggle

        self._pressed_vks: set[int] = set()
        self._suppressed_vks: set[int] = set()
        self._hold_active = False
        self._toggle_latched = False
        self._thread_id: int | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._hook_proc_ref: HOOKPROC | None = None
        self._hook_handle: wintypes.HHOOK | None = None

    def update_hotkeys(self, hold_hotkey: str, toggle_hotkey: str) -> None:
        """Update configured hotkeys dynamically."""
        self._hold_spec = parse_hotkey(hold_hotkey)
        self._toggle_spec = parse_hotkey(toggle_hotkey)
        self._hold_active = False
        self._toggle_latched = False

    def start(self) -> None:
        """Start the Win32 keyboard hook thread."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._ready.clear()
        self._thread = threading.Thread(target=self._run_loop, name="hotkey-hook", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=2.0)

    def stop(self) -> None:
        """Unhook keyboard listener and exit message loop."""
        if self._thread_id is not None:
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._thread = None
        self._thread_id = None

    def _uses_ctrl_win(self) -> bool:
        for spec in (self._hold_spec, self._toggle_spec):
            if spec is None:
                continue
            groups = [*spec[0], spec[1]]
            has_ctrl = any(g & CTRL_VKS for g in groups)
            has_win = any(g & WIN_VKS for g in groups)
            if has_ctrl and has_win:
                return True
        return False

    def _run_loop(self) -> None:
        self._thread_id = kernel32.GetCurrentThreadId()

        def _callback(n_code: int, w_param: int, l_param: int) -> int:
            if n_code >= 0:
                kb = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                if not (kb.flags & LLKHF_INJECTED):
                    vk = int(kb.vkCode)
                    is_down = w_param in (WM_KEYDOWN, WM_SYSKEYDOWN)
                    is_up = w_param in (WM_KEYUP, WM_SYSKEYUP)

                    if is_down:
                        # Drop any stale modifiers whose KEYUP was swallowed by UAC/elevated windows.
                        for mvk in list(self._pressed_vks & MODIFIER_VKS):
                            if mvk != vk and (user32.GetAsyncKeyState(mvk) & 0x8000) == 0:
                                self._pressed_vks.discard(mvk)
                                self._suppressed_vks.discard(mvk)
                        was_down = vk in self._pressed_vks
                        self._pressed_vks.add(vk)

                        # If Win is pressed while Ctrl is held for Ctrl+Win, swallow Win so Start Menu won't open.
                        swallow_win = (
                            vk in WIN_VKS
                            and self._uses_ctrl_win()
                            and bool(self._pressed_vks & CTRL_VKS)
                        )
                        if swallow_win:
                            self._suppressed_vks.add(vk)

                        if not was_down:
                            hold_hit = not self._hold_active and matches_hotkey_press(
                                self._hold_spec, self._pressed_vks, vk
                            )
                            toggle_hit = (
                                not hold_hit
                                and not self._toggle_latched
                                and matches_hotkey_press(self._toggle_spec, self._pressed_vks, vk)
                            )

                            # If Win landed slightly before Ctrl, mask the standalone Win sequence.
                            if (hold_hit or toggle_hit) and self._uses_ctrl_win() and not swallow_win:
                                if self._pressed_vks & WIN_VKS:
                                    self._dispatch(_send_win_menu_mask_key)

                            if hold_hit:
                                self._hold_active = True
                                self._dispatch(self._on_hold_down)

                            if toggle_hit:
                                self._toggle_latched = True
                                self._dispatch(self._on_toggle)

                        if swallow_win or vk in self._suppressed_vks:
                            return 1

                    elif is_up:
                        self._pressed_vks.discard(vk)
                        if self._hold_active and not spec_is_still_held(
                            self._hold_spec, self._pressed_vks, vk
                        ):
                            self._hold_active = False
                            self._dispatch(self._on_hold_up)

                        if self._toggle_latched and not spec_is_still_held(
                            self._toggle_spec, self._pressed_vks, vk
                        ):
                            self._toggle_latched = False

                        if vk in self._suppressed_vks:
                            self._suppressed_vks.discard(vk)
                            return 1

            return user32.CallNextHookEx(self._hook_handle, n_code, w_param, l_param)

        self._hook_proc_ref = HOOKPROC(_callback)
        self._hook_handle = user32.SetWindowsHookExW(
            WH_KEYBOARD_LL,
            self._hook_proc_ref,
            None,
            0,
        )
        self._ready.set()

        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        if self._hook_handle:
            user32.UnhookWindowsHookEx(self._hook_handle)
            self._hook_handle = None

    @staticmethod
    def _dispatch(fn: Callable[[], None]) -> None:
        # Never block the low-level Win32 keyboard hook callback (Windows drops slow hooks!).
        threading.Thread(target=fn, daemon=True).start()
