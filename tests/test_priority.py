import logging
from types import SimpleNamespace

from simple_printer.notion_source import NotionTask
from simple_printer.priority import validate_priority


def make_task(priority="Low", due="2026-01-01"):
    return NotionTask(id="x", title="Server is down", notion_priority=priority, due_date=due, url="u")


def tool_response(data):
    block = SimpleNamespace(type="tool_use", name="set_priority", input=data)
    return SimpleNamespace(content=[block])


class FakeClient:
    def __init__(self, response=None, exc=None):
        self.calls = []
        self.messages = self
        self._response, self._exc = response, exc

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self._exc:
            raise self._exc
        return self._response


def test_valid_upgrade():
    client = FakeClient(tool_response({"priority": "high", "reason": "Outage"}))
    result = validate_priority(make_task("Low"), client=client)
    assert (result.priority, result.reason) == ("HIGH", "Outage")
    call = client.calls[0]
    assert call["model"] == "claude-haiku-5-5"
    assert "Server is down" in call["messages"][0]["content"]
    assert "Notion priority: Low" in call["messages"][0]["content"]
    assert call["tool_choice"]["name"] == "set_priority"


def test_model_from_env(monkeypatch):
    monkeypatch.setenv("CLAUDE_MODEL", "custom-model")
    client = FakeClient(tool_response({"priority": "LOW", "reason": "ok"}))
    validate_priority(make_task(), client=client)
    assert client.calls[0]["model"] == "custom-model"


def test_invalid_output_falls_back(caplog):
    client = FakeClient(tool_response({"priority": "URGENT", "reason": "x"}))
    with caplog.at_level(logging.WARNING):
        result = validate_priority(make_task("High"), client=client)
    assert result.priority == "HIGH"
    assert "Priority validation failed" in caplog.text


def test_no_tool_call_falls_back_to_medium():
    client = FakeClient(SimpleNamespace(content=[SimpleNamespace(type="text", text="hi")]))
    assert validate_priority(make_task(None), client=client).priority == "MEDIUM"


def test_exception_falls_back():
    client = FakeClient(exc=RuntimeError("boom"))
    result = validate_priority(make_task("Low"), client=client)
    assert result.priority == "LOW"
    assert "boom" in result.reason


def test_missing_api_key_falls_back(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = validate_priority(make_task("Weird"))
    assert result.priority == "MEDIUM"
    assert "ANTHROPIC_API_KEY" in result.reason


# ---- extract_task (chat messages) ------------------------------------------

import pytest

from simple_printer.priority import extract_task


def extraction(data):
    return SimpleNamespace(content=[SimpleNamespace(type="tool_use", name="record_task", input=data)])


def test_extract_task_valid():
    client = FakeClient(extraction({"is_task": True, "title": "Fix login", "priority": "high",
                                    "reason": "customers blocked", "due_date": "2026-11-02"}))
    result = extract_task("login is broken for customers", client=client)
    assert (result.title, result.priority, result.due_date) == ("Fix login", "HIGH", "2026-11-02")
    call = client.calls[0]
    assert call["tool_choice"]["name"] == "record_task"
    assert "login is broken" in call["messages"][0]["content"]
    assert "untrusted" in call["system"]


def test_extract_task_not_a_task_returns_none():
    client = FakeClient(extraction({"is_task": False, "reason": "chit-chat"}))
    assert extract_task("thanks!", client=client) is None


def test_extract_task_bad_due_date_dropped_and_title_truncated():
    client = FakeClient(extraction({"is_task": True, "title": "x" * 200, "priority": "LOW",
                                    "reason": "r", "due_date": "next friday"}))
    result = extract_task("m", client=client)
    assert result.due_date is None and len(result.title) == 80


@pytest.mark.parametrize("data", [
    {"is_task": "yes", "reason": "r"},
    {"is_task": True, "title": "", "priority": "LOW", "reason": "r"},
    {"is_task": True, "title": "t", "priority": "URGENT", "reason": "r"},
])
def test_extract_task_invalid_output_raises(data):
    with pytest.raises(ValueError):
        extract_task("m", client=FakeClient(extraction(data)))


def test_extract_task_api_error_raises():
    with pytest.raises(RuntimeError):
        extract_task("m", client=FakeClient(exc=RuntimeError("boom")))


def test_extract_task_missing_key_raises(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        extract_task("m")


def test_category_is_sent_to_claude():
    task = make_task("Low")
    task.category = "Finance"
    client = FakeClient(tool_response({"priority": "HIGH", "reason": "money"}))
    validate_priority(task, client=client)
    assert "Category: Finance" in client.calls[0]["messages"][0]["content"]
    assert "category" in client.calls[0]["system"].lower()


def test_missing_category_says_not_set():
    client = FakeClient(tool_response({"priority": "LOW", "reason": "ok"}))
    validate_priority(make_task(), client=client)
    assert "Category: not set" in client.calls[0]["messages"][0]["content"]


def test_fallback_results_are_flagged_and_real_ones_are_not():
    assert validate_priority(make_task("Low"), client=FakeClient(exc=RuntimeError("x"))).fallback is True
    ok = validate_priority(make_task("Low"), client=FakeClient(tool_response({"priority": "HIGH", "reason": "r"})))
    assert ok.fallback is False
