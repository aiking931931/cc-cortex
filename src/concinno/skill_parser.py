"""concinno.skill_parser -- SKILL.md frontmatter parser.

Extracted from :mod:`concinno.gui.server` (pre-2.31.0) and hardened for
plugin use: third-party ``concinno-skills-*`` packages ship SKILL.md
files whose frontmatter we do not control, so the parser must tolerate
malformed input without crashing and without leaking exceptions.

Supported frontmatter shapes (all YAML-lite, no PyYAML dep)::

    ---
    name: my_skill
    description: one line
    triggers: [a, b]             # inline list
    user-invocable: true
    ---

    body...

Or with block list::

    ---
    name: my_skill
    triggers:
      - a
      - b
    ---

    body...

Tolerated malformations:

* UTF-8 BOM prefix
* CRLF line endings (normalised to LF)
* Missing closing ``---`` (returns what was parsed up to EOF)
* Only opening ``---`` with no closing marker
* Empty file / only frontmatter / only body
* Over-long description fields (>500 chars)
* Special chars in values: quotes, colons, emoji

Hard-rejected (returns empty dict, no crash):

* Unreadable file (permission, encoding)

The parser is intentionally **permissive** — it prefers returning a
partial dict over raising. Callers that need strict validation should
layer validation on top (see :mod:`concinno.plugins.features`
``_validate_feature_meta`` for an example).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

# Truthy token normalisation for boolean-shaped frontmatter fields
# (mirrors Python ``yaml.safe_load`` behaviour without the PyYAML dep).
_TRUTHY_TOKENS = frozenset({"true", "yes", "on", "1"})
_FALSY_TOKENS = frozenset({"false", "no", "off", "0"})


def _strip_bom(text: str) -> str:
    """Strip UTF-8 BOM if present."""
    if text.startswith("﻿"):
        return text[1:]
    return text


def _normalise_newlines(text: str) -> str:
    """Collapse CRLF/CR to LF so ``find('\\n---')`` is robust."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _extract_frontmatter(text: str) -> tuple[str, str]:
    """Split ``text`` into ``(frontmatter_body, rest)``.

    Returns ``("", text)`` if no frontmatter fence is found. Returns
    ``(frontmatter_body, "")`` if opening ``---`` is present but the
    closing fence is missing — the whole remaining text is treated as
    frontmatter so the parser can recover whatever key/value pairs it
    can before EOF.
    """
    if not text.startswith("---"):
        return "", text
    # Skip past opening fence (which may be ``---\n`` or ``---\r\n``).
    body = text[3:]
    # If the opening is followed by non-newline characters it is not
    # actually a frontmatter fence — return as body.
    if body and body[0] not in ("\n", " "):
        return "", text
    # Find closing fence.
    close = body.find("\n---")
    if close < 0:
        # No closing — treat all of ``body`` as frontmatter.
        return body, ""
    after_close = close + len("\n---")
    return body[:close], body[after_close:]


def _parse_inline_list(value: str) -> list[str]:
    """Parse ``[a, b, c]`` into ``["a", "b", "c"]``.

    Tolerates trailing commas, missing brackets, unquoted items. Strips
    surrounding whitespace and matched quotes on each item. Returns
    ``[]`` for empty or malformed input.
    """
    v = value.strip()
    if v.startswith("[") and v.endswith("]"):
        v = v[1:-1]
    items = [item.strip() for item in v.split(",")]
    cleaned: list[str] = []
    for item in items:
        if not item:
            continue
        # Strip matched quotes.
        if len(item) >= 2 and item[0] == item[-1] and item[0] in ("'", '"'):
            item = item[1:-1]
        cleaned.append(item)
    return cleaned


def _parse_bool(value: str) -> Any:
    """Parse YAML-like truthy/falsy tokens. Returns the original
    stripped string when the token is neither truthy nor falsy so
    callers can distinguish ``"maybe"`` from ``True``/``False``.
    """
    lowered = value.strip().lower()
    if lowered in _TRUTHY_TOKENS:
        return True
    if lowered in _FALSY_TOKENS:
        return False
    return value.strip()


def _parse_frontmatter_body(body: str) -> dict[str, Any]:
    """Turn a frontmatter text block into a dict.

    Block-style lists (``triggers:\\n  - a\\n  - b``) are rebuilt from
    consecutive ``- `` lines. Inline lists (``triggers: [a, b]``) are
    handled by :func:`_parse_inline_list`. Unknown shapes fall through
    as raw strings.
    """
    meta: dict[str, Any] = {}
    current_list_key: str | None = None
    current_list: list[str] = []

    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            # Blank line terminates any pending block list.
            if current_list_key is not None:
                meta[current_list_key] = current_list
                current_list_key = None
                current_list = []
            continue

        # Continuation of a block list.
        if current_list_key is not None and stripped.startswith("- "):
            item = stripped[2:].strip()
            if len(item) >= 2 and item[0] == item[-1] and item[0] in ("'", '"'):
                item = item[1:-1]
            current_list.append(item)
            continue

        # Close pending block list on non-list line.
        if current_list_key is not None:
            meta[current_list_key] = current_list
            current_list_key = None
            current_list = []

        if ":" not in stripped:
            # Unparseable line — skip.
            continue
        key, _, raw_value = stripped.partition(":")
        key = key.strip()
        raw_value = raw_value.strip()

        if not raw_value:
            # Possibly a block list header. Mark and wait.
            current_list_key = key
            current_list = []
            continue

        # Inline list.
        if raw_value.startswith("[") and raw_value.endswith("]"):
            meta[key] = _parse_inline_list(raw_value)
            continue

        # Strip matched outer quotes.
        if (
            len(raw_value) >= 2
            and raw_value[0] == raw_value[-1]
            and raw_value[0] in ("'", '"')
        ):
            raw_value = raw_value[1:-1]

        # Boolean-shaped keys get truthy token conversion.
        if key in ("user-invocable", "enabled", "cosmetic", "ziq_autotunable"):
            meta[key] = _parse_bool(raw_value)
        else:
            meta[key] = raw_value

    # Flush trailing block list.
    if current_list_key is not None:
        meta[current_list_key] = current_list

    return meta


def parse_skill_md(path: Path) -> dict[str, Any]:
    """Parse a SKILL.md file. Returns an empty dict on unreadable input.

    Fields commonly present: ``name``, ``description``, ``triggers``
    (list), ``user-invocable`` (bool), ``scope`` (str). Unknown fields
    are preserved verbatim.

    Backward-compatible with the pre-2.31.0 ``_parse_skill_md`` output
    shape — callers that only read ``name`` and ``description`` see no
    behavioural change.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return {}
    if not text:
        return {}
    text = _strip_bom(text)
    text = _normalise_newlines(text)
    body, _rest = _extract_frontmatter(text)
    if not body:
        return {}
    return _parse_frontmatter_body(body)


# ── Backward-compat shim ───────────────────────────────────────────
#
# :mod:`concinno.gui.server` historically exposed ``_parse_skill_md``
# as a private helper. External callers (tests, plugins, downstream
# users) may have imported it directly; keep a shim so 2.31.0 is not a
# breaking change.


def _parse_skill_md(path: Path) -> dict[str, Any]:
    """Deprecated private alias — kept for backward-compat with
    callers that imported from :mod:`concinno.gui.server`. Prefer
    :func:`parse_skill_md`.
    """
    return parse_skill_md(path)
