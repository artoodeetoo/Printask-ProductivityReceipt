import json

import pytest

from simple_printer import settings


@pytest.fixture(autouse=True)
def reset():
    yield
    settings.configure(None)


def test_precedence_file_then_env_then_default(tmp_path, monkeypatch):
    monkeypatch.delenv("SLACK_TASK_MODE", raising=False)
    store = settings.configure(str(tmp_path / "s.json"))
    assert settings.get("SLACK_TASK_MODE") == "claude"           # default
    monkeypatch.setenv("SLACK_TASK_MODE", "bots")
    assert settings.get("SLACK_TASK_MODE") == "bots"             # env
    assert store.update({"SLACK_TASK_MODE": "all"}) == []
    assert settings.get("SLACK_TASK_MODE") == "all"              # file beats env
    assert store.update({"SLACK_TASK_MODE": ""}) == []           # blank removes the override
    assert settings.get("SLACK_TASK_MODE") == "bots"


def test_persists_and_reloads(tmp_path):
    path = str(tmp_path / "sub" / "s.json")
    settings.configure(path).update({"POLL_INTERVAL_SECONDS": "120", "SLACK_CHANNEL_IDS": "C1ABC, C2DEF"})
    store = settings.configure(path)
    assert store.get("POLL_INTERVAL_SECONDS") == "120"
    assert store.get("SLACK_CHANNEL_IDS") == "C1ABC,C2DEF"
    assert sorted(json.load(open(path))) == ["POLL_INTERVAL_SECONDS", "SLACK_CHANNEL_IDS"]


@pytest.mark.parametrize("name,value", [
    ("SLACK_TASK_MODE", "weird"),
    ("POLL_INTERVAL_SECONDS", "5"),          # below the 30s minimum
    ("POLL_INTERVAL_SECONDS", "abc"),
    ("DEDUPE_WINDOW_DAYS", "-1"),
    ("SLACK_MAX_PER_CYCLE", "0"),
    ("SLACK_LOOKBACK_HOURS", "9999"),
    ("SLACK_CHANNEL_IDS", "general, random"),
    ("SLACK_KEYWORD", "x" * 101),
    ("ENABLE_QR_CODES", "maybe"),
])
def test_invalid_values_rejected_and_nothing_saved(tmp_path, name, value):
    store = settings.configure(str(tmp_path / "s.json"))
    assert store.update({name: value, "DEDUPE_WINDOW_DAYS" if name != "DEDUPE_WINDOW_DAYS" else "POLL_INTERVAL_SECONDS": "60"})
    assert store.overrides() == {}
    assert not (tmp_path / "s.json").exists()


def test_keyword_mode_requires_keyword(tmp_path, monkeypatch):
    monkeypatch.delenv("SLACK_KEYWORD", raising=False)
    store = settings.configure(str(tmp_path / "s.json"))
    assert any("Trigger text" in e for e in store.update({"SLACK_TASK_MODE": "keyword"}))
    assert store.update({"SLACK_TASK_MODE": "keyword", "SLACK_KEYWORD": "todo:"}) == []


def test_secrets_cannot_be_set(tmp_path):
    store = settings.configure(str(tmp_path / "s.json"))
    store.update({"SLACK_BOT_TOKEN": "xoxb-evil", "ANTHROPIC_API_KEY": "x", "POLL_INTERVAL_SECONDS": "60"})
    assert set(store.overrides()) == {"POLL_INTERVAL_SECONDS"}


def test_corrupt_file_raises(tmp_path):
    (tmp_path / "s.json").write_text("{nope")
    with pytest.raises(RuntimeError, match="invalid"):
        settings.configure(str(tmp_path / "s.json"))


def test_unknown_keys_in_file_ignored(tmp_path):
    (tmp_path / "s.json").write_text('{"SLACK_BOT_TOKEN": "x", "DEDUPE_WINDOW_DAYS": "3"}')
    assert settings.configure(str(tmp_path / "s.json")).overrides() == {"DEDUPE_WINDOW_DAYS": "3"}


def test_show_category_setting(tmp_path, monkeypatch):
    monkeypatch.delenv("SHOW_CATEGORY", raising=False)
    store = settings.configure(str(tmp_path / "s.json"))
    assert settings.get("SHOW_CATEGORY") == "true"
    assert store.update({"SHOW_CATEGORY": "false"}) == []
    assert settings.get("SHOW_CATEGORY") == "false"
    assert store.update({"SHOW_CATEGORY": "sometimes"})


def test_write_back_setting_defaults_off(tmp_path, monkeypatch):
    monkeypatch.delenv("NOTION_WRITE_BACK_PRIORITY", raising=False)
    store = settings.configure(str(tmp_path / "s.json"))
    assert settings.get("NOTION_WRITE_BACK_PRIORITY") == "false"
    assert store.update({"NOTION_WRITE_BACK_PRIORITY": "true"}) == []
    assert settings.get("NOTION_WRITE_BACK_PRIORITY") == "true"
