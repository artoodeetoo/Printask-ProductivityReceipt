"""State shared between the poll loop and the web UI."""

import threading
import time
from typing import Dict, List, Optional


class Status:
    def __init__(self):
        self._lock = threading.Lock()
        self.started_at = time.time()
        self.sources: List[str] = []
        self.last_cycle_at: Optional[float] = None
        self.last_stats: Dict[str, int] = {}
        self.last_error: Optional[str] = None
        self.wake = threading.Event()  # set to start the next cycle immediately

    def set_sources(self, names: List[str]) -> None:
        with self._lock:
            self.sources = list(names)

    def record_cycle(self, stats: Dict[str, int]) -> None:
        with self._lock:
            self.last_cycle_at, self.last_stats, self.last_error = time.time(), dict(stats), None

    def record_error(self, message: str) -> None:
        with self._lock:
            self.last_cycle_at, self.last_error = time.time(), message

    def snapshot(self) -> Dict:
        with self._lock:
            return {"started_at": self.started_at, "sources": list(self.sources),
                    "last_cycle_at": self.last_cycle_at, "last_stats": dict(self.last_stats),
                    "last_error": self.last_error}
