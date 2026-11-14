"""Tests for conflict-safe BYO cloud backup, drive auto-detection, and restore."""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from whisper_lm.config import AppConfig
from whisper_lm.history import DictationStore, HistoryEntry
from whisper_lm.meetings import MeetingRecord, MeetingStore
from whisper_lm.sync import (
    CloudSyncManager,
    _FirebaseBackend,
    _GitHubGistBackend,
    detect_cloud_drives,
    merge_snapshots,
    resolve_auto_backup_drive,
)


def test_detect_cloud_drives_and_existing_backup_preference(tmp_path: Path) -> None:
    fake_home = tmp_path / "home"
    onedrive = fake_home / "OneDrive"
    gdrive = tmp_path / "GDrive" / "My Drive"
    onedrive.mkdir(parents=True)
    gdrive.mkdir(parents=True)

    # Create an existing WhisperLM backup specifically inside OneDrive
    (onedrive / "WhisperLM").mkdir(parents=True)
    (onedrive / "WhisperLM" / "whisperlm_backup.json").write_text("{}", encoding="utf-8")

    drives = detect_cloud_drives(
        home=fake_home,
        env={"OneDrive": str(onedrive)},
        drive_roots=[gdrive],
    )
    providers = [d.provider for d in drives]
    assert "google_drive" in providers
    assert "onedrive" in providers

    chosen = resolve_auto_backup_drive(drives)
    assert chosen is not None
    assert chosen.provider == "onedrive"


def test_merge_snapshots_preserves_both_devices_and_deletes() -> None:
    local = {
        "dictations": {
            "d1": {"id": "d1", "cleaned_text": "Local note", "created_at": "2026-11-14T08:00:00"},
            "d_del": {"id": "d_del", "cleaned_text": "To delete", "created_at": "2026-11-14T07:00:00"},
        },
        "deleted_dictation_ids": [],
        "meetings": {
            "m1": {
                "id": "m1",
                "title": "Sync",
                "raw_transcript": "raw",
                "cleaned_transcript": "cleaned",
                "summary": "",
                "duration_seconds": 30,
                "started_at": "2026-11-14 08:00",
            }
        },
        "deleted_meeting_ids": [],
        "custom_dictionary": ["WhisperLM", "Vulkan"],
    }
    remote = {
        "dictations": {
            "d2": {"id": "d2", "cleaned_text": "Other PC note", "created_at": "2026-11-14T08:05:00"},
        },
        "deleted_dictation_ids": ["d_del"],
        "meetings": {
            "m1": {
                "id": "m1",
                "title": "Sync",
                "raw_transcript": "raw",
                "cleaned_transcript": "cleaned",
                "summary": "Summary completed on PC B",
                "duration_seconds": 30,
                "started_at": "2026-11-14 08:00",
            }
        },
        "deleted_meeting_ids": [],
        "custom_dictionary": ["Vulkan", "Qwen3"],
    }

    merged = merge_snapshots(local, remote)
    assert set(merged["dictations"].keys()) == {"d1", "d2"}
    assert "d_del" not in merged["dictations"]
    assert merged["meetings"]["m1"]["summary"] == "Summary completed on PC B"
    assert merged["custom_dictionary"] == ["WhisperLM", "Vulkan", "Qwen3"]


def test_cloud_sync_folder_backup_and_new_pc_restore(tmp_path: Path) -> None:
    shared_cloud = tmp_path / "GoogleDrive"
    shared_cloud.mkdir()

    # PC A creates history, a meeting, and custom terms
    pc_a_data = tmp_path / "pc_a"
    cfg_a = AppConfig(
        cloud_backup_mode="folder",
        cloud_backup_folder=str(shared_cloud),
        custom_dictionary=["WhisperLM"],
    )
    hist_a = DictationStore(pc_a_data / "history")
    meet_a = MeetingStore(pc_a_data / "meetings")
    hist_a.append(HistoryEntry("09:00:00", "Code.exe", "hello world", "Hello world.", "en", id="d_100"))
    meet_a.save(
        MeetingRecord(
            id="20261114_0900_standup",
            title="Morning Standup",
            app_name="Google Meet",
            started_at="2026-11-14 09:00",
            duration_seconds=45,
            raw_transcript="we shipped v2",
            cleaned_transcript="We shipped v2.",
            summary="### Overview\nShipped v2.",
            status="completed",
        )
    )
    sync_a = CloudSyncManager(cfg_a, hist_a, meet_a, data_root=pc_a_data)
    res_a = sync_a.sync_now(drives=[])
    assert res_a.ok is True
    assert (shared_cloud / "WhisperLM" / "whisperlm_backup.json").exists()
    assert (shared_cloud / "WhisperLM" / "Meetings" / "20261114_0900_standup.md").exists()

    # Fresh PC B boots empty against the same shared folder and restores automatically
    pc_b_data = tmp_path / "pc_b"
    cfg_b = AppConfig(
        cloud_backup_mode="folder",
        cloud_backup_folder=str(shared_cloud),
        custom_dictionary=[],
    )
    hist_b = DictationStore(pc_b_data / "history")
    meet_b = MeetingStore(pc_b_data / "meetings")
    restored_flags: list[bool] = []
    sync_b = CloudSyncManager(
        cfg_b,
        hist_b,
        meet_b,
        data_root=pc_b_data,
        on_data_restored=lambda: restored_flags.append(True),
    )
    res_b = sync_b.sync_now(drives=[])
    assert res_b.ok is True
    assert res_b.restored_count == 2
    assert restored_flags == [True]
    assert [e.cleaned_text for e in hist_b.list_all()] == ["Hello world."]
    assert meet_b.list_all()[0].summary == "### Overview\nShipped v2."
    assert cfg_b.custom_dictionary == ["WhisperLM"]


def test_firebase_and_github_gist_backends_with_mock_http(monkeypatch) -> None:
    firebase_store: dict[str, object] = {}

    def fb_handler(request: httpx.Request) -> httpx.Response:
        assert "auth=sec-123" in str(request.url)
        if request.method == "GET":
            return httpx.Response(200, json=firebase_store.get("payload"))
        if request.method == "PUT":
            firebase_store["payload"] = json.loads(request.content.decode("utf-8"))
            return httpx.Response(200, json=firebase_store["payload"])
        return httpx.Response(405)

    orig_client = httpx.Client

    def make_fb_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(fb_handler)
        return orig_client(*args, **kwargs)

    monkeypatch.setattr("whisper_lm.sync.httpx.Client", make_fb_client)
    fb = _FirebaseBackend("my-db-default-rtdb.firebaseio.com", auth_token="sec-123")
    assert fb.read() is None
    fb.write({"dictations": {"a": {"id": "a", "cleaned_text": "Saved"}}})
    assert fb.read() == {"dictations": {"a": {"id": "a", "cleaned_text": "Saved"}}}

    gist_files: dict[str, str] = {}
    gist_ids: list[str] = []

    def gh_handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("Authorization") == "Bearer gh-tok"
        if request.method == "GET" and request.url.path == "/gists":
            return httpx.Response(200, json=[])
        if request.method == "POST" and request.url.path == "/gists":
            body = json.loads(request.content.decode("utf-8"))
            assert body["public"] is False
            gist_files["content"] = body["files"]["whisperlm_backup.json"]["content"]
            return httpx.Response(201, json={"id": "gist_42"})
        if request.method == "GET" and request.url.path == "/gists/gist_42":
            return httpx.Response(
                200,
                json={"id": "gist_42", "files": {"whisperlm_backup.json": {"content": gist_files["content"]}}},
            )
        return httpx.Response(404)

    monkeypatch.setattr(
        "whisper_lm.sync.httpx.Client",
        lambda *a, **kw: orig_client(*a, **{**kw, "transport": httpx.MockTransport(gh_handler)}),
    )
    gh = _GitHubGistBackend("gh-tok", on_gist_id=gist_ids.append)
    gh.write({"dictations": {"x": {"id": "x"}}})
    assert gist_ids == ["gist_42"]
    assert gh.read() == {"dictations": {"x": {"id": "x"}}}
