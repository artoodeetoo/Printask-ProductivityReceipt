"""Image generation for task cards."""

import tempfile
import textwrap
from datetime import datetime

from .config import PIL_AVAILABLE, Image, ImageDraw, ImageFont
from .qr_generator import generate_task_qr_code


def _draw_perforation(draw, x1, y, x2):
    """Draw a perforation line (dashed line effect)."""
    dash_length = 10
    gap_length = 8
    current_x = x1
    
    while current_x < x2:
        end_x = min(current_x + dash_length, x2)
        draw.line([(current_x, y), (end_x, y)], fill="#000000", width=2)
        current_x += dash_length + gap_length


def _format_due_date(task_data):
    """Format due date with ordinal suffix."""
    try:
        if "due_date" in task_data:
            due_date_str = task_data["due_date"]
            if due_date_str.lower() == 'today':
                due_date_obj = datetime.now()
            else:
                due_date_obj = datetime.strptime(due_date_str, '%Y-%m-%d')
        else:
            due_date_obj = datetime.now()
    except (ValueError, AttributeError):
        due_date_obj = datetime.now()
    
    # Format with ordinal suffix
    day = due_date_obj.day
    if 4 <= day <= 20 or 24 <= day <= 30:
        suffix = "th"
    else:
        suffix = ["st", "nd", "rd"][day % 10 - 1]
    
    return f"{due_date_obj.strftime('%B %d')}{suffix}"



def create_task_image(task_data):
    """Create task card image with modern format and optional QR code.
    
    NEW FORMAT (used for ALL cards):
    - Priority dots at top
    - Title with dynamic font sizing
    - Perforation separator
    - Due date with ordinal suffix
    - Optional QR code section (if qr_code_data is provided):
      - Perforation separator
      - QR code image
      - "Scan to view source" label
    - Bottom perforation
    
    Features:
    - Gradient styling and modern design
    - Dynamic font sizing based on title length
    - Consistent format whether QR codes are enabled or disabled
    """
    if not PIL_AVAILABLE:
        print("PIL not available - skipping image generation")
        return None

    try:
        # Image dimensions (optimized for thermal printer)
        width = 512  # TM-T88V thermal printer width (512 pixels max - 72mm at 180 DPI)
        
        # Load fonts with better sizing
        import platform
        system = platform.system()
        
        # Font paths for different platforms
        font_paths = []
        bold_font_paths = []
        
        if system == "Darwin":  # macOS
            font_paths = [
                "/System/Library/Fonts/Supplemental/DIN Alternate Bold.ttf",
                "/System/Library/Fonts/Supplemental/DIN Condensed Bold.ttf",
            ]
            bold_font_paths = [
               "/System/Library/Fonts/Supplemental/DIN Alternate Bold.ttf",
                "/System/Library/Fonts/Supplemental/DIN Condensed Bold.ttf",
            ]
        elif system == "Windows":
            font_paths = [
                "C:/Windows/Fonts/arial.ttf",
                "C:/Windows/Fonts/Arial.ttf",
            ]
            bold_font_paths = [
                "C:/Windows/Fonts/arialbd.ttf",
                "C:/Windows/Fonts/Arial Bold.ttf",
            ]
        else:  # Linux
            font_paths = [
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            ]
            bold_font_paths = [
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
            ]
        
        # Check if QR code data exists to determine layout
        has_qr = False
        qr_data = None
        if hasattr(task_data, 'qr_code_data') and task_data.qr_code_data:
            has_qr = True
            qr_data = task_data.qr_code_data
        elif isinstance(task_data, dict) and 'qr_code_data' in task_data and task_data['qr_code_data']:
            has_qr = True
            qr_data = task_data['qr_code_data']
        
        # Get title text
        title_text = task_data.get("title") if isinstance(task_data, dict) else task_data.title
        title_length = len(title_text)
        
        # Calculate dynamic title font size based on length
        if title_length <= 20:
            title_font_size = 48  # Larger for short titles
        elif title_length <= 30:
            title_font_size = 42
        elif title_length <= 40:
            title_font_size = 36
        elif title_length <= 50:
            title_font_size = 32
        elif title_length <= 60:
            title_font_size = 28
        else:
            title_font_size = 24
        
        # Load fonts
        title_font = None
        date_font = None
        label_font = None
        
        for font_path in bold_font_paths + font_paths:
            try:
                title_font = ImageFont.truetype(font_path, title_font_size)
                break
            except:
                continue
        
        for font_path in bold_font_paths + font_paths:
            try:
                date_font = ImageFont.truetype(font_path, 32)  # Date font size
                break
            except:
                continue
        
        for font_path in font_paths:
            try:
                label_font = ImageFont.truetype(font_path, 16)
                break
            except:
                continue
        
        # Fallback to default if no font found
        if title_font is None:
            title_font = ImageFont.load_default()
        if date_font is None:
            date_font = ImageFont.load_default()
        if label_font is None:
            label_font = ImageFont.load_default()

        # Wrap title text
        chars_per_line = max(15, int(512 / (title_font_size * 0.6)))
        title_lines = textwrap.wrap(title_text, width=chars_per_line)
        
        # ===== ALWAYS USE NEW FORMAT =====
        # Use the modern format with gradients and dynamic sizing for ALL cards
        # QR code section will be included only if qr_data is available
        return _create_new_format_image(
            task_data, width, title_lines, title_font, date_font,
            label_font, title_font_size, qr_data
        )

    except Exception as e:
        print(f"Error creating task card image: {str(e)}")
        import traceback
        traceback.print_exc()
        return None


def _create_old_format_image(task_data, width, title_lines, title_font, date_font, title_font_size):
    """DEPRECATED: Create OLD FORMAT card with drawn circles.
    
    This function is kept for reference only and is no longer used.
    All cards now use _create_new_format_image() for consistent formatting.
    
    OLD DESIGN SPECIFICATIONS:
    - Three large solid circles at top (evenly spaced)
    - Circle diameter: 60-70px
    - Title text below circles (large, bold)
    - Dashed separator line below title
    - Date centered below separator
    - Dashed separator line at bottom
    - Priority logic: HIGH=3 dots, MEDIUM=2 dots, LOW=1 dot
    """
    # Load fonts
    import platform
    system = platform.system()
    
    try:
        if system == "Windows":
            title_font = ImageFont.truetype("arial.ttf", 64)
            date_font = ImageFont.truetype("arial.ttf", 32)
        elif system == "Darwin":  # macOS
            title_font = ImageFont.truetype("/Library/Fonts/Arial.ttf", 64)
            date_font = ImageFont.truetype("/Library/Fonts/Arial.ttf", 32)
        else:  # Linux
            title_font = ImageFont.truetype("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf", 64)
            date_font = ImageFont.truetype("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf", 32)
    except:
        title_font = ImageFont.load_default()
        date_font = ImageFont.load_default()
    
    # Wrap title
    title_text = task_data.get("title") if isinstance(task_data, dict) else task_data.title
    title_lines = textwrap.wrap(title_text, width=18)
    
    # Calculate height
    dots_section_height = 100
    title_height = len(title_lines) * 75 + 40
    separator_height = 30
    date_height = 60
    bottom_separator_height = 30
    
    height = dots_section_height + title_height + separator_height + date_height + bottom_separator_height + 40
    
    # Create image with white background
    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)
    
    current_y = 30
    
    # Determine number of dots based on priority
    priority = task_data.get("priority", "MEDIUM") if isinstance(task_data, dict) else getattr(task_data, "priority", "MEDIUM")
    if priority.upper() == "HIGH":
        num_dots = 3
    elif priority.upper() == "MEDIUM":
        num_dots = 2
    else:
        num_dots = 1
    
    # Draw large solid circles at top (evenly spaced across card width)
    dot_diameter = 65
    dot_radius = dot_diameter // 2
    
    # Calculate spacing to distribute dots evenly across card width
    margin = 80  # Margin from edges
    available_width = width - (2 * margin)
    
    if num_dots == 1:
        # Single dot centered
        dot_positions = [width // 2]
    elif num_dots == 2:
        # Two dots evenly spaced
        spacing = available_width // 2
        dot_positions = [margin + spacing // 2, margin + spacing + spacing // 2]
    else:  # 3 dots
        # Three dots evenly spaced
        spacing = available_width // 2
        dot_positions = [margin, width // 2, width - margin]
    
    # Draw the circles
    for dot_x in dot_positions:
        dot_y = current_y + 30
        draw.ellipse(
            [dot_x - dot_radius, dot_y - dot_radius,
             dot_x + dot_radius, dot_y + dot_radius],
            fill="#000000"
        )
    
    current_y += dots_section_height
    
    # Draw title text (centered, bold)
    for i, line in enumerate(title_lines):
        line_bbox = draw.textbbox((0, 0), line, font=title_font)
        line_width = line_bbox[2] - line_bbox[0]
        line_x = (width - line_width) // 2
        draw.text((line_x, current_y + i * 75), line, fill="black", font=title_font)
    
    current_y += title_height
    
    # Draw dashed separator line
    _draw_dashed_line(draw, 40, current_y, width - 40)
    current_y += separator_height
    
    # Draw due date (centered)
    due_date_text = _format_due_date(task_data)
    date_bbox = draw.textbbox((0, 0), due_date_text, font=date_font)
    date_width = date_bbox[2] - date_bbox[0]
    date_x = (width - date_width) // 2
    draw.text((date_x, current_y), due_date_text, fill="black", font=date_font)
    
    current_y += date_height
    
    # Draw bottom dashed separator line
    _draw_dashed_line(draw, 40, current_y, width - 40)
    
    # Save to temporary file
    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
    img.save(temp_file.name, "PNG")
    temp_file.close()
    
    print(f"DEPRECATED: Task card image created (OLD FORMAT - no QR): {temp_file.name}")
    return temp_file.name


def _create_new_format_image(task_data, width, title_lines, title_font, date_font,
                             label_font, title_font_size, qr_data):
    """Create NEW FORMAT card (with separator lines and optional QR code)."""
    # Calculate heights - NEW FORMAT with separators
    category = task_data.get("category") if isinstance(task_data, dict) else getattr(task_data, "category", None)
    category = (category or "").strip().upper()[:30]
    priority_section_height = 120 + (40 if category else 0)
    title_height = len(title_lines) * (title_font_size + 10) + 40
    separator_height = 30  # Perforation lines
    date_height = 70
    qr_section_height = 150 + 40 + 30 if qr_data else 0  # QR + label + padding (only if QR exists)
    total_padding = 50
    
    height = priority_section_height + title_height + separator_height + date_height + separator_height + total_padding + qr_section_height
    
    # Create image
    img = Image.new("RGB", (width, height), "#ffffff")
    draw = ImageDraw.Draw(img)
    
    current_y = 30
    
    # Draw priority dots (NEW FORMAT: standard size)
    priority = task_data.get("priority", "MEDIUM").upper()
    if priority == "HIGH":
        num_dots = 3
    elif priority == "MEDIUM":
        num_dots = 2
    else:
        num_dots = 1
    
    dot_radius = 35
    dot_spacing = 80
    total_dots_width = (num_dots - 1) * dot_spacing
    start_x = (width - total_dots_width) // 2
    
    for i in range(num_dots):
        dot_x = start_x + (i * dot_spacing)
        dot_y = current_y + 40
        draw.ellipse(
            [dot_x - dot_radius, dot_y - dot_radius,
             dot_x + dot_radius, dot_y + dot_radius],
            fill="#000000"
        )
    
    if category:
        try:
            category_font = ImageFont.truetype(label_font.path, 24)
        except (AttributeError, OSError):
            category_font = label_font
        bbox = draw.textbbox((0, 0), category, font=category_font)
        draw.text(((width - (bbox[2] - bbox[0])) // 2, current_y + 40 + dot_radius + 14),
                  category, fill="#1a1a1a", font=category_font)

    current_y += priority_section_height
    
    # Draw PERFORATION separator (NEW FORMAT)
    _draw_perforation(draw, 40, current_y, width - 40)
    current_y += separator_height
    
    # Draw title
    for i, line in enumerate(title_lines):
        line_bbox = draw.textbbox((0, 0), line, font=title_font)
        line_width = line_bbox[2] - line_bbox[0]
        line_x = (width - line_width) // 2
        draw.text((line_x, current_y + i * (title_font_size + 8)), line, fill="#1a1a1a", font=title_font)
    
    current_y += title_height
    
    # Draw PERFORATION separator (NEW FORMAT)
    _draw_perforation(draw, 40, current_y, width - 40)
    current_y += separator_height
    
    # Draw due date
    due_date_text = _format_due_date(task_data)
    date_bbox = draw.textbbox((0, 0), due_date_text, font=date_font)
    date_width = date_bbox[2] - date_bbox[0]
    date_x = (width - date_width) // 2
    draw.text((date_x, current_y), due_date_text, fill="#000000", font=date_font)
    current_y += date_height
    
    # Add QR code section only if qr_data is provided (NEW FORMAT)
    if qr_data:
        # Draw PERFORATION separator before QR section
        _draw_perforation(draw, 40, current_y, width - 40)
        current_y += separator_height
        
        try:
            qr_image = generate_task_qr_code(qr_data, size=150)
            qr_x = (width - 150) // 2
            qr_y = current_y + 20
            img.paste(qr_image, (qr_x, qr_y))
            
            # Add label below QR code
            label_y = qr_y + 150 + 10
            label_text = "Scan to view source"
            # Thermal printers print pure black/white dots, so light grey 16px text breaks up into
            # unreadable specks. Use a bold face, a little larger, in solid black.
            try:
                qr_label_font = ImageFont.truetype(date_font.path, 20)
            except (AttributeError, OSError):
                qr_label_font = label_font
            label_bbox = draw.textbbox((0, 0), label_text, font=qr_label_font)
            label_width = label_bbox[2] - label_bbox[0]
            label_x = (width - label_width) // 2
            draw.text((label_x, label_y), label_text, fill="#000000", font=qr_label_font)
        except Exception as e:
            print(f"Warning: Failed to generate QR code for image: {e}")
    else:
        # No QR code - add bottom perforation
        _draw_perforation(draw, 40, current_y, width - 40)
    
    # Save to temporary file
    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
    img.save(temp_file.name, "PNG")
    temp_file.close()
    
    qr_status = "with QR" if qr_data else "without QR"
    print(f"Task card image created (NEW FORMAT - {qr_status}): {temp_file.name}")
    return temp_file.name


def _draw_dashed_line(draw, x1, y, x2):
    """Draw a dashed line (DEPRECATED - used by old format function)."""
    dash_length = 8
    gap_length = 6
    current_x = x1
    
    while current_x < x2:
        end_x = min(current_x + dash_length, x2)
        draw.line([(current_x, y), (end_x, y)], fill="#666666", width=3)
        current_x += dash_length + gap_length


