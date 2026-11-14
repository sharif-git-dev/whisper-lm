"""Tests for first-run runtime & model bootstrapper."""

from __future__ import annotations

import io
from pathlib import Path
import zipfile

from whisper_flow import bootstrap
from whisper_flow.config import AppConfig


def _make_fake_zip_bytes(filename: str, payload: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(filename, payload)
    return buf.getvalue()


class _FakeStreamResponse:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self.headers = {"content-length": str(len(data))}

    def __enter__(self) -> "_FakeStreamResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def raise_for_status(self) -> None:
        return None

    def iter_bytes(self, chunk_size: int = 1024):
        for idx in range(0, len(self._data), chunk_size):
            yield self._data[idx : idx + chunk_size]


def test_missing_assets_detects_absent_or_small_files(tmp_path: Path) -> None:
    cfg = AppConfig()
    missing = bootstrap.missing_assets(cfg, root=tmp_path)
    labels = [spec.label for spec in missing]
    assert "Voice engine" in labels
    assert "Speech detector" in labels
    assert "Whisper speech model" in labels
    assert "AI runtime" in labels
    assert "AI summary model" in labels


def test_ensure_assets_downloaded_streams_and_extracts(monkeypatch, tmp_path: Path) -> None:
    whisper_zip = _make_fake_zip_bytes("whisper-server.exe", b"W" * 1024)
    vad_bytes = b"V" * 1024
    whisper_model_bytes = b"M" * 2048

    urls = {
        bootstrap.WHISPER_ZIP_URL: whisper_zip,
        bootstrap.SILERO_VAD_URL: vad_bytes,
        bootstrap.WHISPER_MODEL_URL: whisper_model_bytes,
    }

    def fake_stream(method: str, url: str, follow_redirects: bool = True, timeout: float = 120.0):
        assert method == "GET"
        return _FakeStreamResponse(urls[url])

    monkeypatch.setattr(bootstrap.httpx, "stream", fake_stream)
    monkeypatch.setattr(
        bootstrap,
        "get_required_assets",
        lambda config, root=None: [
            bootstrap.AssetSpec(
                label="Voice engine",
                url=bootstrap.WHISPER_ZIP_URL,
                target_path=(tmp_path / "bin" / "whisper" / "whisper-server.exe"),
                min_bytes=512,
                extract_dir=(tmp_path / "bin" / "whisper"),
            ),
            bootstrap.AssetSpec(
                label="Speech detector",
                url=bootstrap.SILERO_VAD_URL,
                target_path=(tmp_path / "models" / "silero_vad.onnx"),
                min_bytes=512,
            ),
            bootstrap.AssetSpec(
                label="Whisper speech model",
                url=bootstrap.WHISPER_MODEL_URL,
                target_path=(tmp_path / "models" / "ggml-large-v3-turbo.bin"),
                min_bytes=1024,
            ),
            bootstrap.AssetSpec(
                label="AI summary model",
                url=bootstrap.LLAMA_MODEL_URL,
                target_path=(tmp_path / "models" / "qwen.gguf"),
                min_bytes=1024,
                optional=True,
            ),
        ],
    )

    cfg = AppConfig(
        llm_cleanup_enabled=False,
        meeting_cleanup_enabled=False,
        meeting_summary_enabled=False,
    )
    progress: list[str] = []
    ok = bootstrap.ensure_assets_downloaded(cfg, on_progress=progress.append, root=tmp_path)

    assert ok is True
    assert (tmp_path / "bin" / "whisper" / "whisper-server.exe").read_bytes() == b"W" * 1024
    assert (tmp_path / "models" / "silero_vad.onnx").read_bytes() == vad_bytes
    assert (tmp_path / "models" / "ggml-large-v3-turbo.bin").read_bytes() == whisper_model_bytes
    assert not (tmp_path / "models" / "qwen.gguf").exists()
    assert not (tmp_path / "tmp_dl").exists()
    assert any("Downloading Whisper speech model" in msg for msg in progress)
