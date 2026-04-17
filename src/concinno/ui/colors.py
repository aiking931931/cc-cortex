"""ANSI color helpers with NO_COLOR support (https://no-color.org/)."""

from __future__ import annotations

import os
import sys

# ANSI escape codes
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"

FG_RED = "\033[31m"
FG_GREEN = "\033[32m"
FG_YELLOW = "\033[33m"
FG_BLUE = "\033[34m"
FG_MAGENTA = "\033[35m"
FG_CYAN = "\033[36m"
FG_WHITE = "\033[37m"
FG_GRAY = "\033[90m"

# Bright colors (used by demo scripts)
BRIGHT_BLACK = "\033[90m"
BRIGHT_RED = "\033[91m"
BRIGHT_GREEN = "\033[92m"
BRIGHT_YELLOW = "\033[93m"
BRIGHT_BLUE = "\033[94m"
BRIGHT_MAGENTA = "\033[95m"
BRIGHT_CYAN = "\033[96m"
BRIGHT_WHITE = "\033[97m"

BG_RED = "\033[41m"
BG_GREEN = "\033[42m"
BG_BLUE = "\033[44m"

_STYLES: dict[str, str] = {
    "reset": RESET,
    "bold": BOLD,
    "dim": DIM,
    "red": FG_RED,
    "green": FG_GREEN,
    "yellow": FG_YELLOW,
    "blue": FG_BLUE,
    "magenta": FG_MAGENTA,
    "cyan": FG_CYAN,
    "white": FG_WHITE,
    "gray": FG_GRAY,
    "bg_red": BG_RED,
    "bg_green": BG_GREEN,
    "bg_blue": BG_BLUE,
    "success": FG_GREEN + BOLD,
    "warning": FG_YELLOW + BOLD,
    "error": FG_RED + BOLD,
    "info": FG_CYAN,
    "header": FG_BLUE + BOLD,
    "muted": FG_GRAY,
}


def supports_color() -> bool:
    """Check if the terminal supports ANSI colors."""
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    if not hasattr(sys.stdout, "isatty"):
        return False
    if not sys.stdout.isatty():
        return False
    # Enable ANSI on Windows
    if sys.platform == "win32":
        try:
            os.system("")  # noqa: S605 — enables ANSI escape processing on Windows
        except Exception:
            pass
    return True


_COLOR_ENABLED: bool | None = None


def _is_color_enabled() -> bool:
    global _COLOR_ENABLED
    if _COLOR_ENABLED is None:
        _COLOR_ENABLED = supports_color()
    return _COLOR_ENABLED


def style(text: str, *styles: str) -> str:
    """Apply ANSI styles to text. No-op if color is disabled."""
    if not _is_color_enabled():
        return text
    prefix = ""
    for s in styles:
        code = _STYLES.get(s, "")
        prefix += code
    if not prefix:
        return text
    return f"{prefix}{text}{RESET}"


def c(text: str, *codes: str) -> str:
    """Compose ANSI codes directly (raw escape strings). No-op if color disabled."""
    if not _is_color_enabled():
        return text
    prefix = "".join(codes)
    if not prefix:
        return text
    return f"{prefix}{text}{RESET}"


def reset_color_cache() -> None:
    """Reset cached color detection (useful after setting NO_COLOR at runtime)."""
    global _COLOR_ENABLED
    _COLOR_ENABLED = None
