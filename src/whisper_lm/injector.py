"""Win32 active window inspection and Unicode clipboard paste injection."""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_EXTENDEDKEY = 0x0001

VK_CONTROL = 0x11
VK_LCONTROL = 0xA2
VK_RCONTROL = 0xA3
VK_MENU = 0x12
VK_LMENU = 0xA4
VK_RMENU = 0xA5
VK_SHIFT = 0x10
VK_LSHIFT = 0xA0
VK_RSHIFT = 0xA1
VK_LWIN = 0x5B
VK_RWIN = 0x5C
VK_V = 0x56

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
ULONG_PTR = ctypes.c_uint64 if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_uint32


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [
        ("mi", MOUSEINPUT),
        ("ki", KEYBDINPUT),
        ("hi", HARDWAREINPUT),
    ]


class INPUT(ctypes.Structure):
    _fields_ = [
        ("type", wintypes.DWORD),
        ("union", _INPUT_UNION),
    ]


# Configure explicit argument and return types for Win32 FFI safety on 64-bit Python.
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wintypes.UINT
user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.OpenClipboard.restype = wintypes.BOOL
user32.CloseClipboard.argtypes = []
user32.CloseClipboard.restype = wintypes.BOOL
user32.EmptyClipboard.argtypes = []
user32.EmptyClipboard.restype = wintypes.BOOL
user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
user32.GetClipboardData.argtypes = [wintypes.UINT]
user32.GetClipboardData.restype = wintypes.HANDLE
user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
user32.SetClipboardData.restype = wintypes.HANDLE

kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD),
]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalUnlock.restype = wintypes.BOOL
kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalFree.restype = wintypes.HGLOBAL


@dataclass(frozen=True)
class WindowContext:
    """Context about the currently focused target window."""

    hwnd: int
    app_name: str
    window_title: str


def get_foreground_context() -> WindowContext:
    """Inspect the foreground window title and executable name."""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return WindowContext(hwnd=0, app_name="", window_title="")

    length = user32.GetWindowTextLengthW(hwnd)
    title = ""
    if length > 0:
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value

    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    app_name = ""
    if pid.value:
        proc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if proc:
            try:
                size = wintypes.DWORD(1024)
                exe_buf = ctypes.create_unicode_buffer(size.value)
                if kernel32.QueryFullProcessImageNameW(proc, 0, exe_buf, ctypes.byref(size)):
                    app_name = Path(exe_buf.value).name
            finally:
                kernel32.CloseHandle(proc)

    return WindowContext(hwnd=int(hwnd), app_name=app_name, window_title=title)


def _open_clipboard_with_retry(retries: int = 10, delay_s: float = 0.015) -> bool:
    for _ in range(retries):
        if user32.OpenClipboard(None):
            return True
        time.sleep(delay_s)
    return False


def get_clipboard_text() -> str | None:
    """Read current Unicode clipboard text if available."""
    if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
        return None
    if not _open_clipboard_with_retry():
        return None
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return None
        try:
            return ctypes.wstring_at(ptr)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def set_clipboard_text(text: str) -> bool:
    """Write Unicode text to the Windows clipboard."""
    if not _open_clipboard_with_retry():
        return False
    try:
        user32.EmptyClipboard()
        encoded = (text + "\0").encode("utf-16-le")
        hmem = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(encoded))
        if not hmem:
            return False
        ptr = kernel32.GlobalLock(hmem)
        if not ptr:
            kernel32.GlobalFree(hmem)
            return False
        ctypes.memmove(ptr, encoded, len(encoded))
        kernel32.GlobalUnlock(hmem)
        if not user32.SetClipboardData(CF_UNICODETEXT, hmem):
            kernel32.GlobalFree(hmem)
            return False
        return True
    finally:
        user32.CloseClipboard()


def _key_input(vk: int, key_up: bool = False, extended: bool = False) -> INPUT:
    flags = 0
    if extended:
        flags |= KEYEVENTF_EXTENDEDKEY
    if key_up:
        flags |= KEYEVENTF_KEYUP
    return INPUT(
        type=INPUT_KEYBOARD,
        union=_INPUT_UNION(
            ki=KEYBDINPUT(wVk=vk, wScan=0, dwFlags=flags, time=0, dwExtraInfo=0)
        ),
    )


def _release_active_modifiers() -> None:
    """Release any modifier keys that are logically down right after a hotkey release."""
    modifiers = [
        (VK_RCONTROL, True),
        (VK_LCONTROL, False),
        (VK_CONTROL, False),
        (VK_RMENU, True),
        (VK_LMENU, False),
        (VK_MENU, False),
        (VK_RSHIFT, True),
        (VK_LSHIFT, False),
        (VK_SHIFT, False),
        (VK_LWIN, True),
        (VK_RWIN, True),
    ]
    ups = [
        _key_input(vk, key_up=True, extended=ext)
        for vk, ext in modifiers
        if user32.GetAsyncKeyState(vk) & 0x8000
    ]
    if ups:
        arr = (INPUT * len(ups))(*ups)
        user32.SendInput(len(ups), arr, ctypes.sizeof(INPUT))
        time.sleep(0.02)


def send_ctrl_v() -> None:
    """Inject a Ctrl+V keystroke via Win32 SendInput."""
    _release_active_modifiers()
    seq = (INPUT * 4)(
        _key_input(VK_CONTROL, key_up=False),
        _key_input(VK_V, key_up=False),
        _key_input(VK_V, key_up=True),
        _key_input(VK_CONTROL, key_up=True),
    )
    user32.SendInput(4, seq, ctypes.sizeof(INPUT))


def paste_text_at_cursor(
    text: str,
    restore_clipboard: bool = True,
    restore_delay_ms: int = 220,
) -> bool:
    """Put text onto the clipboard, send Ctrl+V, and optionally restore prior clipboard text."""
    if not text:
        return False

    previous = get_clipboard_text() if restore_clipboard else None
    if not set_clipboard_text(text):
        return False

    time.sleep(0.03)
    send_ctrl_v()

    if restore_clipboard and previous is not None:
        time.sleep(max(0.05, restore_delay_ms / 1000.0))
        set_clipboard_text(previous)
    return True
