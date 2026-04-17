"""concinno.cache.cache_break_detector — Prompt cache break diagnostics.

@module cache_break_detector
@responsibility Track cache-affecting fields of every LLM request and
    pinpoint WHICH field caused an Anthropic prompt cache break.
    Without per-field diffs the caller only knows "cache missed";
    with them they can see "77% of tool breaks are AgentTool dynamic
    embed churn" and take corrective action.
@dependencies concinno.core.state_store
@exports CacheBreakDetector, PreviousState, BreakReport,
    CacheBreakReason, hash_field, hash_per_tool

Ported from Claude Code's services/api/promptCacheBreakDetection.ts.
The TS version tracks ~15 fields tied to QuerySource/AgentId; this
port exposes the subset that matters for library consumers:

    system_hash, tools_hash, per_tool_hashes, betas, effort_value,
    global_cache_strategy

The public diff API returns a tuple of reasons (multiple fields can
change at once) plus an optional ``changed_tool`` pointer for the
common "a single tool's embed drifted" case.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from hashlib import sha256
from typing import Any, Literal

from concinno.core.state_store import StateStore

_NS = "cache_break_detector"
_FLAT_NAME = "state.json"

CacheBreakReason = Literal[
    "first_request",
    "system_changed",
    "tools_changed",
    "per_tool_changed",
    "betas_changed",
    "effort_changed",
    "strategy_changed",
    "no_break",
]


@dataclass
class PreviousState:
    """Snapshot of the last request's cache-affecting fields."""

    system_hash: str = ""
    tools_hash: str = ""
    per_tool_hashes: dict[str, str] = field(default_factory=dict)
    betas: tuple[str, ...] = ()
    effort_value: str = ""
    global_cache_strategy: str = ""

    def is_empty(self) -> bool:
        """True when nothing has been recorded yet (first_request baseline)."""
        return (
            not self.system_hash
            and not self.tools_hash
            and not self.per_tool_hashes
            and not self.betas
            and not self.effort_value
            and not self.global_cache_strategy
        )

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable dict (tuples become lists)."""
        data = asdict(self)
        data["betas"] = list(self.betas)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PreviousState:
        return cls(
            system_hash=str(data.get("system_hash", "")),
            tools_hash=str(data.get("tools_hash", "")),
            per_tool_hashes=dict(data.get("per_tool_hashes", {}) or {}),
            betas=tuple(data.get("betas", ()) or ()),
            effort_value=str(data.get("effort_value", "")),
            global_cache_strategy=str(data.get("global_cache_strategy", "")),
        )


@dataclass
class BreakReport:
    """Result of a single diff. Multiple fields can differ at once."""

    reasons: tuple[CacheBreakReason, ...]
    details: dict[str, tuple[str, str]]
    changed_tool: str | None = None


def hash_field(value: Any) -> str:
    """Deterministic SHA-256 hex digest of *value*.

    - ``str`` → hashed as UTF-8 bytes directly.
    - ``None`` → hashed as the literal string ``"null"``.
    - ``dict`` / ``list`` / ``tuple`` / other JSON-compatible values →
      serialized via ``json.dumps(..., sort_keys=True, ensure_ascii=False)``
      before hashing. Tuples collapse to lists under JSON, which makes
      ``hash_field((1, 2))`` equal to ``hash_field([1, 2])`` — this is
      intentional: JSON has no tuple concept and the goal is stable
      content-addressable fingerprinting, not type identity.
    """
    if value is None:
        payload = b"null"
    elif isinstance(value, str):
        payload = value.encode("utf-8")
    else:
        payload = json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return sha256(payload).hexdigest()


def hash_per_tool(tool_defs: list[dict[str, Any]]) -> dict[str, str]:
    """Per-tool hash keyed by tool name.

    Lets callers pinpoint which single tool's embed drifted when the
    aggregate ``tools_hash`` differs. A tool dict missing ``"name"``
    falls back to ``"tool_{index}"`` so the mapping stays total.
    """
    result: dict[str, str] = {}
    for idx, tool in enumerate(tool_defs):
        name_obj = tool.get("name") if isinstance(tool, dict) else None
        name = name_obj if isinstance(name_obj, str) and name_obj else f"tool_{idx}"
        result[name] = hash_field(tool)
    return result


class CacheBreakDetector:
    """Track prompt-cache fingerprints across requests and diff on demand.

    Args:
        cache_dir: Root directory for persisted state. When ``None``
            the detector runs in-memory only (useful for tests and
            library callers that already manage their own persistence).
        session_id: Session identifier used to scope persistence files.
            Ignored when ``cache_dir`` is ``None``.
    """

    def __init__(
        self,
        *,
        cache_dir: str | None = None,
        session_id: str | None = None,
    ) -> None:
        self._previous: PreviousState = PreviousState()
        self._stats: dict[str, int] = {}
        self._session_id = session_id or "default"
        self._store: StateStore | None = (
            StateStore(cache_dir) if cache_dir is not None else None
        )
        # Opportunistic load so a freshly constructed detector picks up
        # whatever the last process committed. Silent on failure — the
        # detector must never crash the caller's API path.
        if self._store is not None:
            try:
                self.load()
            except Exception:
                self._previous = PreviousState()

    # ── snapshot / diff / commit primitives ──────────────────────────

    def snapshot(
        self,
        *,
        system: str,
        tools: list[dict[str, Any]],
        betas: tuple[str, ...] = (),
        effort: str = "",
        strategy: str = "",
    ) -> PreviousState:
        """Compute a ``PreviousState`` for the current request.

        Pure — does not touch stored state. Callers decide when to
        commit by passing the result through :meth:`diff` and/or
        :meth:`commit`.
        """
        return PreviousState(
            system_hash=hash_field(system),
            tools_hash=hash_field(tools),
            per_tool_hashes=hash_per_tool(tools),
            betas=tuple(sorted(betas)),
            effort_value=effort,
            global_cache_strategy=strategy,
        )

    def diff(self, new: PreviousState) -> BreakReport:
        """Compare *new* against the stored previous state.

        Returns a :class:`BreakReport` whose ``reasons`` tuple lists
        every field that differs. ``"first_request"`` and ``"no_break"``
        are mutually exclusive with the specific-field reasons.
        """
        prev = self._previous
        if prev.is_empty():
            return BreakReport(reasons=("first_request",), details={})

        reasons: list[CacheBreakReason] = []
        details: dict[str, tuple[str, str]] = {}
        changed_tool: str | None = None

        if prev.system_hash != new.system_hash:
            reasons.append("system_changed")
            details["system_hash"] = (prev.system_hash, new.system_hash)

        if prev.tools_hash != new.tools_hash:
            reasons.append("tools_changed")
            details["tools_hash"] = (prev.tools_hash, new.tools_hash)

        per_tool_diff = self._diff_per_tool(prev.per_tool_hashes, new.per_tool_hashes)
        if per_tool_diff:
            reasons.append("per_tool_changed")
            # Mirror the TS changedToolSchemas field — comma-joined for
            # analytics, but we keep old/new hash pairs for each name.
            for tool_name, (old_h, new_h) in per_tool_diff.items():
                details[f"tool:{tool_name}"] = (old_h, new_h)
            if len(per_tool_diff) == 1:
                changed_tool = next(iter(per_tool_diff))
            else:
                changed_tool = "multiple"

        if prev.betas != new.betas:
            reasons.append("betas_changed")
            details["betas"] = (
                ",".join(prev.betas),
                ",".join(new.betas),
            )

        if prev.effort_value != new.effort_value:
            reasons.append("effort_changed")
            details["effort_value"] = (prev.effort_value, new.effort_value)

        if prev.global_cache_strategy != new.global_cache_strategy:
            reasons.append("strategy_changed")
            details["global_cache_strategy"] = (
                prev.global_cache_strategy,
                new.global_cache_strategy,
            )

        if not reasons:
            return BreakReport(reasons=("no_break",), details={})

        return BreakReport(
            reasons=tuple(reasons),
            details=details,
            changed_tool=changed_tool,
        )

    def commit(self, state: PreviousState) -> None:
        """Overwrite the stored previous state with *state*.

        Persists via :class:`StateStore` when the detector was
        constructed with a ``cache_dir``; otherwise updates in-memory
        only.
        """
        self._previous = state
        if self._store is not None:
            self.save()

    # ── convenience ──────────────────────────────────────────────────

    def detect(
        self,
        *,
        system: str,
        tools: list[dict[str, Any]],
        betas: tuple[str, ...] = (),
        effort: str = "",
        strategy: str = "",
    ) -> BreakReport:
        """Run the full snapshot → diff → commit pipeline.

        Returns the diff report before committing the new state, then
        commits so the next call compares against this one. Also bumps
        the stats counter for each reason returned.
        """
        new = self.snapshot(
            system=system,
            tools=tools,
            betas=betas,
            effort=effort,
            strategy=strategy,
        )
        report = self.diff(new)
        for reason in report.reasons:
            self._stats[reason] = self._stats.get(reason, 0) + 1
        self.commit(new)
        return report

    def stats(self) -> dict[str, int]:
        """Return a copy of break-reason counters since construction."""
        return dict(self._stats)

    # ── persistence ──────────────────────────────────────────────────

    def save(self) -> None:
        """Persist the current previous state + stats to disk.

        No-op when ``cache_dir`` was not supplied at construction.
        """
        if self._store is None:
            return
        payload = {
            "session_id": self._session_id,
            "previous": self._previous.to_dict(),
            "stats": dict(self._stats),
        }
        self._store.write_flat(_NS, _FLAT_NAME, payload)

    def load(self) -> None:
        """Reload the previous state + stats from disk.

        No-op (and stats/previous remain defaults) when ``cache_dir``
        was not supplied. Corrupt / missing files are handled by
        :class:`StateStore.read_flat` returning the default.
        """
        if self._store is None:
            return
        data = self._store.read_flat(_NS, _FLAT_NAME, default={})
        if not isinstance(data, dict):
            return
        prev_raw = data.get("previous")
        if isinstance(prev_raw, dict):
            self._previous = PreviousState.from_dict(prev_raw)
        stats_raw = data.get("stats")
        if isinstance(stats_raw, dict):
            self._stats = {str(k): int(v) for k, v in stats_raw.items()}

    # ── internals ────────────────────────────────────────────────────

    @staticmethod
    def _diff_per_tool(
        old: dict[str, str],
        new: dict[str, str],
    ) -> dict[str, tuple[str, str]]:
        """Return {name: (old_hash, new_hash)} for every tool that
        changed, was added, or was removed between *old* and *new*."""
        changed: dict[str, tuple[str, str]] = {}
        all_names = set(old) | set(new)
        for name in all_names:
            old_h = old.get(name, "")
            new_h = new.get(name, "")
            if old_h != new_h:
                changed[name] = (old_h, new_h)
        return changed
