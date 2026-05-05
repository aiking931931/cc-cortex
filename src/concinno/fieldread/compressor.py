# SPDX-License-Identifier: AGPL-3.0-or-later
"""FieldReadCompressor — 3-tier compression with breadcrumb audit trail.

Cigito v3 patent moat axis 3 surface (governance side, Concinno main).
Standalone implementation — **no aiking_core / lyceum runtime dep** —
so Concinno can ship as the canonical upstream of any consumer that
wants the 5-namespace contract without inheriting an AGPL implementation
detail from aiking_core's heterogeneous-KV layer.

Three-tier compression budgets:

    L1 (index)   : ≤200 chars  — single-line gist, used in handoff Index
    L2 (summary) : ≤1500 chars — section-bullet form, used in summaries
    L3 (archive) : unbounded   — raw content + provenance metadata

The compressor never *calls* an LLM — it is a pure markdown / text
heuristic so it can run inside hot prompt-build paths (PromptEngine
inject) without latency or cost.

Switch
------
``cfg.feature("fieldread.compressor", "enabled")`` (default ``True``).
When disabled, :meth:`compress` returns the input as-is wrapped in a
:class:`CompressedContent` whose ``compressed`` flag is ``False`` so
callers can still inspect the breadcrumb without losing fidelity.

Env override ``CONCINNO_FIELDREAD_DISABLED=1`` short-circuits the same
path without requiring the full ``cc_config`` machinery — useful inside
tests / cold-import diagnostics.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Optional

from concinno.fieldread.breadcrumb import Breadcrumb, breadcrumb_from_path
from concinno.fieldread.namespaces import (
    NAMESPACES,
    Namespace,
    is_namespace,
    route,
)

__all__ = [
    "CompressedContent",
    "FieldReadCompressor",
    "L1_BUDGET_CHARS",
    "L2_BUDGET_CHARS",
    "Tier",
]


# ── Tier budgets ───────────────────────────────────────────────────

#: L1 (index) tier — single-line gist for handoff Index injection.
L1_BUDGET_CHARS: Final[int] = 200

#: L2 (summary) tier — section-bullet form for handoff Summary.
L2_BUDGET_CHARS: Final[int] = 1500

#: L3 (archive) tier — unbounded.
_L3_UNBOUNDED: Final[int] = -1

#: Tier identifiers (use string for forward-compat with config values).
Tier = str  # "l1" | "l2" | "l3"
_TIERS: Final[tuple[str, ...]] = ("l1", "l2", "l3")
_BUDGETS: Final[dict[str, int]] = {
    "l1": L1_BUDGET_CHARS,
    "l2": L2_BUDGET_CHARS,
    "l3": _L3_UNBOUNDED,
}


# ── Markdown / heading heuristics ─────────────────────────────────

_HEADING_RE: Final[re.Pattern[str]] = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_BULLET_RE: Final[re.Pattern[str]] = re.compile(r"^\s*[-*+]\s+(.+)$")
_FRONTMATTER_RE: Final[re.Pattern[str]] = re.compile(r"^---\s*$")
_PENDING_RE: Final[re.Pattern[str]] = re.compile(r"[⬜⏸]")
_DONE_RE: Final[re.Pattern[str]] = re.compile(r"[✅]")


# ── Public dataclass ──────────────────────────────────────────────


@dataclass
class CompressedContent:
    """Result of a :meth:`FieldReadCompressor.compress` call.

    Attributes:
        namespace: One of :data:`concinno.fieldread.namespaces.NAMESPACES`.
        tier: One of ``"l1" | "l2" | "l3"``.
        content: Compressed markdown ready for prompt injection.
        breadcrumb: :class:`Breadcrumb` audit trail.
        original_chars: Length of the source content before compression.
        compressed: ``True`` iff content was actually compressed (False
            when source was already short enough or feature was disabled).
    """

    namespace: str
    tier: str
    content: str
    breadcrumb: Breadcrumb
    original_chars: int
    compressed: bool = False

    @property
    def chars(self) -> int:
        """Length of the (post-compression) :attr:`content`."""
        return len(self.content)

    @property
    def reduction_ratio(self) -> float:
        """Fraction of original characters removed (0.0–1.0).

        Returns ``0.0`` when the source was empty.
        """
        if self.original_chars <= 0:
            return 0.0
        saved = max(0, self.original_chars - self.chars)
        return saved / self.original_chars


# ── Internal helpers ──────────────────────────────────────────────


def _strip_frontmatter(text: str) -> str:
    """Remove leading ``---``/``---`` YAML frontmatter, if present."""
    lines = text.splitlines()
    if not lines or not _FRONTMATTER_RE.match(lines[0]):
        return text
    for idx in range(1, len(lines)):
        if _FRONTMATTER_RE.match(lines[idx]):
            return "\n".join(lines[idx + 1:])
    # Unterminated frontmatter — return original text untouched.
    return text


def _first_heading(text: str) -> Optional[str]:
    """Return the text of the first markdown heading in ``text``."""
    for line in text.splitlines():
        m = _HEADING_RE.match(line)
        if m:
            return m.group(2).strip()
    return None


def _first_bullet(text: str) -> Optional[str]:
    """Return the text of the first bullet line in ``text``."""
    for line in text.splitlines():
        m = _BULLET_RE.match(line)
        if m:
            return m.group(1).strip()
    return None


def _count_status_markers(text: str) -> tuple[int, int, int]:
    """Return ``(pending_count, blocked_count, done_count)`` for ``text``."""
    pending = sum(1 for _ in re.finditer(r"⬜", text))
    blocked = sum(1 for _ in re.finditer(r"⏸", text))
    done = sum(1 for _ in re.finditer(r"✅", text))
    return pending, blocked, done


def _truncate_to_budget(text: str, budget: int) -> str:
    """Return ``text`` truncated to ``budget`` chars, with ellipsis suffix.

    A negative budget is treated as unbounded.
    """
    if budget < 0 or len(text) <= budget:
        return text
    if budget <= 3:
        return text[:budget]
    keep = budget - 3
    return text[:keep].rstrip() + "..."


def _l1_index(text: str) -> str:
    """Compress to a single-line index ≤ :data:`L1_BUDGET_CHARS` chars.

    Selection priority:
        1. First heading text (drops the leading ``#`` markers).
        2. Pending/blocked/done counts (e.g. ``"3 pending, 1 blocked"``).
        3. First bullet text.
        4. Naive truncate of the first non-empty line.
    """
    if not text:
        return ""

    body = _strip_frontmatter(text)

    heading = _first_heading(body)
    pending, blocked, done = _count_status_markers(body)

    parts: list[str] = []
    if heading:
        parts.append(heading)

    if pending or blocked:
        status_bits: list[str] = []
        if pending:
            status_bits.append(f"{pending} pending")
        if blocked:
            status_bits.append(f"{blocked} blocked")
        if done:
            status_bits.append(f"{done} done")
        parts.append(", ".join(status_bits))
    elif done and not heading:
        parts.append(f"{done} done")

    if not parts:
        bullet = _first_bullet(body)
        if bullet:
            parts.append(bullet)

    if not parts:
        for line in body.splitlines():
            stripped = line.strip()
            if stripped:
                parts.append(stripped)
                break

    if not parts:
        return ""

    joined = " — ".join(parts)
    return _truncate_to_budget(joined, L1_BUDGET_CHARS)


def _l2_summary(text: str) -> str:
    """Compress to a section-bullet summary ≤ :data:`L2_BUDGET_CHARS` chars.

    Walks the document section by section (## headings); for each section
    keeps the heading + first bullet/paragraph until the budget is hit.
    """
    if not text:
        return ""

    body = _strip_frontmatter(text)
    if len(body) <= L2_BUDGET_CHARS:
        return body.strip()

    out: list[str] = []
    used = 0
    current_heading: Optional[str] = None
    section_buffer: list[str] = []

    def _flush() -> bool:
        """Flush the current section. Returns True if budget exhausted."""
        nonlocal used
        if not current_heading and not section_buffer:
            return False
        chunk_lines: list[str] = []
        if current_heading:
            chunk_lines.append(current_heading)
        # Prefer the first bullet; else the first non-empty line.
        first_bullet = next(
            (ln for ln in section_buffer if _BULLET_RE.match(ln)),
            None,
        )
        if first_bullet:
            chunk_lines.append(first_bullet)
        else:
            for ln in section_buffer:
                if ln.strip():
                    chunk_lines.append(f"  {ln.strip()}")
                    break
        chunk = "\n".join(chunk_lines)
        if used + len(chunk) + 1 > L2_BUDGET_CHARS:
            remaining = L2_BUDGET_CHARS - used
            if remaining > 8:
                out.append(_truncate_to_budget(chunk, remaining))
                used = L2_BUDGET_CHARS
            return True
        out.append(chunk)
        used += len(chunk) + 1  # newline
        return False

    for line in body.splitlines():
        m = _HEADING_RE.match(line)
        if m:
            if _flush():
                break
            current_heading = f"{m.group(1)} {m.group(2)}".rstrip()
            section_buffer = []
            continue
        section_buffer.append(line)

    if used < L2_BUDGET_CHARS:
        _flush()

    summary = "\n".join(out).strip()
    return _truncate_to_budget(summary, L2_BUDGET_CHARS)


def _validate_namespace(namespace: str) -> str:
    """Raise :class:`ValueError` if ``namespace`` is not in :data:`NAMESPACES`.

    Returns the validated namespace string (allows fluent chaining).
    """
    if not is_namespace(namespace):
        raise ValueError(
            f"namespace {namespace!r} is not one of {NAMESPACES!r}",
        )
    return namespace


def _is_disabled_via_env() -> bool:
    """Return True if env var ``CONCINNO_FIELDREAD_DISABLED`` is truthy."""
    raw = os.environ.get("CONCINNO_FIELDREAD_DISABLED", "")
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _is_disabled_via_config() -> bool:
    """Return True if `cfg.feature('fieldread.compressor', 'enabled')` is False.

    Soft-imports concinno.core.config so this module stays importable
    inside lean test fixtures that don't ship the full feature_config
    surface.
    """
    try:  # pragma: no cover - soft path
        from concinno.core.config import get_config
    except ImportError:
        return False
    try:
        cfg = get_config()
        enabled = cfg.feature("fieldread.compressor", "enabled")
    except Exception:
        return False
    if enabled is None:
        return False
    return not bool(enabled)


# ── Public API ─────────────────────────────────────────────────────


@dataclass
class FieldReadCompressor:
    """3-tier FieldRead compressor with breadcrumb audit trail.

    Stateless — safe to instantiate per-call or hold as a module-level
    singleton. The class is a dataclass so callers may override the
    namespace router or the budget table for testing.

    Attributes:
        budgets: Mapping of tier → char budget. Defaults to
            ``{"l1": 200, "l2": 1500, "l3": -1}``. Negative values mean
            "unbounded".
    """

    budgets: dict[str, int] = field(
        default_factory=lambda: dict(_BUDGETS),
    )

    # ─── Instance API ────

    def compress(
        self,
        content: str,
        namespace: str,
        *,
        tier: str = "l2",
        section: Optional[str] = None,
    ) -> CompressedContent:
        """Compress ``content`` into the requested ``tier`` budget.

        Args:
            content: Raw source text (markdown).
            namespace: One of :data:`NAMESPACES`. Use :func:`route` to
                derive a namespace from a path or query first.
            tier: ``"l1"`` (index ≤200ch) / ``"l2"`` (summary ≤1500ch)
                / ``"l3"`` (archive, unbounded). Defaults to ``"l2"``.
            section: Optional section identifier to record in the
                returned breadcrumb. Plain string; not validated.

        Returns:
            :class:`CompressedContent` with the compressed body and an
            audit-trail breadcrumb. When the feature is disabled (env or
            config), the original content is returned unchanged with
            ``compressed=False``.

        Raises:
            ValueError: If ``namespace`` is not in :data:`NAMESPACES` or
                ``tier`` is not one of ``("l1", "l2", "l3")``.
        """
        ns = _validate_namespace(namespace)
        tier_lc = (tier or "l2").lower()
        if tier_lc not in _TIERS:
            raise ValueError(
                f"tier {tier!r} not in {_TIERS!r}",
            )

        original_len = len(content) if content else 0
        crumb = Breadcrumb(
            namespace=ns,
            depth=0,
            ancestors=(),
            section=section,
        )

        # Disable short-circuit — env wins over config.
        if _is_disabled_via_env() or _is_disabled_via_config():
            return CompressedContent(
                namespace=ns,
                tier=tier_lc,
                content=content or "",
                breadcrumb=crumb,
                original_chars=original_len,
                compressed=False,
            )

        if not content:
            return CompressedContent(
                namespace=ns,
                tier=tier_lc,
                content="",
                breadcrumb=crumb,
                original_chars=0,
                compressed=False,
            )

        budget = self.budgets.get(tier_lc, _L3_UNBOUNDED)
        if budget < 0 or original_len <= budget:
            # Already within budget — return unchanged (l3 path / short).
            return CompressedContent(
                namespace=ns,
                tier=tier_lc,
                content=content if tier_lc == "l3" else content.strip(),
                breadcrumb=crumb,
                original_chars=original_len,
                compressed=False,
            )

        if tier_lc == "l1":
            body = _l1_index(content)
        elif tier_lc == "l2":
            body = _l2_summary(content)
        else:  # l3
            body = content

        return CompressedContent(
            namespace=ns,
            tier=tier_lc,
            content=body,
            breadcrumb=crumb,
            original_chars=original_len,
            compressed=True,
        )

    def breadcrumb(
        self,
        path: str | Path,
        *,
        namespace: Optional[str] = None,
    ) -> Breadcrumb:
        """Return a :class:`Breadcrumb` for a filesystem path.

        Args:
            path: Filesystem path.
            namespace: Override; when ``None`` the namespace is auto-
                routed via :func:`route` on ``str(path)``.

        Returns:
            Breadcrumb capturing namespace + ancestor chain + section.
        """
        ns = namespace if namespace is not None else self.route(str(path))
        _validate_namespace(ns)
        return breadcrumb_from_path(path, ns)

    def route(self, query: str) -> Namespace:
        """Route a query (path or keyword) to one of :data:`NAMESPACES`.

        Thin wrapper around :func:`concinno.fieldread.namespaces.route`
        so tests can monkey-patch the routing behaviour per-instance.
        """
        return route(query)
