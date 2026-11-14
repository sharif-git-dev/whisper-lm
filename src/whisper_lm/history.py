"""Persistent local dictation history storage with date/time categorization and search helpers."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime
import json
from pathlib import Path
import threading
import uuid


@dataclass(slots=True)
class HistoryEntry:
    """One completed dictation item persisted locally with calendar date and time."""

    timestamp: str
    app_name: str
    raw_text: str
    cleaned_text: str
    detected_language: str
    date: str = ""
    created_at: str = ""
    id: str = ""

    def __post_init__(self) -> None:
        now = datetime.now()
        if not self.date:
            self.date = now.strftime("%Y-%m-%d")
        if not self.timestamp:
            self.timestamp = now.strftime("%H:%M:%S")
        if not self.created_at:
            self.created_at = f"{self.date}T{self.timestamp}"
        if not self.id:
            date_compact = self.date.replace("-", "")
            time_compact = self.timestamp.replace(":", "")
            self.id = f"{date_compact}_{time_compact}_{uuid.uuid4().hex[:8]}"


def parse_entry_date(date_str: str) -> date | None:
    """Parse ``YYYY-MM-DD`` calendar date safely."""
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def date_category_label(date_str: str, today: date | None = None) -> str:
    """Categorize a history entry into human-friendly time buckets (Today, Yesterday, Month...)."""
    parsed = parse_entry_date(date_str)
    if parsed is None:
        return "Earlier"

    ref = today or datetime.now().date()
    delta = (ref - parsed).days
    if delta <= 0:
        return "Today"
    if delta == 1:
        return "Yesterday"
    if delta <= 6:
        return "Earlier This Week"
    if parsed.year == ref.year and parsed.month == ref.month:
        return "Earlier This Month"
    return parsed.strftime("%B %Y")


def matches_time_filter(date_str: str, window_key: str, today: date | None = None) -> bool:
    """Return whether *date_str* falls inside the selected Hub history filter."""
    if window_key == "all":
        return True
    parsed = parse_entry_date(date_str)
    if parsed is None:
        return False
    ref = today or datetime.now().date()
    delta = (ref - parsed).days
    if window_key == "today":
        return delta <= 0
    if window_key == "week":
        return delta <= 6
    if window_key == "month":
        return delta <= 30
    if window_key == "older":
        return delta >= 30
    return True


def format_entry_when(entry: HistoryEntry, today: date | None = None) -> str:
    """Format short card time string (`HH:MM:SS` for today, `Mon, Oct 14 · HH:MM` for older dates)."""
    parsed = parse_entry_date(entry.date)
    ref = today or datetime.now().date()
    short_time = entry.timestamp[:5] if len(entry.timestamp) >= 5 else entry.timestamp
    if parsed is None or parsed == ref:
        return entry.timestamp
    if (ref - parsed).days == 1:
        return f"Yesterday · {short_time}"
    if parsed.year == ref.year:
        return f"{parsed.strftime('%b %d')} · {short_time}"
    return f"{parsed.strftime('%b %d, %Y')} · {short_time}"


class DictationStore:
    """Thread-safe local JSONL persistence for all historical dictations across months."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.file_path = directory / "dictations.jsonl"
        self._lock = threading.Lock()
        self.directory.mkdir(parents=True, exist_ok=True)

    def append(self, entry: HistoryEntry) -> None:
        with self._lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            line = json.dumps(asdict(entry), ensure_ascii=False)
            with self.file_path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")

    def list_all(self) -> list[HistoryEntry]:
        with self._lock:
            items = self._read_unlocked()
            items.sort(key=lambda item: (item.created_at, item.id), reverse=True)
            return items

    def delete(self, entry_id: str) -> None:
        with self._lock:
            if not self.file_path.exists():
                return
            remaining = [item for item in self._read_unlocked() if item.id != entry_id]
            self._write_unlocked(remaining)

    def clear(self) -> None:
        with self._lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            self.file_path.write_text("", encoding="utf-8")

    def _read_unlocked(self) -> list[HistoryEntry]:
        if not self.file_path.exists():
            return []
        try:
            lines = self.file_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        valid_fields = set(HistoryEntry.__dataclass_fields__)
        items: list[HistoryEntry] = []
        for raw_line in lines:
            line = raw_line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
                filtered = {k: v for k, v in raw.items() if k in valid_fields}
                items.append(HistoryEntry(**filtered))
            except Exception:
                continue
        return items

    def _write_unlocked(self, entries: list[HistoryEntry]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        payload = "".join(json.dumps(asdict(item), ensure_ascii=False) + "\n" for item in entries)
        self.file_path.write_text(payload, encoding="utf-8")
