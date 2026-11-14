"""Windows sign-in startup registration helpers for WhisperLM."""

from __future__ import annotations

import logging
from pathlib import Path
import sys

RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
STARTUP_VALUE_NAME = "WhisperLM"

_logger = logging.getLogger("whisper_lm")


def build_startup_command(
    *,
    executable: str | None = None,
    frozen: bool | None = None,
) -> str:
    """Build the Windows Run command for either frozen WhisperLM.exe or source pythonw."""
    exe_path = Path(executable or sys.executable).resolve()
    is_frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    if is_frozen:
        return f'"{exe_path}" --startup'

    pythonw = exe_path.with_name("pythonw.exe")
    runner = pythonw if exe_path.name.lower() == "python.exe" and pythonw.exists() else exe_path
    return f'"{runner}" -m whisper_lm --startup'


def is_windows_startup_enabled(
    *,
    reg_path: str = RUN_KEY_PATH,
    value_name: str = STARTUP_VALUE_NAME,
) -> bool:
    """Return True if WhisperLM currently has a non-empty HKCU Run startup entry."""
    if sys.platform != "win32":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, reg_path, 0, winreg.KEY_READ) as key:
            value, _reg_type = winreg.QueryValueEx(key, value_name)
        return isinstance(value, str) and bool(value.strip())
    except OSError:
        return False


def set_windows_startup(
    enabled: bool,
    *,
    command: str | None = None,
    reg_path: str = RUN_KEY_PATH,
    value_name: str = STARTUP_VALUE_NAME,
) -> bool:
    """Register or remove WhisperLM in HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run."""
    if sys.platform != "win32":
        return False
    import winreg

    if enabled:
        cmd = command or build_startup_command()
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, reg_path) as key:
            winreg.SetValueEx(key, value_name, 0, winreg.REG_SZ, cmd)
        return True

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, reg_path, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, value_name)
    except FileNotFoundError:
        pass
    return False


def sync_windows_startup(
    enabled: bool,
    *,
    command: str | None = None,
    reg_path: str = RUN_KEY_PATH,
    value_name: str = STARTUP_VALUE_NAME,
) -> bool:
    """Best-effort sync of Windows Run startup registration without raising during app boot."""
    try:
        return set_windows_startup(
            enabled,
            command=command,
            reg_path=reg_path,
            value_name=value_name,
        )
    except OSError as exc:
        _logger.warning("Failed to update Windows startup entry: %s", exc)
        return False
