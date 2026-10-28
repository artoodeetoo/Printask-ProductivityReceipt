"""Fetch To Do tasks from a Notion database."""

import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

NOTION_VERSION = "2022-06-28"
QUERY_URL = "https://api.notion.com/v1/databases/{database_id}/query"
TODO_STATUSES = {"todo", "notstarted"}
UNTITLED = "Untitled"


@dataclass
class NotionTask:
    id: str
    title: str
    notion_priority: Optional[str]
    due_date: Optional[str]
    url: str
    category: Optional[str] = None


def _normalize_status(status: str) -> str:
    # Same normalization as agent.py: "To Do" / "to-do" / "Not started" -> "todo" / "notstarted"
    return status.replace(" ", "").replace("-", "").lower()


def _status_of(properties: Dict[str, Any]) -> str:
    for prop in properties.values():
        if prop.get("type") == "status" and prop.get("status"):
            return prop["status"].get("name", "")
    status_prop = properties.get("Status")
    if status_prop and status_prop.get("type") == "select" and status_prop.get("select"):
        return status_prop["select"].get("name", "")
    return ""


def _title_of(properties: Dict[str, Any]) -> str:
    for prop in properties.values():
        if prop.get("type") == "title":
            text = "".join(part.get("plain_text", "") for part in prop.get("title", []))
            return text.strip() or UNTITLED
    return UNTITLED


def _priority_of(properties: Dict[str, Any]) -> Optional[str]:
    prop = properties.get("Priority")
    if prop and prop.get("type") == "select" and prop.get("select"):
        return prop["select"].get("name") or None
    return None


def _category_of(properties: Dict[str, Any]) -> Optional[str]:
    prop = properties.get("Category")
    if not prop:
        return None
    if prop.get("type") == "select" and prop.get("select"):
        return prop["select"].get("name") or None
    if prop.get("type") == "multi_select" and prop.get("multi_select"):
        names = [o.get("name") for o in prop["multi_select"] if o.get("name")]
        return " / ".join(names) or None
    return None


def _due_date_of(properties: Dict[str, Any]) -> Optional[str]:
    prop = properties.get("Due Date")
    if prop and prop.get("type") == "date" and prop.get("date"):
        return prop["date"].get("start") or None
    return None


def _page_url(page_id: str) -> str:
    return f"https://notion.so/{page_id.replace('-', '')}"


def _parse_page(page: Dict[str, Any]) -> Optional[NotionTask]:
    properties = page.get("properties", {})
    if _normalize_status(_status_of(properties)) not in TODO_STATUSES:
        return None
    return NotionTask(
        id=page["id"],
        title=_title_of(properties),
        notion_priority=_priority_of(properties),
        due_date=_due_date_of(properties),
        url=_page_url(page["id"]),
        category=_category_of(properties),
    )


PRIORITY_PROPERTY = "Priority"


def update_priority(page_id: str, priority: str, api_key: Optional[str] = None) -> None:
    """Set the page's ``Priority`` select to ``priority`` (HIGH/MEDIUM/LOW -> High/Medium/Low).

    Needs the integration's "Update content" capability. Raises RuntimeError on failure.
    """
    api_key = api_key or os.getenv("NOTION_API_KEY")
    if not api_key:
        raise ValueError("NOTION_API_KEY is not set")
    response = httpx.patch(
        f"https://api.notion.com/v1/pages/{page_id}",
        json={"properties": {PRIORITY_PROPERTY: {"select": {"name": priority.strip().title()}}}},
        headers={"Authorization": f"Bearer {api_key}", "Notion-Version": NOTION_VERSION,
                 "Content-Type": "application/json"},
        timeout=30,
    )
    if response.status_code != 200:
        hint = (" (give the integration the 'Update content' capability)"
                if response.status_code in (403, 404) else "")
        raise RuntimeError(f"Notion update failed: {response.status_code} - {response.text}{hint}")


def fetch_todo_tasks(database_id: Optional[str] = None, api_key: Optional[str] = None) -> List[NotionTask]:
    """Return all To Do / Not started tasks, following Notion pagination.

    ``database_id`` and ``api_key`` fall back to NOTION_DATABASE_ID / NOTION_API_KEY.
    """
    database_id = database_id or os.getenv("NOTION_DATABASE_ID")
    api_key = api_key or os.getenv("NOTION_API_KEY")
    if not database_id:
        raise ValueError("NOTION_DATABASE_ID is not set")
    if not api_key:
        raise ValueError("NOTION_API_KEY is not set")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }
    url = QUERY_URL.format(database_id=database_id)
    tasks: List[NotionTask] = []
    cursor: Optional[str] = None

    while True:
        body: Dict[str, Any] = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        response = httpx.post(url, json=body, headers=headers, timeout=30)
        if response.status_code != 200:
            raise RuntimeError(f"Notion API error: {response.status_code} - {response.text}")
        data = response.json()
        for page in data.get("results", []):
            task = _parse_page(page)
            if task:
                tasks.append(task)
        if not data.get("has_more") or not data.get("next_cursor"):
            break
        cursor = data["next_cursor"]

    logger.info("Fetched %d To Do tasks from Notion", len(tasks))
    return tasks
