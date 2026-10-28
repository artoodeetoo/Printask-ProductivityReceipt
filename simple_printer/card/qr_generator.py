"""QR code generation module for task cards.

This module provides functionality to generate QR codes that link to task sources
(JIRA, Notion, Gmail, etc.) for easy access from printed task cards.
"""

from typing import Optional
from urllib.parse import urlparse

import qrcode
from PIL import Image


def generate_task_qr_code(url: str, size: int = 150) -> Image.Image:
    """Generate a QR code for a task URL.
    
    Creates a QR code image that can be embedded in task cards to provide
    quick access to the original task source (JIRA issue, Notion page, etc.).
    
    Args:
        url: The URL to encode in the QR code. Must be a valid URL.
        size: The desired size of the QR code image in pixels (default: 150).
              The image will be square (size x size).
    
    Returns:
        PIL Image object containing the QR code, resized to exact dimensions.
    
    Raises:
        ValueError: If the URL is empty, invalid, or malformed.
        Exception: If QR code generation fails for any other reason.
    
    Example:
        >>> qr_image = generate_task_qr_code("https://jira.example.com/browse/TASK-123")
        >>> qr_image.save("task_qr.png")
    """
    # Validate URL
    if not url or not url.strip():
        raise ValueError("URL cannot be empty")
    
    url = url.strip()
    
    # Basic URL validation
    try:
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            raise ValueError(f"Invalid URL format: {url}")
    except Exception as e:
        raise ValueError(f"Invalid URL: {url}") from e
    
    try:
        # Create QR code instance with specified settings
        qr = qrcode.QRCode(
            version=1,  # Auto-adjust version based on data
            error_correction=qrcode.constants.ERROR_CORRECT_M,  # 15% recovery
            box_size=3,  # 3 pixels per box
            border=1,  # 1 box border (minimum)
        )
        
        # Add data and generate
        qr.add_data(url)
        qr.make(fit=True)
        
        # Create image
        qr_image = qr.make_image(fill_color="black", back_color="white")
        
        # Resize to exact dimensions
        qr_image = qr_image.resize((size, size), Image.Resampling.LANCZOS)
        
        return qr_image
        
    except Exception as e:
        raise Exception(f"Failed to generate QR code for URL '{url}': {str(e)}") from e


def validate_qr_url(url: Optional[str]) -> bool:
    """Validate if a URL is suitable for QR code generation.
    
    Args:
        url: The URL to validate.
    
    Returns:
        True if the URL is valid and can be used for QR code generation,
        False otherwise.
    
    Example:
        >>> validate_qr_url("https://example.com/task/123")
        True
        >>> validate_qr_url("")
        False
    """
    if not url or not url.strip():
        return False
    
    try:
        parsed = urlparse(url.strip())
        return bool(parsed.scheme and parsed.netloc)
    except Exception:
        return False