"""Tests for Windows sign-in startup registration helpers."""

from __future__ import annotations

import uuid
import winreg

from whisper_lm.startup import (
    build_startup_command,
    is_windows_startup_enabled,
    set_windows_startup,
)


def test_build_startup_command_frozen_and_source(tmp_path) -> None:
    exe = tmp_path / "WhisperLM.exe"
    cmd_frozen = build_startup_command(executable=str(exe), frozen=True)
    assert cmd_frozen == f'"{exe.resolve()}" --startup'

    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    py = scripts / "python.exe"
    pyw = scripts / "pythonw.exe"
    py.write_bytes(b"")
    pyw.write_bytes(b"")
    cmd_source = build_startup_command(executable=str(py), frozen=False)
    assert cmd_source == f'"{pyw.resolve()}" -m whisper_lm --startup'


def test_windows_startup_registry_roundtrip() -> None:
    test_subkey = rf"Software\WhisperLM_Test_{uuid.uuid4().hex}"
    val_name = "WhisperLMTest"
    try:
        assert is_windows_startup_enabled(reg_path=test_subkey, value_name=val_name) is False

        ok = set_windows_startup(
            True,
            command=r'"C:\WhisperLM\WhisperLM.exe" --startup',
            reg_path=test_subkey,
            value_name=val_name,
        )
        assert ok is True
        assert is_windows_startup_enabled(reg_path=test_subkey, value_name=val_name) is True

        disabled = set_windows_startup(False, reg_path=test_subkey, value_name=val_name)
        assert disabled is False
        assert is_windows_startup_enabled(reg_path=test_subkey, value_name=val_name) is False

        # Idempotent delete when missing
        assert set_windows_startup(False, reg_path=test_subkey, value_name=val_name) is False
    finally:
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, test_subkey)
        except FileNotFoundError:
            pass
