"""Tests for LLM cleanup prompting and response parsing."""

from whisper_flow.cleanup import (
    LlmCleanupClient,
    build_cleanup_system_prompt,
    extract_transcript_only,
    split_transcript_chunks,
)
from whisper_flow.config import AppConfig
from whisper_flow.injector import WindowContext


def test_extract_transcript_only_strips_wrapping_artifacts() -> None:
    assert extract_transcript_only('  "Hello world."  ') == "Hello world."
    assert extract_transcript_only("<transcript>Hi there</transcript>") == "Hi there"
    assert extract_transcript_only("```text\nCleaned text.\n```") == "Cleaned text."


def test_system_prompt_includes_context_and_mode() -> None:
    cfg_en = AppConfig(whisper_language="en", custom_dictionary=["إن شاء الله", "PySide6"])
    prompt_en = build_cleanup_system_prompt(cfg_en)
    assert "ENGLISH MODE" in prompt_en
    assert "PySide6" in prompt_en
    assert "إن شاء الله" not in prompt_en

    cfg = AppConfig(whisper_language="auto", arabic_output_mode="arabic_script", custom_dictionary=["إن شاء الله"])
    ctx = WindowContext(hwnd=1, app_name="Code.exe", window_title="notes.md")
    prompt = build_cleanup_system_prompt(cfg, ctx)

    assert "Code.exe" in prompt
    assert "notes.md" in prompt
    assert "إن شاء الله" in prompt
    assert "NEVER translate English words" in prompt
    assert "keep BOTH words" in prompt


def test_cleanup_falls_back_to_raw_when_server_unreachable() -> None:
    cfg = AppConfig(llama_port=59999, llm_timeout_seconds=0.3)
    client = LlmCleanupClient(cfg)
    try:
        assert client.clean("hello world") == "hello world"
        assert client.clean("   ...   ") == ""
    finally:
        client.close()


def test_split_transcript_chunks_respects_bound_and_cleans() -> None:
    p1 = "First paragraph about architecture. " * 25
    p2 = "Second paragraph about release dates. " * 25
    chunks = split_transcript_chunks(f"{p1}\n\n{p2}", max_chars=1100)
    assert len(chunks) == 2
    assert all(len(c) <= 1100 for c in chunks)

    cfg = AppConfig(llama_port=59999, llm_timeout_seconds=0.2)
    client = LlmCleanupClient(cfg)
    try:
        fallback = client.clean_meeting_transcript("Hello team.\n\nSecond point.", app_name="Zoom")
        assert fallback == "Hello team.\n\nSecond point."
    finally:
        client.close()


def test_clean_meeting_transcript_preserves_speaker_turns(monkeypatch) -> None:
    import httpx
    from whisper_flow.cleanup import LlmCleanupClient
    from whisper_flow.config import AppConfig

    raw = "[00:04] You: um let's ship it\n\n[00:12] Speaker 1: uh sounds good"

    def _fake_post(_self, _url, **_kwargs):
        class _Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"choices": [{"message": {"content": "[00:04] You: Let's ship it.\n\n[00:12] Speaker 1: Sounds good."}}]}

        return _Resp()

    monkeypatch.setattr(httpx.Client, "post", _fake_post)
    client = LlmCleanupClient(AppConfig(llm_cleanup_enabled=True))
    cleaned = client.clean_meeting_transcript(raw, app_name="Zoom")
    assert "[00:04] You:" in cleaned
    assert "[00:12] Speaker 1:" in cleaned


def test_clean_meeting_transcript_falls_back_if_llm_drops_speakers(monkeypatch) -> None:
    import httpx
    from whisper_flow.cleanup import LlmCleanupClient
    from whisper_flow.config import AppConfig

    raw = "[00:04] You: let's ship it\n\n[00:12] Speaker 1: sounds good"

    def _fake_post(_self, _url, **_kwargs):
        class _Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"choices": [{"message": {"content": "Let's ship it. Sounds good."}}]}

        return _Resp()

    monkeypatch.setattr(httpx.Client, "post", _fake_post)
    client = LlmCleanupClient(AppConfig(llm_cleanup_enabled=True))
    cleaned = client.clean_meeting_transcript(raw, app_name="Zoom")
    assert cleaned == raw


def test_summarize_meeting_transcript_single_and_map_reduce(monkeypatch) -> None:
    import httpx
    from whisper_flow.cleanup import LlmCleanupClient, format_clean_summary
    from whisper_flow.config import AppConfig

    assert format_clean_summary("<think>hmm</think>\n## Overview\n- Synced on v2.") == "Overview\n• Synced on v2."

    prompts_seen: list[str] = []

    def _fake_post(_self, _url, **kwargs):
        payload = kwargs["json"]
        prompts_seen.append(payload["messages"][0]["content"])
        content = payload["messages"][1]["content"]

        class _Resp:
            def raise_for_status(self):
                pass

            def json(self):
                if "Part 1:" in content:
                    return {"choices": [{"message": {"content": "## Overview\nMerged overview.\n\n## Action Items\n- [ ] You: Deploy"}}]}
                if "<segment " in content:
                    return {"choices": [{"message": {"content": "- Segment key point"}}]}
                return {"choices": [{"message": {"content": "<think>ok</think>\n## Overview\nQuick sync on release."}}]}

        return _Resp()

    monkeypatch.setattr(httpx.Client, "post", _fake_post)
    client = LlmCleanupClient(AppConfig())

    short_summary = client.summarize_meeting_transcript("[00:02] You: Release is ready.", title="Standup")
    assert short_summary == "Overview\nQuick sync on release."

    long_transcript = "\n\n".join(f"[{i:02d}:00] You: Point number {i} is " + ("word " * 120) for i in range(12))
    long_summary = client.summarize_meeting_transcript(long_transcript, title="Deep Dive")
    assert "Merged overview." in long_summary
    assert len(prompts_seen) >= 4
