import json
import sqlite3
from datetime import timedelta

import pytest

from simple_printer import history as history_module
from simple_printer.history import History, normalize_title
from simple_printer.notion_source import NotionTask
from simple_printer.priority import PriorityResult


def task(i, title="Pay invoice"):
    return NotionTask(id=f"id{i}", title=title, notion_priority="Low", due_date="2026-11-01", url=f"u{i}")


@pytest.fixture
def hist(tmp_path):
    h = History(str(tmp_path / "sub" / "history.db"))
    yield h
    h.close()


def test_normalize_title():
    assert normalize_title("  Pay   Invoice!! ") == "pay invoice"
    assert normalize_title("PAY-invoice") == "pay invoice"
    assert normalize_title("Ｐay invoice") == "pay invoice"  # full-width char (NFKC)
    assert normalize_title(None) == ""


def test_record_and_has_seen(hist):
    assert not hist.has_seen("id1")
    hist.record_printed(task(1), PriorityResult("HIGH", "overdue"))
    assert hist.has_seen("id1")
    ((_, status, priority, title, page_id, source),) = hist.recent()
    assert (status, priority, title, page_id, source) == ("printed", "HIGH", "Pay invoice", "id1", "notion")


def test_duplicate_found_within_window(hist):
    hist.record_printed(task(1, "Pay invoice"), PriorityResult("LOW", "r"))
    assert hist.find_duplicate("pay INVOICE!", 7) == "id1"
    assert hist.find_duplicate("Something else", 7) is None


def test_window_zero_disables(hist):
    hist.record_printed(task(1), PriorityResult("LOW", "r"))
    assert hist.find_duplicate("Pay invoice", 0) is None


def test_old_task_outside_window_is_not_a_duplicate(hist, monkeypatch):
    real_now = history_module._now()
    monkeypatch.setattr(history_module, "_now", lambda: real_now - timedelta(days=10))
    hist.record_printed(task(1), PriorityResult("LOW", "r"))
    monkeypatch.setattr(history_module, "_now", lambda: real_now)
    assert hist.find_duplicate("Pay invoice", 7) is None
    assert hist.find_duplicate("Pay invoice", 30) == "id1"


def test_untitled_never_duplicates(hist):
    hist.record_printed(task(1, "Untitled"), PriorityResult("LOW", "r"))
    assert hist.find_duplicate("Untitled", 7) is None
    assert hist.find_duplicate("", 7) is None


def test_duplicate_rows_do_not_match_further_duplicates(hist):
    hist.record_duplicate(task(2), "id1")
    assert hist.has_seen("id2")
    assert hist.find_duplicate("Pay invoice", 7) is None  # only printed rows count


def test_legacy_json_import_is_idempotent(tmp_path):
    legacy = tmp_path / "printed.json"
    legacy.write_text(json.dumps({"printed": ["a", "b"]}))
    path = str(tmp_path / "h.db")
    h = History(path, legacy_json_path=str(legacy))
    assert h.has_seen("a") and h.has_seen("b")
    h.close()
    h = History(path, legacy_json_path=str(legacy))  # second start: no error, no dupes
    assert len(h.recent()) == 2
    # imported rows have no title, so they never trigger title dedupe
    assert h.find_duplicate("", 7) is None
    h.close()


def test_missing_legacy_file_is_fine(tmp_path):
    h = History(str(tmp_path / "h.db"), legacy_json_path=str(tmp_path / "nope.json"))
    assert h.recent() == []
    h.close()


def test_corrupt_legacy_file_raises(tmp_path):
    legacy = tmp_path / "printed.json"
    legacy.write_text("{not json")
    with pytest.raises(RuntimeError, match="corrupt"):
        History(str(tmp_path / "h.db"), legacy_json_path=str(legacy))


def test_corrupt_database_raises(tmp_path):
    path = tmp_path / "h.db"
    path.write_bytes(b"this is not a sqlite database" * 50)
    with pytest.raises(RuntimeError, match="unusable"):
        History(str(path))


def test_two_connections_share_data(tmp_path):
    path = str(tmp_path / "h.db")
    a, b = History(path), History(path)
    a.record_printed(task(1), PriorityResult("LOW", "r"))
    assert b.has_seen("id1")
    a.close(), b.close()


def test_cli_lists_recent(tmp_path, monkeypatch, capsys):
    path = str(tmp_path / "h.db")
    h = History(path)
    h.record_printed(task(1), PriorityResult("HIGH", "r"))
    h.close()
    monkeypatch.setenv("HISTORY_PATH", path)
    assert history_module.main(["5"]) == 0
    assert "Pay invoice" in capsys.readouterr().out


def test_ignored_status_recorded_and_not_used_for_dedupe(hist):
    from simple_printer.slack_source import SlackMessage
    msg = SlackMessage(id="slack:C1:1", title="thanks all", text="thanks all", channel="C1", ts="1",
                       url="u", is_bot=False)
    hist.record_ignored(msg, "not a task")
    assert hist.has_seen("slack:C1:1")
    assert hist.find_duplicate("thanks all", 7) is None
    ((_, status, _, _, _, source),) = hist.recent()
    assert (status, source) == ("ignored", "slack")


def test_migrates_first_schema_database(tmp_path):
    path = str(tmp_path / "old.db")
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE tasks (
            page_id TEXT PRIMARY KEY, title TEXT NOT NULL, title_key TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('printed', 'duplicate')),
            priority TEXT, reason TEXT, due_date TEXT, url TEXT, duplicate_of TEXT, created_at TEXT NOT NULL);
        CREATE INDEX idx_tasks_title_key ON tasks (title_key, status, created_at);
        INSERT INTO tasks VALUES ('old1', 'Old task', 'old task', 'printed', 'LOW', 'r', NULL, 'u', NULL,
                                  '2999-01-01T00:00:00.000000Z');
    """)
    db.commit()
    db.close()
    h = History(path)
    assert h.has_seen("old1")
    assert h.find_duplicate("Old task", 7) == "old1"
    from simple_printer.slack_source import SlackMessage
    h.record_ignored(SlackMessage(id="s", title="t", text="t", channel="C", ts="1", url="", is_bot=False))
    ((_, _, _, _, _, source_new), (_, _, _, _, _, source_old)) = sorted(h.recent(), key=lambda r: r[4] != "s")
    assert (source_new, source_old) == ("slack", "notion")
    h.close()
    History(path).close()  # reopening a migrated DB is a no-op


def test_recent_detailed_and_filter(hist):
    hist.record_printed(task(1, "Printed one"), PriorityResult("HIGH", "because"))
    hist.record_duplicate(task(2, "Dup"), "id1")
    rows = hist.recent_detailed(10)
    assert {r["page_id"] for r in rows} == {"id1", "id2"}
    only = hist.recent_detailed(10, "printed")
    assert [(r["title"], r["priority"], r["reason"], r["url"]) for r in only] == [("Printed one", "HIGH", "because", "u1")]
    with pytest.raises(ValueError):
        hist.recent_detailed(10, "nonsense")


# ---- clearing --------------------------------------------------------------

def _seed(hist):
    hist.record_printed(task(1, "Printed one"), PriorityResult("HIGH", "r"))
    hist.record_duplicate(task(2, "Dup"), "id1")
    hist.record_ignored(task(3, "Chit chat"), "not a task")


def test_clear_scopes(hist):
    _seed(hist)
    assert hist.count_for_clear("all") == 3
    assert hist.count_for_clear("noise") == 2
    assert hist.count_for_clear("ignored") == 1
    assert hist.count_for_clear("duplicate") == 1
    assert hist.clear("ignored") == 1
    assert hist.has_seen("id1") and hist.has_seen("id2") and not hist.has_seen("id3")
    assert hist.clear("noise") == 1          # the duplicate is left
    assert hist.has_seen("id1") and not hist.has_seen("id2")
    assert hist.clear("all") == 1
    assert hist.recent() == []


def test_count_does_not_delete(hist):
    _seed(hist)
    hist.count_for_clear("all")
    assert len(hist.recent()) == 3


def test_clear_older_than_days(hist, monkeypatch):
    real_now = history_module._now()
    monkeypatch.setattr(history_module, "_now", lambda: real_now - timedelta(days=40))
    hist.record_printed(task(1, "Old"), PriorityResult("LOW", "r"))
    monkeypatch.setattr(history_module, "_now", lambda: real_now)
    hist.record_printed(task(2, "New"), PriorityResult("LOW", "r"))
    assert hist.count_for_clear("older", 30) == 1
    assert hist.clear("older", 30) == 1
    assert not hist.has_seen("id1") and hist.has_seen("id2")


@pytest.mark.parametrize("scope,days", [("bogus", None), ("older", None), ("older", 0), ("older", "7")])
def test_clear_rejects_bad_arguments(hist, scope, days):
    with pytest.raises(ValueError):
        hist.clear(scope, days)


def test_cleared_task_can_be_seen_again_as_new(hist):
    hist.record_printed(task(1, "Pay rent"), PriorityResult("LOW", "r"))
    assert hist.find_duplicate("Pay rent", 7) == "id1"
    hist.clear("all")
    assert not hist.has_seen("id1") and hist.find_duplicate("Pay rent", 7) is None


def test_legacy_file_is_renamed_so_clear_is_not_undone(tmp_path):
    legacy = tmp_path / "printed.json"
    legacy.write_text(json.dumps({"printed": ["a", "b"]}))
    path = str(tmp_path / "h.db")
    h = History(path, legacy_json_path=str(legacy))
    assert h.has_seen("a")
    assert not legacy.exists() and (tmp_path / "printed.json.imported").exists()
    h.clear("all")
    h.close()
    h = History(path, legacy_json_path=str(legacy))   # restart: nothing is brought back
    assert h.recent() == []
    h.close()
