"""Local SQLite history of tasks seen: what printed, when, with what priority,
and which tasks were skipped as duplicates. Stdlib only, no external service."""

import json
import logging
import os
import re
import sqlite3
import sys
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_HISTORY_PATH = "/data/history.db"
NOT_DEDUPED = {"", "untitled"}  # placeholder titles never count as duplicates

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    page_id      TEXT PRIMARY KEY,
    title        TEXT NOT NULL,
    title_key    TEXT NOT NULL,
    status       TEXT NOT NULL,
    priority     TEXT,
    reason       TEXT,
    due_date     TEXT,
    url          TEXT,
    duplicate_of TEXT,
    created_at   TEXT NOT NULL,
    source       TEXT NOT NULL DEFAULT 'notion'
);
CREATE INDEX IF NOT EXISTS idx_tasks_title_key ON tasks (title_key, status, created_at);
"""

# Status values: printed | duplicate | ignored (a chat message Claude judged not to be a task)
STATUSES = ("printed", "duplicate", "ignored")


def normalize_title(title: str) -> str:
    """Lowercase, strip punctuation and collapse whitespace so trivial variants match."""
    text = unicodedata.normalize("NFKC", title or "").casefold()
    text = re.sub(r"[^\w\s]", " ", text)
    return " ".join(text.split())


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class History:
    STATUSES = STATUSES

    def __init__(self, path: str, legacy_json_path: Optional[str] = None):
        self.path = path
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        try:
            self._db = sqlite3.connect(path, timeout=30)
            self._migrate()
            self._db.executescript(SCHEMA)
        except sqlite3.DatabaseError as exc:
            # A damaged DB must stop the app, not silently reprint everything.
            raise RuntimeError(f"History database {path} is unusable: {exc}") from exc
        if legacy_json_path:
            self._import_legacy(legacy_json_path)

    def close(self) -> None:
        self._db.close()

    def _migrate(self) -> None:
        """Upgrade a database from the first schema (status CHECK, no source column)."""
        row = self._db.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'tasks'"
        ).fetchone()
        if row is None or ("CHECK" not in row[0] and "source" in row[0]):
            return
        logger.info("Migrating history database to the current schema")
        self._db.executescript(
            "BEGIN;"
            "DROP INDEX IF EXISTS idx_tasks_title_key;"
            "ALTER TABLE tasks RENAME TO tasks_old;"
            + SCHEMA.replace("CREATE TABLE IF NOT EXISTS", "CREATE TABLE")
            + "INSERT INTO tasks (page_id, title, title_key, status, priority, reason, due_date, url,"
              " duplicate_of, created_at, source) "
              "SELECT page_id, title, title_key, status, priority, reason, due_date, url,"
              " duplicate_of, created_at, 'notion' FROM tasks_old;"
            "DROP TABLE tasks_old;"
            "COMMIT;"
        )

    def _import_legacy(self, json_path: str) -> None:
        """Import page IDs from the old printed.json so upgrading never reprints."""
        try:
            with open(json_path) as f:
                ids = json.load(f)["printed"]
        except FileNotFoundError:
            return
        except (ValueError, KeyError, TypeError) as exc:
            raise RuntimeError(f"State file {json_path} is corrupt: {exc}") from exc
        now = _stamp(_now())
        with self._db:
            cursor = self._db.executemany(
                "INSERT OR IGNORE INTO tasks (page_id, title, title_key, status, reason, created_at) "
                "VALUES (?, '', '', 'printed', 'imported from printed.json', ?)",
                [(page_id, now) for page_id in ids],
            )
        if cursor.rowcount:
            logger.info("Imported %d printed task(s) from %s", cursor.rowcount, json_path)
        # Rename it so a later "clear history" is not undone by re-importing it on restart.
        try:
            os.replace(json_path, json_path + ".imported")
        except OSError as exc:
            logger.warning("Could not rename %s after importing it (%s); it will be re-imported on "
                           "restart", json_path, exc)

    def has_seen(self, page_id: str) -> bool:
        row = self._db.execute("SELECT 1 FROM tasks WHERE page_id = ?", (page_id,)).fetchone()
        return row is not None

    def find_duplicate(self, title: str, window_days: int) -> Optional[str]:
        """Page ID of a task with the same normalized title printed within
        ``window_days`` days, or None. ``window_days <= 0`` disables the check."""
        key = normalize_title(title)
        if window_days <= 0 or key in NOT_DEDUPED:
            return None
        cutoff = _stamp(_now() - timedelta(days=window_days))
        row = self._db.execute(
            "SELECT page_id FROM tasks WHERE title_key = ? AND status = 'printed' AND created_at >= ? "
            "ORDER BY created_at DESC LIMIT 1",
            (key, cutoff),
        ).fetchone()
        return row[0] if row else None

    def _record(self, task: Any, status: str, priority=None, reason=None, duplicate_of=None) -> None:
        if status not in STATUSES:
            raise ValueError(f"unknown status {status!r}")
        with self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO tasks "
                "(page_id, title, title_key, status, priority, reason, due_date, url, duplicate_of,"
                " created_at, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (task.id, task.title, normalize_title(task.title), status, priority, reason,
                 getattr(task, "due_date", None), getattr(task, "url", None), duplicate_of,
                 _stamp(_now()), getattr(task, "source", "notion")),
            )

    def record_printed(self, task: Any, result: Any) -> None:
        self._record(task, "printed", priority=result.priority, reason=result.reason)

    def record_duplicate(self, task: Any, duplicate_of: str) -> None:
        self._record(task, "duplicate", duplicate_of=duplicate_of)

    def record_ignored(self, task: Any, reason: str = "") -> None:
        """Remember a message that is not a task, so it is not sent to Claude every cycle."""
        self._record(task, "ignored", reason=reason)

    CLEAR_SCOPES = ("all", "ignored", "duplicate", "noise", "older")

    def _clear_where(self, scope: str, older_than_days: Optional[int]):
        if scope == "all":
            return "", ()
        if scope in ("ignored", "duplicate"):
            return "WHERE status = ?", (scope,)
        if scope == "noise":
            return "WHERE status IN ('ignored', 'duplicate')", ()
        if scope == "older":
            if not isinstance(older_than_days, int) or older_than_days < 1:
                raise ValueError("older_than_days must be a whole number of at least 1")
            return "WHERE created_at < ?", (_stamp(_now() - timedelta(days=older_than_days)),)
        raise ValueError(f"unknown scope {scope!r}")

    def count_for_clear(self, scope: str, older_than_days: Optional[int] = None) -> int:
        """How many rows ``clear`` with the same arguments would delete."""
        where, params = self._clear_where(scope, older_than_days)
        return self._db.execute(f"SELECT COUNT(*) FROM tasks {where}", params).fetchone()[0]

    def clear(self, scope: str, older_than_days: Optional[int] = None) -> int:
        """Forget tasks: ``all``, ``ignored``, ``duplicate``, ``noise`` (ignored + duplicate), or
        ``older`` (every status, created more than ``older_than_days`` days ago). Returns rows deleted."""
        where, params = self._clear_where(scope, older_than_days)
        with self._db:
            deleted = self._db.execute(f"DELETE FROM tasks {where}", params).rowcount
        logger.info("History cleared (%s%s): %d row(s)", scope,
                    f", older than {older_than_days}d" if scope == "older" else "", deleted)
        return deleted

    def recent_detailed(self, limit: int = 100, status: Optional[str] = None) -> List[dict]:
        """Newest first, with the fields the web UI shows. ``status`` filters if given."""
        if status is not None and status not in STATUSES:
            raise ValueError(f"unknown status {status!r}")
        query = ("SELECT created_at, source, status, priority, title, reason, url, page_id FROM tasks "
                 + ("WHERE status = ? " if status else "") + "ORDER BY created_at DESC LIMIT ?")
        params = ((status,) if status else ()) + (limit,)
        keys = ("created_at", "source", "status", "priority", "title", "reason", "url", "page_id")
        return [dict(zip(keys, row)) for row in self._db.execute(query, params).fetchall()]

    def recent(self, limit: int = 20) -> List[tuple]:
        return self._db.execute(
            "SELECT created_at, status, priority, title, page_id, source FROM tasks "
            "ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()


def main(argv=None) -> int:
    """`python -m simple_printer.history [N]`: show the last N tasks seen."""
    argv = sys.argv[1:] if argv is None else argv
    limit = int(argv[0]) if argv else 20
    history = History(os.getenv("HISTORY_PATH", DEFAULT_HISTORY_PATH))
    for created_at, status, priority, title, page_id, source in history.recent(limit):
        print(f"{created_at[:19]}  {source:<6} {status:<9} {priority or '-':<6} {title or '(imported)'}  [{page_id}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
