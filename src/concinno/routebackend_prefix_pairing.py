#!/usr/bin/env python3
"""concinno.routebackend_prefix_pairing — Pair model alias swaps with router prefix updates.

@module routebackend_prefix_pairing
@responsibility When a developer edits a psyche-engine cognition module
    to swap a backend ``model`` alias (e.g. ``gemma4-nsfw`` → ``qwen3-3b-nsfw``),
    verify that the corresponding ``isSancioRouted`` allowlist in
    ``psyche-engine/src/anthropic.ts`` includes the new prefix. If not,
    emit a PostToolUse warning so the operator wires the pair before
    deploy. Warn-only — never blocks.
@dependencies concinno.guards.base, concinno.feature_config (optional)
@exports RoutebackendPrefixPairingGuard

Wave D Step C — sediment from 2026-05-02 multi-LLM ship where
``mode-extract.ts`` swapped ``model: 'qwen3-3b-nsfw'`` but
``anthropic.ts::isSancioRouted`` still ``startsWith('gemma4')`` only,
silently routing through the default Anthropic upstream and burning a
debug session. This guard prevents repeats by surfacing the missing
pair at edit time, not deploy time.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from concinno.guards.base import (
    BaseGuard,
    GuardCategory,
    GuardContext,
    GuardResult,
)

# ── Patterns ─────────────────────────────────────────────

# Files we care about — psyche-engine cognition modules where backend
# aliases are picked. Both .ts source and compiled .js artefacts.
_COGNITION_PATH_RE = re.compile(
    r"psyche-engine[\\/](?:src|dist)[\\/]cognition[\\/][^\\/]+\.(?:ts|js)$",
    re.IGNORECASE,
)

# Anthropic gateway file — single source of truth for routing allowlist.
_ANTHROPIC_RELATIVE = "psyche-engine/src/anthropic.ts"

# Match `model: 'value'` or `model: "value"` literals. Case-sensitive
# because TS aliases are.
_MODEL_LITERAL_RE = re.compile(
    r"""\bmodel\s*:\s*['"]([a-zA-Z0-9][a-zA-Z0-9._-]*)['"]""",
)

# Match ``startsWith('prefix')`` / ``startsWith("prefix")`` calls inside
# isSancioRouted. We extract the prefix string.
_STARTSWITH_RE = re.compile(
    r"""\bstartsWith\(\s*['"]([^'"]+)['"]\s*\)""",
)

# Aliases the guard should ignore. ``claude-*`` and ``gpt-*`` and the
# upstream Anthropic / OpenAI native model strings are routed by SDK
# default, not by ``isSancioRouted``.
_IGNORED_PREFIXES = frozenset({
    "claude",
    "gpt",
    "anthropic",
    "openai",
})

# Max issues surfaced per warning so the additionalContext stays small.
_MAX_REPORTED = 5


# ── Helpers ──────────────────────────────────────────────


def _model_prefix(alias: str) -> str:
    """Pull the routing prefix out of a full model alias.

    ``qwen3-3b-nsfw`` → ``qwen3``, ``gemma4-nsfw`` → ``gemma4``.
    Heuristic: take the first segment before ``-`` or ``.``. The
    canonical isSancioRouted allowlist matches by ``startsWith``, so
    the first segment is what matters.
    """
    if not alias:
        return ""
    for sep in ("-", ".", "/"):
        idx = alias.find(sep)
        if idx > 0:
            return alias[:idx].lower()
    return alias.lower()


def _extract_model_aliases(text: str) -> set[str]:
    """All ``model: '<value>'`` literals in *text*."""
    if not text:
        return set()
    return {m.group(1) for m in _MODEL_LITERAL_RE.finditer(text)}


def _extract_routed_prefixes(anthropic_text: str) -> set[str]:
    """Prefixes whitelisted via ``startsWith`` in anthropic.ts.

    We do not parse TypeScript — the regex catches every ``startsWith``
    literal in the file. False positives are harmless (extra prefixes
    in the allowlist mean fewer warnings, not unsafe routing).
    """
    if not anthropic_text:
        return set()
    return {m.group(1).lower() for m in _STARTSWITH_RE.finditer(anthropic_text)}


def _find_anthropic_ts(workspace: str, cognition_path: str) -> Optional[Path]:
    """Resolve the anthropic.ts companion to *cognition_path*.

    Walks up from the cognition file looking for ``psyche-engine`` and
    re-anchors to ``psyche-engine/src/anthropic.ts``. Falls back to the
    workspace-relative canonical path if the walk fails.
    """
    if not cognition_path:
        return None

    p = Path(cognition_path).resolve()
    parts = p.parts
    for i, segment in enumerate(parts):
        if segment.lower() == "psyche-engine":
            base = Path(*parts[: i + 1])
            candidate = base / "src" / "anthropic.ts"
            if candidate.exists():
                return candidate
            break

    if workspace:
        fallback = Path(workspace) / _ANTHROPIC_RELATIVE
        if fallback.exists():
            return fallback

    return None


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


# ── Guard ────────────────────────────────────────────────


class RoutebackendPrefixPairingGuard(BaseGuard):
    """PostToolUse warn when a model alias has no companion router prefix.

    Active for Edit / Write / NotebookEdit on psyche-engine cognition
    modules. Read-only — never denies. Warning is surfaced as
    ``additionalContext`` so the operator sees the missing pair in the
    next turn and fixes both files before docker-cp / deploy.
    """

    name = "routebackend_prefix_pairing"
    category = GuardCategory.QUALITY
    feature_name = "routebackend_prefix_pairing"

    # PreToolUse → no opinion. All work happens on PostToolUse.
    def check(self, ctx: GuardContext) -> Optional[GuardResult]:
        return None

    def on_post_tool(self, ctx: GuardContext) -> Optional[GuardResult]:
        if ctx.tool_name not in {"Edit", "Write", "NotebookEdit"}:
            return None

        target = ctx.tool_input.get("file_path", "")
        if not target or not isinstance(target, str):
            return None

        normalized = target.replace("\\", "/")
        if not _COGNITION_PATH_RE.search(normalized):
            return None

        cognition_path = Path(target)
        if not cognition_path.exists():
            return None

        cognition_text = _read_text(cognition_path)
        aliases = _extract_model_aliases(cognition_text)
        if not aliases:
            return None

        anthropic_path = _find_anthropic_ts(ctx.workspace, target)
        if anthropic_path is None:
            return GuardResult.allow_advisory(
                context=(
                    "[routebackend_prefix_pairing] could not locate "
                    "psyche-engine/src/anthropic.ts to verify routing "
                    "allowlist. Aliases found in edit: "
                    + ", ".join(sorted(aliases))
                ),
            )

        anthropic_text = _read_text(anthropic_path)
        routed = _extract_routed_prefixes(anthropic_text)

        missing: list[tuple[str, str]] = []
        for alias in sorted(aliases):
            prefix = _model_prefix(alias)
            if not prefix or prefix in _IGNORED_PREFIXES:
                continue
            if prefix in routed:
                continue
            missing.append((alias, prefix))

        if not missing:
            return None

        head = missing[:_MAX_REPORTED]
        more = len(missing) - len(head)
        lines = [
            "[routebackend_prefix_pairing] missing routing pair detected.",
            f"Edited: {target}",
            f"Router file: {anthropic_path}",
            "Aliases referenced in the edit but NOT covered by"
            " isSancioRouted startsWith():",
        ]
        for alias, prefix in head:
            lines.append(f"  - alias='{alias}' (prefix='{prefix}')")
        if more > 0:
            lines.append(f"  ...and {more} more")
        lines.append(
            "Action: add a startsWith('<prefix>') branch in "
            "isSancioRouted before redeploying. Skipping leaks the call "
            "to the default Anthropic upstream silently."
        )

        return GuardResult.allow_advisory(context="\n".join(lines))


# Convenience alias for guard registry imports. Some callers expect a
# bare ``Guard`` symbol.
Guard = RoutebackendPrefixPairingGuard
