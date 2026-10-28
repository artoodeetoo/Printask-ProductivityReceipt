"""Fetch recent Slack messages from chosen channels as task candidates.

Uses the Slack Web API directly (httpx), polled on the same loop as Notion.
The bot needs the ``channels:history`` scope (``groups:history`` for private
channels) and must be invited to each channel.
"""

import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

API = "https://slack.com/api"
MODES = ("claude", "all", "bots", "keyword")
TITLE_MAX = 80
IGNORED_SUBTYPES_EXCEPT = {None, "bot_message"}  # every other subtype is system noise (joins, edits, ...)


@dataclass
class SlackMessage:
    """A Slack message, shaped like a task so history and printing can use it."""

    id: str
    title: str
    text: str
    channel: str
    ts: str
    url: str
    is_bot: bool
    due_date: Optional[str] = None
    notion_priority: Optional[str] = None
    category: Optional[str] = None
    source: str = "slack"


def clean_text(text: str) -> str:
    """Turn Slack mrkdwn into plain text: links, mentions and entities."""
    text = re.sub(r"<(https?://[^|>]+)\|([^>]+)>", r"\2 (\1)", text)
    text = re.sub(r"<(https?://[^>]+)>", r"\1", text)
    text = re.sub(r"<#[A-Z0-9]+\|([^>]+)>", r"#\1", text)
    text = re.sub(r"<@([A-Z0-9]+)(?:\|([^>]+))?>", lambda m: "@" + (m.group(2) or m.group(1)), text)
    text = re.sub(r"<!(?:here|channel|everyone)[^>]*>", "", text)
    text = text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    return " ".join(text.split())


def _message_text(message: Dict[str, Any]) -> str:
    """Body text, plus attachment text: ticket bots often post only attachments."""
    parts = [message.get("text") or ""]
    for attachment in message.get("attachments") or []:
        for key in ("pretext", "title", "text", "fallback"):
            value = attachment.get(key)
            if value and value not in parts:
                parts.append(value)
    return clean_text("\n".join(p for p in parts if p))


def provisional_title(text: str) -> str:
    first = text.split("\n")[0].strip() if text else ""
    first = first or text
    return first if len(first) <= TITLE_MAX else first[: TITLE_MAX - 1].rstrip() + "…"


def _get(method: str, token: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    response = httpx.get(f"{API}/{method}", params=params or {}, timeout=30,
                         headers={"Authorization": f"Bearer {token}"})
    if response.status_code == 429:
        raise RuntimeError(f"Slack rate limit on {method}; retry after "
                           f"{response.headers.get('Retry-After', '?')}s")
    if response.status_code != 200:
        raise RuntimeError(f"Slack HTTP error {response.status_code} on {method}")
    data = response.json()
    if not data.get("ok"):
        error = data.get("error", "unknown_error")
        hint = " (invite the bot to the channel: /invite @botname)" if error == "not_in_channel" else ""
        raise RuntimeError(f"Slack error '{error}' on {method}{hint}")
    return data


def fetch_messages(
    token: str,
    channels: List[str],
    mode: str = "claude",
    keyword: str = "",
    lookback_hours: float = 24,
    max_per_channel: int = 200,
    now: Optional[float] = None,
) -> List[SlackMessage]:
    """Messages from the last ``lookback_hours`` that pass the mode's filter, oldest first.

    Modes: ``claude`` and ``all`` keep every message (Claude judges them later in
    ``claude`` mode); ``bots`` keeps only bot posts; ``keyword`` keeps messages
    containing ``keyword`` (case-insensitive).
    """
    if mode not in MODES:
        raise ValueError(f"SLACK_TASK_MODE must be one of {', '.join(MODES)}; got {mode!r}")
    if mode == "keyword" and not keyword:
        raise ValueError("SLACK_KEYWORD is required when SLACK_TASK_MODE=keyword")

    auth = _get("auth.test", token)
    workspace_url = (auth.get("url") or "").rstrip("/")
    own_user = auth.get("user_id")
    oldest = (now if now is not None else time.time()) - lookback_hours * 3600

    found: List[SlackMessage] = []
    for channel in channels:
        cursor: Optional[str] = None
        collected = 0
        while collected < max_per_channel:
            params: Dict[str, Any] = {"channel": channel, "oldest": f"{oldest:.6f}", "limit": 100}
            if cursor:
                params["cursor"] = cursor
            data = _get("conversations.history", token, params)
            for message in data.get("messages", []):
                collected += 1
                if message.get("subtype") not in IGNORED_SUBTYPES_EXCEPT:
                    continue
                if message.get("user") == own_user:
                    continue
                text = _message_text(message)
                if not text:
                    continue
                is_bot = bool(message.get("bot_id")) or message.get("subtype") == "bot_message"
                if mode == "bots" and not is_bot:
                    continue
                if mode == "keyword" and keyword.lower() not in text.lower():
                    continue
                ts = message["ts"]
                found.append(SlackMessage(
                    id=f"slack:{channel}:{ts}",
                    title=provisional_title(text),
                    text=text,
                    channel=channel,
                    ts=ts,
                    url=f"{workspace_url}/archives/{channel}/p{ts.replace('.', '')}" if workspace_url else "",
                    is_bot=is_bot,
                ))
            cursor = (data.get("response_metadata") or {}).get("next_cursor")
            if not data.get("has_more") or not cursor:
                break

    found.sort(key=lambda m: float(m.ts))
    logger.info("Fetched %d Slack message(s) from %d channel(s) (mode=%s)", len(found), len(channels), mode)
    return found
