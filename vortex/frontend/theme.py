from __future__ import annotations

from datetime import datetime

BG = "#0B0F17"
SURFACE = "#111827"
SURFACE_ALT = "#0F1522"
BORDER = "#1F2937"
ACCENT = "#60A5FA"
ACCENT_STRONG = "#3B82F6"
SUCCESS = "#10B981"
DANGER = "#EF4444"
WARNING = "#F59E0B"
TEXT = "#E5E7EB"
MUTED = "#94A3B8"

KIND_ICONS = {
    "folder": "folder",
    "video": "movie",
    "audio": "audiotrack",
    "image": "image",
    "text": "description",
    "code": "code",
    "pdf": "picture_as_pdf",
    "archive": "archive",
    "doc": "article",
    "spreadsheet": "table_chart",
    "presentation": "slideshow",
    "design": "palette",
    "font": "text_format",
    "database": "storage",
    "model3d": "view_in_ar",
    "package": "inventory_2",
    "binary": "memory",
    "ebook": "menu_book",
    "other": "insert_drive_file",
}

KIND_COLORS = {
    "folder": ACCENT,
    "video": "#F472B6",
    "audio": "#A78BFA",
    "image": SUCCESS,
    "text": TEXT,
    "code": "#38BDF8",
    "pdf": DANGER,
    "archive": WARNING,
    "doc": "#818CF8",
    "spreadsheet": "#34D399",
    "presentation": "#FB923C",
    "design": "#F472B6",
    "font": "#C084FC",
    "database": "#38BDF8",
    "model3d": "#2DD4BF",
    "package": "#FBBF24",
    "binary": "#F87171",
    "ebook": "#A78BFA",
    "other": MUTED,
}


def icon_for_kind(kind: str) -> str:
    return KIND_ICONS.get(kind, "insert_drive_file")


def color_for_kind(kind: str) -> str:
    return KIND_COLORS.get(kind, MUTED)


def human_size(size: int) -> str:
    if size <= 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size)
    index = 0
    while value >= 1024 and index < len(units) - 1:
        value /= 1024
        index += 1
    if index == 0:
        return f"{int(value)} {units[index]}"
    return f"{value:.1f} {units[index]}"


def human_speed(bytes_per_second: float) -> str:
    if bytes_per_second <= 0:
        return "-"
    return f"{human_size(int(bytes_per_second))}/s"


def format_time(timestamp: float) -> str:
    try:
        return datetime.fromtimestamp(timestamp).strftime("%d/%m/%Y %H:%M")
    except (ValueError, OSError):
        return "-"
