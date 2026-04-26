"""concinno.time_steward — DAG-aware wall-clock scheduling for autonomous agents.

@module time_steward
@responsibility Prevent the main agent from wasting wall-clock time when
    one or more sub-agents are running in the background. Six narrow
    behaviours, all behavioural-signal driven (no LLM-as-judge call on
    the hot path, zero per-turn API cost):

      1. Pre-spawn ⬜ DAG visualiser advisory — when the agent is about
         to spawn another sub-agent, remind it to first list current ⬜
         items grouped by critical-path / parallelisable / blocked, and
         to prefer batched parallel spawn over serial.
      2. Pre-spawn contention check — if ≥1 sub-agent is already in
         flight, remind the agent to declare which files the new spawn
         will touch and to scope-narrow / defer / wait on overlap.
      3. Idle-waiting detection — if the agent's own last 3 turns
         contain "等子代理回來" / "wait for sub-agent" style phrasing
         AND ≥1 sub-agent is active, suggest spawning parallel work on
         non-overlapping ⬜ items instead of idling.
      4. Sub-agent budget tracker — when a tracked sub-agent passes
         ``estimated_minutes × 1.5`` since spawn, suggest reviewing the
         scope; past ``× 2`` issue a stronger cancel-restart hint.
      5. Re-triage on completion — when a background sub-agent reports
         completion, prompt the main agent to re-evaluate the DAG (did
         this unlock blocked items? what's now critical-path?) before
         processing the result, so the next batch of parallelisable
         work goes out immediately.
      6. Cancel + restart heuristic — stronger variant of (4) once a
         sub-agent is past ``× 2`` of its estimate.

@dependencies stdlib only (``json``, ``pathlib``, ``re``, ``time``,
    ``hashlib``); soft import of ``concinno.core.config`` and
    ``concinno.core.atomic`` for feature-flag and atomic-rename support.
@exports TimeSteward, run_time_steward, register_subagent_spawn,
    register_subagent_complete, ACTIVE_SUBAGENTS_NS

Design constraints:

- **No LLM call.** Every detector is a syntactic regex or a simple
  state lookup so the hook stays on the per-turn fast path with zero
  budget anxiety (CP-optimal per L0 #6 item 5).
- **Behavioural signal, not text-on-user.** Idle-phrase matching looks
  at the agent's own recent turns, never the user's message — we are
  detecting our own waste, not policing the user's wording (CBUA-
  optimal per L0 #6 item 6, see also feedback_cbua_markers_are_anchors
  rationale).
- **Always fail-open.** Any exception inside this module is swallowed
  so the host hook system never crashes; the worst-case behaviour is
  a missed advisory, not a broken prompt submit.
- **Cooldown.** Idle detection emits at most one advisory per three
  turns of the same session to avoid drowning the agent in repetition.
- **Concurrent-safe state.** Sub-agent registry is stored in a single
  JSON file under ``~/.concinno/state/active_subagents.json`` and read /
  written through ``concinno.core.atomic`` for the atomic-rename and
  file-lock primitives the rest of the codebase already uses.
- **Multilingual matching by meaning, not by keyword stuffing.** This
  module's canonical languages are English and Traditional Chinese
  (matching the codebase's user-facing surface); other languages are
  handled at the LLM-as-judge or upper-prompt-rewrite layer (per
  ``rules/L1/multilingual_triggers.md``).

Supersedes the placeholder ``parallel_spawn_reminder`` hook by adding
five capabilities on top of plain idle-warning.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

# ── Constants ─────────────────────────────────────────────

ACTIVE_SUBAGENTS_NS = "active_subagents"
"""Namespace token used by the sub-agent registry file."""

DEFAULT_STATE_DIR = Path.home() / ".concinno" / "state"
"""Default location for the registry JSON. Honoured when the caller
does not pass an explicit ``state_dir`` to :class:`TimeSteward`."""

REGISTRY_FILENAME = "active_subagents.json"
"""Single registry file — there is no per-session split because sub-
agents from any session can race each other for the same files."""

# Cool-down state lives next to the registry. Per-session, so a fresh
# session always starts uncooled.
COOLDOWN_FILENAME = "time_steward_cooldown.json"

# Idle-phrase match: only the agent's OWN recent turns. The patterns
# are syntactic (verb phrases) so they do not depend on the surrounding
# punctuation / register and they bias against false positives by
# requiring an explicit "wait for / 等" verb plus a sub-agent / agent /
# 子代理 / 後台 noun within a short character window.
_IDLE_PATTERNS_EN = re.compile(
    r"\b(?:wait(?:ing)?\s+for|hold\s+(?:on\s+)?for|stand(?:ing)?\s+by\s+for)\b"
    r"[^\n]{0,30}\b(?:sub-?agent|background\s+agent|delegate)\b",
    re.IGNORECASE,
)
_IDLE_PATTERNS_ZH = re.compile(
    r"(?:等(?:候|待)?|等候|等到)[^\n]{0,20}(?:子代理|sub-?agent|背景代理|"
    r"後台|背景|代理回來|代理回應|代理結果|代理完成)"
)

# Cooldown window: at most one idle injection per N turns per session.
IDLE_COOLDOWN_TURNS = 3

# Estimated-duration multipliers.
BUDGET_WARN_MULTIPLIER = 1.5
BUDGET_CANCEL_MULTIPLIER = 2.0

# Cap how many backlog hints / sub-agent ids we render in any single
# advisory so the inject stays small.
MAX_AGENTS_RENDERED = 4
MAX_BACKLOG_HINTS = 3

# Soft cap on a single advisory body in characters — keeps each
# capability's inject under roughly 80 tokens of English so the hook
# stays cheap on the receiving prompt budget.
MAX_INJECT_CHARS = 480


# ── Capability identifiers ─────────────────────────────────

CAP_DAG_VISUALIZER = "dag_visualizer"
CAP_PRE_SPAWN_CONTENTION = "pre_spawn_contention"
CAP_IDLE_DETECTION = "idle_detection"
CAP_BUDGET_TRACKER = "budget_tracker"
CAP_RETRIAGE_ON_COMPLETE = "retriage_on_complete"
CAP_CANCEL_RESTART = "cancel_restart"

ALL_CAPABILITIES: tuple[str, ...] = (
    CAP_DAG_VISUALIZER,
    CAP_PRE_SPAWN_CONTENTION,
    CAP_IDLE_DETECTION,
    CAP_BUDGET_TRACKER,
    CAP_RETRIAGE_ON_COMPLETE,
    CAP_CANCEL_RESTART,
)


# ── Sub-agent record ───────────────────────────────────────


@dataclass
class SubagentRecord:
    """A single in-flight sub-agent entry tracked by ``TimeSteward``.

    Attributes:
        id: opaque sub-agent identifier supplied by the host runtime;
            short ids are kept verbatim, longer ids are truncated for
            display.
        spawned_at: epoch seconds at spawn time.
        est_minutes: estimated wall-clock duration parsed from the
            spawn brief (``0`` if no estimate could be extracted).
        brief_summary: short human description, capped at ~80 chars,
            used only for rendering — never used as a state key.
        files_touched: optional set of file paths the spawn brief
            mentioned writing to; used by the contention check.
    """

    id: str
    spawned_at: float
    est_minutes: int = 0
    brief_summary: str = ""
    files_touched: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "spawned_at": self.spawned_at,
            "est_minutes": self.est_minutes,
            "brief_summary": self.brief_summary,
            "files_touched": list(self.files_touched),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SubagentRecord":
        return cls(
            id=str(data.get("id", "")),
            spawned_at=float(data.get("spawned_at", 0.0)),
            est_minutes=int(data.get("est_minutes", 0) or 0),
            brief_summary=str(data.get("brief_summary", ""))[:120],
            files_touched=[str(p) for p in data.get("files_touched", []) if p],
        )

    def age_minutes(self, now: float | None = None) -> float:
        if now is None:
            now = time.time()
        return max(0.0, (now - self.spawned_at) / 60.0)

    def short_id(self) -> str:
        return self.id if len(self.id) <= 12 else self.id[:10] + ".."


# ── Helpers ───────────────────────────────────────────────


def _feature_enabled(feature_name: str = "time_steward") -> bool:
    """Return ``True`` unless ``cfg.feature(feature_name, "enabled") is False``.

    Failure modes (config import error, missing feature, malformed
    cc_config.json) all default to enabled so a half-set-up workspace
    still benefits from the hook. The single way to disable it is an
    explicit ``False`` value.
    """
    try:
        from concinno.core.config import get_config

        return bool(get_config().feature(feature_name, "enabled"))
    except Exception:
        return True


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Best-effort atomic JSON write.

    Uses :func:`concinno.core.atomic.write_atomic` when available so we
    inherit the rename-then-fsync semantics the rest of the codebase
    already uses for state files. Falls back to a plain temp-file
    rename when the optional helper is missing — same crash-safety
    posture, just no fsync on platforms that need it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        from concinno.core.atomic import write_atomic

        # write_atomic accepts the raw object and json.dumps it itself.
        write_atomic(str(path), payload)
        return
    except Exception:
        pass
    # Fallback: temp + replace.
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(str(tmp), str(path))


def _safe_read_json(path: Path) -> dict[str, Any]:
    """Return ``{}`` on any read / parse error."""
    try:
        if not path.is_file():
            return {}
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            return {}
        data = json.loads(text)
        if not isinstance(data, dict):
            return {}
        return data
    except Exception:
        return {}


def _truncate(text: str, limit: int = MAX_INJECT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


# ── Public registration API ────────────────────────────────


def register_subagent_spawn(
    agent_id: str,
    *,
    brief: str = "",
    state_dir: Path | None = None,
) -> SubagentRecord:
    """Append a sub-agent to the registry on spawn.

    Idempotent: if ``agent_id`` is already present, the existing entry
    is updated rather than duplicated. Safe to call from a
    PostToolUse-on-Agent hook.

    Args:
        agent_id: opaque id supplied by the host runtime. Empty strings
            are rejected (returns a record with id="" and no side
            effect) so a malformed spawn event cannot poison the
            registry.
        brief: free-form spawn brief text — used to extract the
            estimated duration and a short human label, plus to mine a
            best-effort list of files the sub-agent will touch.
        state_dir: override the default ``~/.concinno/state`` location
            (mostly used by tests).

    Returns:
        The :class:`SubagentRecord` that ended up in the registry.
    """
    record = SubagentRecord(
        id=str(agent_id or ""),
        spawned_at=time.time(),
        est_minutes=_extract_estimated_minutes(brief),
        brief_summary=_summarise_brief(brief),
        files_touched=_extract_files_from_brief(brief),
    )
    if not record.id:
        return record
    steward = TimeSteward(state_dir=state_dir)
    steward._upsert_record(record)
    return record


def register_subagent_complete(
    agent_id: str,
    *,
    state_dir: Path | None = None,
) -> SubagentRecord | None:
    """Remove a sub-agent from the registry on completion.

    Returns the removed record (so the SubagentStop hook can render a
    completion advisory) or ``None`` when the id was not tracked.
    """
    if not agent_id:
        return None
    steward = TimeSteward(state_dir=state_dir)
    return steward._remove_record(str(agent_id))


# ── Brief parsing helpers ─────────────────────────────────


_DURATION_PATTERNS = [
    # "30-60 min" / "30-60 minutes" — pick the upper bound.
    re.compile(
        r"(\d{1,3})\s*[-~–]\s*(\d{1,3})\s*(?:min|mins|minutes|分鐘|分钟)\b",
        re.IGNORECASE,
    ),
    # "should take ~5 min" / "~10 mins" — single number after ~.
    re.compile(
        r"(?:~|about|around|大約|大概|約)\s*(\d{1,3})\s*"
        r"(?:min|mins|minutes|分鐘|分钟)\b",
        re.IGNORECASE,
    ),
    # plain "30 min" / "10 minutes".
    re.compile(
        r"(\d{1,3})\s*(?:min|mins|minutes|分鐘|分钟)\b",
        re.IGNORECASE,
    ),
    # hours: "1-2 hr" / "2 hours" — convert to minutes.
    re.compile(
        r"(\d{1,2})\s*[-~–]\s*(\d{1,2})\s*(?:hr|hrs|hour|hours|小時|小时)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"(\d{1,2})\s*(?:hr|hrs|hour|hours|小時|小时)\b",
        re.IGNORECASE,
    ),
]


def _extract_estimated_minutes(brief: str) -> int:
    """Best-effort scrape of an estimated duration from a spawn brief.

    Range estimates ("30-60 min") collapse to the upper bound on
    purpose: the budget tracker's job is to flag stalls, so a slightly
    looser ceiling has lower false-positive rate. Returns 0 when no
    estimate can be parsed — the budget tracker treats 0 as "unknown,
    do not warn", so a missing brief never produces noise.
    """
    if not brief:
        return 0
    text = brief.lower()
    for pat in _DURATION_PATTERNS[:3]:
        m = pat.search(text)
        if not m:
            continue
        if m.lastindex and m.lastindex >= 2:
            try:
                return int(m.group(2))
            except (TypeError, ValueError):
                continue
        try:
            return int(m.group(1))
        except (TypeError, ValueError):
            continue
    # Hours: convert to minutes.
    for pat in _DURATION_PATTERNS[3:]:
        m = pat.search(text)
        if not m:
            continue
        if m.lastindex and m.lastindex >= 2:
            try:
                return int(m.group(2)) * 60
            except (TypeError, ValueError):
                continue
        try:
            return int(m.group(1)) * 60
        except (TypeError, ValueError):
            continue
    return 0


def _summarise_brief(brief: str) -> str:
    """Cap the brief to a short human label."""
    if not brief:
        return ""
    first = brief.strip().splitlines()[0] if brief.strip() else ""
    if len(first) <= 80:
        return first
    return first[:78].rstrip() + "…"


_FILE_PATH_PATTERN = re.compile(
    r"`([\w./\\-]+\.(?:py|md|json|toml|yaml|yml|ts|tsx|js))`"
)


def _extract_files_from_brief(brief: str) -> list[str]:
    """Pull backtick-quoted file paths out of the spawn brief."""
    if not brief:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for m in _FILE_PATH_PATTERN.finditer(brief):
        path = m.group(1)
        if path not in seen:
            seen.add(path)
            out.append(path)
    return out


# ── Idle-phrase detection ─────────────────────────────────


def _flatten_content(content: Any) -> str:
    """Reduce a hook content payload (str | list[part]) to a flat string."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    chunks: list[str] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") != "text":
            continue
        chunk = part.get("text", "")
        if isinstance(chunk, str):
            chunks.append(chunk)
    return "\n".join(chunks)


def _last_n_agent_turns(
    transcript: Iterable[dict[str, Any]],
    n: int = 3,
) -> list[str]:
    """Pull text content from the last *n* assistant turns of the transcript.

    The transcript shape mirrors what Claude Code passes through hook
    inputs: a list of ``{"role": "assistant"|"user", "content": str |
    list[{"type": ..., "text": ...}]}`` dicts. Robust to malformed
    entries — anything that does not fit the shape is skipped silently.
    """
    out: list[str] = []
    for msg in reversed(list(transcript)):
        if not isinstance(msg, dict):
            continue
        if msg.get("role") != "assistant":
            continue
        out.append(_flatten_content(msg.get("content", "")))
        if len(out) >= n:
            break
    return out


def _detect_idle_phrase(text: str) -> bool:
    """Return ``True`` when *text* contains a wait-for-sub-agent phrase."""
    if not text:
        return False
    if _IDLE_PATTERNS_EN.search(text):
        return True
    if _IDLE_PATTERNS_ZH.search(text):
        return True
    return False


# ── TimeSteward class ─────────────────────────────────────


class TimeSteward:
    """Stateful entry point for the six time_steward capabilities.

    Holds the registry-file location and the cool-down state path.
    All public methods are best-effort: they swallow any exception and
    return ``{}`` (or ``None``) on failure rather than raising into the
    host hook system.
    """

    def __init__(
        self,
        *,
        state_dir: Path | None = None,
        feature_name: str = "time_steward",
    ) -> None:
        self.state_dir = Path(state_dir) if state_dir else DEFAULT_STATE_DIR
        self.feature_name = feature_name

    # ── Path helpers ──

    @property
    def registry_path(self) -> Path:
        return self.state_dir / REGISTRY_FILENAME

    @property
    def cooldown_path(self) -> Path:
        return self.state_dir / COOLDOWN_FILENAME

    # ── Registry I/O ──

    def _load_registry(self) -> list[SubagentRecord]:
        data = _safe_read_json(self.registry_path)
        items = data.get("agents", []) if isinstance(data, dict) else []
        out: list[SubagentRecord] = []
        if isinstance(items, list):
            for entry in items:
                if isinstance(entry, dict):
                    rec = SubagentRecord.from_dict(entry)
                    if rec.id:
                        out.append(rec)
        return out

    def _save_registry(self, records: list[SubagentRecord]) -> None:
        payload = {"agents": [r.to_dict() for r in records]}
        try:
            _atomic_write_json(self.registry_path, payload)
        except Exception:
            # Persistent registry is advisory — continue even on failure.
            pass

    def _upsert_record(self, record: SubagentRecord) -> None:
        records = self._load_registry()
        records = [r for r in records if r.id != record.id]
        records.append(record)
        self._save_registry(records)

    def _remove_record(self, agent_id: str) -> SubagentRecord | None:
        records = self._load_registry()
        before = len(records)
        kept: list[SubagentRecord] = []
        removed: SubagentRecord | None = None
        for r in records:
            if r.id == agent_id and removed is None:
                removed = r
                continue
            kept.append(r)
        if len(kept) != before:
            self._save_registry(kept)
        return removed

    # ── Active-set helpers ──

    def _active_records(self, now: float | None = None) -> list[SubagentRecord]:
        """Return all currently tracked sub-agent records.

        Decay rule: anything older than 6 hours since spawn is treated
        as completed-but-unreported and silently dropped (does not
        return). This is the safety hatch for environments where
        SubagentStop hooks do not run.
        """
        records = self._load_registry()
        cutoff = (now or time.time()) - 6 * 3600
        kept = [r for r in records if r.spawned_at >= cutoff]
        if len(kept) != len(records):
            self._save_registry(kept)
        return kept

    # ── Cool-down ──

    def _cooldown_get(self, session_id: str) -> int:
        """Return the turn index of the last idle injection for *session_id*."""
        data = _safe_read_json(self.cooldown_path)
        if not isinstance(data, dict):
            return -10**9
        entry = data.get(session_id, {})
        if not isinstance(entry, dict):
            return -10**9
        last = entry.get("last_idle_turn", -10**9)
        try:
            return int(last)
        except (TypeError, ValueError):
            return -10**9

    def _cooldown_set(self, session_id: str, turn_index: int) -> None:
        data = _safe_read_json(self.cooldown_path) or {}
        if not isinstance(data, dict):
            data = {}
        data[session_id] = {"last_idle_turn": int(turn_index)}
        try:
            _atomic_write_json(self.cooldown_path, data)
        except Exception:
            pass

    # ── Capability 1 — pre-spawn DAG visualiser ──

    def advise_pre_spawn_dag(self) -> dict[str, Any]:
        """Advisory: list ⬜ DAG and prefer batched parallel spawn."""
        if not _feature_enabled(self.feature_name):
            return {}
        text = (
            "⚠ About to spawn sub-agent. Before you do: list current "
            "⬜ DAG. Mark each ⬜ as (a) critical-path / "
            "(b) parallelisable / (c) blocked-on-other. Group "
            "parallelisable into a single batch — spawn ALL non-"
            "conflicting sub-agents in one message, not serially."
        )
        return {
            "inject": _truncate(text),
            "metadata": {"capability": CAP_DAG_VISUALIZER},
        }

    # ── Capability 2 — pre-spawn contention check ──

    def advise_pre_spawn_contention(
        self,
        new_files_touched: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        """Advisory when ≥1 sub-agent is in flight.

        When *new_files_touched* is supplied, the contention test
        compares it against the union of ``files_touched`` across all
        active records. Empty / unsupplied keeps the advisory generic
        ("state which files your new sub-agent will touch").
        """
        if not _feature_enabled(self.feature_name):
            return {}
        active = self._active_records()
        if not active:
            return {}
        new_files = {str(p) for p in (new_files_touched or []) if p}
        active_files = set()
        for r in active:
            for p in r.files_touched:
                active_files.add(str(p))
        overlap = sorted(new_files & active_files) if new_files else []
        if new_files and not overlap:
            # Caller supplied scope and there is no overlap — silent ack.
            return {
                "inject": "",
                "metadata": {
                    "capability": CAP_PRE_SPAWN_CONTENTION,
                    "overlap": [],
                    "active_count": len(active),
                },
            }
        rendered = ", ".join(
            r.short_id() + (f" [{r.brief_summary}]" if r.brief_summary else "")
            for r in active[:MAX_AGENTS_RENDERED]
        )
        if overlap:
            body = (
                f"⚠ {len(active)} background sub-agent(s) running: "
                f"{rendered}. Your new spawn overlaps these files: "
                f"{', '.join(overlap[:5])}. Scope-narrow, defer, or wait."
            )
        else:
            body = (
                f"⚠ {len(active)} background sub-agent(s) running: "
                f"{rendered}. State which files your new sub-agent will "
                "touch. If overlap with in-flight: scope-narrow, defer, "
                "or wait. If no overlap: proceed."
            )
        return {
            "inject": _truncate(body),
            "metadata": {
                "capability": CAP_PRE_SPAWN_CONTENTION,
                "overlap": overlap,
                "active_count": len(active),
            },
        }

    # ── Capability 3 — idle-waiting detection ──

    def advise_idle(
        self,
        *,
        agent_recent_turns: Iterable[str],
        session_id: str,
        turn_index: int,
        backlog_hints: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        """Detect "etc. waiting for sub-agent" phrases in the agent's own turns.

        Args:
            agent_recent_turns: text of the agent's own last 3 turns
                (most recent first). Caller-supplied so the hook can
                feed whatever transcript shape it has.
            session_id: caller's session id, used for the cool-down.
            turn_index: monotonically-increasing turn counter on the
                caller side. The cool-down compares this against the
                stored value: if ``turn_index - last_idle_turn <
                IDLE_COOLDOWN_TURNS`` we silently skip.
            backlog_hints: optional list of currently-known ⬜ items the
                agent could pursue — when supplied, included in the
                advisory body.
        """
        if not _feature_enabled(self.feature_name):
            return {}
        active = self._active_records()
        if not active:
            return {}
        if not any(_detect_idle_phrase(t) for t in agent_recent_turns):
            return {}
        last = self._cooldown_get(session_id)
        if turn_index - last < IDLE_COOLDOWN_TURNS:
            return {}
        rendered = ", ".join(r.short_id() for r in active[:MAX_AGENTS_RENDERED])
        body_parts = [
            "⚠ Idle waiting detected. Active sub-agent(s): "
            f"{rendered}.",
        ]
        hints = list(backlog_hints or [])[:MAX_BACKLOG_HINTS]
        if hints:
            body_parts.append(
                "Backlog ⬜ that don't conflict with their scope: "
                + "; ".join(hints)
                + "."
            )
        body_parts.append(
            "Spawn parallel sub-agent for non-overlapping ⬜ now "
            "(design / sediment / docs / spec). Don't idle. Per "
            "feedback_idle_waiting_is_anti_pattern.md."
        )
        self._cooldown_set(session_id, turn_index)
        return {
            "inject": _truncate(" ".join(body_parts)),
            "metadata": {
                "capability": CAP_IDLE_DETECTION,
                "active_count": len(active),
                "session_id": session_id,
            },
        }

    # ── Capability 4 / 6 — budget tracker + cancel-restart ──

    def advise_budget(
        self,
        *,
        backlog_hints: Iterable[str] | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Flag sub-agents past 1.5× / 2× their estimated runtime.

        Returns the strongest advisory only — a sub-agent past 2× is
        always reported as cancel-restart, never as the milder warn.
        Sub-agents with no estimate (``est_minutes == 0``) are skipped
        because we have no baseline to compare against.
        """
        if not _feature_enabled(self.feature_name):
            return {}
        records = self._active_records(now=now)
        if not records:
            return {}
        now = now if now is not None else time.time()
        warn: list[tuple[SubagentRecord, float]] = []
        cancel: list[tuple[SubagentRecord, float]] = []
        for r in records:
            if r.est_minutes <= 0:
                continue
            age = r.age_minutes(now=now)
            if age >= r.est_minutes * BUDGET_CANCEL_MULTIPLIER:
                cancel.append((r, age))
            elif age >= r.est_minutes * BUDGET_WARN_MULTIPLIER:
                warn.append((r, age))
        hints = list(backlog_hints or [])[:MAX_BACKLOG_HINTS]
        hint_suffix = (
            " Other ⬜ to spawn meanwhile: " + "; ".join(hints) + "."
            if hints
            else ""
        )
        if cancel:
            r, age = cancel[0]
            body = (
                f"⛔ Sub-agent {r.short_id()} running {age:.0f}min, "
                f"est {r.est_minutes}min (×2 over). High likelihood "
                "task miscoped. Recommend: (a) TaskStop sub-agent "
                "(b) revised brief addressing the likely block "
                "(c) spawn fresh." + hint_suffix
            )
            return {
                "inject": _truncate(body),
                "metadata": {
                    "capability": CAP_CANCEL_RESTART,
                    "agent_id": r.id,
                    "age_minutes": age,
                    "est_minutes": r.est_minutes,
                },
            }
        if warn:
            r, age = warn[0]
            body = (
                f"⚠ Sub-agent {r.short_id()} running {age:.0f}min, "
                f"est {r.est_minutes}min — likely stuck or task scoped "
                "wrong. Consider TaskStop + revised brief." + hint_suffix
            )
            return {
                "inject": _truncate(body),
                "metadata": {
                    "capability": CAP_BUDGET_TRACKER,
                    "agent_id": r.id,
                    "age_minutes": age,
                    "est_minutes": r.est_minutes,
                },
            }
        return {}

    # ── Capability 5 — re-triage on completion ──

    def advise_retriage(
        self,
        *,
        completed_agent_id: str,
        agent_currently_processing_result: bool = False,
    ) -> dict[str, Any]:
        """Inject re-triage advisory when a background sub-agent completes.

        Skipped when the main agent is already mid-result-processing on
        a separate turn (caller signals via
        ``agent_currently_processing_result=True``) so we do not double
        up on the natural reflection step.
        """
        if not _feature_enabled(self.feature_name):
            return {}
        if agent_currently_processing_result:
            return {}
        if not completed_agent_id:
            return {}
        body = (
            f"✅ Sub-agent {completed_agent_id[:12]} completed. Re-"
            "evaluate ⬜ DAG: did this unlock any blocked ⬜? "
            "What's now critical-path? Spawn next batch of parallel-able "
            "⬜ before processing this result."
        )
        return {
            "inject": _truncate(body),
            "metadata": {
                "capability": CAP_RETRIAGE_ON_COMPLETE,
                "agent_id": completed_agent_id,
            },
        }


# ── Top-level orchestrator (UserPromptSubmit hot path) ─────


def run_time_steward(
    *,
    agent_recent_turns: Iterable[str] | None = None,
    session_id: str = "",
    turn_index: int = 0,
    backlog_hints: Iterable[str] | None = None,
    state_dir: Path | None = None,
) -> dict[str, Any]:
    """Run the per-turn slice of capabilities (3 + 4 + 6).

    The pre-spawn capabilities (1, 2) and the on-completion capability
    (5) are dispatched from their own hook events (PreToolUse on Agent,
    SubagentStop) — calling them at UserPromptSubmit time would either
    miss the event or fire too late. This top-level helper intentionally
    runs only the trio that makes sense on the prompt-submit hot path.

    Returns a dict shaped ``{"inject": str, "metadata": {...}}``
    matching the in-tree ``prompt_hooks`` convention. Returns ``{}``
    when nothing fires.
    """
    if not _feature_enabled():
        return {}
    steward = TimeSteward(state_dir=state_dir)
    # Cancel-restart wins over plain budget-warn (advise_budget already
    # picks the strongest internally).
    budget = steward.advise_budget(backlog_hints=backlog_hints)
    if budget.get("inject"):
        return budget
    if agent_recent_turns is not None:
        idle = steward.advise_idle(
            agent_recent_turns=agent_recent_turns,
            session_id=session_id,
            turn_index=turn_index,
            backlog_hints=backlog_hints,
        )
        if idle.get("inject"):
            return idle
    return {}


__all__ = [
    "ACTIVE_SUBAGENTS_NS",
    "ALL_CAPABILITIES",
    "CAP_BUDGET_TRACKER",
    "CAP_CANCEL_RESTART",
    "CAP_DAG_VISUALIZER",
    "CAP_IDLE_DETECTION",
    "CAP_PRE_SPAWN_CONTENTION",
    "CAP_RETRIAGE_ON_COMPLETE",
    "SubagentRecord",
    "TimeSteward",
    "register_subagent_complete",
    "register_subagent_spawn",
    "run_time_steward",
]
