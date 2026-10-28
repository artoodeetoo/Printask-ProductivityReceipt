"""Poll loop: Notion To Do tasks -> Claude priority -> network printer."""

import json
import logging
import os
import signal
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import notion_source, printing, priority, settings, slack_source
from .status import Status
from .history import DEFAULT_HISTORY_PATH, History

logger = logging.getLogger("simple_printer")

DEFAULT_POLL_INTERVAL = 300
DEFAULT_STATE_PATH = "/data/printed.json"  # legacy file, imported into the history DB
DEFAULT_DEDUPE_DAYS = 7
REQUIRED_ENV = ("PRINTER_HOST",)  # plus at least one source: Notion and/or Slack


@dataclass
class Source:
    """A place tasks come from.

    ``enrich`` (optional) turns a raw candidate into ``(task, PriorityResult)`` or
    ``None`` if it is not a task. It runs only for candidates not already in the
    history. ``max_per_cycle`` caps how many new candidates are handled per cycle;
    the rest are picked up on the next one.
    """

    name: str
    fetch: Callable[[], List]
    enrich: Optional[Callable[[Any], Optional[Tuple[Any, Any]]]] = None
    max_per_cycle: Optional[int] = None
    # called after a successful print and history record; returns True if it wrote something
    after_print: Optional[Callable[[Any, Any], bool]] = None


def run_cycle(
    history: History,
    fetch: Optional[Callable[[], List]] = None,
    validate: Optional[Callable] = None,
    print_task: Optional[Callable] = None,
    stop: Optional[threading.Event] = None,
    dedupe_days: int = DEFAULT_DEDUPE_DAYS,
    sources: Optional[List[Source]] = None,
) -> Dict[str, int]:
    """One pass over every source. A failure on one task or one source never stops the others.

    Raises only if every source failed to fetch.
    """
    if sources is None:
        sources = [Source("notion", fetch or (lambda: notion_source.fetch_todo_tasks()))]
    validate = validate or priority.validate_priority
    print_task = print_task or printing.print_task
    stats = {"fetched": 0, "skipped": 0, "duplicates": 0, "ignored": 0, "printed": 0,
             "failed": 0, "source_errors": 0, "written_back": 0, "writeback_failed": 0}

    for source in sources:
        try:
            tasks = source.fetch()
        except Exception:  # noqa: BLE001
            logger.exception("Fetching from %s failed", source.name)
            stats["source_errors"] += 1
            continue
        stats["fetched"] += len(tasks)
        handled = 0
        for task in tasks:
            if stop is not None and stop.is_set():
                break
            if history.has_seen(task.id):
                stats["skipped"] += 1
                continue
            if source.max_per_cycle is not None and handled >= source.max_per_cycle:
                logger.info("%s: per-cycle cap (%d) reached; remaining messages wait for the next cycle",
                            source.name, source.max_per_cycle)
                break
            handled += 1
            try:
                result = None
                if source.enrich:
                    enriched = source.enrich(task)
                    if enriched is None:
                        history.record_ignored(task, "not a task")
                        stats["ignored"] += 1
                        continue
                    task, result = enriched
                original = history.find_duplicate(task.title, dedupe_days)
                if original:
                    history.record_duplicate(task, original)
                    logger.info("Skipping %r (%s): same title as %s printed in the last %d days",
                                task.title, task.id, original, dedupe_days)
                    stats["duplicates"] += 1
                    continue
                result = result or validate(task)
                print_task(task, result)
                history.record_printed(task, result)  # only after a successful print
                stats["printed"] += 1
                if source.after_print:
                    try:
                        if source.after_print(task, result):
                            stats["written_back"] += 1
                    except Exception as exc:  # noqa: BLE001 - the card already printed; never fail the task
                        logger.warning("Write-back for %r failed: %s", task.title, exc)
                        stats["writeback_failed"] += 1
            except Exception:  # noqa: BLE001
                logger.exception("Failed to process task %s (%r)", task.id, task.title)
                stats["failed"] += 1
    logger.info("Cycle done: %s", stats)
    if stats["source_errors"] == len(sources):
        raise RuntimeError("Every source failed to fetch")
    return stats


def _notion_write_back(task, result) -> bool:
    """Set the Notion Priority to the priority that printed, if Claude changed it."""
    if getattr(result, "fallback", False):
        return False  # no decision was made, so there is nothing to write
    current = (task.notion_priority or "").strip().casefold()
    if current == result.priority.casefold():
        return False
    notion_source.update_priority(task.id, result.priority)
    logger.info("Notion priority for %r: %s -> %s", task.title, task.notion_priority or "unset", result.priority)
    return True


def _slack_enrich(mode: str) -> Optional[Callable]:
    """Claude mode: Claude decides if a message is a task and fills in title/priority/due date."""
    if mode != "claude":
        return None

    def enrich(message):
        extracted = priority.extract_task(message.text)
        if extracted is None:
            return None
        message.title = extracted.title
        message.due_date = extracted.due_date
        return message, priority.PriorityResult(extracted.priority, extracted.reason)

    return enrich


def build_sources() -> List[Source]:
    """Sources enabled by the environment: Notion and/or Slack."""
    sources: List[Source] = []
    if os.getenv("NOTION_API_KEY") and os.getenv("NOTION_DATABASE_ID"):
        write_back = settings.get("NOTION_WRITE_BACK_PRIORITY").lower() == "true"
        sources.append(Source("notion", lambda: notion_source.fetch_todo_tasks(),
                              after_print=_notion_write_back if write_back else None))
    if os.getenv("SLACK_BOT_TOKEN") and settings.get("SLACK_CHANNEL_IDS"):
        token = os.environ["SLACK_BOT_TOKEN"]
        channels = [c.strip() for c in settings.get("SLACK_CHANNEL_IDS").split(",") if c.strip()]
        mode = settings.get("SLACK_TASK_MODE").lower()
        keyword = settings.get("SLACK_KEYWORD")
        lookback = float(settings.get("SLACK_LOOKBACK_HOURS"))
        cap = int(settings.get("SLACK_MAX_PER_CYCLE"))
        # validate settings now so a typo fails at startup, not at the first poll
        if mode not in slack_source.MODES:
            raise ValueError(f"SLACK_TASK_MODE must be one of {', '.join(slack_source.MODES)}; got {mode!r}")
        if mode == "keyword" and not keyword:
            raise ValueError("SLACK_KEYWORD is required when SLACK_TASK_MODE=keyword")
        if mode == "claude" and not os.getenv("ANTHROPIC_API_KEY"):
            raise ValueError("ANTHROPIC_API_KEY is required when SLACK_TASK_MODE=claude")
        sources.append(Source(
            "slack",
            lambda: slack_source.fetch_messages(token, channels, mode=mode, keyword=keyword,
                                                lookback_hours=lookback),
            enrich=_slack_enrich(mode),
            max_per_cycle=cap,
        ))
    return sources


class _Health:
    def __init__(self):
        self.last: Dict = {"status": "starting"}


def _start_health_server(port: int, health: _Health) -> HTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/health":
                self.send_error(404)
                return
            body = json.dumps(health.last).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main(argv=None) -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    missing = [name for name in REQUIRED_ENV if not os.getenv(name)]
    if missing:
        logger.error("Missing required environment variables: %s", ", ".join(missing))
        return 2
    try:
        settings.configure(os.getenv("SETTINGS_PATH", settings.DEFAULT_SETTINGS_PATH))
        sources = build_sources()
    except ValueError as exc:
        logger.error("%s", exc)
        return 2
    except RuntimeError as exc:
        logger.error("%s", exc)
        return 1
    if not sources:
        logger.error("No task source configured: set NOTION_API_KEY + NOTION_DATABASE_ID "
                     "and/or SLACK_BOT_TOKEN + SLACK_CHANNEL_IDS")
        return 2
    logger.info("Sources: %s", ", ".join(source.name for source in sources))

    state_path = os.getenv("STATE_PATH", DEFAULT_STATE_PATH)
    history_path = os.getenv("HISTORY_PATH", DEFAULT_HISTORY_PATH)
    try:
        history = History(history_path, legacy_json_path=state_path)
    except RuntimeError as exc:
        logger.error("%s", exc)
        return 1

    stop = threading.Event()
    status = Status()
    status.set_sources([source.name for source in sources])

    def request_stop(*_):
        stop.set()
        status.wake.set()  # interrupt the sleep between polls

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, request_stop)

    health = _Health()
    health_port = os.getenv("HEALTH_PORT")
    server = _start_health_server(int(health_port), health) if health_port else None
    ui_server = None
    ui_port = os.getenv("UI_PORT")
    if ui_port:
        if os.getenv("UI_PASSWORD"):
            from . import webui

            if len(os.environ["UI_PASSWORD"]) < 12:
                logger.warning("UI_PASSWORD is shorter than 12 characters; use a longer one, it is the "
                               "only protection for the web UI")
            ui_server = webui.start(int(ui_port), os.environ["UI_PASSWORD"], history_path, status)
            logger.info("Web UI listening on port %s", ui_port)
        else:
            logger.warning("UI_PORT is set but UI_PASSWORD is not: the web UI stays off")

    exit_code = 0
    try:
        while not stop.is_set():
            try:
                current = build_sources()  # re-read so settings changes apply on this poll
                status.set_sources([source.name for source in current])
                stats = run_cycle(history, sources=current, stop=stop,
                                  dedupe_days=int(settings.get("DEDUPE_WINDOW_DAYS")))
                health.last = {"status": "ok", **stats}
                status.record_cycle(stats)
                exit_code = 0
            except Exception as exc:  # noqa: BLE001 - fetch/settings errors: log and retry next cycle
                logger.exception("Cycle failed")
                health.last = {"status": "error"}
                status.record_error(f"{type(exc).__name__}: {exc}")
                exit_code = 1
            interval = int(settings.get("POLL_INTERVAL_SECONDS"))
            if interval <= 0:
                break
            exit_code = 0  # a polling service only exits non-zero if a one-shot run failed
            status.wake.wait(interval)
            status.wake.clear()
    finally:
        for srv in (server, ui_server):
            if srv:
                srv.shutdown()
        history.close()
    logger.info("Shut down")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
