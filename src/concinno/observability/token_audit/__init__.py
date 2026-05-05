"""concinno.observability.token_audit — Token Audit Autopilot.

Per-session accounting of the four largest sources of context-window
spend that the operator has *some* leverage over:

* **System prompt floor** — the static portion of the harness system
  prompt (Anthropic CC ships a known ~14 000-token floor; configurable
  via ``system_prompt_floor_tokens`` for users on a forked harness).
* **Per-skill schema overhead** — the L1 frontmatter + L2 SKILL.md
  cost paid for every skill mounted into the session, even when the
  skill is not invoked.
* **Per-MCP tool schema overhead** — JSON schema cost for each MCP
  server's tool definitions, paid up-front at handshake.
* **Sub-agent overhead** — brief size on spawn + return-report size
  on join, charged against the parent session's context window.

The audit is **best-effort accounting**, not enforcement: it records
what was loaded, sums the deltas at session end, and emits a
JSON-Lines record per session under ``~/.concinno/audit/`` so the
operator can later reason about which surfaces are eating their
budget. The :class:`ArchiveAdvisor` extends this with a ZIQ
FTRL-driven recommendation for skills the operator has not invoked
in a configurable retention window.

Public surface:

    TokenAudit              — per-session recorder + summariser.
    TokenAuditSummary       — typed summary returned by ``session_summary``.
    ArchiveAdvisor          — ZIQ FTRL archive recommender.
    ArchiveCandidate        — typed advisor recommendation.

This module **does not fork** any third-party code. The shape of the
recorded fields is informed by the public ``slima4/claude-tui`` MIT
spec (cleanroom: spec only, no source) and the Anthropic CC public
documentation on prompt caching + system prompts.
"""

from __future__ import annotations

from concinno.observability.token_audit.archive_advisor import (
    ArchiveAdvisor,
    ArchiveCandidate,
)
from concinno.observability.token_audit.audit import (
    TokenAudit,
    TokenAuditSummary,
)

__all__ = [
    "ArchiveAdvisor",
    "ArchiveCandidate",
    "TokenAudit",
    "TokenAuditSummary",
]
