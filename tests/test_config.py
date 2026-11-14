"""Tests for AppConfig persistence and Whisper prompt construction."""

from pathlib import Path
import sys

import pytest

from whisper_lm.config import AppConfig, get_project_root, migrate_legacy_data


def test_config_roundtrip(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.json"
    cfg = AppConfig(
        hotkey_hold="f9",
        arabic_output_mode="transliterated",
        custom_dictionary=["Inshallah", "Assalamu Alaikum"],
        always_on_pill=False,
    )
    cfg.save(cfg_path)

    loaded = AppConfig.load(cfg_path)
    assert loaded.hotkey_hold == "f9"
    assert loaded.arabic_output_mode == "transliterated"
    assert loaded.custom_dictionary == ["Inshallah", "Assalamu Alaikum"]
    assert loaded.always_on_pill is False


def test_whisper_prompt_respects_arabic_mode() -> None:
    cfg_en = AppConfig(whisper_language="en", custom_dictionary=["السلام عليكم", "Kubernetes"])
    prompt_en = cfg_en.build_whisper_prompt()
    assert "السلام" not in prompt_en
    assert "Arabic" not in prompt_en
    assert "Kubernetes" in prompt_en

    cfg_ar = AppConfig(
        whisper_language="auto",
        arabic_output_mode="arabic_script",
        custom_dictionary=["السلام عليكم"],
    )
    assert "Arabic script" in cfg_ar.build_whisper_prompt()
    assert "السلام عليكم" in cfg_ar.build_whisper_prompt()

    cfg_lat = AppConfig(
        whisper_language="auto",
        arabic_output_mode="transliterated",
        custom_dictionary=["Inshallah"],
    )
    assert "Latin" in cfg_lat.build_whisper_prompt()
    assert "Inshallah" in cfg_lat.build_whisper_prompt()


def test_meeting_config_defaults_and_backward_compat(tmp_path: Path) -> None:
    legacy_path = tmp_path / "legacy.json"
    legacy_path.write_text('{"hotkey_hold": "ctrl+win", "restore_clipboard": false}', encoding="utf-8")

    loaded = AppConfig.load(legacy_path)
    assert loaded.hotkey_hold == "ctrl+win"
    assert loaded.restore_clipboard is False
    assert loaded.always_on_pill is True
    assert loaded.meeting_auto_detect is True
    assert loaded.meeting_capture_system_audio is True
    assert loaded.meeting_save_audio is True
    assert loaded.meeting_cleanup_enabled is True
    assert loaded.meeting_chunk_seconds == 20


def test_get_project_root_frozen_and_source(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_exe = tmp_path / "portable" / "WhisperLM.exe"
    fake_exe.parent.mkdir(parents=True)
    fake_exe.write_bytes(b"")

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(fake_exe))
    assert get_project_root() == fake_exe.parent.resolve()

    monkeypatch.delattr(sys, "frozen", raising=False)
    root = get_project_root()
    assert (root / "src" / "whisper_lm" / "config.py").exists()


def test_migrate_legacy_data_preserves_existing_and_is_idempotent(tmp_path: Path) -> None:
    proj = tmp_path / "project"
    data = tmp_path / "appdata"
    (proj / "history").mkdir(parents=True)
    (proj / "meetings").mkdir(parents=True)
    (proj / "config.json").write_text('{"hotkey_hold": "f9"}', encoding="utf-8")
    (proj / "history" / "dictations.jsonl").write_text('{"id": "h1"}\n', encoding="utf-8")
    (proj / "meetings" / "m1.json").write_text('{"id": "m1"}', encoding="utf-8")

    assert migrate_legacy_data(proj, data) is True
    assert (data / "config.json").read_text(encoding="utf-8") == '{"hotkey_hold": "f9"}'
    assert (data / "history" / "dictations.jsonl").exists()
    assert (data / "meetings" / "m1.json").exists()

    # Empty dictionary keeps feature enabled without pre-seeded phrases
    assert AppConfig().custom_dictionary == []

    # Does not overwrite user edits already saved in persistent data_root
    (data / "config.json").write_text('{"hotkey_hold": "ctrl+win"}', encoding="utf-8")
    assert migrate_legacy_data(proj, data) is False
    assert (data / "config.json").read_text(encoding="utf-8") == '{"hotkey_hold": "ctrl+win"}'
