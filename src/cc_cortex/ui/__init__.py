"""Terminal UI components for cc-cortex — zero external dependencies."""

from cc_cortex.ui.colors import (
    BOLD,
    BRIGHT_BLACK,
    BRIGHT_CYAN,
    BRIGHT_GREEN,
    BRIGHT_RED,
    BRIGHT_YELLOW,
    DIM,
    RESET,
    c,
    reset_color_cache,
    style,
    supports_color,
)
from cc_cortex.ui.dashboard import render_dashboard

__all__ = [
    "BOLD",
    "BRIGHT_BLACK",
    "BRIGHT_CYAN",
    "BRIGHT_GREEN",
    "BRIGHT_RED",
    "BRIGHT_YELLOW",
    "DIM",
    "RESET",
    "c",
    "render_dashboard",
    "reset_color_cache",
    "style",
    "supports_color",
]
