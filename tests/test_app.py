import signal
import threading

import pytest

from simple_printer import app
from simple_printer.history import History
from simple_printer.slack_source import SlackMessage
from simple_printer.notion_source import NotionTask
from simple_printer.priority import PriorityResult


def task(i):
    return NotionTask(id=f"id{i}", title=f"T{i}", notion_priority=None, due_date=None, url="u")


def ok_validate(t):
    return PriorityResult("MEDIUM", "r")


@pytest.fixture
def hist(tmp_path):
    h = History(str(tmp_path / "history.db"))
    yield h
    h.close()


def seen_ids(h):
    return {row[4] for row in h.recent(100)}


def test_skips_already_printed(hist):
    hist.record_printed(task(1), PriorityResult("LOW", "r"))
    printed = []
    stats = app.run_cycle(hist, fetch=lambda: [task(1), task(2)], validate=ok_validate,
                          print_task=lambda t, p: printed.append(t.id))
    assert printed == ["id2"]
    assert stats == {"fetched": 2, "skipped": 1, "duplicates": 0, "ignored": 0, "printed": 1,
                     "failed": 0, "source_errors": 0, "written_back": 0, "writeback_failed": 0}
    assert seen_ids(hist) == {"id1", "id2"}


def test_records_only_after_success_and_continues(hist):
    def print_task(t, p):
        if t.id == "id2":
            raise OSError("printer offline")

    stats = app.run_cycle(hist, fetch=lambda: [task(1), task(2), task(3)], validate=ok_validate,
                          print_task=print_task)
    assert stats["printed"] == 2 and stats["failed"] == 1
    assert seen_ids(hist) == {"id1", "id3"}  # failed task retried next cycle


def test_validate_error_does_not_stop_others(hist):
    def validate(t):
        if t.id == "id1":
            raise RuntimeError("x")
        return PriorityResult("LOW", "r")

    stats = app.run_cycle(hist, fetch=lambda: [task(1), task(2)], validate=validate,
                          print_task=lambda t, p: None)
    assert seen_ids(hist) == {"id2"} and stats["failed"] == 1


def test_stop_event_halts_between_tasks(hist):
    stop = threading.Event()
    printed = []

    def print_task(t, p):
        printed.append(t.id)
        stop.set()

    app.run_cycle(hist, fetch=lambda: [task(1), task(2)], validate=ok_validate,
                  print_task=print_task, stop=stop)
    assert printed == ["id1"]


def test_duplicate_title_skipped_without_claude_or_printing(hist):
    hist.record_printed(NotionTask("old", "Pay invoice", None, None, "u"), PriorityResult("LOW", "r"))
    called = []
    new = NotionTask("new", "pay INVOICE!", None, None, "u")
    stats = app.run_cycle(hist, fetch=lambda: [new], validate=lambda t: called.append("v") or ok_validate(t),
                          print_task=lambda t, p: called.append("p"))
    assert called == [] and stats["duplicates"] == 1 and stats["printed"] == 0
    assert hist.has_seen("new")  # recorded, so it is not re-checked every poll
    stats = app.run_cycle(hist, fetch=lambda: [new], validate=ok_validate, print_task=lambda t, p: None)
    assert stats["skipped"] == 1 and stats["duplicates"] == 0


def test_same_title_twice_in_one_fetch(hist):
    a, b = NotionTask("a", "Same", None, None, "u"), NotionTask("b", "same", None, None, "u")
    printed = []
    stats = app.run_cycle(hist, fetch=lambda: [a, b], validate=ok_validate,
                          print_task=lambda t, p: printed.append(t.id))
    assert printed == ["a"] and stats["duplicates"] == 1


def test_dedupe_can_be_disabled(hist):
    a, b = NotionTask("a", "Same", None, None, "u"), NotionTask("b", "Same", None, None, "u")
    printed = []
    app.run_cycle(hist, fetch=lambda: [a, b], validate=ok_validate,
                  print_task=lambda t, p: printed.append(t.id), dedupe_days=0)
    assert printed == ["a", "b"]


def test_missing_env_exits_2(monkeypatch):
    for name in app.REQUIRED_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None, raising=False)
    assert app.main() == 2


def test_main_run_once(monkeypatch, tmp_path):
    for name in app.REQUIRED_ENV + ("NOTION_API_KEY", "NOTION_DATABASE_ID"):
        monkeypatch.setenv(name, "x")
    monkeypatch.setenv("POLL_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("HISTORY_PATH", str(tmp_path / "history.db"))
    monkeypatch.setenv("STATE_PATH", str(tmp_path / "printed.json"))
    monkeypatch.setattr(app.notion_source, "fetch_todo_tasks", lambda: [task(1)])
    monkeypatch.setattr(app.priority, "validate_priority", ok_validate)
    printed = []
    monkeypatch.setattr(app.printing, "print_task", lambda t, p: printed.append(t.id))
    monkeypatch.setattr(signal, "signal", lambda *a: None)
    assert app.main() == 0
    assert printed == ["id1"]


def test_main_upgrade_does_not_reprint_legacy_tasks(monkeypatch, tmp_path):
    (tmp_path / "printed.json").write_text('{"printed": ["id1"]}')
    for name in app.REQUIRED_ENV + ("NOTION_API_KEY", "NOTION_DATABASE_ID"):
        monkeypatch.setenv(name, "x")
    monkeypatch.setenv("POLL_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("HISTORY_PATH", str(tmp_path / "history.db"))
    monkeypatch.setenv("STATE_PATH", str(tmp_path / "printed.json"))
    monkeypatch.setattr(app.notion_source, "fetch_todo_tasks", lambda: [task(1), task(2)])
    monkeypatch.setattr(app.priority, "validate_priority", ok_validate)
    printed = []
    monkeypatch.setattr(app.printing, "print_task", lambda t, p: printed.append(t.id))
    monkeypatch.setattr(signal, "signal", lambda *a: None)
    assert app.main() == 0
    assert printed == ["id2"]


def test_main_corrupt_history_exits_1(monkeypatch, tmp_path):
    (tmp_path / "history.db").write_bytes(b"not a database" * 100)
    for name in app.REQUIRED_ENV + ("NOTION_API_KEY", "NOTION_DATABASE_ID"):
        monkeypatch.setenv(name, "x")
    monkeypatch.setenv("HISTORY_PATH", str(tmp_path / "history.db"))
    monkeypatch.setenv("STATE_PATH", str(tmp_path / "printed.json"))
    monkeypatch.setattr(signal, "signal", lambda *a: None)
    assert app.main() == 1


def test_main_without_any_source_exits_2(monkeypatch, tmp_path):
    for name in ("NOTION_API_KEY", "NOTION_DATABASE_ID", "SLACK_BOT_TOKEN", "SLACK_CHANNEL_IDS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PRINTER_HOST", "x")
    monkeypatch.setenv("HISTORY_PATH", str(tmp_path / "h.db"))
    monkeypatch.setattr(signal, "signal", lambda *a: None)
    assert app.main() == 2


# ---- multiple sources / Slack -------------------------------------------------

def slack_msg(i, title="Ticket 42 broken"):
    return SlackMessage(id=f"slack:C1:{i}", title=title, text=title, channel="C1", ts=str(i),
                        url="u", is_bot=True)


def claude_enrich(message):
    if "thanks" in message.text:
        return None
    message.title = "Fix: " + message.text
    return message, PriorityResult("HIGH", "customer facing")


def test_enrich_prints_with_claude_priority_and_skips_non_tasks(hist):
    msgs = [slack_msg(1, "login broken"), slack_msg(2, "thanks all")]
    printed = []
    src = app.Source("slack", lambda: msgs, enrich=claude_enrich)

    def validate(t):  # must not run: enrich already supplied the priority
        raise AssertionError("validate called")

    stats = app.run_cycle(hist, sources=[src], validate=validate,
                          print_task=lambda t, p: printed.append((t.title, p.priority)))
    assert printed == [("Fix: login broken", "HIGH")]
    assert stats["printed"] == 1 and stats["ignored"] == 1
    assert hist.has_seen("slack:C1:2")  # ignored messages are remembered
    # next cycle: nothing is re-sent to Claude
    calls = []
    app.run_cycle(hist, sources=[app.Source("slack", lambda: msgs, enrich=lambda m: calls.append(m))],
                  print_task=lambda t, p: None)
    assert calls == []


def test_enrich_error_leaves_message_for_retry(hist):
    def boom(message):
        raise RuntimeError("anthropic down")

    msgs = [slack_msg(1)]
    stats = app.run_cycle(hist, sources=[app.Source("slack", lambda: msgs, enrich=boom)],
                          print_task=lambda t, p: None)
    assert stats["failed"] == 1 and not hist.has_seen("slack:C1:1")


def test_per_cycle_cap(hist):
    msgs = [slack_msg(i, f"t{i}") for i in range(1, 6)]
    printed = []
    src = app.Source("slack", lambda: msgs, enrich=claude_enrich, max_per_cycle=2)
    app.run_cycle(hist, sources=[src], print_task=lambda t, p: printed.append(t.id))
    assert len(printed) == 2
    app.run_cycle(hist, sources=[src], print_task=lambda t, p: printed.append(t.id))
    assert len(printed) == 4  # the rest wait their turn


def test_one_source_failing_does_not_stop_the_other(hist):
    def bad():
        raise RuntimeError("slack 500")

    printed = []
    stats = app.run_cycle(hist, sources=[app.Source("slack", bad), app.Source("notion", lambda: [task(1)])],
                          validate=ok_validate, print_task=lambda t, p: printed.append(t.id))
    assert printed == ["id1"] and stats["source_errors"] == 1


def test_all_sources_failing_raises(hist):
    def bad():
        raise RuntimeError("down")

    with pytest.raises(RuntimeError, match="Every source"):
        app.run_cycle(hist, sources=[app.Source("a", bad), app.Source("b", bad)])


def test_cross_source_duplicate_title(hist):
    # a ticket already printed from Notion is not printed again from Slack
    hist.record_printed(NotionTask("n1", "Fix: login broken", None, None, "u"), PriorityResult("LOW", "r"))
    printed = []
    stats = app.run_cycle(hist, sources=[app.Source("slack", lambda: [slack_msg(1, "login broken")],
                                                    enrich=claude_enrich)],
                          print_task=lambda t, p: printed.append(t.id))
    assert printed == [] and stats["duplicates"] == 1


def slack_env(monkeypatch, **extra):
    for name in ("NOTION_API_KEY", "NOTION_DATABASE_ID", "SLACK_TASK_MODE", "SLACK_KEYWORD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-1")
    monkeypatch.setenv("SLACK_CHANNEL_IDS", "C1, C2")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    for key, value in extra.items():
        monkeypatch.setenv(key, value)


def test_build_sources_slack_modes(monkeypatch):
    slack_env(monkeypatch)
    (src,) = app.build_sources()
    assert src.name == "slack" and src.enrich is not None and src.max_per_cycle == 10
    slack_env(monkeypatch, SLACK_TASK_MODE="bots")
    (src,) = app.build_sources()
    assert src.enrich is None  # non-claude modes use the normal priority check


def test_build_sources_validates_settings(monkeypatch):
    slack_env(monkeypatch, SLACK_TASK_MODE="nonsense")
    with pytest.raises(ValueError, match="SLACK_TASK_MODE"):
        app.build_sources()
    slack_env(monkeypatch, SLACK_TASK_MODE="keyword")
    with pytest.raises(ValueError, match="SLACK_KEYWORD"):
        app.build_sources()
    slack_env(monkeypatch)
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        app.build_sources()


def test_main_bad_slack_mode_exits_2(monkeypatch, tmp_path):
    slack_env(monkeypatch, SLACK_TASK_MODE="nonsense", PRINTER_HOST="x", HISTORY_PATH=str(tmp_path / "h.db"))
    monkeypatch.setattr(signal, "signal", lambda *a: None)
    assert app.main() == 2


# ---- write-back of Claude's priority to Notion --------------------------------

def _wb_task(i, notion_priority):
    return NotionTask(id=f"id{i}", title=f"T{i}", notion_priority=notion_priority, due_date=None, url="u")


@pytest.fixture
def notion_updates(monkeypatch):
    calls = []
    monkeypatch.setattr(app.notion_source, "update_priority", lambda page_id, priority: calls.append((page_id, priority)))
    return calls


def _notion_source(tasks, **kw):
    return app.Source("notion", lambda: tasks, after_print=app._notion_write_back, **kw)


def test_write_back_when_claude_changes_priority(hist, notion_updates):
    stats = app.run_cycle(hist, sources=[_notion_source([_wb_task(1, "Low")])],
                          validate=lambda t: PriorityResult("HIGH", "overdue"), print_task=lambda t, p: None)
    assert notion_updates == [("id1", "HIGH")] and stats["written_back"] == 1


def test_no_write_when_priority_unchanged_in_any_case(hist, notion_updates):
    app.run_cycle(hist, sources=[_notion_source([_wb_task(1, "high")])],
                  validate=lambda t: PriorityResult("HIGH", "same"), print_task=lambda t, p: None)
    assert notion_updates == []


def test_write_back_fills_an_unset_priority(hist, notion_updates):
    app.run_cycle(hist, sources=[_notion_source([_wb_task(1, None)])],
                  validate=lambda t: PriorityResult("MEDIUM", "r"), print_task=lambda t, p: None)
    assert notion_updates == [("id1", "MEDIUM")]


def test_fallback_results_are_never_written_back(hist, notion_updates):
    # no key / API error: nothing was decided, so Notion must not be touched
    app.run_cycle(hist, sources=[_notion_source([_wb_task(1, "Urgent")])],
                  validate=lambda t: PriorityResult("MEDIUM", "Fallback to Notion priority: x", fallback=True),
                  print_task=lambda t, p: None)
    assert notion_updates == []


def test_no_write_when_print_failed_or_duplicate(hist, notion_updates):
    def boom(t, p):
        raise OSError("printer offline")

    app.run_cycle(hist, sources=[_notion_source([_wb_task(1, "Low")])],
                  validate=lambda t: PriorityResult("HIGH", "r"), print_task=boom)
    assert notion_updates == []
    hist.record_printed(NotionTask("old", "T2", None, None, "u"), PriorityResult("LOW", "r"))
    app.run_cycle(hist, sources=[_notion_source([_wb_task(2, "Low")])],
                  validate=lambda t: PriorityResult("HIGH", "r"), print_task=lambda t, p: None)
    assert notion_updates == []


def test_write_back_failure_does_not_fail_the_task(hist, monkeypatch):
    def denied(page_id, priority):
        raise RuntimeError("403 restricted")

    monkeypatch.setattr(app.notion_source, "update_priority", denied)
    stats = app.run_cycle(hist, sources=[_notion_source([_wb_task(1, "Low")])],
                          validate=lambda t: PriorityResult("HIGH", "r"), print_task=lambda t, p: None)
    assert stats["printed"] == 1 and stats["failed"] == 0 and stats["writeback_failed"] == 1
    assert hist.has_seen("id1")  # still recorded: it must not reprint next cycle


def test_write_back_is_off_by_default_and_follows_the_setting(monkeypatch, tmp_path):
    from simple_printer import settings
    for name in ("SLACK_BOT_TOKEN", "SLACK_CHANNEL_IDS", "NOTION_WRITE_BACK_PRIORITY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NOTION_API_KEY", "x")
    monkeypatch.setenv("NOTION_DATABASE_ID", "x")
    store = settings.configure(str(tmp_path / "s.json"))
    try:
        (src,) = app.build_sources()
        assert src.after_print is None
        store.update({"NOTION_WRITE_BACK_PRIORITY": "true"})
        (src,) = app.build_sources()
        assert src.after_print is app._notion_write_back
    finally:
        settings.configure(None)
