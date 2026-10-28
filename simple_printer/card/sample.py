"""Render a sample card exactly as the printer receives it (pure black and white dots).

The print library turns the card into a 1-bit image before sending it, which is why light
grey or tiny text prints as unreadable specks. Run this inside the Docker image so the font
matches the real printout:

    docker run --rm -v "$PWD/docs/images:/out" --entrypoint python printask:latest \
        -m simple_printer.card.sample /out/sample-card.png
"""

import os
import sys
from typing import Optional

from escpos.image import EscposImage
from PIL import Image, ImageOps

from .image_generator import create_task_image

SAMPLE_CARD = {
    "title": "Pay the electricity bill",
    "priority": "HIGH",
    "due_date": "2026-11-07",
    "qr_code_data": "https://www.notion.so",
    "category": "Finance",
}


def as_printed(card: dict) -> Image.Image:
    """The card after the same conversion python-escpos applies: 1-bit, black ink on white."""
    path = create_task_image(card)
    try:
        inverted = EscposImage(path)._im  # python-escpos stores ink as 1, so it is inverted
    finally:
        os.remove(path)
    return ImageOps.invert(inverted.convert("L")).convert("1", dither=Image.Dither.NONE)


def main(argv: Optional[list] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    out = argv[0] if argv else "sample-card.png"
    as_printed(SAMPLE_CARD).save(out)
    print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
