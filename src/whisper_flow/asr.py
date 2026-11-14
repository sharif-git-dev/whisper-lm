"""HTTP client for whisper-server.exe (/inference)."""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from whisper_flow.config import AppConfig


@dataclass(frozen=True)
class TranscriptionResult:
    """Result returned by the ASR backend."""

    text: str
    detected_language: str | None = None


class WhisperServerClient:
    """Client for local whisper.cpp HTTP inference server."""

    def __init__(self, config: AppConfig, timeout_seconds: float = 30.0) -> None:
        self.config = config
        self.base_url = f"http://{config.whisper_host}:{config.whisper_port}"
        self._client = httpx.Client(timeout=timeout_seconds)

    def close(self) -> None:
        self._client.close()

    def transcribe(
        self,
        wav_bytes: bytes,
        language: str | None = None,
        prompt: str | None = None,
    ) -> TranscriptionResult:
        """Send 16 kHz mono WAV bytes to whisper-server and return transcribed text."""
        lang = language if language is not None else self.config.whisper_language
        initial_prompt = prompt if prompt is not None else self.config.build_whisper_prompt()

        data = {
            "temperature": "0.0",
            "temperature_inc": "0.2",
            "beam_size": "5",
            "best_of": "5",
            "response_format": "verbose_json",
            "language": lang,
            "prompt": initial_prompt,
        }
        files = {
            "file": ("dictation.wav", wav_bytes, "audio/wav"),
        }

        resp = self._client.post(f"{self.base_url}/inference", data=data, files=files)
        resp.raise_for_status()
        payload = resp.json()

        raw_text = str(payload.get("text", "")).strip()
        if raw_text and self._looks_like_silence_hallucination(raw_text, payload):
            raw_text = ""
        detected = payload.get("language")
        return TranscriptionResult(
            text=raw_text,
            detected_language=str(detected) if detected else None,
        )

    @staticmethod
    def _looks_like_silence_hallucination(text: str, payload: dict[str, object]) -> bool:
        """Drop classic Whisper empty-audio phantom tokens when decoder confidence is very low."""
        norm = " ".join(text.lower().strip(".,!?\"' ").split())
        phantoms = {"you", "thank you", "thanks for watching", "bye", "subtitles by the amara org community"}
        if norm not in phantoms:
            return False
        segments = payload.get("segments")
        if not isinstance(segments, list) or not segments:
            return False
        first = segments[0]
        if not isinstance(first, dict):
            return False
        avg_lp = first.get("avg_logprob")
        words = first.get("words")
        word_prob = words[0].get("probability") if isinstance(words, list) and words and isinstance(words[0], dict) else None
        low_lp = isinstance(avg_lp, (int, float)) and float(avg_lp) < -0.50
        low_wp = isinstance(word_prob, (int, float)) and float(word_prob) < 0.38
        return low_lp or low_wp
