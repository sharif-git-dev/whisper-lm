"""Configuration management for Whisper Flow."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
import re
import sys
from typing import Literal

ArabicOutputMode = Literal["arabic_script", "transliterated"]
_HAS_ARABIC_RE = re.compile(r"[\u0600-\u06FF]")


def get_project_root() -> Path:
    """Return the app root directory (executable folder when frozen, repo root otherwise)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


DEFAULT_CUSTOM_DICTIONARY: list[str] = [
    "السلام عليكم",
    "وعليكم السلام",
    "إن شاء الله",
    "ما شاء الله",
    "الحمد لله",
    "جزاك الله خيرا",
    "سبحان الله",
    "بسم الله",
]


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
    always_on_pill: bool = True
    activation_sound_enabled: bool = True
    restore_clipboard: bool = True
    clipboard_restore_delay_ms: int = 220
    history_limit: int = 100
    history_dir: str = "history"

    # Meetings (Auto-detect & full recording + transcription)
    meeting_auto_detect: bool = True
    meeting_capture_system_audio: bool = True
    meeting_cleanup_enabled: bool = True
    meeting_summary_enabled: bool = True
    meeting_chunk_seconds: int = 20
    meetings_dir: str = "meetings"

    def resolve_path(self, relative_or_abs: str, base_dir: Path | None = None) -> Path:
        """Resolve a configured path relative to the project root (or given base_dir)."""
        path = Path(relative_or_abs)
        if path.is_absolute():
            return path
        root = base_dir if base_dir is not None else get_project_root()
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
            sample = ", ".join(terms[:8]) if terms else "السلام عليكم، إن شاء الله، الحمد لله"
            return (
                "This is mainly English dictation with occasional formal Modern Standard Arabic "
                f"words written in Arabic script, such as: {sample}."
            )
        sample = ", ".join(terms[:8]) if terms else "Assalamu Alaikum, Inshallah, Alhamdulillah"
        return (
            "This is English dictation with occasional formal Arabic terms written in Latin "
            f"letters, such as: {sample}."
        )

    @classmethod
    def load(cls, path: Path | None = None) -> "AppConfig":
        """Load configuration from JSON, falling back to defaults for missing keys."""
        target = path if path is not None else get_project_root() / "config.json"
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
        target = path if path is not None else get_project_root() / "config.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False), encoding="utf-8")
