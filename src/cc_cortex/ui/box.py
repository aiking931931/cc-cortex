"""Box drawing utilities for terminal dashboards."""

from __future__ import annotations

# Double-line box characters
DOUBLE_TL = "\u2554"  # ╔
DOUBLE_TR = "\u2557"  # ╗
DOUBLE_BL = "\u255a"  # ╚
DOUBLE_BR = "\u255d"  # ╝
DOUBLE_H = "\u2550"  # ═
DOUBLE_V = "\u2551"  # ║
DOUBLE_LT = "\u2560"  # ╠
DOUBLE_RT = "\u2563"  # ╣
DOUBLE_HV = "\u256c"  # ╬

# Single-line (for separators inside double box)
SINGLE_H = "\u2500"  # ─
SINGLE_LT = "\u255f"  # ╟ (double-left, single-right)
SINGLE_RT = "\u2562"  # ╢


def box_top(width: int) -> str:
    return DOUBLE_TL + DOUBLE_H * width + DOUBLE_TR


def box_bottom(width: int) -> str:
    return DOUBLE_BL + DOUBLE_H * width + DOUBLE_BR


def box_separator(width: int, double: bool = True) -> str:
    if double:
        return DOUBLE_LT + DOUBLE_H * width + DOUBLE_RT
    return SINGLE_LT + SINGLE_H * width + SINGLE_RT


def box_row(content: str, width: int) -> str:
    """Create a box row, padding content to width. Accounts for ANSI codes in length."""
    visible_len = _visible_length(content)
    padding = max(0, width - visible_len)
    return DOUBLE_V + content + " " * padding + DOUBLE_V


def _visible_length(s: str) -> int:
    """Calculate visible length of string, ignoring ANSI escape codes."""
    import re

    ansi_pattern = re.compile(r"\033\[[0-9;]*m")
    return len(ansi_pattern.sub("", s))


def center_text(text: str, width: int) -> str:
    """Center text within width, accounting for ANSI codes."""
    visible_len = _visible_length(text)
    if visible_len >= width:
        return text
    left_pad = (width - visible_len) // 2
    right_pad = width - visible_len - left_pad
    return " " * left_pad + text + " " * right_pad
