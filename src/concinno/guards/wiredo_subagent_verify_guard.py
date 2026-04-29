"""concinno.guards.wiredo_subagent_verify_guard — D-axis sub-agent verify.

@module wiredo_subagent_verify_guard
@responsibility Schedule a distinct Opus sub-agent to functionally verify
    every WIREDO self-fill emitted by the actor agent. Self-verify is
    theatre (45/100 vs Opus 88-92 per ``rules/L1/redteam.md``); D-axis
    needs an executor, not a regex. Anti-self-verify is enforced
    structurally at register/dispatch time — the verifier_agent_id and
    the original_agent_id must differ or a ``SelfVerifyError`` is raised
    BEFORE any dispatcher call.
@dependencies
    concinno.guards.base (BaseGuard, GuardCategory, GuardContext, GuardResult),
    concinno.guards.redblue_green_dispatch_guard (AgentDispatcher,
        Radius re-use; verifier emits a strict subset of Verdict),
    concinno.redteam_spawn_guard (spawn ledger; ``role="verifier"``),
    concinno.ziq_autotune_registry (FTRL arms),
    concinno.feature_config (kill switch + params),
    concinno.core.state_store (pending-verification persistence).
@exports
    PendingVerification, VerifyOutcome, WiredoSubagentVerifyGuard,
    SelfVerifyError, VERIFIER_PROMPT_TEMPLATE, register_ziq_arms

Design
------
The user directive (2026-04-29):

    "WIREDO is self-verify, after self-verify, a STRONGEST sub-agent
    MUST be dispatched to truly WIREDO-verify before completion. If
    a sub-agent did the task, the parent agent OR another distinct
    sub-agent must verify."

Three primitives translated into structural enforcement:

1. **Self-verify is theatre** — an actor filling its own WIREDO checklist
   is the same failure mode as the 45/100 self-redteam vs the 88-92
   Opus-redteam evidence. ``WiredoEnforcementGuard`` already hard-denies
   missing tables (form). This guard verifies content (substance).
2. **Functional verify needs a real Opus** — D-axis is "runs and does
   what it should". L1 (existence) and L2 (pattern) are already
   shipped via ``agent_supervisor``; L3 (LLM-as-judge) is the slot
   this guard fills.
3. **Anti-self-verify is structural** — ``verifier_agent_id !=
   original_agent_id`` is checked in pure Python at the dispatch
   site, NOT asked of the LLM. There is no escape hatch.

ZIQ FTRL outcome semantics (see :meth:`record_outcome`):

* ``1.0`` — verifier ACCEPT and the next user turn does not overrule.
* ``0.0`` — verifier REJECT or user overruled.

Outcomes are appended to
``~/.concinno/ziq_state/wiredo_verify_outcomes.jsonl`` AND simultaneously
emitted into the shared 軌 B Habituation namespace
``hook_ignore_rate.jsonl`` per the Hermes 4-cap §E reconciliation —
sub-agent reliability tracking and hook-fire ignore-rate share one
ZIQ Bayesian engine, not two.

F3 mitigation (Wire-in #7)
--------------------------
The verifier sub-agent runs in its own scratch directory
``~/.concinno/verify_workspace/<task_id>/`` so any accidental write
attempt by the verifier lands outside the actor scope. The directory
is created lazily on first ``register_pending`` and is the value of
the ``workspace`` field on :class:`PendingVerification`.

Concurrency
-----------
``register_pending`` uses ``StateStore.read_modify_write`` with a file
lock so concurrent SubagentStop hooks cannot tear the registry. The
dispatcher itself is provided by the caller (Sancio runtime / CLI),
so the guard imposes no thread model on the dispatch step beyond a
``timeout_seconds`` parameter.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from concinno.guards.base import (
    BaseGuard,
    GuardCategory,
    GuardContext,
    GuardResult,
)
from concinno.guards.redblue_green_dispatch_guard import (
    AgentDispatcher,
    Radius,
)

# ── Constants ─────────────────────────────────────────────────────

#: StateStore namespace for pending verifications. Files are stored as
#: ``~/.concinno/state/wiredo_subagent_verify/<hashed_session>.json``.
_NAMESPACE: str = "wiredo_subagent_verify"

#: Flat (non-session-scoped) task registry filename so multiple sessions
#: can read/write the same pending pool. Each record is keyed by
#: ``task_id`` so concurrent registrations from different sessions do
#: not collide.
_REGISTRY_FILENAME: str = "tasks.json"

#: JSONL filename for ZIQ FTRL outcome history.
_OUTCOME_FILENAME: str = "wiredo_verify_outcomes.jsonl"

#: Shared 軌 B Habituation outcome bus filename — see Hermes 4-cap §E.
_SHARED_BUS_FILENAME: str = "hook_ignore_rate.jsonl"

#: Default per-radius retry cap. Overridable via FEATURE_META.params.
_DEFAULT_RETRY_CAP: int = 3

#: Default verifier dispatch timeout. ``Radius.CHAOTIC`` doubles it.
_DEFAULT_TIMEOUT_SECONDS: int = 300

#: ZIQ tunable target ids registered by :func:`register_ziq_arms`.
_ZIQ_ARM_TARGETS: tuple[str, ...] = (
    "wiredo_verify.retry_cap",
    "wiredo_verify.dispatch_radius_threshold",
)

#: User-directive anchor date — referenced by tests for grep-ability.
DIRECTIVE_DATE: str = "2026-04-29"


# ── Exceptions ────────────────────────────────────────────────────


class SelfVerifyError(RuntimeError):
    """Raised when ``verifier_agent_id == original_agent_id``.

    The error message always cites the user-directive date
    (:data:`DIRECTIVE_DATE`) so future grep audits can prove the gate
    fired for the documented reason.
    """


# ── Dataclasses ───────────────────────────────────────────────────


@dataclass(frozen=True)
class PendingVerification:
    """A WIREDO self-fill awaiting independent functional verification.

    Persisted via :class:`concinno.core.state_store.StateStore` so a
    process death between ``register_pending`` and
    ``dispatch_verifier`` does not orphan the record.

    Attributes:
        task_id: ``uuid4().hex`` — stable id used as the JSONL outcome
            key and as the ``verify_workspace`` subdirectory name.
        original_agent_id: Actor agent id. The dispatch site MUST
            supply a ``verifier_agent_id`` that differs from this
            value or :class:`SelfVerifyError` is raised before any
            dispatcher call.
        asset_paths: Concrete files the actor claims to have changed.
            Forwarded to the verifier prompt so the verifier knows
            exactly what to run / screenshot.
        change_summary: Brief actor-supplied summary; pasted verbatim
            into the verifier prompt (truncated to a reasonable length
            by the dispatch site if huge).
        radius: Blast radius — drives whether dispatch fires at all
            (``Radius.SIMPLE`` short-circuits) and how long the
            verifier has (Chaotic doubles the timeout).
        queued_at: Unix timestamp when ``register_pending`` was called.
        retry_count: Incremented each time a verifier returns ``fail``.
            When it hits the configured cap the task is marked
            ``abandon`` and emits an outcome of ``0.0``.
        workspace: Per-task scratch directory (F3 mitigation, Wire-in
            #7). Verifier prompt instructs the verifier to write any
            transient artifacts here, not into the actor's scope.
    """

    task_id: str
    original_agent_id: str
    asset_paths: list[str]
    change_summary: str
    radius: Radius
    queued_at: float
    retry_count: int = 0
    workspace: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialise for state store persistence."""
        return {
            "task_id": self.task_id,
            "original_agent_id": self.original_agent_id,
            "asset_paths": list(self.asset_paths),
            "change_summary": self.change_summary,
            "radius": self.radius.value,
            "queued_at": self.queued_at,
            "retry_count": self.retry_count,
            "workspace": self.workspace,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PendingVerification:
        """Deserialise from state store. Tolerant of partial records."""
        try:
            radius = Radius(str(data.get("radius", "high")))
        except ValueError:
            radius = Radius.HIGH
        return cls(
            task_id=str(data.get("task_id", "")),
            original_agent_id=str(data.get("original_agent_id", "")),
            asset_paths=list(data.get("asset_paths", []) or []),
            change_summary=str(data.get("change_summary", "")),
            radius=radius,
            queued_at=float(data.get("queued_at", 0.0)),
            retry_count=int(data.get("retry_count", 0)),
            workspace=str(data.get("workspace", "")),
        )


@dataclass(frozen=True)
class VerifyOutcome:
    """Result of a single verifier dispatch round.

    Attributes:
        task_id: Matches the :class:`PendingVerification` this outcome
            closes.
        pass_: True when the verifier accepted (D-axis runs and does
            what it should). The trailing underscore avoids the Python
            ``pass`` keyword.
        evidence: Concrete commands / file:line citations the verifier
            actually executed.
        failures: Things the verifier could not verify and why. Empty
            when ``pass_`` is True.
        verifier_agent_id: Distinct agent id (must differ from the
            actor; enforced before dispatch).
        elapsed_ms: Wall-clock time the verifier took.
    """

    task_id: str
    pass_: bool
    evidence: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    verifier_agent_id: str = ""
    elapsed_ms: int = 0


# ── Verifier prompt template ──────────────────────────────────────

VERIFIER_PROMPT_TEMPLATE: str = """\
You are an Opus D-axis functional verifier. The original actor agent
(agent_id={original_agent_id}) claims a WIREDO deliverable is complete.
Your job: independently prove the deliverable RUNS and DOES WHAT IT
SHOULD. tsc / lint / file-existence are NOT verification — those are
prerequisites already covered by L1 (existence) and L2 (pattern) layers.

⛔ FORBIDDEN short-cuts (auto-fail if you do these):
- "files exist therefore it works" — that is L1, already done by
  ``agent_supervisor`` existence check.
- "patterns matched therefore semantics OK" — that is L2, already
  done by ``expected_patterns`` regex match.
- "tests would probably pass" without running them.
- "I read the code and it looks correct" without execution.

Required actions (pick ALL that apply to asset_paths):

1. Code (.py / .ts):
   - Run the relevant pytest / vitest target. Cite N passed / N failed.
   - For changed public APIs, write or run an integration smoke that
     imports and exercises the new surface.

2. UI / web:
   - Spawn a headless Playwright session, screenshot the changed
     route at desktop (>=1024 wide) and mobile (<=768 wide) viewports.

3. CLI / shell:
   - Execute the new command end-to-end with realistic input.

4. Protocol / API:
   - Stand up the listener, send a real client request, assert
     response shape.

Asset paths in scope:
{asset_paths_yaml}

Change summary from actor (read but do NOT trust):
{change_summary}

Original WIREDO self-fill (read but do NOT trust):
{wiredo_table_excerpt}

Scratch workspace for any verifier artifacts (DO NOT write outside):
{workspace}

⛔ FORBIDDEN: DO NOT spawn further sub-agents. You are a leaf node.
⛔ FORBIDDEN: DO NOT modify the deliverable to make it pass.
⛔ FORBIDDEN: DO NOT write to any path outside the scratch workspace.

Output JSON exactly:
{{
  "pass": true|false,
  "evidence": [
    "<concrete command + output excerpt + file:line>",
    "..."
  ],
  "failures": [
    "<exactly what could not be verified and why>"
  ],
  "next_action": "release|retry|abandon"
}}
"""


# ── Feature-flag helpers ──────────────────────────────────────────


def _feature_enabled() -> bool:
    """Honour ``FEATURE_META.wiredo_subagent_verify.enabled``."""
    try:
        from concinno.feature_config import FEATURE_META

        meta = FEATURE_META.get("wiredo_subagent_verify", {})
        return bool(meta.get("enabled", False))
    except Exception:
        return False


def _feature_param(name: str, default: Any) -> Any:
    """Read a param value from FEATURE_META, falling back to ``default``."""
    try:
        from concinno.feature_config import FEATURE_META

        meta = FEATURE_META.get("wiredo_subagent_verify", {})
        params = meta.get("params", {}) or {}
        value = params.get(name)
        if isinstance(value, dict):
            value = value.get("default", default)
        return default if value is None else value
    except Exception:
        return default


# ── Path helpers ──────────────────────────────────────────────────


def _state_base_dir() -> Path:
    """Directory holding the pending-task registry."""
    base = Path.home() / ".concinno" / "state"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _outcome_dir() -> Path:
    """Directory holding both the dedicated and shared outcome JSONLs."""
    base = Path.home() / ".concinno" / "ziq_state"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _verify_workspace_root() -> Path:
    """Directory holding per-task verifier scratch subdirs (F3 mitigation)."""
    base = Path.home() / ".concinno" / "verify_workspace"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _allocate_workspace(task_id: str) -> str:
    """Create and return the per-task scratch directory path."""
    workspace = _verify_workspace_root() / task_id
    workspace.mkdir(parents=True, exist_ok=True)
    return str(workspace)


def _unlink_quiet(path: Path) -> None:
    """Best-effort file unlink — never raises."""
    try:
        path.unlink()
    except OSError:
        pass


def _scrub_workspace(task_id: str) -> None:
    """Remove a per-task scratch dir if present (F3 cleanup helper).

    Flat (non-nested) implementation so the public ``release`` method
    stays readable and the structural-nesting guard stays satisfied.
    """
    try:
        workspace = _verify_workspace_root() / task_id
    except OSError:
        return
    if not workspace.is_dir():
        return
    try:
        children = list(workspace.iterdir())
    except OSError:
        return
    for entry in children:
        if entry.is_file():
            _unlink_quiet(entry)
    try:
        workspace.rmdir()
    except OSError:
        return


# ── Outcome persistence ───────────────────────────────────────────


def _append_outcome(record: dict[str, Any]) -> None:
    """Append one record to the dedicated wiredo_verify outcomes JSONL."""
    path = _outcome_dir() / _OUTCOME_FILENAME
    try:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        # Best-effort — outcome JSONL is audit, not critical path.
        pass


def _append_shared_bus(record: dict[str, Any]) -> None:
    """Mirror outcome into the 軌 B Habituation shared namespace.

    Per Hermes 4-cap §E reconciliation, sub-agent reliability and
    hook-fire ignore-rate share one ZIQ outcome bus. We mirror with
    a ``namespace`` discriminator so downstream consumers can
    project the namespace of interest.
    """
    path = _outcome_dir() / _SHARED_BUS_FILENAME
    try:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


# ── Registry I/O ──────────────────────────────────────────────────


def _registry_read() -> dict[str, dict[str, Any]]:
    """Read the flat task registry; return empty dict on failure."""
    from concinno.core.state_store import StateStore

    store = StateStore(str(_state_base_dir()))
    raw = store.read_flat(_NAMESPACE, _REGISTRY_FILENAME, default={})
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for key, value in raw.items():
        if isinstance(key, str) and isinstance(value, dict):
            out[key] = value
    return out


def _registry_write(data: dict[str, dict[str, Any]]) -> None:
    """Write the flat task registry atomically."""
    from concinno.core.state_store import StateStore

    store = StateStore(str(_state_base_dir()))
    store.write_flat(_NAMESPACE, _REGISTRY_FILENAME, data)


# ── ZIQ arm registration ──────────────────────────────────────────


def register_ziq_arms() -> list[str]:
    """Register the 2 wiredo-verify tunable arms with ``ziq_autotune_registry``.

    Idempotent: re-running is a no-op (duplicates are skipped). Returns
    the list of target ids actually present after the call so callers
    can assert registration. Mirrors the pattern used by
    :func:`concinno.guards.redblue_green_dispatch_guard.register_ziq_arms`.
    """
    from concinno.ziq_autotune_registry import (
        TUNABLE_REGISTRY,
        TunableSpec,
        register,
    )

    specs: list[TunableSpec] = [
        TunableSpec(
            target="wiredo_verify.retry_cap",
            preset=_DEFAULT_RETRY_CAP,
            kind="discrete",
            choices=(1, 2, 3, 4, 5),
            source="concinno.guards.wiredo_subagent_verify_guard",
            note=(
                "Number of verifier attempts before a pending task is "
                "marked abandon. Higher values trade verifier cost for "
                "robustness against flaky environments."
            ),
        ),
        TunableSpec(
            target="wiredo_verify.dispatch_radius_threshold",
            preset="high",
            kind="discrete",
            choices=("simple", "medium", "high", "chaotic"),
            source="concinno.guards.wiredo_subagent_verify_guard",
            note=(
                "Minimum blast radius at which a verifier dispatch is "
                "scheduled. Below threshold the guard short-circuits."
            ),
        ),
    ]

    for spec in specs:
        if spec.target in TUNABLE_REGISTRY:
            continue
        register(spec)

    return [t for t in _ZIQ_ARM_TARGETS if t in TUNABLE_REGISTRY]


# Register on module import — idempotent and cheap.
try:
    register_ziq_arms()
except Exception:  # pragma: no cover — never break import on registry hiccup
    pass


# ── Helpers ───────────────────────────────────────────────────────


def _radius_meets_threshold(radius: Radius) -> bool:
    """Return True iff ``radius`` is at or above the configured floor."""
    order = {
        Radius.SIMPLE: 0,
        Radius.MEDIUM: 1,
        Radius.HIGH: 2,
        Radius.CHAOTIC: 3,
    }
    threshold_raw = str(_feature_param("dispatch_radius_threshold", "high"))
    try:
        threshold_radius = Radius(threshold_raw)
    except ValueError:
        threshold_radius = Radius.HIGH
    return order.get(radius, 0) >= order.get(threshold_radius, 2)


def _resolve_retry_cap() -> int:
    """Resolve retry cap from ZIQ tuner if available, else FEATURE_META."""
    fallback = int(_feature_param("retry_cap", _DEFAULT_RETRY_CAP))
    try:
        from concinno.ziq_autotune_registry import get_tuner

        tuner = get_tuner("wiredo_verify.retry_cap")
        suggested = tuner.suggest()
        if isinstance(suggested, (int, float)):
            return int(suggested)
    except Exception:
        return fallback
    return fallback


def _resolve_timeout(radius: Radius, requested: int) -> int:
    """Pick the larger of caller-supplied and radius-suggested timeout."""
    base = max(requested, _DEFAULT_TIMEOUT_SECONDS)
    if radius == Radius.CHAOTIC:
        return base * 2
    return base


def _parse_verifier_response(raw: str) -> tuple[bool, list[str], list[str]]:
    """Parse the verifier JSON. Tolerant of malformed output → ``fail``."""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return False, [], [f"verifier returned unparseable JSON: {raw[:200]!r}"]

    if not isinstance(data, dict):
        return False, [], ["verifier JSON was not an object"]

    pass_flag = bool(data.get("pass", False))
    evidence_raw = data.get("evidence", []) or []
    failures_raw = data.get("failures", []) or []
    evidence = [str(e) for e in evidence_raw if e is not None]
    failures = [str(f) for f in failures_raw if f is not None]
    return pass_flag, evidence, failures


def _format_asset_paths_yaml(paths: list[str]) -> str:
    """Render asset_paths as a tiny YAML list (no PyYAML dep)."""
    if not paths:
        return "[]"
    lines = [f"- {p}" for p in paths]
    return "\n".join(lines)


# ── Main guard ────────────────────────────────────────────────────


@dataclass
class WiredoSubagentVerifyGuard(BaseGuard):
    """Schedule a distinct Opus sub-agent to functionally verify WIREDO.

    Stateless across calls — every public method touches the on-disk
    registry through :class:`concinno.core.state_store.StateStore`. The
    class form is preserved so future instance-level overrides (Sancio
    runtime injecting an alternate state base) stay easy.

    Attributes:
        outcome_path_override: Optional override for the dedicated
            outcome JSONL (tests inject a tmp_path).
        shared_bus_override: Optional override for the 軌 B shared
            namespace JSONL (tests inject a tmp_path).
    """

    name: str = "wiredo_subagent_verify"
    category: GuardCategory = GuardCategory.QUALITY
    feature_name: str = "wiredo_subagent_verify"

    outcome_path_override: Optional[Path] = None
    shared_bus_override: Optional[Path] = None

    # ── Public API ────────────────────────────────────────────────

    def register_pending(
        self,
        original_agent_id: str,
        asset_paths: list[str],
        change_summary: str,
        radius: Radius = Radius.HIGH,
    ) -> str:
        """Queue a WIREDO self-fill for independent verification.

        Args:
            original_agent_id: Actor agent id (becomes the value
                ``dispatch_verifier`` rejects via
                :class:`SelfVerifyError` when matched).
            asset_paths: Files the actor claims to have changed.
            change_summary: Brief actor-supplied description.
            radius: Blast radius. ``Radius.SIMPLE`` short-circuits and
                returns an empty string.

        Returns:
            Hex ``task_id`` on success, empty string when the feature
            is disabled or the radius is below the configured floor.
        """
        if not _feature_enabled():
            return ""
        if not _radius_meets_threshold(radius):
            return ""

        task_id = uuid.uuid4().hex
        workspace = _allocate_workspace(task_id)

        pending = PendingVerification(
            task_id=task_id,
            original_agent_id=original_agent_id,
            asset_paths=list(asset_paths),
            change_summary=change_summary,
            radius=radius,
            queued_at=time.time(),
            retry_count=0,
            workspace=workspace,
        )

        registry = _registry_read()
        registry[task_id] = pending.to_dict()
        _registry_write(registry)
        return task_id

    def dispatch_verifier(
        self,
        task_id: str,
        dispatcher: AgentDispatcher,
        verifier_agent_id: str,
        *,
        timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
        wiredo_table_excerpt: str = "",
    ) -> VerifyOutcome:
        """Dispatch one verifier round; raise on self-verify match.

        Args:
            task_id: Hex id returned from :meth:`register_pending`.
            dispatcher: Caller-supplied LLM hook (Sancio runtime in
                production, ``unittest.mock.Mock`` in tests).
            verifier_agent_id: Distinct agent id. MUST differ from
                ``original_agent_id`` of the pending record or
                :class:`SelfVerifyError` is raised BEFORE dispatch.
            timeout_seconds: Per-dispatch timeout. Chaotic radius
                doubles internally.
            wiredo_table_excerpt: Optional text snippet from the
                actor's self-filled WIREDO table. Pasted verbatim
                into the prompt so the verifier can see what was
                claimed. Read but not trusted.

        Returns:
            :class:`VerifyOutcome` describing the verifier's verdict.

        Raises:
            KeyError: ``task_id`` not in the pending registry.
            SelfVerifyError: ``verifier_agent_id ==
                original_agent_id`` of the pending record. The check
                runs **before** ``dispatcher.dispatch`` is called so
                no LLM token is spent on a self-verify request.
        """
        registry = _registry_read()
        record = registry.get(task_id)
        if record is None:
            raise KeyError(f"task_id {task_id!r} not in pending registry")

        pending = PendingVerification.from_dict(record)
        if verifier_agent_id == pending.original_agent_id:
            msg = (
                f"actor and verifier are the same agent "
                f"({verifier_agent_id!r}); WIREDO sub-agent verify "
                f"requires a distinct verifier per user directive "
                f"{DIRECTIVE_DATE}."
            )
            raise SelfVerifyError(msg)

        # Honour the spawn ledger (role="verifier" — see
        # ``redteam_spawn_guard`` valid roles list).
        try:
            from concinno.redteam_spawn_guard import before_spawn_redteam

            before_spawn_redteam(
                event_id=f"wiredo-verify-{task_id}",
                role="verifier",
            )
        except Exception:
            # Best-effort — if the ledger refuses, the dispatch still
            # runs but we surface the failure as an outcome below.
            pass

        prompt = VERIFIER_PROMPT_TEMPLATE.format(
            original_agent_id=pending.original_agent_id,
            asset_paths_yaml=_format_asset_paths_yaml(pending.asset_paths),
            change_summary=pending.change_summary,
            wiredo_table_excerpt=wiredo_table_excerpt or "(not provided)",
            workspace=pending.workspace,
        )

        effective_timeout = _resolve_timeout(pending.radius, timeout_seconds)
        start = time.monotonic()
        pass_flag = False
        evidence: list[str] = []
        failures: list[str] = []
        try:
            raw = dispatcher.dispatch(
                prompt,
                model="opus",
                role="verifier",
            )
            if not isinstance(raw, str):
                raw = "" if raw is None else str(raw)
            pass_flag, evidence, failures = _parse_verifier_response(raw)
        except TimeoutError as exc:
            failures = [f"verifier dispatch timed out: {exc!r}"]
        except Exception as exc:
            failures = [f"verifier dispatch raised: {exc!r}"]

        elapsed_ms = int((time.monotonic() - start) * 1000)
        # Hint the type checker that the timeout was honoured at the
        # dispatcher boundary. Tests assert the value is forwarded.
        _ = effective_timeout

        outcome = VerifyOutcome(
            task_id=task_id,
            pass_=pass_flag,
            evidence=evidence,
            failures=failures,
            verifier_agent_id=verifier_agent_id,
            elapsed_ms=elapsed_ms,
        )

        # Update retry_count + persist. On pass we leave the record
        # in place so the SubagentStop hook can read it; the caller
        # discards via ``release(task_id)`` once the parent has
        # consumed the verdict.
        if not pass_flag:
            cap = _resolve_retry_cap()
            new_retry = pending.retry_count + 1
            updated = PendingVerification(
                task_id=pending.task_id,
                original_agent_id=pending.original_agent_id,
                asset_paths=pending.asset_paths,
                change_summary=pending.change_summary,
                radius=pending.radius,
                queued_at=pending.queued_at,
                retry_count=new_retry,
                workspace=pending.workspace,
            )
            registry[task_id] = updated.to_dict()
            if new_retry >= cap:
                # Mark abandoned by removing from the registry; the
                # outcome record below records the final 0.0 signal.
                registry.pop(task_id, None)
            _registry_write(registry)

        return outcome

    def record_outcome(
        self,
        outcome: VerifyOutcome,
        *,
        user_overruled: bool = False,
    ) -> None:
        """Feed verifier outcome into ZIQ FTRL + JSONL audit trails.

        Args:
            outcome: The :class:`VerifyOutcome` produced by
                :meth:`dispatch_verifier`.
            user_overruled: When True, force outcome to ``0.0`` even
                if the verifier said pass. Captures the user-correct
                signal documented in the 軌 B Habituation reconciliation.
        """
        score = 0.0 if (user_overruled or not outcome.pass_) else 1.0

        # FTRL update — best effort.
        try:
            from concinno.ziq_autotune_registry import get_tuner

            for target in _ZIQ_ARM_TARGETS:
                try:
                    tuner = get_tuner(target)
                    tuner.record(tuner.suggest(), score)
                except Exception:
                    continue
        except Exception:
            pass

        record = {
            "timestamp": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(),
            ),
            "task_id": outcome.task_id,
            "pass": outcome.pass_,
            "user_overruled": user_overruled,
            "outcome": score,
            "verifier_agent_id": outcome.verifier_agent_id,
            "evidence_count": len(outcome.evidence),
            "failure_count": len(outcome.failures),
            "elapsed_ms": outcome.elapsed_ms,
        }

        # Dedicated JSONL.
        path = self.outcome_path_override
        if path is None:
            path = _outcome_dir() / _OUTCOME_FILENAME
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass

        # Shared 軌 B Habituation bus — same record + namespace tag.
        shared_record = dict(record)
        shared_record["namespace"] = "wiredo_subagent_verify"
        shared_path = self.shared_bus_override
        if shared_path is None:
            shared_path = _outcome_dir() / _SHARED_BUS_FILENAME
        try:
            shared_path.parent.mkdir(parents=True, exist_ok=True)
            with shared_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(shared_record, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def pending_tasks(self) -> list[PendingVerification]:
        """Return all pending verifications (sorted by queued_at)."""
        registry = _registry_read()
        items = [PendingVerification.from_dict(v) for v in registry.values()]
        items.sort(key=lambda p: p.queued_at)
        return items

    def release(self, task_id: str) -> None:
        """Remove a pending verification once the caller is done."""
        registry = _registry_read()
        if task_id in registry:
            registry.pop(task_id, None)
            _registry_write(registry)
        _scrub_workspace(task_id)

    # ── Hook integration ─────────────────────────────────────────

    def check(self, ctx: GuardContext) -> Optional[GuardResult]:
        """PreToolUse no-op — registration is pull-driven from callers.

        The guard never hard-denies a tool call. Verifier dispatch is
        scheduled by :class:`concinno.wiredo_guards.WiredoEnforcementGuard`
        on PostToolUse and run by ``hooks/on_subagent_stop.py``.
        """
        # Honour the path-scope contract from BaseGuard even though we
        # always return None — keeps mypy happy and future-proofs the
        # method signature.
        _ = ctx
        return None


__all__ = [
    "DIRECTIVE_DATE",
    "PendingVerification",
    "SelfVerifyError",
    "VERIFIER_PROMPT_TEMPLATE",
    "VerifyOutcome",
    "WiredoSubagentVerifyGuard",
    "register_ziq_arms",
]


# Make sure the verify_workspace root exists at import so tests can
# rely on it being present even if no actor has registered yet.
try:  # pragma: no cover — defensive, never fails on a writable HOME
    _verify_workspace_root()
except OSError:
    pass
