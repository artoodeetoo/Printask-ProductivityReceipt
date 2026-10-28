"""One-off actions behind the web UI buttons. None of them write to the history."""

import os
from typing import Any, Dict, List

from . import printing, priority, settings, slack_source
from .history import History
from .notion_source import NotionTask
from .priority import PriorityResult

PREVIEW_LIMIT = 10


def print_test_card() -> str:
    """Print a sample card. Raises on any printer error."""
    task = NotionTask(id="printask-test", title="Printask test card", notion_priority=None,
                      due_date=None, url="https://example.com/printask-test",
                      category="Test")
    printing.print_task(task, PriorityResult("MEDIUM", "test card"))
    return f"Test card sent to {os.getenv('PRINTER_HOST')}:{os.getenv('PRINTER_PORT', '9100')}"


def slack_configured() -> bool:
    return bool(os.getenv("SLACK_BOT_TOKEN") and settings.get("SLACK_CHANNEL_IDS"))


def preview_slack(history: History) -> List[Dict[str, Any]]:
    """Show what the next poll would do with recent Slack messages, without printing,
    recording anything, or classifying messages that were already handled."""
    token = os.getenv("SLACK_BOT_TOKEN")
    channels = [c.strip() for c in settings.get("SLACK_CHANNEL_IDS").split(",") if c.strip()]
    if not token or not channels:
        raise RuntimeError("Slack is not configured: set SLACK_BOT_TOKEN and the channel IDs")
    mode = settings.get("SLACK_TASK_MODE")
    messages = slack_source.fetch_messages(
        token, channels, mode=mode, keyword=settings.get("SLACK_KEYWORD"),
        lookback_hours=float(settings.get("SLACK_LOOKBACK_HOURS")))
    rows: List[Dict[str, Any]] = []
    classified = 0
    for message in messages:
        row = {"text": message.text[:200], "url": message.url, "decision": "", "title": "", "priority": ""}
        if history.has_seen(message.id):
            row["decision"] = "already handled"
        elif mode != "claude":
            row.update(decision="would be sent to the priority check", title=message.title)
        elif classified >= PREVIEW_LIMIT:
            row["decision"] = f"not checked (preview limit of {PREVIEW_LIMIT} new messages)"
        else:
            classified += 1
            try:
                extracted = priority.extract_task(message.text)
                if extracted is None:
                    row["decision"] = "not a task"
                else:
                    row.update(decision="task", title=extracted.title, priority=extracted.priority)
            except Exception as exc:  # noqa: BLE001
                row["decision"] = f"error: {type(exc).__name__}: {exc}"
        rows.append(row)
    return rows
