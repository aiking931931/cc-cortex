# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deprecated alias. Use aiking_core.ziq instead.

Removal in concinno 6.0.0 (~2026-11-01).
"""
import warnings

from aiking_core.ziq import *  # noqa: F401, F403

try:
    from aiking_core.ziq import __all__  # noqa: F401
except ImportError:
    __all__ = []

warnings.warn(
    "concinno.ziq is deprecated; use aiking_core.ziq. "
    "Removal in concinno 6.0.0 (~2026-11-01).",
    DeprecationWarning,
    stacklevel=2,
)


def __getattr__(name):
    import importlib

    return getattr(importlib.import_module("aiking_core.ziq"), name)
