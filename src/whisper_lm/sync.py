"""Conflict-safe BYO cloud backup and auto-detected drive sync for WhisperLM."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import string
import sys
import threading
from typing import Any, Callable, Mapping

import httpx

from whisper_lm.config import AppConfig, get_data_root
from whisper_lm.history import DictationStore, HistoryEntry
from whisper_lm.meetings import MeetingRecord, MeetingStore

_BACKUP_FILENAME = "whisperlm_backup.json"
_GIST_DESCRIPTION = "WhisperLM Cloud Backup"
_TOMBSTONE_FILENAME = "sync_tombstones.json"


@dataclass(frozen=True)
class CloudDriveInfo:
    """One locally mounted desktop cloud storage folder detected on this PC."""

    provider: str
    display_name: str
    root_path: Path

    @property
    def backup_path(self) -> Path:
        return (self.root_path / "WhisperLM").resolve()

    @property
    def has_existing_backup(self) -> bool:
        return (self.backup_path / _BACKUP_FILENAME).exists()


@dataclass(frozen=True)
class SyncResult:
    """Outcome summary from a background or manual backup sync pass."""

    ok: bool
    provider_label: str
    dictations_count: int = 0
    meetings_count: int = 0
    restored_count: int = 0
    detail: str = ""


def _candidate_drive_roots() -> list[Path]:
    if sys.platform != "win32":
        return []
    roots: list[Path] = []
    for letter in string.ascii_uppercase[3:]:  # D: through Z:
        candidate = Path(f"{letter}:\\My Drive")
        try:
            if candidate.exists() and candidate.is_dir():
                roots.append(candidate)
        except OSError:
            continue
    return roots


def detect_cloud_drives(
    *,
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
    drive_roots: list[Path] | None = None,
) -> list[CloudDriveInfo]:
    """Detect mounted Google Drive, OneDrive, Dropbox, and iCloud Drive folders cleanly."""
    user_home = (home or Path.home()).resolve()
    env_map = env if env is not None else os.environ

    candidates: list[tuple[str, str, Path]] = []
    for gd_root in (drive_roots if drive_roots is not None else _candidate_drive_roots()):
        candidates.append(("google_drive", "Google Drive", gd_root))
    for rel in ("Google Drive/My Drive", "Google Drive", "My Drive"):
        candidates.append(("google_drive", "Google Drive", user_home / rel))

    for var in ("OneDriveConsumer", "OneDrive", "OneDriveCommercial"):
        val = (env_map.get(var) or "").strip()
        if val:
            candidates.append(("onedrive", "OneDrive", Path(val)))
    candidates.append(("onedrive", "OneDrive", user_home / "OneDrive"))
    candidates.append(("dropbox", "Dropbox", user_home / "Dropbox"))
    candidates.append(("icloud", "iCloud Drive", user_home / "iCloudDrive"))

    seen: set[str] = set()
    found: list[CloudDriveInfo] = []
    for provider, display_name, path in candidates:
        try:
            if not path.exists() or not path.is_dir():
                continue
            resolved = path.resolve()
        except OSError:
            continue
        key = str(resolved).lower()
        if key in seen:
            continue
        seen.add(key)
        found.append(CloudDriveInfo(provider=provider, display_name=display_name, root_path=resolved))
    return found


def resolve_auto_backup_drive(drives: list[CloudDriveInfo]) -> CloudDriveInfo | None:
    """Pick a cloud drive automatically, preferring one that already contains WhisperLM backups."""
    for drive in drives:
        if drive.has_existing_backup:
            return drive
    return drives[0] if drives else None


def describe_cloud_target(config: AppConfig, drives: list[CloudDriveInfo] | None = None) -> str:
    """Return a concise human-readable label describing the active backup destination."""
    mode = (config.cloud_backup_mode or "auto").lower()
    if mode == "off":
        return "Off (Local AppData only)"
    if mode == "firebase":
        return "Own Firebase Realtime DB" if config.cloud_firebase_url.strip() else "Firebase (URL missing)"
    if mode == "github":
        return "GitHub Private Gist" if config.cloud_github_token.strip() else "GitHub Gist (Token missing)"
    if mode == "folder":
        folder = config.cloud_backup_folder.strip()
        if not folder:
            return "Custom Folder (not chosen)"
        p = Path(folder)
        return f"Folder: {p.parent.name or p.name}" if p.name.lower() == "whisperlm" else f"Folder: {p.name}"

    detected = drives if drives is not None else detect_cloud_drives()
    chosen = resolve_auto_backup_drive(detected)
    if chosen is not None:
        return f"Auto: {chosen.display_name}"
    return "Local AppData only (no drive detected)"


def _meeting_score(raw: Mapping[str, Any]) -> tuple[int, int, int, int, str]:
    return (
        1 if str(raw.get("summary") or "").strip() else 0,
        len(str(raw.get("cleaned_transcript") or "").strip()),
        len(str(raw.get("raw_transcript") or "").strip()),
        int(raw.get("duration_seconds") or 0),
        str(raw.get("started_at") or ""),
    )


def merge_snapshots(local_snap: Mapping[str, Any], remote_snap: Mapping[str, Any]) -> dict[str, Any]:
    """Merge local and remote backup snapshots deterministically without clobbering edits."""
    del_dict = set(str(x) for x in local_snap.get("deleted_dictation_ids") or ())
    del_dict.update(str(x) for x in remote_snap.get("deleted_dictation_ids") or ())

    del_meet = set(str(x) for x in local_snap.get("deleted_meeting_ids") or ())
    del_meet.update(str(x) for x in remote_snap.get("deleted_meeting_ids") or ())

    del_terms = set(str(x).strip() for x in local_snap.get("deleted_dictionary_terms") or () if str(x).strip())
    del_terms.update(str(x).strip() for x in remote_snap.get("deleted_dictionary_terms") or () if str(x).strip())

    merged_dict: dict[str, dict[str, Any]] = {}
    for src in (remote_snap.get("dictations") or {}, local_snap.get("dictations") or {}):
        if not isinstance(src, Mapping):
            continue
        for entry_id, payload in src.items():
            eid = str(entry_id)
            if eid in del_dict or not isinstance(payload, Mapping):
                continue
            item = dict(payload)
            item.setdefault("id", eid)
            existing = merged_dict.get(eid)
            if existing is None:
                merged_dict[eid] = item
            else:
                cur_key = (len(str(existing.get("cleaned_text") or "")), str(existing.get("created_at") or ""))
                new_key = (len(str(item.get("cleaned_text") or "")), str(item.get("created_at") or ""))
                if new_key >= cur_key:
                    merged_dict[eid] = item

    merged_meetings: dict[str, dict[str, Any]] = {}
    for src in (remote_snap.get("meetings") or {}, local_snap.get("meetings") or {}):
        if not isinstance(src, Mapping):
            continue
        for meeting_id, payload in src.items():
            mid = str(meeting_id)
            if mid in del_meet or not isinstance(payload, Mapping):
                continue
            item = dict(payload)
            item.setdefault("id", mid)
            existing = merged_meetings.get(mid)
            if existing is None:
                merged_meetings[mid] = item
            else:
                kept_audio = str(item.get("audio_file") or existing.get("audio_file") or "").strip()
                winner = item if _meeting_score(item) >= _meeting_score(existing) else existing
                if kept_audio and not str(winner.get("audio_file") or "").strip():
                    winner = dict(winner)
                    winner["audio_file"] = kept_audio
                merged_meetings[mid] = winner

    seen_terms: set[str] = set()
    merged_terms: list[str] = []
    for lst in (local_snap.get("custom_dictionary") or [], remote_snap.get("custom_dictionary") or []):
        for term in lst:
            t = str(term).strip()
            if t and t not in del_terms and t not in seen_terms:
                seen_terms.add(t)
                merged_terms.append(t)

    return {
        "schema_version": 1,
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "dictations": merged_dict,
        "deleted_dictation_ids": sorted(del_dict),
        "meetings": merged_meetings,
        "deleted_meeting_ids": sorted(del_meet),
        "custom_dictionary": merged_terms,
        "deleted_dictionary_terms": sorted(del_terms),
    }


def _snapshot_content_equal(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    keys = (
        "dictations",
        "deleted_dictation_ids",
        "meetings",
        "deleted_meeting_ids",
        "custom_dictionary",
        "deleted_dictionary_terms",
    )
    return all((a.get(k) or []) == (b.get(k) or []) if k != "dictations" and k != "meetings" else a.get(k) == b.get(k) for k in keys)


class _FolderBackend:
    """Stores merged JSON snapshot and human-readable Markdown notes in a synced folder."""

    def __init__(self, target_dir: Path, label: str) -> None:
        self.target_dir = target_dir
        self.label = label
        self.snapshot_file = target_dir / _BACKUP_FILENAME

    def read(self) -> dict[str, Any] | None:
        if not self.snapshot_file.exists():
            return None
        try:
            raw = json.loads(self.snapshot_file.read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else None
        except Exception:
            return None

    def write(self, snapshot: Mapping[str, Any]) -> None:
        self.target_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.snapshot_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.snapshot_file)

        meetings = snapshot.get("meetings") or {}
        if isinstance(meetings, Mapping) and meetings:
            notes_dir = self.target_dir / "Meetings"
            notes_dir.mkdir(parents=True, exist_ok=True)
            for mid, raw in meetings.items():
                if not isinstance(raw, Mapping):
                    continue
                title = str(raw.get("title") or "Meeting")
                body = str(raw.get("cleaned_transcript") or raw.get("raw_transcript") or "_No speech captured._")
                summary = str(raw.get("summary") or "").strip()
                parts = [f"# {title}", ""]
                if summary:
                    parts.extend(["## Summary", "", summary, ""])
                parts.extend(["## Transcript", "", body, ""])
                md_tmp = (notes_dir / f"{mid}.md.tmp")
                md_tmp.write_text("\n".join(parts), encoding="utf-8")
                md_tmp.replace(notes_dir / f"{mid}.md")


class _FirebaseBackend:
    """Stores WhisperLM snapshot in the user's own Firebase Realtime Database via REST API."""

    def __init__(self, base_url: str, auth_token: str = "") -> None:
        clean_url = base_url.strip().rstrip("/")
        if clean_url and not clean_url.startswith(("http://", "https://")):
            clean_url = f"https://{clean_url}"
        self.endpoint = f"{clean_url}/whisperlm/backup.json"
        self.token = auth_token.strip()
        self.label = "Own Firebase"

    def _params(self) -> dict[str, str]:
        return {"auth": self.token} if self.token else {}

    def read(self) -> dict[str, Any] | None:
        with httpx.Client(timeout=12.0, follow_redirects=True) as client:
            resp = client.get(self.endpoint, params=self._params())
            resp.raise_for_status()
            if not resp.content or not resp.content.strip():
                return None
            data = resp.json()
            return data if isinstance(data, dict) else None

    def write(self, snapshot: Mapping[str, Any]) -> None:
        with httpx.Client(timeout=15.0, follow_redirects=True) as client:
            resp = client.put(self.endpoint, params=self._params(), json=snapshot)
            resp.raise_for_status()


class _GitHubGistBackend:
    """Stores WhisperLM snapshot in a single private GitHub Gist owned by the user."""

    def __init__(
        self,
        token: str,
        gist_id: str = "",
        on_gist_id: Callable[[str], None] | None = None,
    ) -> None:
        self.token = token.strip()
        self.gist_id = gist_id.strip()
        self._on_gist_id = on_gist_id
        self.label = "GitHub Private Gist"

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
        }

    def _discover_gist_id(self, client: httpx.Client) -> str:
        if self.gist_id:
            return self.gist_id
        resp = client.get("https://api.github.com/gists", headers=self._headers(), params={"per_page": 30})
        resp.raise_for_status()
        for item in resp.json():
            if (
                isinstance(item, Mapping)
                and item.get("description") == _GIST_DESCRIPTION
                and _BACKUP_FILENAME in (item.get("files") or {})
            ):
                self.gist_id = str(item["id"])
                if self._on_gist_id is not None:
                    self._on_gist_id(self.gist_id)
                return self.gist_id
        return ""

    def read(self) -> dict[str, Any] | None:
        with httpx.Client(timeout=12.0, follow_redirects=True) as client:
            gid = self._discover_gist_id(client)
            if not gid:
                return None
            resp = client.get(f"https://api.github.com/gists/{gid}", headers=self._headers())
            if resp.status_code == 404:
                self.gist_id = ""
                return None
            resp.raise_for_status()
            files = resp.json().get("files") or {}
            entry = files.get(_BACKUP_FILENAME) or {}
            content = str(entry.get("content") or "").strip()
            if not content:
                return None
            raw = json.loads(content)
            return raw if isinstance(raw, dict) else None

    def write(self, snapshot: Mapping[str, Any]) -> None:
        payload = {
            "description": _GIST_DESCRIPTION,
            "public": False,
            "files": {
                _BACKUP_FILENAME: {
                    "content": json.dumps(snapshot, indent=2, ensure_ascii=False),
                }
            },
        }
        with httpx.Client(timeout=15.0, follow_redirects=True) as client:
            gid = self._discover_gist_id(client)
            if gid:
                resp = client.patch(f"https://api.github.com/gists/{gid}", headers=self._headers(), json=payload)
            else:
                resp = client.post("https://api.github.com/gists", headers=self._headers(), json=payload)
            resp.raise_for_status()
            created_id = str(resp.json().get("id") or "")
            if created_id and created_id != self.gist_id:
                self.gist_id = created_id
                if self._on_gist_id is not None:
                    self._on_gist_id(created_id)


class CloudSyncManager:
    """Thread-safe local-first cloud backup and cross-PC restore manager."""

    def __init__(
        self,
        config: AppConfig,
        history_store: DictationStore,
        meeting_store: MeetingStore,
        data_root: Path | None = None,
        on_config_updated: Callable[[AppConfig], None] | None = None,
        on_data_restored: Callable[[], None] | None = None,
        on_status: Callable[[SyncResult], None] | None = None,
    ) -> None:
        self.config = config
        self.history_store = history_store
        self.meeting_store = meeting_store
        self.data_root = data_root if data_root is not None else get_data_root()
        self._on_config_updated = on_config_updated
        self._on_data_restored = on_data_restored
        self._on_status = on_status
        self._sync_lock = threading.Lock()
        self._tombstone_path = self.data_root / _TOMBSTONE_FILENAME

    def update_config(self, config: AppConfig) -> None:
        self.config = config

    def _load_tombstones(self) -> tuple[list[str], list[str], list[str]]:
        if not self._tombstone_path.exists():
            return [], [], []
        try:
            raw = json.loads(self._tombstone_path.read_text(encoding="utf-8"))
            d = [str(x) for x in raw.get("dictations") or []]
            m = [str(x) for x in raw.get("meetings") or []]
            t = [str(x) for x in raw.get("dictionary_terms") or []]
            return d, m, t
        except Exception:
            return [], [], []

    def _save_tombstones(
        self,
        del_dict: list[str],
        del_meet: list[str],
        del_terms: list[str] | None = None,
    ) -> None:
        self.data_root.mkdir(parents=True, exist_ok=True)
        payload = {
            "dictations": sorted(set(del_dict)),
            "meetings": sorted(set(del_meet)),
            "dictionary_terms": sorted(set(del_terms or [])),
        }
        self._tombstone_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def record_deleted_dictation(self, entry_id: str) -> None:
        if not entry_id:
            return
        d, m, t = self._load_tombstones()
        if entry_id not in d:
            d.append(entry_id)
            self._save_tombstones(d, m, t)

    def record_deleted_meeting(self, meeting_id: str) -> None:
        if not meeting_id:
            return
        d, m, t = self._load_tombstones()
        if meeting_id not in m:
            m.append(meeting_id)
            self._save_tombstones(d, m, t)

    def record_dictionary_diff(self, old_terms: list[str], new_terms: list[str]) -> None:
        """Track deleted or re-added dictionary terms so removals stay removed across sync."""
        old_set = {x.strip() for x in old_terms if x.strip()}
        new_set = {x.strip() for x in new_terms if x.strip()}
        if old_set == new_set:
            return
        d, m, del_terms = self._load_tombstones()
        tomb_set = set(del_terms)
        tomb_set.update(old_set - new_set)
        tomb_set.difference_update(new_set)
        self._save_tombstones(d, m, sorted(tomb_set))

    def _build_local_snapshot(self) -> dict[str, Any]:
        del_dict, del_meet, del_terms = self._load_tombstones()
        dict_map = {item.id: asdict(item) for item in self.history_store.list_all()}
        meet_map = {rec.id: asdict(rec) for rec in self.meeting_store.list_all()}
        return {
            "schema_version": 1,
            "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "dictations": dict_map,
            "deleted_dictation_ids": sorted(set(del_dict)),
            "meetings": meet_map,
            "deleted_meeting_ids": sorted(set(del_meet)),
            "custom_dictionary": list(self.config.custom_dictionary),
            "deleted_dictionary_terms": sorted(set(del_terms)),
        }

    def _apply_merged_local(self, local_snap: Mapping[str, Any], merged: Mapping[str, Any]) -> int:
        restored = 0
        self._save_tombstones(
            list(merged.get("deleted_dictation_ids") or []),
            list(merged.get("deleted_meeting_ids") or []),
            list(merged.get("deleted_dictionary_terms") or []),
        )

        valid_hist = set(HistoryEntry.__dataclass_fields__)
        local_dict_ids = set(local_snap.get("dictations") or {})
        merged_dict = merged.get("dictations") or {}
        if merged_dict != (local_snap.get("dictations") or {}):
            entries: list[HistoryEntry] = []
            for eid, raw in merged_dict.items():
                filtered = {k: v for k, v in raw.items() if k in valid_hist}
                entries.append(HistoryEntry(**filtered))
                if eid not in local_dict_ids:
                    restored += 1
            self.history_store.replace_all(entries)

        valid_meet = set(MeetingRecord.__dataclass_fields__)
        local_meetings = local_snap.get("meetings") or {}
        merged_meetings = merged.get("meetings") or {}
        for mid in set(local_meetings) - set(merged_meetings):
            self.meeting_store.delete(str(mid))
        for mid, raw in merged_meetings.items():
            if raw != local_meetings.get(mid):
                filtered = {k: v for k, v in raw.items() if k in valid_meet}
                self.meeting_store.save(MeetingRecord(**filtered))
                if mid not in local_meetings:
                    restored += 1

        merged_terms = list(merged.get("custom_dictionary") or [])
        if merged_terms != list(self.config.custom_dictionary):
            self.config.custom_dictionary = merged_terms
            if self._on_config_updated is not None:
                self._on_config_updated(self.config)

        if restored > 0 or merged_meetings != local_meetings or merged_dict != (local_snap.get("dictations") or {}):
            if self._on_data_restored is not None:
                self._on_data_restored()
        return restored

    def _resolve_backend(self, drives: list[CloudDriveInfo] | None = None):
        mode = (self.config.cloud_backup_mode or "auto").lower()
        if mode == "off":
            return None
        if mode == "firebase":
            url = self.config.cloud_firebase_url.strip()
            return _FirebaseBackend(url, self.config.cloud_firebase_token) if url else None
        if mode == "github":
            token = self.config.cloud_github_token.strip()
            if not token:
                return None

            def _save_gist_id(new_id: str) -> None:
                if new_id and new_id != self.config.cloud_github_gist_id:
                    self.config.cloud_github_gist_id = new_id
                    if self._on_config_updated is not None:
                        self._on_config_updated(self.config)

            return _GitHubGistBackend(token, self.config.cloud_github_gist_id, on_gist_id=_save_gist_id)
        if mode == "folder":
            raw = self.config.cloud_backup_folder.strip()
            if not raw:
                return None
            p = Path(raw)
            target = p if p.name.lower() == "whisperlm" else (p / "WhisperLM")
            return _FolderBackend(target.resolve(), p.name or "Custom Folder")

        detected = drives if drives is not None else detect_cloud_drives()
        chosen = resolve_auto_backup_drive(detected)
        if chosen is None:
            return None
        return _FolderBackend(chosen.backup_path, chosen.display_name)

    def sync_now(self, drives: list[CloudDriveInfo] | None = None) -> SyncResult:
        """Perform one synchronous bi-directional merge and backup push."""
        with self._sync_lock:
            backend = self._resolve_backend(drives=drives)
            if backend is None:
                label = describe_cloud_target(self.config, drives=drives)
                result = SyncResult(
                    ok=True,
                    provider_label=label,
                    dictations_count=len(self.history_store.list_all()),
                    meetings_count=len(self.meeting_store.list_all()),
                    detail="Saved locally",
                )
                if self._on_status is not None:
                    self._on_status(result)
                return result

            try:
                local_snap = self._build_local_snapshot()
                remote_snap = backend.read() or {}
                merged = merge_snapshots(local_snap, remote_snap)
                restored = self._apply_merged_local(local_snap, merged)
                if not remote_snap or not _snapshot_content_equal(merged, remote_snap):
                    backend.write(merged)
                result = SyncResult(
                    ok=True,
                    provider_label=backend.label,
                    dictations_count=len(merged["dictations"]),
                    meetings_count=len(merged["meetings"]),
                    restored_count=restored,
                    detail=f"Synced to {backend.label}",
                )
            except Exception as exc:
                result = SyncResult(
                    ok=False,
                    provider_label=backend.label,
                    detail=f"Backup paused ({exc})",
                )
            if self._on_status is not None:
                self._on_status(result)
            return result

    def trigger_async(self) -> None:
        """Run a background sync without blocking dictation or UI interactions."""
        threading.Thread(target=self.sync_now, name="cloud-sync", daemon=True).start()
