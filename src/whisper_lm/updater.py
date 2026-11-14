"""GitHub Releases auto-update check, verified download, and silent installer handoff."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Callable

import httpx

from whisper_lm import __version__

DEFAULT_RELEASE_REPO = "sharif-git-dev/whisper-lm"
_VERSION_RE = re.compile(r"\d+")


@dataclass(frozen=True)
class UpdateInfo:
    """Metadata for a newer WhisperLM release available on GitHub Releases."""

    version: str
    tag_name: str
    release_notes: str
    asset_name: str
    download_url: str
    size_bytes: int
    sha256: str = ""
    html_url: str = ""


def parse_version(version_str: str) -> tuple[int, ...]:
    """Extract numeric version parts from strings like 'v0.2.0' or '0.1.0'."""
    parts = [int(m.group(0)) for m in _VERSION_RE.finditer(version_str or "")]
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:4])


def is_newer_version(remote_version: str, current_version: str = __version__) -> bool:
    """Return True when remote_version is strictly newer than current_version."""
    if not remote_version:
        return False
    return parse_version(remote_version) > parse_version(current_version)


def check_for_update(
    current_version: str = __version__,
    *,
    repo: str = DEFAULT_RELEASE_REPO,
    client: httpx.Client | None = None,
) -> UpdateInfo | None:
    """Query GitHub Releases /latest and return UpdateInfo if a newer installer exists."""
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    headers = {"Accept": "application/vnd.github+json"}

    if client is None:
        with httpx.Client(timeout=10.0, follow_redirects=True) as owned:
            resp = owned.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()
    else:
        resp = client.get(url, headers=headers)
        resp.raise_for_status()
        data = resp.json()

    tag = str(data.get("tag_name") or "").strip()
    clean_ver = tag.lstrip("vV")
    if not is_newer_version(clean_ver, current_version):
        return None

    assets = list(data.get("assets") or [])
    exe_asset = next(
        (
            a
            for a in assets
            if str(a.get("name") or "").lower().endswith(".exe") and a.get("browser_download_url")
        ),
        None,
    )
    if exe_asset is None:
        return None

    digest = str(exe_asset.get("digest") or "").strip()
    sha256 = digest.split(":", 1)[1].lower() if digest.lower().startswith("sha256:") else ""

    return UpdateInfo(
        version=clean_ver,
        tag_name=tag or f"v{clean_ver}",
        release_notes=str(data.get("body") or "").strip(),
        asset_name=str(exe_asset.get("name") or "WhisperLM-Setup.exe"),
        download_url=str(exe_asset["browser_download_url"]),
        size_bytes=int(exe_asset.get("size") or 0),
        sha256=sha256,
        html_url=str(data.get("html_url") or ""),
    )


def download_update(
    info: UpdateInfo,
    *,
    dest_dir: Path | None = None,
    on_progress: Callable[[int, str], None] | None = None,
) -> Path:
    """Stream the update installer to temp storage and verify its SHA-256 checksum."""
    base_dir = dest_dir if dest_dir is not None else Path(tempfile.gettempdir()) / "WhisperLM-Update"
    base_dir.mkdir(parents=True, exist_ok=True)
    target = base_dir / info.asset_name
    part = target.with_name(target.name + ".part")

    hasher = hashlib.sha256()
    last_pct = -1
    timeout = httpx.Timeout(20.0, read=180.0)
    with httpx.stream("GET", info.download_url, follow_redirects=True, timeout=timeout) as resp:
        resp.raise_for_status()
        total = int(resp.headers.get("content-length") or info.size_bytes or 0)
        received = 0
        with part.open("wb") as fh:
            for chunk in resp.iter_bytes(chunk_size=512 * 1024):
                if not chunk:
                    continue
                fh.write(chunk)
                hasher.update(chunk)
                received += len(chunk)
                if on_progress is not None and total > 0:
                    pct = min(99, int(received * 100 / total))
                    if pct != last_pct:
                        last_pct = pct
                        mb_done = received / (1024.0 * 1024.0)
                        mb_tot = total / (1024.0 * 1024.0)
                        on_progress(pct, f"Downloading WhisperLM v{info.version}... {pct}% ({mb_done:.0f}/{mb_tot:.0f} MB)")

    if info.sha256 and hasher.hexdigest().lower() != info.sha256.lower():
        part.unlink(missing_ok=True)
        raise ValueError("Downloaded update failed SHA-256 verification.")

    part.replace(target)
    if on_progress is not None:
        on_progress(100, f"Installing WhisperLM v{info.version}...")
    return target


def apply_update_and_relaunch(installer_path: Path, *, is_frozen: bool | None = None) -> bool:
    """Launch the downloaded installer silently when installed (.exe) or interactively otherwise."""
    frozen = getattr(sys, "frozen", False) if is_frozen is None else is_frozen
    args = [str(installer_path)]
    if frozen:
        args.extend(["/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/RELAUNCH"])

    flags = 0
    if sys.platform == "win32":
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(args, creationflags=flags, close_fds=True)
    return True
