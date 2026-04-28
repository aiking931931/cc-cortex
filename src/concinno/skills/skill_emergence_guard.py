"""concinno.skills.skill_emergence_guard — Auto-propose Skills from observed patterns.

@module skills.skill_emergence_guard
@responsibility Observe PostToolUse tool-call streams, detect repetitive
    workflows / fix patterns / correction patterns, and propose Skill drafts
    to a staging directory for the user to accept or reject. Never installs
    a Skill directly — the user retains sole authority over what lands in
    ``~/.claude/skills/``.
@dependencies concinno.core.state_store, concinno.core.config,
    concinno.ziq_emit_helpers (best-effort)
@exports SkillEmergenceGuard, EmergenceSignal, SkillDraft, propose_draft

Why this exists
---------------
A user who repeats the same multi-step shape ≥ N times in one session is
telling the harness "this is worth a Skill". Existing guards only fire on
errors; this one fires on *patterns* — both happy-path repetition and
red-flag cycles (error→fix, user correction). The output is a markdown
draft on disk, not an install. ``concinno skill-emerge accept <slug>``
moves the draft into the live skill directory.

Detection model
---------------
Three orthogonal triggers run on every observation:

1. **tool_pattern_repeat** — same canonical tool-call shape ≥
   ``min_pattern_occurrences`` times within the session window.
   Canonical shape = ``(tool_name, command_head|file_extension)``;
   we deliberately strip arguments/paths/timestamps so the same
   underlying *workflow* (e.g. "ruff check then pytest then commit")
   collapses to one fingerprint regardless of which file is being
   ruffed.

2. **error_to_working** — a chain of ≥ 2 consecutive failures on the
   same fingerprint terminating in a success. The recovery itself is
   the Skill candidate — encoding the fix into a reusable workflow.

3. **user_correction** — the *previous* user prompt carries a
   syntactic correction signal (paraphrase/negation marker). The
   surrounding 3-tool window is the candidate for a "do X like
   <correction>" Skill.

These are **behavioural signals, not keyword regex**. Per
`rules/L1/multilingual_triggers.md` we never grep "之前不就" /
"actually" / "instead" — instead the on_prompt_submit hook tags the
turn with a structural metadata flag and we read that flag here. (When
the flag is unavailable, trigger #3 simply silently skips — better to
under-fire than to depend on monolingual keyword soup.)

Caps
----
* ``max_auto_skills_per_day`` — hard ceiling on draft writes per UTC day
* ``min_pattern_occurrences`` — floor for trigger #1
* ``cooldown_hours`` — same fingerprint won't re-propose for N hours
* ``draft_retention_days`` — drafts older than N days are pruned at observe()

ZIQ integration (opt-in via ``ziq_autotunable=True``)
-----------------------------------------------------
Each accept/reject by the user emits an :class:`Outcome` to the ZIQ bus
under ``skill_emergence_guard.threshold_quality``. Reward = 1.0 on
accept, 0.0 on reject. FTRL learners can converge on the right
``min_pattern_occurrences`` floor over time without the user editing
config files.

State
-----
Per-process state — pattern counters, cooldown registry, draft index,
daily counter — lives under ``Path.home() / ".concinno" / "skill_drafts"``
plus a sidecar JSON state file. We never write to ``~/.claude/skills/``
directly: that's the user's surface, and the L0 boundary rule forbids
any guard from auto-installing into it.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

__all__ = [
    "EmergenceSignal",
    "SkillDraft",
    "SkillEmergenceGuard",
    "draft_root",
    "live_skill_root",
    "propose_draft",
]


# ── Constants ─────────────────────────────────────────────

_DEFAULT_MAX_PER_DAY = 5
_DEFAULT_MIN_OCCURRENCES = 3
_DEFAULT_COOLDOWN_HOURS = 2.0
_DEFAULT_RETENTION_DAYS = 30

_HARD_CEILING_PER_DAY = 20  # Absolute max regardless of config — anti-runaway

# Always-allowed tools that never count toward pattern detection
# (read-only / meta tools; their repetition rarely indicates a Skill candidate)
_OBSERVATIONAL_ONLY_TOOLS = frozenset({
    "Read", "Glob", "TodoWrite", "WebSearch",
})

# Window for "consecutive" failure detection — beyond N events ago we
# stop chaining failures across unrelated work.
_FAIL_CHAIN_WINDOW = 8


# ── Data types ────────────────────────────────────────────


@dataclass(frozen=True)
class EmergenceSignal:
    """One observation fed into the guard.

    The guard never reads raw hook JSON — callers (the
    ``on_post_tool.py`` hook entry point and the test fixtures) build
    one of these per tool call.

    Attributes:
        tool_name: Name of the tool that just ran (e.g. ``"Bash"``).
        canonical_shape: Tool fingerprint after argument stripping.
            For Bash this is the command head + first non-flag token.
            For Edit/Write it is the file extension. The same shape
            across calls means "same workflow primitive".
        success: Whether the call succeeded. Drives the
            error-to-working trigger.
        timestamp: Unix seconds when observed (defaults to ``time.time()``).
        had_user_correction: Whether the *most recent* user prompt
            (the one that opened this turn) carried a structural
            correction marker — the on-prompt hook sets this flag.
            Defaults to False so callers without the flag silently
            skip trigger #3.
    """

    tool_name: str
    canonical_shape: str
    success: bool = True
    timestamp: float = field(default_factory=time.time)
    had_user_correction: bool = False


@dataclass
class SkillDraft:
    """A proposed Skill awaiting user acceptance.

    Persisted as ``<slug>.md`` under ``draft_root()`` plus an entry in
    the sidecar index. The markdown body uses the standard Skill
    frontmatter shape (name / description / triggers) so
    ``concinno skill-emerge accept`` can move it into
    ``~/.claude/skills/<slug>/SKILL.md`` without rewriting.
    """

    slug: str
    name: str
    description: str
    trigger_keywords: list[str]
    pattern_signature: str
    proposed_at: float
    trigger_kind: str  # "tool_pattern" | "error_to_working" | "user_correction"
    occurrences: int
    sample_canonical_shapes: list[str]

    def to_markdown(self) -> str:
        """Render Skill draft as a Claude Code skill markdown file."""
        triggers = ", ".join(self.trigger_keywords) if self.trigger_keywords else ""
        proposed_iso = datetime.fromtimestamp(
            self.proposed_at, tz=timezone.utc,
        ).isoformat()
        body = (
            "---\n"
            f"name: {self.name}\n"
            f"description: {self.description}\n"
            f"triggers: [{triggers}]\n"
            "---\n\n"
            f"# {self.name}\n\n"
            f"_Auto-proposed by SkillEmergenceGuard at {proposed_iso}._\n\n"
            f"**Trigger kind:** `{self.trigger_kind}`\n\n"
            f"**Pattern signature:** `{self.pattern_signature}`\n\n"
            f"**Observed occurrences:** {self.occurrences}\n\n"
            "## Observed shapes\n\n"
        )
        for shape in self.sample_canonical_shapes:
            body += f"- `{shape}`\n"
        body += (
            "\n## Suggested workflow\n\n"
            "_Edit this section to describe the workflow this Skill should encode "
            "before accepting._\n\n"
            "## Acceptance\n\n"
            f"- **Accept**: review and edit this draft, then move it to "
            f"`~/.claude/skills/{self.slug}/SKILL.md` to install.\n"
            f"- **Reject**: delete `{self.slug}.md` from "
            f"`~/.concinno/skill_drafts/`.\n"
            f"- Run `concinno skill-emerge accept {self.slug}` to install, "
            f"or `concinno skill-emerge reject {self.slug}` to discard. "
            f"`concinno skill-emerge list` shows pending drafts.\n"
        )
        return body


# ── Path helpers ──────────────────────────────────────────


def draft_root() -> Path:
    """Return the per-user staging directory for proposed Skill drafts.

    Override via env ``CONCINNO_SKILL_DRAFT_DIR`` for tests / sandboxed
    deployments. Default ``~/.concinno/skill_drafts``. Never returns
    a path under ``~/.claude/skills/`` — drafts are not installed
    Skills.
    """
    override = os.environ.get("CONCINNO_SKILL_DRAFT_DIR", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".concinno" / "skill_drafts"


def live_skill_root() -> Path:
    """Return the live Skill install directory used by ``skill-emerge accept``.

    Override via env ``CONCINNO_LIVE_SKILL_ROOT`` for tests / sandboxed
    deployments. Default ``~/.claude/skills`` (Claude Code's standard
    Skill discovery path). The CLI never writes here automatically —
    only on explicit ``concinno skill-emerge accept <slug>``.
    """
    override = os.environ.get("CONCINNO_LIVE_SKILL_ROOT", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".claude" / "skills"


def _state_path() -> Path:
    """Sidecar JSON tracking pattern counters / cooldown / daily count."""
    override = os.environ.get("CONCINNO_SKILL_DRAFT_STATE", "").strip()
    if override:
        return Path(override)
    return draft_root() / "_state.json"


# ── Slug + name composition ───────────────────────────────


_SAFE_SLUG_CHARS = "abcdefghijklmnopqrstuvwxyz0123456789-_"


def _slugify(text: str, *, max_len: int = 40) -> str:
    """Lowercase, ASCII-only, hyphenated slug. Empty → ``"skill"``."""
    norm = (text or "").lower().strip()
    out_chars: list[str] = []
    for ch in norm:
        if ch in _SAFE_SLUG_CHARS:
            out_chars.append(ch)
        elif ch in " /\\.:,;|()[]{}<>'\"`":
            out_chars.append("-")
    cleaned = "".join(out_chars).strip("-_")
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    if not cleaned:
        cleaned = "skill"
    return cleaned[:max_len].rstrip("-_")


def _compose_name_and_description(
    *, kind: str, signature: str, occurrences: int, shapes: list[str],
) -> tuple[str, str, list[str]]:
    """Infer human-readable name + description + trigger keywords from pattern."""
    # Name: kind-prefix + signature head
    head = signature.split("|", 1)[0].strip() or "workflow"
    # ``shapes`` is reserved for future per-sample customisation
    # (e.g. picking the most representative shape rather than the first).
    _ = shapes
    if kind == "error_to_working":
        name = f"recover-{head}"
        description = (
            f"Auto-proposed recovery workflow for repeated failure→success on "
            f"`{head}` (observed {occurrences}× this session)."
        )
        triggers = [head, "recover", "fix", "error"]
    elif kind == "user_correction":
        name = f"correct-{head}"
        description = (
            f"Auto-proposed correction workflow following user feedback on "
            f"`{head}` patterns."
        )
        triggers = [head, "correct", "redo"]
    else:  # tool_pattern_repeat
        name = f"workflow-{head}"
        description = (
            f"Auto-proposed workflow for repeated `{head}` calls "
            f"(observed {occurrences}× this session)."
        )
        triggers = [head, "workflow", "repeat"]
    # Dedup + cap triggers, drop empties
    seen: set[str] = set()
    final_triggers: list[str] = []
    for t in triggers:
        t = t.strip()
        if t and t not in seen:
            seen.add(t)
            final_triggers.append(t)
    final_triggers = final_triggers[:6]
    return name, description, final_triggers


# ── State store (file-backed) ─────────────────────────────


@dataclass
class _GuardState:
    """In-memory snapshot of state persisted to ``_state_path()``."""

    occurrence_counts: dict[str, int] = field(default_factory=dict)
    last_proposed_at: dict[str, float] = field(default_factory=dict)
    daily_count: dict[str, int] = field(default_factory=dict)  # YYYY-MM-DD → count
    recent_events: list[dict[str, Any]] = field(default_factory=list)
    drafts_index: dict[str, dict[str, Any]] = field(default_factory=dict)


def _load_state() -> _GuardState:
    p = _state_path()
    if not p.exists():
        return _GuardState()
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _GuardState()
    if not isinstance(raw, dict):
        return _GuardState()
    return _GuardState(
        occurrence_counts=dict(raw.get("occurrence_counts") or {}),
        last_proposed_at=dict(raw.get("last_proposed_at") or {}),
        daily_count=dict(raw.get("daily_count") or {}),
        recent_events=list(raw.get("recent_events") or [])[-50:],
        drafts_index=dict(raw.get("drafts_index") or {}),
    )


def _save_state(state: _GuardState) -> None:
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "occurrence_counts": state.occurrence_counts,
        "last_proposed_at": state.last_proposed_at,
        "daily_count": state.daily_count,
        "recent_events": state.recent_events[-50:],
        "drafts_index": state.drafts_index,
    }
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, p)


# ── Public propose helper ─────────────────────────────────


def propose_draft(draft: SkillDraft) -> Path:
    """Write a SkillDraft to disk and update the index.

    Used by tests and by accept/reject CLI commands. Returns the
    markdown file path (``<root>/<slug>.md``).
    """
    root = draft_root()
    root.mkdir(parents=True, exist_ok=True)
    md_path = root / f"{draft.slug}.md"
    md_path.write_text(draft.to_markdown(), encoding="utf-8")
    state = _load_state()
    state.drafts_index[draft.slug] = asdict(draft)
    _save_state(state)
    return md_path


# ── Feature config helpers ────────────────────────────────


def _read_feature_param(param: str, default: float | int) -> float | int:
    """Read a feature param via concinno.core.config when available.

    Falls back to ``default`` when the config layer is unavailable
    (e.g. early hook bootstrap). Never raises.
    """
    try:
        from concinno.core.config import get_config
    except Exception:
        return default
    try:
        cfg = get_config()
        val = cfg.feature("skill_emergence_guard", param)
    except Exception:
        return default
    if val is None:
        return default
    if isinstance(default, int) and isinstance(val, (int, float)):
        return int(val)
    if isinstance(default, float) and isinstance(val, (int, float)):
        return float(val)
    return default


def _is_enabled() -> bool:
    """Return True only when the feature is enabled in config.

    Default-OFF (4.0.0 senior-dev permissive baseline). Caller is
    responsible for wiring the config check at the hook entry point;
    this method is the runtime fallback.
    """
    try:
        from concinno.core.config import get_config
        cfg = get_config()
        return bool(cfg.feature("skill_emergence_guard", "enabled"))
    except Exception:
        return False


# ── Guard implementation ──────────────────────────────────


class SkillEmergenceGuard:
    """Observe tool-call patterns and propose Skill drafts.

    Threading: All public methods are thread-safe via a single lock.
    The guard itself is process-local; cross-process state is the
    JSON sidecar at :func:`_state_path`.

    Lifecycle: Construct one per hook process. The hook calls
    :meth:`observe` once per :class:`EmergenceSignal`. The guard
    decides whether to write a draft and inline-emits a stderr notice
    via the optional ``stderr_emit`` callable supplied by the caller.

    Acceptance / rejection: Out of band — the user runs the
    ``concinno skill-emerge accept|reject`` CLI. The guard exposes
    :meth:`record_accept` / :meth:`record_reject` so the CLI can feed
    ZIQ outcome signals back without circular imports.
    """

    name = "skill_emergence_guard"

    def __init__(
        self,
        *,
        max_auto_skills_per_day: Optional[int] = None,
        min_pattern_occurrences: Optional[int] = None,
        cooldown_hours: Optional[float] = None,
        draft_retention_days: Optional[int] = None,
        stderr_emit: Optional[Any] = None,
    ) -> None:
        self._lock = threading.RLock()
        self._stderr_emit = stderr_emit
        self._max_per_day_override = max_auto_skills_per_day
        self._min_occurrences_override = min_pattern_occurrences
        self._cooldown_override = cooldown_hours
        self._retention_override = draft_retention_days

    # ── Param resolution ──────────────────────────────────

    def _max_per_day(self) -> int:
        if self._max_per_day_override is not None:
            return min(int(self._max_per_day_override), _HARD_CEILING_PER_DAY)
        cfg_val = int(_read_feature_param(
            "max_auto_skills_per_day", _DEFAULT_MAX_PER_DAY,
        ))
        return min(cfg_val, _HARD_CEILING_PER_DAY)

    def _min_occurrences(self) -> int:
        # Floor of 1 — caller may legitimately want immediate-fire for
        # error_to_working / user_correction triggers; the daily cap
        # and cooldown already prevent runaway proposing.
        if self._min_occurrences_override is not None:
            return max(1, int(self._min_occurrences_override))
        return max(1, int(_read_feature_param(
            "min_pattern_occurrences", _DEFAULT_MIN_OCCURRENCES,
        )))

    def _cooldown_seconds(self) -> float:
        if self._cooldown_override is not None:
            return max(0.0, float(self._cooldown_override) * 3600.0)
        hours = float(_read_feature_param(
            "cooldown_hours", _DEFAULT_COOLDOWN_HOURS,
        ))
        return max(0.0, hours * 3600.0)

    def _retention_seconds(self) -> float:
        if self._retention_override is not None:
            return max(0.0, float(self._retention_override) * 86400.0)
        days = int(_read_feature_param(
            "draft_retention_days", _DEFAULT_RETENTION_DAYS,
        ))
        return max(0.0, float(days) * 86400.0)

    # ── Public API ────────────────────────────────────────

    def observe(self, signal: EmergenceSignal) -> Optional[SkillDraft]:
        """Record one observation. Return a draft if one was just proposed.

        Best-effort — never raises. The hook entry point treats the
        return value as informational only; the side effect (draft
        file written) is the real product.
        """
        if not _is_enabled():
            return None
        if signal.tool_name in _OBSERVATIONAL_ONLY_TOOLS:
            return None
        with self._lock:
            try:
                state = _load_state()
                pruned = self._prune_old_drafts(state)
                # Persist pruning before _fire_draft -> propose_draft
                # round-trips state through disk — without this, expired
                # drafts re-emerge from the disk read inside propose_draft.
                if pruned:
                    _save_state(state)
                signature = self._fingerprint(signal)
                self._record_event(state, signal, signature)
                draft = self._evaluate_triggers(state, signal, signature)
                _save_state(state)
                return draft
            except Exception:
                return None

    def record_accept(self, slug: str) -> bool:
        """Mark draft accepted by user; emit ZIQ reward=1.0. Returns success."""
        return self._record_outcome(slug, accepted=True)

    def record_reject(self, slug: str) -> bool:
        """Mark draft rejected by user; emit ZIQ reward=0.0. Returns success."""
        return self._record_outcome(slug, accepted=False)

    # ── Internals ─────────────────────────────────────────

    def _fingerprint(self, signal: EmergenceSignal) -> str:
        """Canonical pattern signature combining tool + shape."""
        shape = (signal.canonical_shape or "").strip()
        return f"{signal.tool_name}|{shape}"

    def _record_event(
        self, state: _GuardState, signal: EmergenceSignal, signature: str,
    ) -> None:
        """Append event to recent-events ring buffer + bump counter."""
        state.recent_events.append({
            "ts": float(signal.timestamp),
            "sig": signature,
            "ok": bool(signal.success),
            "correction": bool(signal.had_user_correction),
        })
        state.recent_events = state.recent_events[-50:]
        state.occurrence_counts[signature] = (
            state.occurrence_counts.get(signature, 0) + 1
        )

    def _evaluate_triggers(
        self, state: _GuardState, signal: EmergenceSignal, signature: str,
    ) -> Optional[SkillDraft]:
        """Run all three triggers; return first one that produces a draft."""
        if self._cooldown_active(state, signature):
            return None
        if self._daily_cap_reached(state):
            return None

        # Trigger 1: tool_pattern_repeat
        if state.occurrence_counts.get(signature, 0) >= self._min_occurrences():
            return self._fire_draft(
                state, signature,
                kind="tool_pattern_repeat",
                shapes=self._sample_shapes(state, signature),
            )

        # Trigger 2: error_to_working
        if signal.success and self._has_recent_failure_chain(state, signature):
            return self._fire_draft(
                state, signature,
                kind="error_to_working",
                shapes=self._sample_shapes(state, signature),
            )

        # Trigger 3: user_correction
        if signal.had_user_correction:
            return self._fire_draft(
                state, signature,
                kind="user_correction",
                shapes=self._sample_shapes(state, signature),
            )

        return None

    def _has_recent_failure_chain(
        self, state: _GuardState, signature: str,
    ) -> bool:
        """Return True if the last N events on this sig are 2+ fails ending now."""
        recent = [
            e for e in state.recent_events[-_FAIL_CHAIN_WINDOW:]
            if e.get("sig") == signature
        ]
        if len(recent) < 3:
            # Need at least: fail, fail, success (current)
            return False
        # Last event = current success; look for ≥2 consecutive failures before it
        prior = recent[:-1]
        fail_streak = 0
        for ev in reversed(prior):
            if not ev.get("ok"):
                fail_streak += 1
            else:
                break
        return fail_streak >= 2

    def _sample_shapes(
        self, state: _GuardState, signature: str, *, n: int = 5,
    ) -> list[str]:
        """Return up to n distinct canonical shapes seen for this signature."""
        seen: list[str] = []
        for ev in state.recent_events:
            if ev.get("sig") != signature:
                continue
            shape = ev.get("sig", "")
            if shape not in seen:
                seen.append(shape)
            if len(seen) >= n:
                break
        return seen or [signature]

    def _cooldown_active(self, state: _GuardState, signature: str) -> bool:
        last = state.last_proposed_at.get(signature)
        if last is None:
            return False
        return (time.time() - float(last)) < self._cooldown_seconds()

    def _daily_cap_reached(self, state: _GuardState) -> bool:
        today = self._today_key()
        return state.daily_count.get(today, 0) >= self._max_per_day()

    def _today_key(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _fire_draft(
        self,
        state: _GuardState,
        signature: str,
        *,
        kind: str,
        shapes: list[str],
    ) -> Optional[SkillDraft]:
        """Compose, persist and announce a draft. Returns the draft."""
        occurrences = state.occurrence_counts.get(signature, 1)
        name, description, triggers = _compose_name_and_description(
            kind=kind,
            signature=signature,
            occurrences=occurrences,
            shapes=shapes,
        )
        slug = self._unique_slug(state, name)
        draft = SkillDraft(
            slug=slug,
            name=name,
            description=description,
            trigger_keywords=triggers,
            pattern_signature=signature,
            proposed_at=time.time(),
            trigger_kind=kind,
            occurrences=int(occurrences),
            sample_canonical_shapes=list(shapes),
        )
        try:
            md_path = propose_draft(draft)
        except OSError:
            return None
        # Re-read state after propose_draft (it modifies disk state)
        fresh = _load_state()
        fresh.last_proposed_at[signature] = draft.proposed_at
        today = self._today_key()
        fresh.daily_count[today] = fresh.daily_count.get(today, 0) + 1
        # Carry forward the running counters from the in-memory state
        # (propose_draft only wrote drafts_index)
        fresh.occurrence_counts.update(state.occurrence_counts)
        fresh.recent_events = state.recent_events
        _save_state(fresh)
        # Mutate caller's state so the post-evaluate _save_state in observe()
        # doesn't clobber our updates
        state.last_proposed_at[signature] = draft.proposed_at
        state.daily_count[today] = fresh.daily_count[today]
        state.drafts_index = fresh.drafts_index
        self._announce(draft, md_path)
        return draft

    def _unique_slug(self, state: _GuardState, name: str) -> str:
        """Slugify name and disambiguate against existing drafts in index."""
        base = _slugify(name)
        if base not in state.drafts_index:
            return base
        # Collision: append 6-char hex
        suffix = uuid.uuid4().hex[:6]
        return f"{base}-{suffix}"

    def _announce(self, draft: SkillDraft, md_path: Path) -> None:
        """Inline stderr notice. No-op if no emit hook supplied."""
        msg = (
            f"concinno: SkillEmergenceGuard proposed draft '{draft.name}' "
            f"at {md_path}. Review and edit the draft, then run "
            f"`concinno skill-emerge accept {draft.slug}` to install at "
            f"`~/.claude/skills/{draft.slug}/SKILL.md`, or "
            f"`concinno skill-emerge reject {draft.slug}` to discard."
        )
        emitter = self._stderr_emit
        if emitter is None:
            return
        try:
            emitter(msg)
        except Exception:
            pass

    def _prune_old_drafts(self, state: _GuardState) -> bool:
        """Drop drafts older than retention window from disk + index.

        Returns True when at least one draft was pruned so the caller
        can persist the truncated index before any subsequent
        ``propose_draft`` round-trips state through disk.
        """
        retention_s = self._retention_seconds()
        if retention_s <= 0:
            return False
        cutoff = time.time() - retention_s
        to_drop: list[str] = []
        for slug, payload in state.drafts_index.items():
            proposed_at = float(payload.get("proposed_at") or 0.0)
            if proposed_at and proposed_at < cutoff:
                to_drop.append(slug)
        for slug in to_drop:
            md = draft_root() / f"{slug}.md"
            try:
                if md.exists():
                    md.unlink()
            except OSError:
                pass
            state.drafts_index.pop(slug, None)
        return bool(to_drop)

    def _record_outcome(self, slug: str, *, accepted: bool) -> bool:
        """Update index + emit ZIQ reward; return True if slug existed."""
        with self._lock:
            state = _load_state()
            if slug not in state.drafts_index:
                return False
            state.drafts_index[slug]["resolution"] = (
                "accepted" if accepted else "rejected"
            )
            state.drafts_index[slug]["resolved_at"] = time.time()
            _save_state(state)
        try:
            from concinno.ziq_emit_helpers import emit_classification_outcome

            emit_classification_outcome(
                "skill_emergence_guard.threshold_quality",
                value="propose",
                correct=accepted,
                source="concinno.skills.skill_emergence_guard",
                metadata={"slug": slug},
            )
        except Exception:
            # Best-effort — guard never breaks the CLI when ZIQ is half-wired
            pass
        return True
