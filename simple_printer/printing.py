"""Network-only printing of a task card to an ESC/POS printer on port 9100."""

import logging
import os
import threading
from typing import Any, Optional, Union

from escpos.printer import Network

from . import settings
from .card.image_generator import create_task_image

logger = logging.getLogger(__name__)

PRINTER_PROFILE = "TM-T88V"  # generic Epson profile; also works on the Rongta ACE-H1
DEFAULT_PORT = 9100
_print_lock = threading.Lock()  # the poll loop and the web UI must not use the printer at once


def _due_date(task: Any) -> str:
    # image_generator only parses YYYY-MM-DD (or "today"); Notion may add a time part.
    due = getattr(task, "due_date", None)
    return due[:10] if due else "Today"


def _category(task: Any) -> Optional[str]:
    if settings.get("SHOW_CATEGORY").lower() == "false":
        return None
    category = (getattr(task, "category", None) or "").strip()
    return category[:30] or None


def _qr_enabled() -> bool:
    return settings.get("ENABLE_QR_CODES").lower() != "false"


def print_task(task: Any, priority: Union[str, Any]) -> None:
    """Render ``task`` as a card and print it. Raises on any failure.

    ``priority`` is "HIGH"/"MEDIUM"/"LOW" or an object with a ``.priority`` attribute.
    """
    host = os.getenv("PRINTER_HOST")
    if not host:
        raise RuntimeError("PRINTER_HOST is not set; cannot reach the network printer")
    port = int(os.getenv("PRINTER_PORT", str(DEFAULT_PORT)))

    card = {
        "title": task.title,
        "priority": getattr(priority, "priority", priority),
        "due_date": _due_date(task),
        "qr_code_data": task.url if _qr_enabled() else None,
        "category": _category(task),
    }
    image_path = create_task_image(card)
    if not image_path:
        raise RuntimeError("create_task_image returned no image")

    with _print_lock:
        _send(host, port, image_path)
    logger.info("Printed %r to %s:%s", task.title, host, port)


def _send(host: str, port: int, image_path: str) -> None:
    printer = Network(host, port=port, profile=PRINTER_PROFILE)
    try:
        printer.image(image_path, impl="bitImageColumn", center=True)
        printer.ln(3)  # feed so the QR section clears the cutter
        printer.cut()
    finally:
        try:
            printer.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            os.remove(image_path)
        except OSError:
            pass
