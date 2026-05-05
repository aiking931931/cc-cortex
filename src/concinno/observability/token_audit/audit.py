"""concinno.observability.token_audit.audit — Core TokenAudit recorder.

Records the per-session token overhead components in memory, then on
``session_summary()`` flushes a JSON-Lines record to
``~/.concinno/audit/token_audit_<session>.jsonl``.

Threading
---------
A single ``threading.Lock`` guards every mutating record (skill load,
sub-agent spawn, MCP load). Reads (``session_summary``) acquire the
same lock briefly so that a concurrent recorder cannot tear a
half-updated overhead total. Recorders never block on I/O — disk
flush only happens at ``session_summary()`` / ``end_session()`` time.

Token estimation
----------------
We measure characters, not tokens, because (a) token counts are
model-specific and we ship without a tokenizer dependency, and (b)
the operator's actionable signal is "skill X costs N characters of
my system prompt", which is stable across model swaps. The chars→
tokens conversion is a single ``CHARS_PER_TOKEN`` knob (default
``4.0`` per Anthropic public cookbook guidance) that the caller
applies at presentation time.

Disabled mode
-------------
``TokenAudit(enabled=False)`` makes every ``record_*`` call a no-op
and ``session_summary()`` returns a zero-totals summary without
flushing. This is the in-memory mirror of the ``token_audit_autopilot``
feature flag; the hooks consult ``cfg.feature(...)`` to decide which
mode to instantiate.

ENABLE_TOOL_SEARCH note
-----------------------
The Plan v3 W3 brief mentions an experimental ``ENABLE_TOOL_SEARCH``
mode; this module **does not** assume any specific savings number
because the figure has not been empirically confirmed in this
codebase yet. The recorder schema carries an optional
``tool_search_enabled`` field so a future experiment can compare
overhead distributions with / without that mode.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

# Default char-to-token conversion. Anthropic cookbook quotes ~4 chars
# per token for English-heavy prompts; users on CJK-heavy prompts can
# override at presentation time. We do not store tokens directly so
# the raw record stays model-agnostic.
DEFAULT_CHARS_PER_TOKEN: float = 4.0

# Anthropic CC ships a known ~14k-token static system-prompt floor
# (per Plan v3 W3 line 96; estimated, awaits empirical confirm on a
# fresh install). The exact value is configurable via the
# ``system_prompt_floor_tokens`` FEATURE_META param so that operators
# on a forked harness do not get a stale anchor.
DEFAULT_SYSTEM_PROMPT_FLOOR_TOKENS: int = 14000

# Default audit jsonl directory under ``~/.concinno/audit/``. Pure
# ``Path.home()`` derivation per the no-personal-paths rule.
def _default_audit_dir() -> Path:
    return Path.home() / ".concinno" / "audit"


@dataclass
class _SkillRecord:
    """One skill load record."""

    name: str
    l2_chars: int
    frontmatter_chars: int
    loaded_at: float
    invocation_count: int = 0


@dataclass
class _SubagentRecord:
    """One sub-agent spawn record."""

    brief_chars: int
    return_chars: int
    spawned_at: float


@dataclass
class _McpRecord:
    """One MCP tool definition record."""

    server: str
    tool: str
    schema_chars: int
    loaded_at: float


@dataclass
class TokenAuditSummary:
    """Typed summary returned by :meth:`TokenAudit.session_summary`."""

    session_id: str
    enabled: bool
    started_at: float
    ended_at: float | None
    # Skill bucket
    skills_loaded: int
    skills_total_overhead: int
    skills_breakdown: dict[str, int]
    # Sub-agent bucket
    subagents_spawned: int
    subagents_total_overhead: int
    # MCP bucket
    mcp_tools_loaded: int
    mcp_total_overhead: int
    mcp_breakdown: dict[str, int]
    # System floor + grand total
    system_prompt_floor: int
    tail_buffer: int
    total_estimated_tokens: int
    # Optional flags for future experiments — schema-stable since
    # 4.5.0 even though the values are not used by anything that ships
    # in this version. ``tool_search_enabled`` carries the
    # ENABLE_TOOL_SEARCH state if the operator has flipped it; default
    # ``False`` until the empirical study lands.
    tool_search_enabled: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class TokenAudit:
    """Per-session token overhead recorder + summariser.

    Construct one instance per session. The hooks call
    ``begin_session()`` on SessionStart and ``session_summary()`` on
    Stop; in between, guards / dispatchers call the ``record_*``
    methods. Every recorder is best-effort — a malformed call (None
    args, negative ints, etc.) is silently coerced rather than raised
    so the audit pipeline cannot break the session.
    """

    def __init__(
        self,
        *,
        session_id: str | None = None,
        enabled: bool = True,
        audit_dir: Path | None = None,
        system_prompt_floor_tokens: int = DEFAULT_SYSTEM_PROMPT_FLOOR_TOKENS,
        chars_per_token: float = DEFAULT_CHARS_PER_TOKEN,
    ) -> None:
        self._lock = threading.Lock()
        self._enabled = bool(enabled)
        self._session_id = session_id or uuid.uuid4().hex
        self._audit_dir = audit_dir or _default_audit_dir()
        self._floor_tokens = max(0, int(system_prompt_floor_tokens))
        self._chars_per_token = max(0.1, float(chars_per_token))
        self._started_at: float = time.time()
        self._ended_at: float | None = None
        self._stopped: bool = False
        # Buckets
        self._skills: dict[str, _SkillRecord] = {}
        self._subagents: list[_SubagentRecord] = []
        self._mcp: list[_McpRecord] = []
        self._tail_buffer_chars: int = 0
        # Future-experimental flag; FEATURE_META exposes a knob but
        # default stays ``False`` per cleanroom rule.
        self._tool_search_enabled: bool = False

    # ─── Public lifecycle ───────────────────────────────────────────

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def enabled(self) -> bool:
        return self._enabled

    def begin_session(self) -> None:
        """Mark session start. Idempotent — repeated calls are no-ops."""
        if not self._enabled:
            return
        with self._lock:
            if self._started_at == 0.0:
                self._started_at = time.time()
            self._ended_at = None
            self._stopped = False

    def end_session(self) -> TokenAuditSummary:
        """Mark session end + flush jsonl + return summary.

        Equivalent to calling :meth:`session_summary` followed by an
        internal flush; the hook layer should prefer this on Stop.
        """
        summary = self.session_summary(flush=True)
        with self._lock:
            self._stopped = True
        return summary

    # ─── Recorders ─────────────────────────────────────────────────

    def record_skill_load(
        self,
        skill_name: str,
        l2_chars: int,
        frontmatter_chars: int = 0,
    ) -> None:
        """Record a skill SKILL.md (L2) + frontmatter (L1) load.

        If a skill is loaded twice in one session (shouldn't happen,
        but the harness has bugs), the second load updates the
        recorded chars to the larger value rather than double-
        counting — the operator pays the larger cost once, not twice.
        """
        if not self._enabled:
            return
        if self._stopped:
            self._warn_after_stop("record_skill_load")
            return
        name = str(skill_name or "").strip()
        if not name:
            return
        l2c = max(0, int(l2_chars))
        fmc = max(0, int(frontmatter_chars))
        with self._lock:
            existing = self._skills.get(name)
            if existing is None:
                self._skills[name] = _SkillRecord(
                    name=name,
                    l2_chars=l2c,
                    frontmatter_chars=fmc,
                    loaded_at=time.time(),
                )
            else:
                existing.l2_chars = max(existing.l2_chars, l2c)
                existing.frontmatter_chars = max(
                    existing.frontmatter_chars, fmc,
                )

    def record_skill_invocation(self, skill_name: str) -> None:
        """Record that a previously-loaded skill was actually invoked.

        Used by :class:`ArchiveAdvisor` to distinguish "loaded but
        never used" skills (archive candidates) from "actually
        invoked" skills (keep).
        """
        if not self._enabled:
            return
        name = str(skill_name or "").strip()
        if not name:
            return
        with self._lock:
            rec = self._skills.get(name)
            if rec is not None:
                rec.invocation_count += 1

    def record_subagent(self, brief_chars: int, return_chars: int) -> None:
        """Record a sub-agent spawn cost (brief + return-report)."""
        if not self._enabled:
            return
        if self._stopped:
            self._warn_after_stop("record_subagent")
            return
        bc = max(0, int(brief_chars))
        rc = max(0, int(return_chars))
        with self._lock:
            self._subagents.append(
                _SubagentRecord(
                    brief_chars=bc,
                    return_chars=rc,
                    spawned_at=time.time(),
                )
            )

    def record_mcp_tool(
        self,
        server: str,
        tool: str,
        schema_chars: int,
    ) -> None:
        """Record an MCP tool schema load."""
        if not self._enabled:
            return
        if self._stopped:
            self._warn_after_stop("record_mcp_tool")
            return
        srv = str(server or "").strip()
        tl = str(tool or "").strip()
        if not srv or not tl:
            return
        sc = max(0, int(schema_chars))
        with self._lock:
            self._mcp.append(
                _McpRecord(
                    server=srv,
                    tool=tl,
                    schema_chars=sc,
                    loaded_at=time.time(),
                )
            )

    def record_tail_buffer(self, chars: int) -> None:
        """Record the tail / scratchpad buffer (transient context, optional)."""
        if not self._enabled:
            return
        with self._lock:
            self._tail_buffer_chars = max(0, int(chars))

    def set_tool_search_enabled(self, enabled: bool) -> None:
        """Mark whether ENABLE_TOOL_SEARCH is active for this session.

        Experimental — awaits empirical confirm. Pure metadata field;
        does not change accounting.
        """
        with self._lock:
            self._tool_search_enabled = bool(enabled)

    # ─── Summary + flush ───────────────────────────────────────────

    def session_summary(self, *, flush: bool = False) -> TokenAuditSummary:
        """Aggregate recorded buckets into a typed summary.

        ``flush=True`` writes one JSON-Lines record to
        ``<audit_dir>/token_audit_<session>.jsonl`` (append mode) and
        returns the same summary object. Disk failures are swallowed
        — the audit pipeline must not break Stop.
        """
        with self._lock:
            self._ended_at = time.time()
            skills_breakdown = {
                name: rec.l2_chars + rec.frontmatter_chars
                for name, rec in sorted(self._skills.items())
            }
            skills_total = sum(skills_breakdown.values())
            sub_total = sum(
                r.brief_chars + r.return_chars for r in self._subagents
            )
            mcp_breakdown_chars: dict[str, int] = {}
            for rec in self._mcp:
                key = f"{rec.server}:{rec.tool}"
                mcp_breakdown_chars[key] = (
                    mcp_breakdown_chars.get(key, 0) + rec.schema_chars
                )
            mcp_total = sum(mcp_breakdown_chars.values())
            tail = self._tail_buffer_chars
            # Convert chars → estimated tokens.
            cpt = self._chars_per_token
            skills_tokens = int(skills_total / cpt) if self._enabled else 0
            sub_tokens = int(sub_total / cpt) if self._enabled else 0
            mcp_tokens = int(mcp_total / cpt) if self._enabled else 0
            tail_tokens = int(tail / cpt) if self._enabled else 0
            floor = self._floor_tokens if self._enabled else 0
            total = floor + skills_tokens + sub_tokens + mcp_tokens + tail_tokens
            summary = TokenAuditSummary(
                session_id=self._session_id,
                enabled=self._enabled,
                started_at=self._started_at,
                ended_at=self._ended_at,
                skills_loaded=len(self._skills),
                skills_total_overhead=skills_tokens,
                skills_breakdown={
                    k: int(v / cpt) for k, v in skills_breakdown.items()
                },
                subagents_spawned=len(self._subagents),
                subagents_total_overhead=sub_tokens,
                mcp_tools_loaded=len(self._mcp),
                mcp_total_overhead=mcp_tokens,
                mcp_breakdown={
                    k: int(v / cpt) for k, v in mcp_breakdown_chars.items()
                },
                system_prompt_floor=floor,
                tail_buffer=tail_tokens,
                total_estimated_tokens=total,
                tool_search_enabled=self._tool_search_enabled,
            )

        if flush and self._enabled:
            self._flush_jsonl(summary)
        return summary

    # ─── Internals ─────────────────────────────────────────────────

    def _flush_jsonl(self, summary: TokenAuditSummary) -> None:
        """Append summary as a JSON-Lines record. Best-effort."""
        try:
            self._audit_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            return
        path = self._audit_dir / f"token_audit_{self._session_id}.jsonl"
        try:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(summary.to_dict(), ensure_ascii=False))
                fh.write("\n")
        except OSError:
            # Disk full / permission denied — never break Stop.
            return

    def _warn_after_stop(self, method: str) -> None:
        """Emit a stderr warning when a recorder fires after Stop.

        We don't ``raise`` because the recorder contract is "never
        block the session"; we just leave a breadcrumb for the
        operator who is reading audit logs.
        """
        try:
            import sys

            sys.stderr.write(
                f"concinno.token_audit: {method} called after end_session() "
                f"on session {self._session_id} — record dropped.\n"
            )
        except Exception:  # pragma: no cover - stderr write should never fail
            pass

    # ─── Module-singleton helpers ──────────────────────────────────

    @classmethod
    def get_default_audit_dir(cls) -> Path:
        """Public accessor for the default audit dir.

        Tests use this to assert the audit jsonl is written under
        ``Path.home() / ".concinno" / "audit"`` without hard-coding
        a personal path.
        """
        return _default_audit_dir()


# ─── Module-level singleton (best-effort) ─────────────────────────

_singleton_lock = threading.Lock()
_singleton: TokenAudit | None = None


def get_audit(
    *,
    session_id: str | None = None,
    enabled: bool | None = None,
) -> TokenAudit:
    """Process-wide :class:`TokenAudit` singleton.

    The hooks construct the audit once on SessionStart and consult
    the singleton from any guard that wants to record overhead. If
    ``enabled`` is ``None`` (the typical case) the singleton honours
    its previous state; passing ``True`` / ``False`` explicitly
    overrides — for example, hooks that pull the current FEATURE_META
    toggle on every Stop.
    """
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = TokenAudit(
                session_id=session_id,
                enabled=True if enabled is None else enabled,
            )
        elif enabled is not None and _singleton.enabled != bool(enabled):
            # Reconstruct — toggling enabled mid-session is rare and
            # we don't want partially-counted state.
            _singleton = TokenAudit(
                session_id=_singleton.session_id,
                enabled=bool(enabled),
            )
    return _singleton


def reset_audit() -> None:
    """Reset the module singleton — used by tests for isolation."""
    global _singleton
    with _singleton_lock:
        _singleton = None


# Aware of test environments that override HOME via monkeypatch.
def _audit_path_for(session_id: str) -> Path:
    """Return the canonical jsonl path for a given session id."""
    base = os.environ.get("CONCINNO_AUDIT_DIR", "").strip()
    root = Path(base) if base else _default_audit_dir()
    return root / f"token_audit_{session_id}.jsonl"


__all__ = [
    "DEFAULT_CHARS_PER_TOKEN",
    "DEFAULT_SYSTEM_PROMPT_FLOOR_TOKENS",
    "TokenAudit",
    "TokenAuditSummary",
    "get_audit",
    "reset_audit",
]
