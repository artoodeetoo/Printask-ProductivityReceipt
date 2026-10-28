"""Runtime-editable, non-secret settings.

Values come from, in order: the settings file (written by the web UI), the
environment, then the built-in default. Secrets (API keys, tokens, the UI
password) are never stored here: they stay in the environment.
"""

import json
import os
import re
import tempfile
import threading
from typing import Dict, List, Optional

DEFAULT_SETTINGS_PATH = "/data/settings.json"

# name -> (kind, constraint, default, label)
FIELDS = {
    "SLACK_TASK_MODE": ("choice", ("claude", "all", "bots", "keyword"), "claude",
                        "How Slack messages become tasks"),
    "SLACK_KEYWORD": ("text", 100, "", "Trigger text (keyword mode)"),
    "SLACK_CHANNEL_IDS": ("channels", None, "", "Slack channel IDs, comma-separated"),
    "SLACK_LOOKBACK_HOURS": ("float", (0.1, 168), "24", "Slack lookback (hours)"),
    "SLACK_MAX_PER_CYCLE": ("int", (1, 100), "10", "Max new Slack messages per poll"),
    "POLL_INTERVAL_SECONDS": ("int", (30, 86400), "300", "Poll interval (seconds)"),
    "DEDUPE_WINDOW_DAYS": ("int", (0, 365), "7", "Duplicate-title window (days, 0 = off)"),
    "ENABLE_QR_CODES": ("choice", ("true", "false"), "true", "QR code on cards"),
    "NOTION_WRITE_BACK_PRIORITY": ("choice", ("true", "false"), "false",
                                   "Write Claude's priority back to Notion (needs edit access)"),
    "SHOW_CATEGORY": ("choice", ("true", "false"), "true", "Print the Notion category on cards"),
}

_lock = threading.Lock()
_current: Optional["Settings"] = None


def _validate(name: str, raw: str) -> str:
    """Return the normalized value or raise ValueError with a readable message."""
    kind, constraint, _default, label = FIELDS[name]
    value = raw.strip()
    if kind == "choice":
        value = value.lower()
        if value not in constraint:
            raise ValueError(f"{label}: must be one of {', '.join(constraint)}")
    elif kind == "text":
        if len(value) > constraint or any(ord(c) < 32 for c in value):
            raise ValueError(f"{label}: at most {constraint} plain characters")
    elif kind == "channels":
        value = value.replace(" ", "")
        if value and not re.fullmatch(r"[A-Z0-9]{3,20}(,[A-Z0-9]{3,20})*", value):
            raise ValueError(f"{label}: use IDs like C0123ABCD, separated by commas")
    elif kind == "int":
        try:
            number = int(value)
        except ValueError:
            raise ValueError(f"{label}: must be a whole number") from None
        if not constraint[0] <= number <= constraint[1]:
            raise ValueError(f"{label}: must be between {constraint[0]} and {constraint[1]}")
        value = str(number)
    elif kind == "float":
        try:
            number = float(value)
        except ValueError:
            raise ValueError(f"{label}: must be a number") from None
        if not constraint[0] <= number <= constraint[1]:
            raise ValueError(f"{label}: must be between {constraint[0]} and {constraint[1]}")
        value = f"{number:g}"
    return value


class Settings:
    def __init__(self, path: str):
        self.path = path
        self._overrides: Dict[str, str] = {}
        try:
            with open(path) as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise TypeError("expected an object")
            for name, raw in data.items():
                if name in FIELDS:
                    self._overrides[name] = _validate(name, str(raw))
        except FileNotFoundError:
            pass
        except (ValueError, TypeError) as exc:
            raise RuntimeError(f"Settings file {path} is invalid: {exc}. "
                               "Fix or delete it to use environment values.") from exc

    def get(self, name: str) -> str:
        with _lock:
            if name in self._overrides:
                return self._overrides[name]
        return os.getenv(name, FIELDS[name][2]).strip() or FIELDS[name][2]

    def overrides(self) -> Dict[str, str]:
        with _lock:
            return dict(self._overrides)

    def update(self, submitted: Dict[str, str]) -> List[str]:
        """Apply form values. A blank value removes the override (env/default applies).
        Returns a list of error messages; nothing is saved if there are any."""
        errors: List[str] = []
        new = self.overrides()
        for name in FIELDS:
            if name not in submitted:
                continue
            raw = submitted[name].strip()
            if raw == "":
                new.pop(name, None)
                continue
            try:
                new[name] = _validate(name, raw)
            except ValueError as exc:
                errors.append(str(exc))
        mode = new.get("SLACK_TASK_MODE") or os.getenv("SLACK_TASK_MODE", "claude").strip().lower()
        keyword = new.get("SLACK_KEYWORD") if "SLACK_KEYWORD" in new else os.getenv("SLACK_KEYWORD", "")
        if mode == "keyword" and not (keyword or "").strip():
            errors.append("Trigger text is required when the mode is keyword")
        if errors:
            return errors
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".settings-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(new, f, indent=2, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
        with _lock:
            self._overrides = new
        return []


def configure(path: Optional[str]) -> Optional[Settings]:
    """Install (or, with None, remove) the process-wide settings."""
    global _current
    _current = Settings(path) if path else None
    return _current


def get(name: str) -> str:
    """Current value of a setting: file override, then environment, then default."""
    if _current is not None:
        return _current.get(name)
    return os.getenv(name, FIELDS[name][2]).strip() or FIELDS[name][2]


def current() -> Optional[Settings]:
    """The installed Settings, or None if running purely from the environment."""
    return _current
