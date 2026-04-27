"""concinno.guards.redblue_green_dispatch_guard — Red+Blue+Green review dispatch.

@module redblue_green_dispatch_guard
@responsibility Coordinate 5-axis red-team / blue-team / green-team (PM)
    Opus dispatches by blast radius (Simple / Medium / High / Chaotic),
    aggregate findings via the 5-axis weighting scheme, run the 4-step
    framing check, emit a 5-state verdict, and feed outcomes back into
    the existing ZIQ FTRL stack via ``ziq_autotune_registry``.
@dependencies
    concinno.redteam_spawn_guard (spawn ledger + cap),
    concinno.ziq_autotune_registry (FTRL arms),
    concinno.feature_config (kill switch + param defaults).
@exports Radius, Axis, Verdict, FramingError, AxisFinding, TeamReport,
    ReviewVerdict, AgentDispatcher, RedBlueGreenDispatchGuard,
    RED_PROMPT_TEMPLATE, BLUE_PROMPT_TEMPLATE, GREEN_PROMPT_TEMPLATE,
    register_ziq_arms

Design
------
This guard is the executable form of ``rules/L1/redteam.md`` (5-axis +
5-state + 4-framing + radius routing). It does not call Anthropic
itself — the caller injects an :class:`AgentDispatcher` so tests stay
offline and Sancio runtime can swap a real Opus dispatcher in.

Spawn count is gated through the existing
``concinno.redteam_spawn_guard.RedteamSpawnLedger`` so the per-event
cap stays unified across the codebase. The ``green`` role was added in
2026-04-27 alongside this guard.

ZIQ FTRL outcome semantics (see ``record_outcome``):

* ``1.0`` — next user turn did NOT correct the verdict. The chosen
  axis weights / thresholds are reinforced.
* ``0.0`` — user overruled the verdict. Arms are penalised.

Outcomes are also appended to
``~/.concinno/ziq_state/redblue_green_outcomes.jsonl`` for post-hoc
analysis.

Concurrency
-----------
Chaotic radius dispatches 3 red Opus calls in parallel via
``concurrent.futures.ThreadPoolExecutor``. The blue dispatch runs
concurrently with the reds; the green PM runs serially **after** the
red+blue results return so its prompt can quote them verbatim.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as _FutTimeout
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Literal, Optional, Protocol

# ── Enums ─────────────────────────────────────────────────────────


class Radius(str, Enum):
    """Blast radius classification (matches ``rules/L1/redteam.md``)."""

    SIMPLE = "simple"
    MEDIUM = "medium"
    HIGH = "high"
    CHAOTIC = "chaotic"


class Axis(str, Enum):
    """5-axis verdict framework (``rules/L1/redteam.md`` lines 11-23)."""

    REAL_DONE = "real_done"
    WIRED = "wired"
    FUNCTIONAL = "functional"
    AI_CAPABILITY = "ai_capability"
    UX_FRICTION = "ux_friction"


class Verdict(str, Enum):
    """5-state verdict (``rules/L1/redteam.md`` lines 105-115)."""

    ACCEPT = "accept"
    ACCEPT_DOWNGRADE = "accept_downgrade"
    REJECT = "reject"
    HOLD = "hold"
    REQUERY = "requery"


class FramingError(str, Enum):
    """4-step framing check categories (``rules/L1/redteam.md`` 88-103)."""

    SCENARIO_PREMISE = "scenario_premise"
    SCOPE_ESCALATION = "scope_escalation"
    CEILING_VS_DEFECT = "ceiling_vs_defect"
    ADVANTAGE_STIGMATIZE = "advantage_stigmatize"


# ── Records ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class AxisFinding:
    """A single 5-axis finding emitted by red or blue."""

    axis: Axis
    severity: Literal["FATAL", "HIGH", "MEDIUM", "LOW"]
    evidence: str
    framing_flag: Optional[FramingError] = None


@dataclass(frozen=True)
class TeamReport:
    """One team (red / blue / green)'s structured output."""

    role: Literal["red", "blue", "green"]
    findings: list[AxisFinding]
    summary: str
    raw_response: str


@dataclass(frozen=True)
class ReviewVerdict:
    """Commander verdict produced by :meth:`RedBlueGreenDispatchGuard.review`."""

    radius: Radius
    verdict: Verdict
    rationale: str
    findings_accepted: list[AxisFinding]
    findings_rejected_framing: list[tuple[AxisFinding, FramingError]]
    spawn_count: int
    elapsed_ms: int


# ── Dispatcher protocol ──────────────────────────────────────────


class AgentDispatcher(Protocol):
    """Caller-supplied LLM dispatch hook.

    The protocol stays minimal so tests can supply a ``Mock`` without
    importing any agent SDK. Real implementations (Sancio runtime,
    Concinno CLI) plug their Opus client behind the same signature.
    """

    def dispatch(
        self,
        prompt: str,
        *,
        model: str = "opus",
        role: str,
    ) -> str:
        ...


# ── Prompt templates ──────────────────────────────────────────────

RED_PROMPT_TEMPLATE: str = """\
You are an Opus architecture attacker + NeurIPS/ICML/OSDI top-tier reviewer
+ competitor product PM.

Your job:
1. **Strongly reject** — find 3+ reasons "this proposal should not exist".
2. **Academic attack** — top-tier reviewer level on novelty / method /
   experiment / comparison; no soft "could consider" — verdicts MUST be
   FATAL or LOW with no middle ground.
3. **Commercial attack** — does the user actually need this? what does the
   competition do? why is yours better (no hand-waving — back with metric)?
4. **Goodhart scan** — for every measure, name the gaming route.
5. **Attack design premises**: should this exist? located in the right
   place? does the user actually need it?

Hard requirements:
- Cite files / line numbers / give FATAL / HIGH / MEDIUM severities.
- No hand-waving, no soft-sell.
- Do NOT learn from the blue perspective (you only attack; defence is
  not your problem).

Five-axis sweep (≥1 attack point per axis):
- real_done: complete or written-half-and-done?
- wired: connected to ≥1 consumer? entry_points / import / route?
- functional: end-to-end test proving it runs?
- ai_capability: agent stronger / faster / more accurate, or just overhead?
- ux_friction: user simpler, or more layers / cognitive load?

⛔ DO NOT spawn further sub-agents. You are the leaf node.

Decision under review:
{decision_context}

Original intent (anchor to this; do NOT drift):
{original_intent}

Output JSON exactly:
{{
  "summary": "<2-line summary>",
  "findings": [
    {{
      "axis": "real_done|wired|functional|ai_capability|ux_friction",
      "severity": "FATAL|HIGH|MEDIUM|LOW",
      "evidence": "<concrete line/file/metric reference>"
    }}
  ]
}}
"""

BLUE_PROMPT_TEMPLATE: str = """\
You are an Opus architecture defender + technical architect + scenario judge.

⛔ Absolute prohibitions:
- ⛔ **Never volunteer concessions** — agreeing to a red attack just because
  it sounds correct = dereliction of duty.
- ⛔ **Do not let red lead by the nose** — if red's framing is wrong, attack
  the framing; do not change the proposal to match the attack.
- ⛔ **Honest** — admit weaknesses honestly, but do not let the admission be
  inflated into "should be cut".

Responsibilities:
1. **Verify wiring** — read the code, cite line numbers, prove the system
   really runs.
2. **Justify with scenarios** — 3+ real scenarios + why the alternative is
   worse.
3. **Honest weakness call-out** — say clearly: bug? defect? design choice?
4. **Counter-attack red framing errors**:
   - Wrong cost model (API cost vs CLI subscription) → call it out.
   - Goal mismatch (novelty vs shipping) → reject.
   - Ceiling limit not architecture (CC L1-L8 vs CBUA) → punt to Sancio /
     future layer.
   - Advantage stigmatised (high-frequency hooks / multi-layer / checklist)
     → counter-stigmatise.
5. **5-axis counter-evidence** — find evidence in every axis.
6. **Prepare commander evidence packet**.

⛔ DO NOT spawn further sub-agents.

Decision under review:
{decision_context}

Original intent:
{original_intent}

Red findings (for reference — do not adopt without scrutiny):
{red_findings_json}

Output JSON exactly:
{{
  "summary": "<2-line summary>",
  "findings": [
    {{
      "axis": "real_done|wired|functional|ai_capability|ux_friction",
      "severity": "FATAL|HIGH|MEDIUM|LOW",
      "evidence": "<line/file/metric>",
      "framing_flag": "scenario_premise|scope_escalation|ceiling_vs_defect|advantage_stigmatize"
    }}
  ]
}}
"""

GREEN_PROMPT_TEMPLATE: str = """\
You are the Green-team PM — a senior product manager reading the red and
blue raw outputs and emitting the commander's 5-state verdict.

You only run at Chaotic radius. You DO NOT generate new attack points.
You synthesise the existing red+blue evidence into a single verdict.

5-state options (pick exactly one):
- accept: real defect + evidence + framing correct
- accept_downgrade: real direction + framing too strong → only soften
  wording / add clarification
- reject: framing error / Goodhart self-attack / ceiling misclassified as
  defect
- hold: unverified + plausible — flag pending, ask for more evidence
- requery: red framing has ambiguity — dispatch a new red with narrowed
  scope

4-step framing check (apply to each FATAL finding):
1. Is the scenario premise correct?
2. Is the attack "should change" or "should be cut"?
3. Ceiling-limit or actual defect?
4. Is an advantage being stigmatised?

⛔ DO NOT spawn further sub-agents.

Decision under review:
{decision_context}

Original intent:
{original_intent}

Red raw response:
{red_raw}

Blue raw response:
{blue_raw}

Output JSON exactly:
{{
  "verdict": "accept|accept_downgrade|reject|hold|requery",
  "rationale": "<2-3 sentence justification>",
  "framing_errors": [
    {{
      "axis": "real_done|wired|functional|ai_capability|ux_friction",
      "framing_flag": "scenario_premise|scope_escalation|ceiling_vs_defect|advantage_stigmatize"
    }}
  ]
}}
"""


# ── ZIQ arm registration ──────────────────────────────────────────

_ZIQ_ARM_TARGETS: tuple[str, ...] = (
    "redblue_green_review.real_done_weight",
    "redblue_green_review.wired_weight",
    "redblue_green_review.functional_weight",
    "redblue_green_review.ai_capability_weight",
    "redblue_green_review.ux_friction_weight",
    "redblue_green_review.green_pm_trust",
    "redblue_green_review.fatal_threshold",
    "redblue_green_review.radius_chaotic_threshold",
)

_AXIS_WEIGHT_ARMS: tuple[float, ...] = (0.10, 0.20, 0.30, 0.40, 0.50)
_GREEN_PM_TRUST_ARMS: tuple[float, ...] = (0.40, 0.55, 0.70, 0.85, 1.00)
_FATAL_THRESHOLD_ARMS: tuple[int, ...] = (2, 3, 4, 5)
_RADIUS_CHAOTIC_THRESHOLD_ARMS: tuple[float, ...] = (0.85, 0.90, 0.93, 0.95)


def register_ziq_arms() -> list[str]:
    """Register all 8 RBG-review tunable arms with ``ziq_autotune_registry``.

    Idempotent — running twice is a no-op (the underlying ``register``
    raises ``KeyError`` on duplicates which we swallow). Returns the list
    of target ids actually present in the registry after the call.
    """
    from concinno.ziq_autotune_registry import (
        TUNABLE_REGISTRY,
        TunableSpec,
        register,
    )

    specs: list[TunableSpec] = [
        TunableSpec(
            target="redblue_green_review.real_done_weight",
            preset=0.20,
            kind="discrete",
            choices=_AXIS_WEIGHT_ARMS,
            source="concinno.guards.redblue_green_dispatch_guard",
            note="Weight of REAL_DONE axis when aggregating findings.",
        ),
        TunableSpec(
            target="redblue_green_review.wired_weight",
            preset=0.20,
            kind="discrete",
            choices=_AXIS_WEIGHT_ARMS,
            source="concinno.guards.redblue_green_dispatch_guard",
            note="Weight of WIRED axis when aggregating findings.",
        ),
        TunableSpec(
            target="redblue_green_review.functional_weight",
            preset=0.25,
            kind="discrete",
            choices=_AXIS_WEIGHT_ARMS,
            source="concinno.guards.redblue_green_dispatch_guard",
            note="Weight of FUNCTIONAL axis when aggregating findings.",
        ),
        TunableSpec(
            target="redblue_green_review.ai_capability_weight",
            preset=0.20,
            kind="discrete",
            choices=_AXIS_WEIGHT_ARMS,
            source="concinno.guards.redblue_green_dispatch_guard",
            note="Weight of AI_CAPABILITY axis when aggregating findings.",
        ),
        TunableSpec(
            target="redblue_green_review.ux_friction_weight",
            preset=0.15,
            kind="discrete",
            choices=_AXIS_WEIGHT_ARMS,
            source="concinno.guards.redblue_green_dispatch_guard",
            note="Weight of UX_FRICTION axis when aggregating findings.",
        ),
        TunableSpec(
            target="redblue_green_review.green_pm_trust",
            preset=0.70,
            kind="discrete",
            choices=_GREEN_PM_TRUST_ARMS,
            source="concinno.guards.redblue_green_dispatch_guard",
            note="Trust weight given to green PM verdict at Chaotic radius.",
        ),
        TunableSpec(
            target="redblue_green_review.fatal_threshold",
            preset=3,
            kind="discrete",
            choices=_FATAL_THRESHOLD_ARMS,
            source="concinno.guards.redblue_green_dispatch_guard",
            note="FATAL count above which verdict cannot be ACCEPT.",
        ),
        TunableSpec(
            target="redblue_green_review.radius_chaotic_threshold",
            preset=0.90,
            kind="discrete",
            choices=_RADIUS_CHAOTIC_THRESHOLD_ARMS,
            source="concinno.guards.redblue_green_dispatch_guard",
            note="Confidence-floor at which radius escalates to Chaotic.",
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


# ── Outcome persistence ───────────────────────────────────────────

_OUTCOME_FILENAME: str = "redblue_green_outcomes.jsonl"


def _resolve_outcome_path() -> Path:
    """Path for ``record_outcome`` JSONL append target."""
    base = Path.home() / ".concinno" / "ziq_state"
    base.mkdir(parents=True, exist_ok=True)
    return base / _OUTCOME_FILENAME


# ── Feature flag ─────────────────────────────────────────────────


def _feature_enabled() -> bool:
    """Honour ``feature_config.redblue_green_review.enabled`` if available."""
    try:
        from concinno.feature_config import FEATURE_META

        meta = FEATURE_META.get("redblue_green_review", {})
        return bool(meta.get("enabled", True))
    except Exception:
        return True


def _feature_param(name: str, default: Any) -> Any:
    """Read a param from the feature meta entry, falling back to ``default``."""
    try:
        from concinno.feature_config import FEATURE_META

        meta = FEATURE_META.get("redblue_green_review", {})
        params = meta.get("params", {}) or {}
        return params.get(name, default)
    except Exception:
        return default


# ── Parsing helpers ──────────────────────────────────────────────


def _parse_team_response(role: str, raw: str) -> TeamReport:
    """Parse a red / blue response into a :class:`TeamReport`.

    Tolerant of malformed JSON: returns an empty findings list with the
    raw response preserved so the commander can still quote it.
    """
    data: dict[str, Any]
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return TeamReport(
            role=role,  # type: ignore[arg-type]
            findings=[],
            summary="(unparsed)",
            raw_response=raw or "",
        )

    findings: list[AxisFinding] = []
    for entry in data.get("findings", []) or []:
        try:
            axis = Axis(str(entry["axis"]).lower())
        except (KeyError, ValueError):
            continue
        sev = str(entry.get("severity", "MEDIUM")).upper()
        if sev not in {"FATAL", "HIGH", "MEDIUM", "LOW"}:
            sev = "MEDIUM"
        flag_raw = entry.get("framing_flag")
        flag: Optional[FramingError] = None
        if flag_raw and flag_raw != "null":
            try:
                flag = FramingError(str(flag_raw).lower())
            except ValueError:
                flag = None
        findings.append(
            AxisFinding(
                axis=axis,
                severity=sev,  # type: ignore[arg-type]
                evidence=str(entry.get("evidence", "")),
                framing_flag=flag,
            ),
        )

    return TeamReport(
        role=role,  # type: ignore[arg-type]
        findings=findings,
        summary=str(data.get("summary", "")),
        raw_response=raw,
    )


def _parse_green_response(raw: str) -> tuple[Verdict, str, list[FramingError]]:
    """Parse green PM JSON into ``(verdict, rationale, framing_errors)``.

    On parse failure returns ``(HOLD, "(unparsed)", [])`` so the commander
    has a safe default rather than crashing.
    """
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return Verdict.HOLD, "(green response unparsed)", []

    try:
        verdict = Verdict(str(data.get("verdict", "hold")).lower())
    except ValueError:
        verdict = Verdict.HOLD
    rationale = str(data.get("rationale", ""))
    flags: list[FramingError] = []
    for entry in data.get("framing_errors", []) or []:
        flag_raw = entry.get("framing_flag") if isinstance(entry, dict) else None
        if not flag_raw:
            continue
        try:
            flags.append(FramingError(str(flag_raw).lower()))
        except ValueError:
            continue
    return verdict, rationale, flags


# ── Aggregation helpers ──────────────────────────────────────────


def _axis_weight(axis: Axis) -> float:
    """Return the ZIQ-suggested weight for ``axis`` (or feature default)."""
    key_map = {
        Axis.REAL_DONE: "real_done_weight",
        Axis.WIRED: "wired_weight",
        Axis.FUNCTIONAL: "functional_weight",
        Axis.AI_CAPABILITY: "ai_capability_weight",
        Axis.UX_FRICTION: "ux_friction_weight",
    }
    param = key_map[axis]
    default = _feature_param(param, 0.20)
    try:
        from concinno.ziq_autotune_registry import get_tuner

        tuner = get_tuner(f"redblue_green_review.{param}")
        return float(tuner.suggest())
    except Exception:
        return float(default)


def _split_framing(
    findings: Iterable[AxisFinding],
) -> tuple[list[AxisFinding], list[tuple[AxisFinding, FramingError]]]:
    """Partition findings into ``(accepted, rejected_with_framing_flag)``."""
    accepted: list[AxisFinding] = []
    rejected: list[tuple[AxisFinding, FramingError]] = []
    for finding in findings:
        if finding.framing_flag is not None:
            rejected.append((finding, finding.framing_flag))
        else:
            accepted.append(finding)
    return accepted, rejected


def _decide_verdict(
    accepted: list[AxisFinding],
    rejected: list[tuple[AxisFinding, FramingError]],
    *,
    fatal_threshold: int,
    green_verdict: Optional[Verdict],
    green_pm_trust: float,
) -> tuple[Verdict, str]:
    """Aggregate findings and (optionally) green PM into a final verdict."""
    fatal_count = sum(1 for f in accepted if f.severity == "FATAL")
    high_count = sum(1 for f in accepted if f.severity == "HIGH")

    # Green PM has trust weight; if Chaotic radius gave us a green verdict
    # AND green_pm_trust >= 0.70, defer to green's call unless it directly
    # contradicts a hard fatal-count rule.
    if green_verdict is not None and green_pm_trust >= 0.70:
        # Honour green's call modulo hard-fatal rule.
        if green_verdict == Verdict.ACCEPT and fatal_count >= fatal_threshold:
            return (
                Verdict.ACCEPT_DOWNGRADE,
                f"Green said ACCEPT but {fatal_count} FATAL ≥ threshold "
                f"{fatal_threshold} — downgrading.",
            )
        return green_verdict, f"Green PM verdict (trust={green_pm_trust:.2f})."

    # No green or low trust → rule-based:
    if rejected and not accepted:
        return Verdict.REJECT, (
            f"All {len(rejected)} findings flagged as framing errors."
        )
    if fatal_count >= fatal_threshold:
        return Verdict.REJECT, (
            f"{fatal_count} FATAL findings ≥ threshold {fatal_threshold}."
        )
    if fatal_count >= 1:
        return Verdict.ACCEPT_DOWNGRADE, (
            f"{fatal_count} FATAL but below threshold {fatal_threshold}."
        )
    if high_count >= 2 and not accepted[:1]:
        return Verdict.HOLD, (
            f"{high_count} HIGH findings without high-confidence verdict."
        )
    if high_count >= 1:
        return Verdict.HOLD, (
            f"{high_count} HIGH-severity finding pending verification."
        )
    if not accepted:
        return Verdict.ACCEPT, "No findings — clean."
    return Verdict.ACCEPT, f"{len(accepted)} non-FATAL findings, none blocking."


# ── Main guard ───────────────────────────────────────────────────


@dataclass
class RedBlueGreenDispatchGuard:
    """Coordinator for 5-axis Red+Blue+Green review dispatch.

    Stateless across calls (every :meth:`review` invocation builds fresh
    spawn / dispatch state). The class form is preserved so future
    instance-level config (e.g. an alternate ledger path injected by
    Sancio) is easy to add without breaking callers.
    """

    outcome_path_override: Optional[Path] = None

    def review(
        self,
        decision_context: str,
        radius: Radius,
        dispatcher: AgentDispatcher,
        *,
        original_intent: str = "",
        spawn_ledger: Optional[Any] = None,
        timeout_seconds: int = 300,
    ) -> ReviewVerdict:
        """Dispatch radius-appropriate review and return the verdict.

        Args:
            decision_context: Free-form description of what's being reviewed.
            radius: Blast radius (Simple short-circuits; Chaotic dispatches
                3R+1B+1G).
            dispatcher: Caller-supplied LLM hook implementing
                :class:`AgentDispatcher`.
            original_intent: Anchor text replayed into every prompt so red
                cannot drift.
            spawn_ledger: Optional pre-constructed
                :class:`concinno.redteam_spawn_guard.RedteamSpawnLedger`.
                When ``None`` a default-path ledger is created.
            timeout_seconds: Per-role timeout for parallel dispatch.

        Returns:
            :class:`ReviewVerdict` with verdict, accepted findings,
            framing-rejected findings, and spawn count.
        """
        start = time.monotonic()

        # Feature kill switch — short-circuit cleanly.
        if not _feature_enabled():
            elapsed = int((time.monotonic() - start) * 1000)
            return ReviewVerdict(
                radius=radius,
                verdict=Verdict.ACCEPT,
                rationale="Feature disabled via redblue_green_review.enabled=False.",
                findings_accepted=[],
                findings_rejected_framing=[],
                spawn_count=0,
                elapsed_ms=elapsed,
            )

        # Simple radius — no dispatch, ACCEPT immediately.
        if radius == Radius.SIMPLE:
            elapsed = int((time.monotonic() - start) * 1000)
            return ReviewVerdict(
                radius=radius,
                verdict=Verdict.ACCEPT,
                rationale="Simple radius — review skipped per redteam.md.",
                findings_accepted=[],
                findings_rejected_framing=[],
                spawn_count=0,
                elapsed_ms=elapsed,
            )

        # Lazy ledger init.
        ledger = spawn_ledger
        if ledger is None:
            from concinno.redteam_spawn_guard import RedteamSpawnLedger

            ledger = RedteamSpawnLedger()

        event_id = (
            f"rbg-review-{int(time.time())}-"
            f"{abs(hash(decision_context)) % 10_000:04d}"
        )

        # Per-radius dispatch plan.
        if radius == Radius.MEDIUM:
            red_count, dispatch_blue, dispatch_green = 1, False, False
        elif radius == Radius.HIGH:
            red_count, dispatch_blue, dispatch_green = 1, True, False
        else:  # CHAOTIC
            red_count, dispatch_blue, dispatch_green = 3, True, True

        # Dispatch.
        red_reports, blue_report, spawn_count = self._dispatch_red_and_blue(
            decision_context=decision_context,
            original_intent=original_intent,
            dispatcher=dispatcher,
            ledger=ledger,
            event_id=event_id,
            red_count=red_count,
            dispatch_blue=dispatch_blue,
            timeout_seconds=timeout_seconds,
        )

        # Aggregate + optional green PM + decide.
        accepted, rejected, green_verdict, green_rationale, green_spawns = (
            self._aggregate_and_green(
                red_reports=red_reports,
                blue_report=blue_report,
                dispatch_green=dispatch_green,
                event_id=event_id,
                decision_context=decision_context,
                original_intent=original_intent,
                dispatcher=dispatcher,
            )
        )
        spawn_count += green_spawns

        verdict, rationale = _decide_verdict(
            accepted=accepted,
            rejected=rejected,
            fatal_threshold=int(_feature_param("fatal_threshold", 3)),
            green_verdict=green_verdict,
            green_pm_trust=float(_feature_param("green_pm_trust", 0.70)),
        )
        if green_rationale and not rationale.startswith("Green"):
            rationale = f"{rationale} | {green_rationale}"

        elapsed = int((time.monotonic() - start) * 1000)
        return ReviewVerdict(
            radius=radius,
            verdict=verdict,
            rationale=rationale,
            findings_accepted=accepted,
            findings_rejected_framing=rejected,
            spawn_count=spawn_count,
            elapsed_ms=elapsed,
        )

    # ── aggregation + green helper ──────────────────────────────

    def _aggregate_and_green(
        self,
        *,
        red_reports: list[TeamReport],
        blue_report: Optional[TeamReport],
        dispatch_green: bool,
        event_id: str,
        decision_context: str,
        original_intent: str,
        dispatcher: AgentDispatcher,
    ) -> tuple[
        list[AxisFinding],
        list[tuple[AxisFinding, FramingError]],
        Optional[Verdict],
        str,
        int,
    ]:
        """Aggregate red+blue findings, optionally run green PM, return parts."""
        all_findings: list[AxisFinding] = []
        for report in red_reports:
            all_findings.extend(report.findings)
        if blue_report is not None:
            all_findings.extend(blue_report.findings)

        accepted, rejected = _split_framing(all_findings)

        if not dispatch_green:
            return accepted, rejected, None, "", 0

        green_verdict: Optional[Verdict] = None
        green_rationale = ""
        green_spawns = 0
        try:
            from concinno.redteam_spawn_guard import before_spawn_redteam

            before_spawn_redteam(event_id=event_id, role="greenteam")
            green_spawns = 1
            red_raw_blob = "\n---\n".join(r.raw_response for r in red_reports)
            blue_raw_blob = blue_report.raw_response if blue_report else ""
            green_prompt = GREEN_PROMPT_TEMPLATE.format(
                decision_context=decision_context,
                original_intent=original_intent,
                red_raw=red_raw_blob,
                blue_raw=blue_raw_blob,
            )
            green_raw = dispatcher.dispatch(
                green_prompt, model="opus", role="green",
            )
            green_verdict, green_rationale, green_flags = _parse_green_response(
                green_raw,
            )
            if green_flags:
                new_accepted: list[AxisFinding] = []
                for finding in accepted:
                    if finding.framing_flag is None:
                        rejected.append((finding, green_flags[0]))
                    else:
                        new_accepted.append(finding)
                accepted = new_accepted
        except Exception as exc:
            green_rationale = f"Green dispatch failed: {exc!r}"

        return accepted, rejected, green_verdict, green_rationale, green_spawns

    # ── private dispatch helper ──────────────────────────────────

    def _dispatch_red_and_blue(
        self,
        *,
        decision_context: str,
        original_intent: str,
        dispatcher: AgentDispatcher,
        ledger: Any,
        event_id: str,
        red_count: int,
        dispatch_blue: bool,
        timeout_seconds: int,
    ) -> tuple[list[TeamReport], Optional[TeamReport], int]:
        """Run red(s) + optional blue concurrently, return parsed reports."""
        from concinno.redteam_spawn_guard import (
            SpawnLimitExceeded,
            before_spawn_redteam,
        )

        red_prompt = RED_PROMPT_TEMPLATE.format(
            decision_context=decision_context,
            original_intent=original_intent,
        )
        spawn_count = 0

        # Pre-flight cap check (atomic, before any thread starts).
        try:
            for _ in range(red_count):
                before_spawn_redteam(event_id=event_id, role="redteam")
                spawn_count += 1
            if dispatch_blue:
                before_spawn_redteam(event_id=event_id, role="blueteam")
                spawn_count += 1
        except SpawnLimitExceeded:
            raise

        max_workers = max(red_count + (1 if dispatch_blue else 0), 1)

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            red_futures = [
                pool.submit(
                    dispatcher.dispatch, red_prompt, model="opus", role="red",
                )
                for _ in range(red_count)
            ]

            # Blue fires concurrently — but its prompt cites red findings,
            # so for medium/high we wait for red(s) then dispatch blue
            # afterwards. For chaotic we can still kick blue off in parallel
            # using empty red placeholders since the prompt template only
            # *references* red findings as context.
            if dispatch_blue:
                # Resolve reds first so blue prompt cites them concretely.
                red_results: list[str] = []
                for fut in red_futures:
                    try:
                        red_results.append(fut.result(timeout=timeout_seconds))
                    except _FutTimeout:
                        red_results.append('{"summary":"(timeout)","findings":[]}')
                    except Exception as exc:
                        red_results.append(
                            json.dumps(
                                {"summary": f"(error: {exc!r})", "findings": []},
                            ),
                        )
                blue_prompt = BLUE_PROMPT_TEMPLATE.format(
                    decision_context=decision_context,
                    original_intent=original_intent,
                    red_findings_json="\n---\n".join(red_results),
                )
                try:
                    blue_raw = dispatcher.dispatch(
                        blue_prompt, model="opus", role="blue",
                    )
                except Exception as exc:
                    blue_raw = json.dumps(
                        {"summary": f"(error: {exc!r})", "findings": []},
                    )
                red_reports = [_parse_team_response("red", r) for r in red_results]
                blue_report = _parse_team_response("blue", blue_raw)
                return red_reports, blue_report, spawn_count

            # No blue — just collect reds.
            red_results = []
            for fut in red_futures:
                try:
                    red_results.append(fut.result(timeout=timeout_seconds))
                except _FutTimeout:
                    red_results.append('{"summary":"(timeout)","findings":[]}')
                except Exception as exc:
                    red_results.append(
                        json.dumps(
                            {"summary": f"(error: {exc!r})", "findings": []},
                        ),
                    )
            red_reports = [_parse_team_response("red", r) for r in red_results]
            return red_reports, None, spawn_count

    # ── outcome recording ──────────────────────────────────────────

    def record_outcome(
        self,
        verdict: ReviewVerdict,
        *,
        user_overruled: bool,
    ) -> None:
        """Feed verdict outcome into ZIQ FTRL + outcomes JSONL.

        ``user_overruled=True`` → outcome ``0.0`` (penalise current arms).
        ``user_overruled=False`` → outcome ``1.0`` (reinforce).
        """
        outcome = 0.0 if user_overruled else 1.0

        # Update each axis-weight tuner.
        try:
            from concinno.ziq_autotune_registry import get_tuner

            for target in _ZIQ_ARM_TARGETS:
                try:
                    tuner = get_tuner(target)
                    tuner.record(tuner.suggest(), outcome)
                except Exception:
                    continue
        except Exception:
            pass

        # Append JSONL.
        path = self.outcome_path_override or _resolve_outcome_path()
        record = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "radius": verdict.radius.value,
            "verdict": verdict.verdict.value,
            "spawn_count": verdict.spawn_count,
            "elapsed_ms": verdict.elapsed_ms,
            "user_overruled": user_overruled,
            "outcome": outcome,
            "n_accepted": len(verdict.findings_accepted),
            "n_rejected_framing": len(verdict.findings_rejected_framing),
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            # Best-effort — outcome JSONL is audit, not critical path.
            pass


__all__ = [
    "BLUE_PROMPT_TEMPLATE",
    "GREEN_PROMPT_TEMPLATE",
    "RED_PROMPT_TEMPLATE",
    "AgentDispatcher",
    "Axis",
    "AxisFinding",
    "FramingError",
    "Radius",
    "RedBlueGreenDispatchGuard",
    "ReviewVerdict",
    "TeamReport",
    "Verdict",
    "register_ziq_arms",
]
