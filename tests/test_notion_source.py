import pytest

from simple_printer import notion_source
from simple_printer.notion_source import NotionTask, fetch_todo_tasks

PAGE_ID = "2a077d23-ba9a-8088-a24e-ea64dfe6525c"


def page(page_id=PAGE_ID, title="Fix bug", status="To Do", priority="High", due="2026-11-01", status_type="status"):
    props = {"Name": {"type": "title", "title": [{"plain_text": title}] if title else []}}
    if status_type == "status":
        props["Status"] = {"type": "status", "status": {"name": status}}
    else:
        props["Status"] = {"type": "select", "select": {"name": status}}
    if priority is not None:
        props["Priority"] = {"type": "select", "select": {"name": priority}}
    if due is not None:
        props["Due Date"] = {"type": "date", "date": {"start": due, "end": None}}
    return {"id": page_id, "properties": props}


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        return self._payload


@pytest.fixture
def fake_post(monkeypatch):
    calls = []
    responses = []

    def post(url, json=None, headers=None, timeout=None):
        calls.append({"url": url, "json": json, "headers": headers})
        return responses.pop(0)

    monkeypatch.setattr(notion_source.httpx, "post", post)
    return calls, responses


def test_paginates_and_filters_status(fake_post):
    calls, responses = fake_post
    responses.append(FakeResponse({
        "results": [page(page_id="a-1", title="One"), page(page_id="a-2", title="Done", status="Done")],
        "has_more": True, "next_cursor": "cur1",
    }))
    responses.append(FakeResponse({
        "results": [page(page_id="b-1", title="Two", status="Not started", status_type="select")],
        "has_more": False, "next_cursor": None,
    }))
    tasks = fetch_todo_tasks("dbid", "key")
    assert [t.title for t in tasks] == ["One", "Two"]
    assert len(calls) == 2
    assert "start_cursor" not in calls[0]["json"]
    assert calls[1]["json"]["start_cursor"] == "cur1"
    assert calls[0]["url"].endswith("/v1/databases/dbid/query")
    assert calls[0]["headers"]["Authorization"] == "Bearer key"


def test_fields_and_url(fake_post):
    _, responses = fake_post
    responses.append(FakeResponse({"results": [page()], "has_more": False}))
    (task,) = fetch_todo_tasks("db", "key")
    assert task == NotionTask(
        id=PAGE_ID, title="Fix bug", notion_priority="High", due_date="2026-11-01",
        url="https://notion.so/2a077d23ba9a8088a24eea64dfe6525c",
    )


def test_missing_priority_and_due_date(fake_post):
    _, responses = fake_post
    responses.append(FakeResponse({"results": [page(priority=None, due=None)], "has_more": False}))
    (task,) = fetch_todo_tasks("db", "key")
    assert task.notion_priority is None
    assert task.due_date is None


def test_untitled_page(fake_post):
    _, responses = fake_post
    responses.append(FakeResponse({"results": [page(title="")], "has_more": False}))
    (task,) = fetch_todo_tasks("db", "key")
    assert task.title == "Untitled"


def test_api_error_raises(fake_post):
    _, responses = fake_post
    responses.append(FakeResponse({"message": "nope"}, status_code=401))
    with pytest.raises(RuntimeError, match="401"):
        fetch_todo_tasks("db", "key")


def test_database_id_from_env_only(monkeypatch):
    monkeypatch.delenv("NOTION_DATABASE_ID", raising=False)
    with pytest.raises(ValueError, match="NOTION_DATABASE_ID"):
        fetch_todo_tasks(api_key="key")


def test_category_select_multi_select_and_missing(fake_post):
    _, responses = fake_post
    with_select = page(page_id="a-1")
    with_select["properties"]["Category"] = {"type": "select", "select": {"name": "Finance"}}
    with_multi = page(page_id="a-2")
    with_multi["properties"]["Category"] = {"type": "multi_select", "multi_select": [{"name": "Work"}, {"name": "Health"}]}
    empty = page(page_id="a-3")
    empty["properties"]["Category"] = {"type": "select", "select": None}
    responses.append(FakeResponse({"results": [with_select, with_multi, empty, page(page_id="a-4")], "has_more": False}))
    tasks = fetch_todo_tasks("db", "key")
    assert [t.category for t in tasks] == ["Finance", "Work / Health", None, None]


def test_update_priority_sends_the_right_request(monkeypatch):
    sent = {}

    def patch(url, json=None, headers=None, timeout=None):
        sent.update(url=url, json=json, headers=headers)
        return FakeResponse({})

    monkeypatch.setattr(notion_source.httpx, "patch", patch)
    notion_source.update_priority("abc-123", "HIGH", api_key="k")
    assert sent["url"] == "https://api.notion.com/v1/pages/abc-123"
    assert sent["json"] == {"properties": {"Priority": {"select": {"name": "High"}}}}
    assert sent["headers"]["Authorization"] == "Bearer k"


def test_update_priority_error_has_a_permission_hint(monkeypatch):
    monkeypatch.setattr(notion_source.httpx, "patch", lambda *a, **k: FakeResponse({"message": "no"}, status_code=403))
    with pytest.raises(RuntimeError, match="Update content"):
        notion_source.update_priority("abc", "LOW", api_key="k")
    monkeypatch.setattr(notion_source.httpx, "patch", lambda *a, **k: FakeResponse({"message": "bad"}, status_code=400))
    with pytest.raises(RuntimeError, match="400") as info:
        notion_source.update_priority("abc", "LOW", api_key="k")
    assert "Update content" not in str(info.value)
