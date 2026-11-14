"""Configuration management for Whisper Flow."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import os
from pathlib import Path
import re
import shutil
import sys
from typing import Literal

ArabicOutputMode = Literal["arabic_script", "transliterated"]
_HAS_ARABIC_RE = re.compile(r"[\u0600-\u06FF]")


def get_project_root() -> Path:
    """Return the app root directory (executable folder when frozen, repo root otherwise)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def get_data_root() -> Path:
    """Return the persistent per-user data directory that survives app updates and uninstalls."""
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) if appdata else (Path.home() / "AppData" / "Roaming")
        return (base / "WhisperLM").resolve()
    return (Path.home() / ".whisper_lm").resolve()


def migrate_legacy_data(project_root: Path, data_root: Path) -> bool:
    """Copy legacy user data from the app/repo folder into data_root once without deleting originals."""
    if project_root.resolve() == data_root.resolve():
        return False

    migrated = False
    data_root.mkdir(parents=True, exist_ok=True)

    legacy_cfg = project_root / "config.json"
    target_cfg = data_root / "config.json"
    if legacy_cfg.exists() and not target_cfg.exists():
        shutil.copy2(legacy_cfg, target_cfg)
        migrated = True

    legacy_hist = project_root / "history" / "dictations.jsonl"
    target_hist = data_root / "history" / "dictations.jsonl"
    if legacy_hist.exists() and not target_hist.exists():
        target_hist.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(legacy_hist, target_hist)
        migrated = True

    legacy_meetings = project_root / "meetings"
    target_meetings = data_root / "meetings"
    if legacy_meetings.exists():
        target_meetings.mkdir(parents=True, exist_ok=True)
        for item in legacy_meetings.iterdir():
            if item.is_file() and item.suffix.lower() in {".json", ".md"}:
                dest = target_meetings / item.name
                if not dest.exists():
                    shutil.copy2(item, dest)
                    migrated = True

    return migrated


DEFAULT_CUSTOM_DICTIONARY: list[str] = []


@dataclass
class AppConfig:
    """Persisted application configuration."""

    # Hotkeys
    hotkey_hold: str = "ctrl+win"
    hotkey_toggle: str = "ctrl+shift+space"
    min_hold_ms: int = 180
    double_tap_ms: int = 380

    # Audio & VAD
    sample_rate: int = 16000
    input_device: int | None = None
    vad_enabled: bool = True
    vad_threshold: float = 0.28
    vad_min_speech_ms: int = 110
    vad_padding_ms: int = 320

    # ASR (whisper.cpp Vulkan)
    whisper_server_path: str = "bin/whisper/Release/whisper-server.exe"
    whisper_model_path: str = "models/ggml-large-v3-turbo.bin"
    whisper_host: str = "127.0.0.1"
    whisper_port: int = 8178
    whisper_device: int = 0
    whisper_language: str = "en"  # "en" (English only) or "auto" (English + Arabic)
    arabic_output_mode: ArabicOutputMode = "arabic_script"
    custom_dictionary: list[str] = field(default_factory=lambda: list(DEFAULT_CUSTOM_DICTIONARY))

    # LLM Cleanup (llama.cpp Vulkan)
    llm_cleanup_enabled: bool = True
    llama_server_path: str = "bin/llama/llama-server.exe"
    llama_model_path: str = "models/Qwen3-4B-Instruct-2507-Q4_K_M.gguf"
    llama_host: str = "127.0.0.1"
    llama_port: int = 8179
    llama_device: str = "Vulkan0"
    llama_ctx_size: int = 4096
    llm_timeout_seconds: float = 20.0

    # Injection & UX
    launch_at_startup: bool = True
    always_on_pill: bool = True
    activation_sound_enabled: bool = True
    restore_clipboard: bool = True
    clipboard_restore_delay_ms: int = 220
    history_limit: int = 100
    history_dir: str = "history"

    # Meetings (Auto-detect & full recording + transcription)
    meeting_auto_detect: bool = True
    meeting_capture_system_audio: bool = True
    meeting_save_audio: bool = True
    meeting_cleanup_enabled: bool = True
    meeting_summary_enabled: bool = True
    meeting_chunk_seconds: int = 20
    meetings_dir: str = "meetings"

    # Cloud Backup & Auto-Update
    cloud_backup_mode: str = "auto"  # "auto" | "folder" | "firebase" | "github" | "off"
    cloud_backup_folder: str = ""
    cloud_firebase_url: str = ""
    cloud_firebase_token: str = ""
    cloud_github_token: str = ""
    cloud_github_gist_id: str = ""
    auto_check_updates: bool = True
    skipped_update_version: str = ""

    def resolve_path(self, relative_or_abs: str, base_dir: Path | None = None) -> Path:
        """Resolve an app binary/model path relative to project_root (or given base_dir)."""
        path = Path(relative_or_abs)
        if path.is_absolute():
            return path
        root = base_dir if base_dir is not None else get_project_root()
        return (root / path).resolve()

    def resolve_data_path(self, relative_or_abs: str, base_dir: Path | None = None) -> Path:
        """Resolve a user-data path relative to persistent data_root (or given base_dir)."""
        path = Path(relative_or_abs)
        if path.is_absolute():
            return path
        root = base_dir if base_dir is not None else get_data_root()
        return (root / path).resolve()

    @property
    def arabic_enabled(self) -> bool:
        """Whether bilingual English+Arabic auto-detection is active."""
        return self.whisper_language != "en"

    def build_whisper_prompt(self) -> str:
        """Build Whisper initial_prompt tuned to English-only or bilingual English+Arabic mode."""
        terms = [t.strip() for t in self.custom_dictionary if t.strip()]
        if not self.arabic_enabled:
            en_terms = [t for t in terms if not _HAS_ARABIC_RE.search(t)]
            if en_terms:
                return (
                    "Accurate English dictation with proper capitalization and punctuation. "
                    f"Preferred terms: {', '.join(en_terms[:10])}."
                )
            return "Accurate English dictation with proper capitalization and punctuation."

        if self.arabic_output_mode == "arabic_script":
            base = (
                "This is mainly English dictation with occasional formal Modern Standard Arabic "
                "words written in Arabic script."
            )
            return f"{base} Preferred terms: {', '.join(terms[:8])}." if terms else base

        base = "This is English dictation with occasional formal Arabic terms written in Latin letters."
        return f"{base} Preferred terms: {', '.join(terms[:8])}." if terms else base

    @classmethod
    def load(cls, path: Path | None = None) -> "AppConfig":
        """Load configuration from JSON, falling back to defaults for missing keys."""
        target = path if path is not None else get_data_root() / "config.json"
        if not target.exists():
            cfg = cls()
            cfg.save(target)
            return cfg

        raw = json.loads(target.read_text(encoding="utf-8"))
        valid_fields = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in raw.items() if k in valid_fields}
        return cls(**filtered)

    def save(self, path: Path | None = None) -> None:
        """Persist configuration to JSON."""
        target = path if path is not None else get_data_root() / "config.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False), encoding="utf-8")
