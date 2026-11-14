"""First-run automatic model & binary bootstrapper for clean portable/source installs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable
import zipfile

import httpx

from whisper_flow.config import AppConfig, get_project_root

WHISPER_ZIP_URL = (
    "https://github.com/lemonade-sdk/whisper.cpp-builds/releases/download/"
    "v1.8.4/whisper-bin-x64-vulkan.zip"
)
LLAMA_ZIP_URL = (
    "https://github.com/ggml-org/llama.cpp/releases/download/"
    "b12374/llama-b12374-bin-win-vulkan-x64.zip"
)
SILERO_VAD_URL = (
    "https://github.com/snakers4/silero-vad/raw/master/src/silero_vad/data/silero_vad.onnx"
)
WHISPER_MODEL_URL = (
    "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo.bin"
)
LLAMA_MODEL_URL = (
    "https://huggingface.co/unsloth/Qwen3-4B-Instruct-2507-GGUF/resolve/main/"
    "Qwen3-4B-Instruct-2507-Q4_K_M.gguf"
)


@dataclass(frozen=True)
class AssetSpec:
    """Describes one downloadable runtime binary or model file."""

    label: str
    url: str
    target_path: Path
    min_bytes: int
    extract_dir: Path | None = None
    optional: bool = False


def get_required_assets(config: AppConfig, root: Path | None = None) -> list[AssetSpec]:
    """Build ordered list of required/optional local engines and AI models."""
    base = root if root is not None else get_project_root()
    needs_llm = (
        config.llm_cleanup_enabled
        or config.meeting_cleanup_enabled
        or config.meeting_summary_enabled
    )

    specs: list[AssetSpec] = [
        AssetSpec(
            label="Voice engine",
            url=WHISPER_ZIP_URL,
            target_path=config.resolve_path(config.whisper_server_path, base_dir=base),
            min_bytes=100_000,
            extract_dir=(base / "bin" / "whisper").resolve(),
        ),
        AssetSpec(
            label="Speech detector",
            url=SILERO_VAD_URL,
            target_path=config.resolve_path("models/silero_vad.onnx", base_dir=base),
            min_bytes=1_000_000,
        ),
        AssetSpec(
            label="Whisper speech model",
            url=WHISPER_MODEL_URL,
            target_path=config.resolve_path(config.whisper_model_path, base_dir=base),
            min_bytes=1_500_000_000,
        ),
        AssetSpec(
            label="AI runtime",
            url=LLAMA_ZIP_URL,
            target_path=config.resolve_path(config.llama_server_path, base_dir=base),
            min_bytes=100_000,
            extract_dir=(base / "bin" / "llama").resolve(),
            optional=True,
        ),
        AssetSpec(
            label="AI summary model",
            url=LLAMA_MODEL_URL,
            target_path=config.resolve_path(config.llama_model_path, base_dir=base),
            min_bytes=2_300_000_000,
            optional=not needs_llm,
        ),
    ]
    return specs


def missing_assets(config: AppConfig, root: Path | None = None) -> list[AssetSpec]:
    """Return missing or truncated runtime assets."""
    missing: list[AssetSpec] = []
    for spec in get_required_assets(config, root=root):
        if not spec.target_path.exists() or spec.target_path.stat().st_size < spec.min_bytes:
            missing.append(spec)
    return missing


def _download_file(url: str, dest: Path, label: str, on_progress: Callable[[str], None] | None) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    part_path = dest.with_name(dest.name + ".part")
    last_pct = -1

    with httpx.stream("GET", url, follow_redirects=True, timeout=120.0) as resp:
        resp.raise_for_status()
        total = int(resp.headers.get("content-length") or 0)
        received = 0
        with part_path.open("wb") as fh:
            for chunk in resp.iter_bytes(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                fh.write(chunk)
                received += len(chunk)
                if on_progress is not None and total > 0:
                    pct = min(99, int(received * 100 / total))
                    if pct != last_pct:
                        last_pct = pct
                        mb_done = received / (1024.0 * 1024.0)
                        mb_tot = total / (1024.0 * 1024.0)
                        on_progress(f"Downloading {label}... {pct}% ({mb_done:.0f}/{mb_tot:.0f} MB)")

    part_path.replace(dest)


def ensure_assets_downloaded(
    config: AppConfig,
    on_progress: Callable[[str], None] | None = None,
    root: Path | None = None,
) -> bool:
    """Download any missing binaries or AI models automatically on first launch."""
    todo = missing_assets(config, root=root)
    if not todo:
        return True

    base = root if root is not None else get_project_root()
    tmp_dir = base / "tmp_dl"
    try:
        for spec in todo:
            if spec.optional and not (
                config.llm_cleanup_enabled
                or config.meeting_cleanup_enabled
                or config.meeting_summary_enabled
            ):
                continue

            if on_progress is not None:
                on_progress(f"Preparing {spec.label}...")

            try:
                if spec.extract_dir is not None:
                    archive_path = tmp_dir / f"{spec.target_path.stem}.zip"
                    _download_file(spec.url, archive_path, spec.label, on_progress)
                    if on_progress is not None:
                        on_progress(f"Extracting {spec.label}...")
                    spec.extract_dir.mkdir(parents=True, exist_ok=True)
                    with zipfile.ZipFile(archive_path, "r") as zf:
                        zf.extractall(spec.extract_dir)
                    archive_path.unlink(missing_ok=True)
                else:
                    _download_file(spec.url, spec.target_path, spec.label, on_progress)
            except Exception:
                if not spec.optional:
                    raise
    finally:
        if tmp_dir.exists():
            for child in tmp_dir.glob("*"):
                child.unlink(missing_ok=True)
            try:
                tmp_dir.rmdir()
            except OSError:
                pass
    return True
