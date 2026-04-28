"""concinno.observability.token_audit.overhead_tracker — per-bucket overhead tracker.

Decomposes the per-session audit into three named buckets (skill /
mcp / subagent) plus the fixed ``system_prompt_floor`` so the
operator can see exactly which surface is dominating their context
window. The :class:`OverheadTracker` is intentionally stateless w.r.t.
the audit jsonl flush — it consults a :class:`TokenAudit` instance,
runs the same aggregation logic the audit uses internally, and
returns a typed :class:`OverheadBreakdown`. Tests use this layer to
assert per-bucket math without snapshotting the full session
summary.

Concurrency
-----------
The tracker borrows the audit's lock by routing every read through
:meth:`TokenAudit.session_summary` (which itself holds the lock).
This makes the tracker safe to call from any thread that already
holds a TokenAudit reference; we never lock externally.

Char↔token conversion
---------------------
Every public field is reported in **estimated tokens** so the
breakdown lines up with :class:`TokenAuditSummary` totals. Internal
helpers retain the raw char counts so a future caller that wants to
re-skin the report (e.g. char counts for ASCII-heavy SKILL.md vs CJK
docs) can subclass and override.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from concinno.observability.token_audit.audit import (
    DEFAULT_CHARS_PER_TOKEN,
    TokenAudit,
    TokenAuditSummary,
)


@dataclass
class OverheadBreakdown:
    """Per-bucket overhead breakdown in estimated tokens.

    Each bucket is reported as
    ``(total_tokens, top_n_breakdown_dict)`` so the operator can both
    see the bucket total at a glance and drill into the worst
    offenders. ``top_n_skills`` / ``top_n_mcp`` defaults are 10 — the
    top-10 cap matches the GUI default and keeps stderr summaries
    one-screen-friendly.
    """

    system_prompt_floor: int
    skills_total: int
    skills_top: dict[str, int] = field(default_factory=dict)
    mcp_total: int = 0
    mcp_top: dict[str, int] = field(default_factory=dict)
    subagents_total: int = 0
    subagents_count: int = 0
    tail_buffer: int = 0
    grand_total: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "system_prompt_floor": self.system_prompt_floor,
            "skills_total": self.skills_total,
            "skills_top": dict(self.skills_top),
            "mcp_total": self.mcp_total,
            "mcp_top": dict(self.mcp_top),
            "subagents_total": self.subagents_total,
            "subagents_count": self.subagents_count,
            "tail_buffer": self.tail_buffer,
            "grand_total": self.grand_total,
        }


class OverheadTracker:
    """Stateless analyser over a :class:`TokenAudit` snapshot."""

    def __init__(
        self,
        audit: TokenAudit,
        *,
        top_n_skills: int = 10,
        top_n_mcp: int = 10,
        chars_per_token: float = DEFAULT_CHARS_PER_TOKEN,
    ) -> None:
        self._audit = audit
        self._top_n_skills = max(0, int(top_n_skills))
        self._top_n_mcp = max(0, int(top_n_mcp))
        # Surface for tests; the audit's own conversion rate is the
        # source of truth for actual numbers, this value is here so
        # downstream consumers can audit what the tracker assumed.
        self._chars_per_token = max(0.1, float(chars_per_token))

    @property
    def chars_per_token(self) -> float:
        return self._chars_per_token

    def breakdown(self) -> OverheadBreakdown:
        """Compute the per-bucket breakdown from the audit snapshot.

        Pulls a non-flushing :meth:`TokenAudit.session_summary` so the
        breakdown does not write to disk; the hook layer is the only
        site that decides to flush.
        """
        summary = self._audit.session_summary(flush=False)
        skills_top = self._top_n(summary.skills_breakdown, self._top_n_skills)
        mcp_top = self._top_n(summary.mcp_breakdown, self._top_n_mcp)
        return OverheadBreakdown(
            system_prompt_floor=summary.system_prompt_floor,
            skills_total=summary.skills_total_overhead,
            skills_top=skills_top,
            mcp_total=summary.mcp_total_overhead,
            mcp_top=mcp_top,
            subagents_total=summary.subagents_total_overhead,
            subagents_count=summary.subagents_spawned,
            tail_buffer=summary.tail_buffer,
            grand_total=summary.total_estimated_tokens,
        )

    def per_skill(self) -> dict[str, int]:
        """Return the full per-skill overhead map (estimated tokens).

        Unlike :meth:`breakdown` this skips the ``top_n`` cap — the
        GUI uses the full map for its detail view.
        """
        summary = self._audit.session_summary(flush=False)
        return dict(summary.skills_breakdown)

    def per_mcp(self) -> dict[str, int]:
        """Return the full per-MCP-tool overhead map (estimated tokens)."""
        summary = self._audit.session_summary(flush=False)
        return dict(summary.mcp_breakdown)

    def system_floor_tokens(self) -> int:
        """Return the configured system-prompt floor in estimated tokens."""
        summary = self._audit.session_summary(flush=False)
        return summary.system_prompt_floor

    def subagent_total(self) -> int:
        """Return the total sub-agent overhead in estimated tokens."""
        summary = self._audit.session_summary(flush=False)
        return summary.subagents_total_overhead

    # ─── Reporting helpers ─────────────────────────────────────────

    def render_text(self, *, lines_max: int = 20) -> str:
        """Render the breakdown as a one-screen text report.

        Useful for stderr session_summary output. ``lines_max``
        truncates long skill / mcp lists so the report fits a
        terminal pane.
        """
        b = self.breakdown()
        lines: list[str] = []
        lines.append(
            f"Token audit (session={self._audit.session_id}): "
            f"~{b.grand_total} tokens total",
        )
        lines.append(
            f"  system prompt floor : {b.system_prompt_floor} tok",
        )
        lines.append(
            f"  skills              : {b.skills_total} tok "
            f"({len(self.per_skill())} loaded)",
        )
        for name, tok in list(b.skills_top.items())[:lines_max]:
            lines.append(f"      - {name:<32s} {tok} tok")
        lines.append(
            f"  mcp tools           : {b.mcp_total} tok "
            f"({len(self.per_mcp())} loaded)",
        )
        for name, tok in list(b.mcp_top.items())[:lines_max]:
            lines.append(f"      - {name:<32s} {tok} tok")
        lines.append(
            f"  sub-agents          : {b.subagents_total} tok "
            f"({b.subagents_count} spawned)",
        )
        if b.tail_buffer:
            lines.append(f"  tail buffer         : {b.tail_buffer} tok")
        return "\n".join(lines)

    def render_dict(self) -> dict[str, Any]:
        """Render the breakdown as a plain dict (CLI ``--format=json``)."""
        return self.breakdown().to_dict()

    # ─── Internals ─────────────────────────────────────────────────

    @staticmethod
    def _top_n(mapping: dict[str, int], n: int) -> dict[str, int]:
        if not mapping or n <= 0:
            return {}
        items = sorted(mapping.items(), key=lambda kv: kv[1], reverse=True)
        return dict(items[:n])


def summarize_session(
    audit: TokenAudit,
    *,
    top_n_skills: int = 10,
    top_n_mcp: int = 10,
) -> OverheadBreakdown:
    """Convenience wrapper — one-shot breakdown without holding a tracker."""
    return OverheadTracker(
        audit,
        top_n_skills=top_n_skills,
        top_n_mcp=top_n_mcp,
    ).breakdown()


def render_summary(audit: TokenAudit, *, lines_max: int = 20) -> str:
    """One-shot text rendering — same idea, one call site."""
    return OverheadTracker(audit).render_text(lines_max=lines_max)


# ─── Per-summary helper (dict-only consumers) ─────────────────────


def breakdown_from_summary(
    summary: TokenAuditSummary,
    *,
    top_n_skills: int = 10,
    top_n_mcp: int = 10,
) -> OverheadBreakdown:
    """Compute a breakdown directly from a :class:`TokenAuditSummary`.

    Useful when the caller has already snapshotted the summary (e.g.
    from a jsonl line) and does not have the live :class:`TokenAudit`
    instance. Mirrors :meth:`OverheadTracker.breakdown` exactly.
    """
    skills_sorted = sorted(
        summary.skills_breakdown.items(), key=lambda kv: kv[1], reverse=True,
    )
    mcp_sorted = sorted(
        summary.mcp_breakdown.items(), key=lambda kv: kv[1], reverse=True,
    )
    return OverheadBreakdown(
        system_prompt_floor=summary.system_prompt_floor,
        skills_total=summary.skills_total_overhead,
        skills_top=dict(skills_sorted[: max(0, top_n_skills)]),
        mcp_total=summary.mcp_total_overhead,
        mcp_top=dict(mcp_sorted[: max(0, top_n_mcp)]),
        subagents_total=summary.subagents_total_overhead,
        subagents_count=summary.subagents_spawned,
        tail_buffer=summary.tail_buffer,
        grand_total=summary.total_estimated_tokens,
    )


__all__ = [
    "OverheadBreakdown",
    "OverheadTracker",
    "breakdown_from_summary",
    "render_summary",
    "summarize_session",
]
