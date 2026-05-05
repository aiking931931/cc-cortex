# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deprecated alias. Use aiking_core.fieldread instead.

Removal in concinno 6.0.0 (~2026-11-01).

Note on private symbol re-export: legacy in-tree concinno consumers
(``concinno.fidelity_delta``, ``concinno.prompt_engine``,
``concinno.cognitive.router``) still import private FieldRead helpers
(``_BULLET_RE``, ``_estimate_tokens``, ``_extract_keywords``,
``_heading_slug``, ``_parse_sections``). The canonical implementation
lives at ``aiking_core.fieldread._core`` and exposes them on
``_core``. This shim re-exports from ``_core`` (not the public
``aiking_core.fieldread`` namespace) so private symbols continue to
resolve transparently. New code should import from
``aiking_core.fieldread`` directly.
"""
import warnings

from aiking_core.fieldread._core import *  # noqa: F401, F403

# Re-bind all names defined on _core (public + private) onto this module so
# `from concinno.field_read import _BULLET_RE` style imports keep working.
from aiking_core.fieldread import _core as _core_mod  # noqa: F401

for _name in dir(_core_mod):
    if _name.startswith("__"):
        continue
    globals()[_name] = getattr(_core_mod, _name)
del _name, _core_mod

try:
    from aiking_core.fieldread._core import __all__  # noqa: F401
except ImportError:
    __all__ = []

warnings.warn(
    "concinno.field_read is deprecated; use aiking_core.fieldread. "
    "Removal in concinno 6.0.0 (~2026-11-01).",
    DeprecationWarning,
    stacklevel=2,
)


def __getattr__(name):
    import importlib

    # Fall through to _core for any private symbol not pre-bound above.
    return getattr(importlib.import_module("aiking_core.fieldread._core"), name)
