"""Tests for persistent local dictation history, date categorization, and time filtering."""

from datetime import date
from pathlib import Path

from whisper_lm.history import (
    DictationStore,
    HistoryEntry,
    date_category_label,
    format_entry_when,
    matches_time_filter,
)


def test_date_categorization_and_filters() -> None:
    today = date(2026, 11, 14)

    assert date_category_label("2026-11-14", today=today) == "Today"
    assert date_category_label("2026-11-13", today=today) == "Yesterday"
    assert date_category_label("2026-11-10", today=today) == "Earlier This Week"
    assert date_category_label("2026-11-01", today=today) == "Earlier This Month"
    assert date_category_label("2026-10-12", today=today) == "October 2026"

    assert matches_time_filter("2026-10-12", "all", today=today)
    assert matches_time_filter("2026-10-12", "older", today=today)
    assert not matches_time_filter("2026-10-12", "week", today=today)
    assert matches_time_filter("2026-11-13", "month", today=today)

    old_entry = HistoryEntry("16:40:00", "Code.exe", "raw", "Cleaned", "en", date="2026-10-12")
    assert format_entry_when(old_entry, today=today) == "Oct 12 · 16:40"


def test_dictation_store_persists_beyond_100_entries(tmp_path: Path) -> None:
    store = DictationStore(tmp_path / "history")

    # Save 125 entries including one from >1 month ago
    old = HistoryEntry("09:15:00", "Chrome", "monthly note", "Monthly note", "en", date="2026-10-05")
    store.append(old)
    for i in range(124):
        store.append(HistoryEntry(f"10:{i // 60:02d}:{i % 60:02d}", "Code.exe", f"raw {i}", f"Clean {i}", "en", date="2026-11-14"))

    loaded = store.list_all()
    assert len(loaded) == 125
    # Newest first, oldest from a month ago still preserved cleanly
    assert loaded[-1].cleaned_text == "Monthly note"
    assert loaded[-1].date == "2026-10-05"

    store.delete(old.id)
    assert len(store.list_all()) == 124
    store.clear()
    assert store.list_all() == []
