"""Tests for GitHub Releases update detection, SHA-256 verification, and installer launch."""

from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
import pytest

from whisper_lm.updater import (
    UpdateInfo,
    apply_update_and_relaunch,
    check_for_update,
    download_update,
    is_newer_version,
    parse_version,
)


def test_parse_and_compare_versions() -> None:
    assert parse_version("v0.2.0") == (0, 2, 0)
    assert is_newer_version("v0.2.0", "0.1.0") is True
    assert is_newer_version("0.2.1", "0.2.0") is True
    assert is_newer_version("v0.2.0", "0.2.0") is False
    assert is_newer_version("v0.1.9", "0.2.0") is False


def test_check_for_update_returns_installer_metadata() -> None:
    payload = {
        "tag_name": "v0.3.0",
        "body": "Adds seamless cloud backup and auto-updates.",
        "html_url": "https://github.com/sharif-git-dev/whisper-lm/releases/tag/v0.3.0",
        "assets": [
            {
                "name": "WhisperLM-Windows-x64.zip",
                "size": 140_000_000,
                "browser_download_url": "https://example.invalid/WhisperLM-Windows-x64.zip",
            },
            {
                "name": "WhisperLM-Setup.exe",
                "size": 80_000_000,
                "digest": "sha256:abcdef1234",
                "browser_download_url": "https://example.invalid/WhisperLM-Setup.exe",
            },
        ],
    }
    transport = httpx.MockTransport(lambda _req: httpx.Response(200, json=payload))
    with httpx.Client(transport=transport) as client:
        info = check_for_update("0.2.0", client=client)

    assert info is not None
    assert info.version == "0.3.0"
    assert info.asset_name == "WhisperLM-Setup.exe"
    assert info.sha256 == "abcdef1234"

    with httpx.Client(transport=transport) as same_client:
        assert check_for_update("0.3.0", client=same_client) is None


def test_download_update_verifies_sha256_and_launches_relaunch(monkeypatch, tmp_path: Path) -> None:
    fake_exe = b"MZ-dummy-setup-binary" * 100
    good_sha = hashlib.sha256(fake_exe).hexdigest()
    info = UpdateInfo(
        version="0.3.0",
        tag_name="v0.3.0",
        release_notes="",
        asset_name="WhisperLM-Setup.exe",
        download_url="https://example.invalid/WhisperLM-Setup.exe",
        size_bytes=len(fake_exe),
        sha256=good_sha,
    )

    transport = httpx.MockTransport(
        lambda _req: httpx.Response(
            200,
            headers={"content-length": str(len(fake_exe))},
            content=fake_exe,
        )
    )
    orig_stream = httpx.stream
    monkeypatch.setattr(
        "whisper_lm.updater.httpx.stream",
        lambda method, url, **kw: httpx.Client(transport=transport).stream(method, url, **kw),
    )

    pct_seen: list[int] = []
    out_path = download_update(info, dest_dir=tmp_path, on_progress=lambda p, _m: pct_seen.append(p))
    assert out_path.exists()
    assert out_path.read_bytes() == fake_exe
    assert 100 in pct_seen

    bad_info = UpdateInfo(
        version="0.3.0",
        tag_name="v0.3.0",
        release_notes="",
        asset_name="WhisperLM-Setup.exe",
        download_url="https://example.invalid/WhisperLM-Setup.exe",
        size_bytes=len(fake_exe),
        sha256="0" * 64,
    )
    with pytest.raises(ValueError, match="SHA-256"):
        download_update(bad_info, dest_dir=tmp_path)

    del orig_stream

    spawned: list[list[str]] = []
    monkeypatch.setattr("whisper_lm.updater.subprocess.Popen", lambda args, **_kw: spawned.append(args))
    assert apply_update_and_relaunch(out_path, is_frozen=True) is True
    assert spawned[0] == [str(out_path), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/RELAUNCH"]
