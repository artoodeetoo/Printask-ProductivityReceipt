import os

import pytest

from simple_printer import printing
from simple_printer.notion_source import NotionTask
from simple_printer.priority import PriorityResult


def make_task(due="2026-11-01T10:00:00.000+00:00"):
    return NotionTask(id="x", title="Ship it", notion_priority=None, due_date=due, url="https://notion.so/abc")


class FakePrinter:
    instances = []

    def __init__(self, host, port=9100, profile=None):
        self.args = (host, port, profile)
        self.calls = []
        FakePrinter.instances.append(self)

    def image(self, path, **kwargs):
        self.calls.append(("image", path, kwargs))

    def ln(self, n):
        self.calls.append(("ln", n))

    def cut(self):
        self.calls.append(("cut",))

    def close(self):
        self.calls.append(("close",))


@pytest.fixture
def fake_env(monkeypatch, tmp_path):
    FakePrinter.instances = []
    cards = []
    image = tmp_path / "card.png"

    def create(card):
        cards.append(card)
        image.write_bytes(b"png")
        return str(image)

    monkeypatch.setattr(printing, "Network", FakePrinter)
    monkeypatch.setattr(printing, "create_task_image", create)
    monkeypatch.setenv("PRINTER_HOST", "10.0.0.5")
    monkeypatch.delenv("PRINTER_PORT", raising=False)
    monkeypatch.delenv("ENABLE_QR_CODES", raising=False)
    return cards, image


def test_call_order_and_args(fake_env):
    cards, image = fake_env
    printing.print_task(make_task(), PriorityResult("HIGH", "r"))
    (printer,) = FakePrinter.instances
    assert printer.args == ("10.0.0.5", 9100, "TM-T88V")
    assert [c[0] for c in printer.calls] == ["image", "ln", "cut", "close"]
    assert printer.calls[0][2] == {"impl": "bitImageColumn", "center": True}
    assert printer.calls[1] == ("ln", 3)
    assert cards[0] == {"title": "Ship it", "priority": "HIGH", "due_date": "2026-11-01",
                        "qr_code_data": "https://notion.so/abc", "category": None}
    assert not image.exists()  # temp card cleaned up


def test_port_and_qr_env(fake_env, monkeypatch):
    cards, _ = fake_env
    monkeypatch.setenv("PRINTER_PORT", "9200")
    monkeypatch.setenv("ENABLE_QR_CODES", "false")
    printing.print_task(make_task(due=None), "LOW")
    assert FakePrinter.instances[0].args[1] == 9200
    assert cards[0]["qr_code_data"] is None
    assert cards[0]["due_date"] == "Today"
    assert cards[0]["priority"] == "LOW"


def test_missing_host_raises(fake_env, monkeypatch):
    monkeypatch.delenv("PRINTER_HOST")
    with pytest.raises(RuntimeError, match="PRINTER_HOST"):
        printing.print_task(make_task(), "HIGH")
    assert FakePrinter.instances == []


def test_print_error_still_closes_and_propagates(fake_env, monkeypatch):
    def boom(self):
        raise OSError("paper out")

    monkeypatch.setattr(FakePrinter, "cut", boom)
    with pytest.raises(OSError):
        printing.print_task(make_task(), "HIGH")
    assert FakePrinter.instances[0].calls[-1] == ("close",)


def test_real_card_renders(monkeypatch):
    path = printing.create_task_image(
        {"title": "Hello", "priority": "HIGH", "due_date": "2026-11-01", "qr_code_data": "https://notion.so/abc"}
    )
    assert path and os.path.exists(path)
    os.remove(path)


def test_category_is_passed_to_the_card(fake_env, monkeypatch):
    cards, _ = fake_env
    task = make_task()
    task.category = "Finance"
    printing.print_task(task, "HIGH")
    assert cards[0]["category"] == "Finance"


def test_category_can_be_switched_off(fake_env, monkeypatch):
    cards, _ = fake_env
    monkeypatch.setenv("SHOW_CATEGORY", "false")
    task = make_task()
    task.category = "Finance"
    printing.print_task(task, "HIGH")
    assert cards[0]["category"] is None


def test_long_category_is_trimmed_and_blank_ignored(fake_env):
    cards, _ = fake_env
    task = make_task()
    task.category = "x" * 80
    printing.print_task(task, "HIGH")
    assert len(cards[0]["category"]) == 30
    task.category = "   "
    printing.print_task(task, "HIGH")
    assert cards[1]["category"] is None


def test_real_card_with_category_is_taller_than_without():
    from PIL import Image
    base = {"title": "Hello", "priority": "HIGH", "due_date": "2026-11-01", "qr_code_data": None}
    plain = printing.create_task_image(dict(base))
    with_cat = printing.create_task_image({**base, "category": "Administrative"})
    try:
        assert Image.open(with_cat).height == Image.open(plain).height + 40
    finally:
        os.remove(plain)
        os.remove(with_cat)


def _label_ink(card):
    """Share of black dots in the band under the QR code once the card is converted for the printer."""
    from simple_printer.card import sample

    image = sample.as_printed(card).convert("L")
    band = image.crop((100, image.height - 40, 412, image.height - 8))
    histogram = band.histogram()
    return sum(histogram[:128]) / (band.width * band.height)


def test_qr_label_survives_the_printers_black_and_white_conversion():
    # Light grey 16px text printed as sparse specks (about 2.5% ink); solid bold text is far denser
    card = {"title": "Hello", "priority": "HIGH", "due_date": "2026-11-01", "qr_code_data": "https://x.example"}
    assert _label_ink(card) > 0.06


def test_sample_is_one_bit_and_card_width():
    from simple_printer.card import sample

    image = sample.as_printed(sample.SAMPLE_CARD)
    assert image.mode == "1" and image.width == 512
