"""cc_cortex.i18n — Internationalization for messages and detection patterns.

@module i18n
@responsibility Centralized locale management. Messages display in user's locale
    (fallback English). Detection patterns merge from ALL active locales.
@dependencies (none — stdlib only)
@exports msg, patterns, get_locale, set_locale, get_active_locales

Architecture:
  - locale/*.json contains {"messages": {...}, "patterns": {...}} per language
  - en.json + zh_TW.json ship as built-in (always loaded for patterns)
  - Users add locales via /locale Skill → new JSON file + CC_UX_LANG env var
  - Messages: display locale → English fallback
  - Patterns: merged from ALL loaded locales (inclusive detection)
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

# ── Constants ────────────────────────────────────────────────────

_LOCALE_DIR = Path(__file__).parent / "locale"
_DEFAULT_LOCALE = "en"
_BUILTIN_LOCALES = ("en", "zh_TW", "ja", "ko", "es")  # Always loaded for pattern detection

# ── Module state ─────────────────────────────────────────────────

_display_locale: str = ""
_message_cache: dict[str, dict[str, str]] = {}
_pattern_cache: dict[str, dict[str, list[str]]] = {}
_loaded: bool = False


# ── Loading ──────────────────────────────────────────────────────


def _load_locale_file(locale: str) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Load a single locale JSON. Returns (messages, patterns)."""
    path = _LOCALE_DIR / f"{locale}.json"
    if not path.is_file():
        return {}, {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data: dict[str, Any] = json.load(f)
        msgs = data.get("messages", {})
        pats = data.get("patterns", {})
        if not isinstance(msgs, dict):
            msgs = {}
        if not isinstance(pats, dict):
            pats = {}
        return msgs, pats
    except (json.JSONDecodeError, OSError):
        return {}, {}


def _resolve_display_locale() -> str:
    """Read CC_UX_LANG env var → normalize to locale key."""
    raw = os.environ.get("CC_UX_LANG", _DEFAULT_LOCALE).strip()
    # Normalize: "zh-TW" → "zh_TW", "zh" → "zh_TW", "en" → "en"
    normalized = raw.replace("-", "_")
    if normalized in ("zh", "zh_TW", "zh_tw"):
        return "zh_TW"
    if normalized.startswith("zh"):
        return "zh_TW"
    return normalized


def _ensure_loaded() -> None:
    """Lazy-load all built-in locales + user's display locale."""
    global _loaded, _display_locale
    if _loaded:
        return

    _display_locale = _resolve_display_locale()

    # Load all built-in locales (for pattern merging)
    for loc in _BUILTIN_LOCALES:
        if loc not in _message_cache:
            msgs, pats = _load_locale_file(loc)
            _message_cache[loc] = msgs
            _pattern_cache[loc] = pats

    # Load user's display locale if not already loaded
    if _display_locale not in _message_cache:
        msgs, pats = _load_locale_file(_display_locale)
        if msgs or pats:
            _message_cache[_display_locale] = msgs
            _pattern_cache[_display_locale] = pats

    _loaded = True


# ── Public API ───────────────────────────────────────────────────


def msg(key: str, **kwargs: Any) -> str:
    """Get a localized message string.

    Lookup order: display locale → English → key itself.
    Supports {placeholder} formatting via kwargs.

    Args:
        key: Message key (e.g. "confidence_gate.deny").
        **kwargs: Format placeholders.

    Returns:
        Formatted message string.
    """
    _ensure_loaded()
    for loc in (_display_locale, _DEFAULT_LOCALE):
        msgs = _message_cache.get(loc, {})
        if key in msgs:
            template = msgs[key]
            return template.format(**kwargs) if kwargs else template
    return key  # Fallback: return key as-is


def patterns(key: str) -> list[str]:
    """Get detection patterns merged from ALL loaded locales.

    Each locale contributes its own patterns for the given key.
    Results are deduplicated while preserving order.

    Args:
        key: Pattern key (e.g. "correction_l1", "research_keywords").

    Returns:
        Merged list of pattern strings from all active locales.
    """
    _ensure_loaded()
    result: list[str] = []
    seen: set[str] = set()
    for pats in _pattern_cache.values():
        for p in pats.get(key, []):
            if p not in seen:
                result.append(p)
                seen.add(p)
    return result


def get_locale() -> str:
    """Return current display locale (e.g. 'en', 'zh_TW')."""
    _ensure_loaded()
    return _display_locale


def get_active_locales() -> list[str]:
    """Return all loaded locale keys (for debugging/status)."""
    _ensure_loaded()
    return list(_pattern_cache.keys())


def set_locale(locale: str) -> None:
    """Set display locale and load its data if not already loaded.

    Also sets CC_UX_LANG env var for child processes.

    Args:
        locale: Locale key (e.g. "en", "zh_TW", "ja", "ko").
    """
    global _display_locale
    _ensure_loaded()
    normalized = locale.replace("-", "_")
    if normalized in ("zh", "zh_tw"):
        normalized = "zh_TW"
    _display_locale = normalized
    os.environ["CC_UX_LANG"] = normalized

    if normalized not in _message_cache:
        msgs, pats = _load_locale_file(normalized)
        if msgs:
            _message_cache[normalized] = msgs
        if pats:
            _pattern_cache[normalized] = pats


def reload() -> None:
    """Force reload all locales from disk. Use after adding new locale files."""
    global _loaded
    _message_cache.clear()
    _pattern_cache.clear()
    _loaded = False
    _ensure_loaded()


def locale_dir() -> Path:
    """Return path to locale directory (for Skill to add new locales)."""
    return _LOCALE_DIR
