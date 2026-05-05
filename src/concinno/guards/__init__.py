# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deprecated alias. Use aiking.governance.guards instead.

Removal in concinno 6.0.0 (~2026-11-01).
"""
import warnings

from aiking.governance.guards import *  # noqa: F401, F403

try:
    from aiking.governance.guards import __all__  # noqa: F401
except ImportError:
    __all__ = []

warnings.warn(
    "concinno.guards is deprecated; use aiking.governance.guards. "
    "Removal in concinno 6.0.0 (~2026-11-01).",
    DeprecationWarning,
    stacklevel=2,
)


def __getattr__(name):
    # PEP 562: handle `from concinno.guards import X` for X not in __all__.
    import importlib

    mod = importlib.import_module("aiking.governance.guards")
    return getattr(mod, name)
