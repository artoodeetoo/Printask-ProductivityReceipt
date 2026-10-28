"""Pillow imports for card rendering."""

try:
    from PIL import Image, ImageDraw, ImageFont

    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False
    Image = None
    ImageDraw = None
    ImageFont = None
    print("Warning: PIL not installed. Task card images will not be generated.")
