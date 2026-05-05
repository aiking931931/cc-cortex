"""concinno.cache.ux_gate — Binary gate for LLM-facing UX injections.

@module cache.ux_gate
@responsibility Single source of truth for "should I inject UX coaching
    context?". Wraps :func:`concinno.config.get` behind a tiny cached
    helper so every UX inject site can guard with a one-line call:

        from concinno.cache.ux_gate import is_ux_enabled
        if not is_ux_enabled():
            return None

    Consumers:
      * CBUA pipeline markers (``guards.cbua_pipeline_guard``)
      * WIREDO checklist injects (``wiredo_guard`` / ``wiredo_enforcement``)
      * Clean-write streak counter (``on_post_tool`` streak fragment)
      * Read:Edit ratio warnings (``thinking_depth_guard``)
      * Token-zone hint injects (``token_monitor``, ``token_zone``)
      * Three-layer thinking injects (``think_inject``)
      * Cognitive anchor / intent anchor injects
      * All ``additionalContext`` coaching produced by ``hooks/on_*.py``

    What it does NOT gate:
      * ``deny`` / ``rewrite`` decisions — safety paths run regardless
      * Tool results / user-visible output
      * Error responses
      * Destruction / butterfly / confidence / boundary guards

@dependencies concinno.config (lazy), stdlib
@exports is_ux_enabled, reset_cache

Failure contract:
    On any exception reading config → return ship default (False).
    A crashed config MUST NOT spam UX into unsuspecting PyPI users.

Performance:
    First call costs one config layer merge (~1-2ms). Subsequent calls
    in the same process return the cached bool (<1µs). Tests call
    :func:`reset_cache` between scenarios so layer overrides land.
"""

from __future__ import annotations

import os
from typing import Optional

__all__ = ["CONCINNO_UX_ENV", "is_ux_enabled", "reset_cache"]

#: One-shot override env var. ``1/true/yes/on`` → force UX on for the
#: current process regardless of config layers. ``0/false/no/off`` →
#: force UX off. Anything else falls through to layered config. This
#: is load-bearing for CLI callers that want a dry-run of "what would
#: this look like with UX on?" without mutating the user's config.
CONCINNO_UX_ENV = "CONCINNO_UX_INJECTION"

_cache: Optional[bool] = None


def _parse_env_override(raw: Optional[str]) -> Optional[bool]:
    """Parse the env var into a tri-state bool.

    Returns ``None`` when the var is unset or malformed so the caller
    falls through to layered config. The tolerant parsing matches
    :func:`concinno.config._parse_env_value` so operators can use the
    same spellings (``1`` / ``true`` / ``yes`` / ``on`` and inverses).
    """
    if raw is None:
        return None
    low = raw.strip().lower()
    if low in ("1", "true", "yes", "on"):
        return True
    if low in ("0", "false", "no", "off"):
        return False
    return None


def is_ux_enabled() -> bool:
    """Return whether LLM-facing UX injections should fire this turn.

    Resolution order (first non-None wins):
      1. ``CONCINNO_UX_INJECTION`` env var (operator one-shot override).
      2. :func:`concinno.config.get` ``ux_injection`` key (layered:
         env ``CONCINNO_UX_INJECTION`` handled above → project
         ``.concinno/config.json`` → user ``~/.concinno/config.json``
         → ship default ``False``).
      3. ``False`` on any exception. UX inject is supplementary; a
         broken config MUST NOT poison the guard pipeline.

    The result is cached for the lifetime of the Python process. Call
    :func:`reset_cache` in tests that flip the flag mid-run.
    """
    global _cache
    if _cache is not None:
        return _cache

    env_override = _parse_env_override(os.environ.get(CONCINNO_UX_ENV))
    if env_override is not None:
        _cache = env_override
        return _cache

    try:
        from concinno.config import get as _cfg_get

        raw = _cfg_get("ux_injection")
    except Exception:
        _cache = False
        return _cache

    if isinstance(raw, bool):
        _cache = raw
    else:
        # Unknown type — degrade to ship default rather than surprise.
        _cache = False
    return _cache


def reset_cache() -> None:
    """Drop the cached value. Call after mutating env / config in tests."""
    global _cache
    _cache = None
