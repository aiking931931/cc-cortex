"""concinno.core.log — Unified structured logging for concinno.

@module log
@responsibility Pre-configured logger factory with consistent format
@dependencies (none — stdlib only)
@exports get_logger
"""

from __future__ import annotations

import logging
import os

_CONFIGURED = False


def _configure_once() -> None:
    """Set up root concinno logger (idempotent)."""
    global _CONFIGURED
    if _CONFIGURED:
        return
    _CONFIGURED = True

    root = logging.getLogger("concinno")
    if root.handlers:
        return  # already configured externally

    level_str = os.environ.get("CONCINNO_LOG_LEVEL", "WARNING").upper()
    level = getattr(logging, level_str, logging.WARNING)
    root.setLevel(level)

    handler = logging.StreamHandler()
    handler.setLevel(level)
    formatter = logging.Formatter(
        "[%(name)s] %(levelname)s: %(message)s",
    )
    handler.setFormatter(formatter)
    root.addHandler(handler)


def get_logger(name: str) -> logging.Logger:
    """Get a namespaced logger under ``concinno``.

    Args:
        name: Module name (typically ``__name__``).

    Returns:
        A :class:`logging.Logger` instance.
    """
    _configure_once()
    return logging.getLogger(name)
