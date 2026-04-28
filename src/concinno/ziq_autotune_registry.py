"""concinno.ziq_autotune_registry — Declared tunable targets for ZIQ auto-tune.

@module ziq_autotune_registry
@responsibility Document which hyperparameters inside the concinno cognitive
    layer are candidates for ``ZIQAutoTuner`` control, and provide a factory
    to create a tuner instance that matches each target's kind / bounds /
    preset defaults.
@dependencies concinno.ziq_autotuner
@exports TUNABLE_REGISTRY, TunableSpec, get_tuner, list_targets

Design
------
This module is **non-intrusive by design** — importing it does not wire any
tuner into production code paths. Callers opt in per-target by:

    >>> from concinno.ziq_autotune_registry import get_tuner
    >>> tuner = get_tuner("escalation.max_retries_per_tier")
    >>> value = tuner.suggest()   # preset (1) until n >= 300 AND env var set

This keeps the library guarantee: zero behavior change for callers that do
not explicitly request auto-tuning, while letting new features (GAIA
benchmark runners, Sancio agent loops) opt in selectively.

Target identifiers use dotted paths that mirror the source module path so
``grep`` traces from a target id to its baseline call site. Example:
``escalation.max_retries_per_tier`` lives at ``concinno.escalation`` line ~160.

The registry is deliberately conservative — we only list parameters where
(a) the baseline value is well-known and stable, (b) outcome feedback is
observable at a single point in the codebase, and (c) the value type fits
``continuous`` / ``discrete`` / ``boolean``. Anything that requires context
routing (e.g. per-task budget) gets a separate tuner per context bucket
maintained by the caller.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from concinno.ziq_autotuner import TargetKind, ZIQAutoTuner


@dataclass(frozen=True)
class TunableSpec:
    """Declarative description of one tunable parameter.

    Fields mirror :class:`ZIQAutoTuner.__init__` kwargs so ``get_tuner`` can
    forward them verbatim. ``source`` is for grep-friendly audit trails.
    """

    target: str
    preset: Any
    kind: Optional[TargetKind] = None
    choices: Optional[tuple[Any, ...]] = None
    vmin: Optional[float] = None
    vmax: Optional[float] = None
    source: str = ""
    note: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def _resolve_field_read_compress_breakeven() -> "TunableSpec":
    """Build the field_read.compress_breakeven_tokens spec from FEATURE_META.

    Sub-agent K wave-2 dedup (4.4.0). The schema lives in
    ``concinno.feature_config.FEATURE_META["field_read"]["params"]
    ["compress_breakeven_tokens"]`` (recommended/min/max/default
    hardened by ``test_feature_meta_schema_v2_36``). To avoid
    drift between FEATURE_META and the ZIQ registry we resolve the
    bounds here at import time. On any failure we fall back to the
    historical hardcoded values so the registry still loads even if
    feature_config is partially broken.
    """
    fallback_preset: int = 2500
    fallback_vmin: float = 1500.0
    fallback_vmax: float = 4000.0
    try:
        from concinno.feature_config import FEATURE_META

        fr_meta = FEATURE_META.get("field_read", {})
        params = fr_meta.get("params", {}) if isinstance(fr_meta, dict) else {}
        cbt = params.get("compress_breakeven_tokens", {})
        if isinstance(cbt, dict):
            preset = int(cbt.get("default", fallback_preset))
            vmin = float(cbt.get("min", fallback_vmin))
            vmax = float(cbt.get("max", fallback_vmax))
        else:
            preset, vmin, vmax = fallback_preset, fallback_vmin, fallback_vmax
    except Exception:
        preset, vmin, vmax = fallback_preset, fallback_vmin, fallback_vmax
    return TunableSpec(
        target="field_read.compress_breakeven_tokens",
        preset=preset,
        kind="continuous",
        vmin=vmin,
        vmax=vmax,
        source=(
            "concinno.feature_config.FEATURE_META "
            "['field_read']['params']['compress_breakeven_tokens']"
        ),
        note=(
            "Token threshold above which FieldRead compression pays off "
            "(plan §63 4.4.0). Schema source-of-truth = feature_config "
            "FEATURE_META; ZIQ registry resolves at import time."
        ),
    )


# Registry of >= 10 tunable hyperparameters found via static scan of
# concinno source on 2026-04-21. Each entry links back to its declaration
# site so future auditors can verify the preset is still current.
TUNABLE_REGISTRY: dict[str, TunableSpec] = {
    # ── LLM gateway ────────────────────────────────────────
    "escalation.max_retries_per_tier": TunableSpec(
        target="escalation.max_retries_per_tier",
        preset=1,
        kind="continuous",
        vmin=0.0,
        vmax=5.0,
        source="concinno.escalation.LLMEscalator (line ~160)",
        note="Retries per tier before escalation. Higher = more $/task.",
    ),
    "escalation.circuit_threshold": TunableSpec(
        target="escalation.circuit_threshold",
        preset=5,
        kind="continuous",
        vmin=2.0,
        vmax=20.0,
        source="concinno.escalation.LLMEscalator (line ~162)",
        note="Failure count that trips the circuit breaker.",
    ),
    # ── Microcompact token budgets ────────────────────────
    "microcompact.token_budget_soft": TunableSpec(
        target="microcompact.token_budget_soft",
        preset=40_000,
        kind="continuous",
        vmin=10_000.0,
        vmax=200_000.0,
        source="concinno.cache.microcompact (line ~430)",
        note="Soft ceiling triggering compact suggestion.",
    ),
    "microcompact.token_budget_hard": TunableSpec(
        target="microcompact.token_budget_hard",
        preset=60_000,
        kind="continuous",
        vmin=20_000.0,
        vmax=400_000.0,
        source="concinno.cache.microcompact (line ~431)",
        note="Hard ceiling forcing compact.",
    ),
    # ── Knowledge / RAG cutoffs ───────────────────────────
    "knowledge.ftrl_threshold": TunableSpec(
        target="knowledge.ftrl_threshold",
        preset=5.0,
        kind="continuous",
        vmin=0.5,
        vmax=20.0,
        source="concinno.knowledge (line ~358)",
        note="FTRL rank cutoff for knowledge inject.",
    ),
    "knowledge.pattern_threshold": TunableSpec(
        target="knowledge.pattern_threshold",
        preset=3,
        kind="continuous",
        vmin=1.0,
        vmax=15.0,
        source="concinno.knowledge (line ~589)",
        note="Repetition count before pattern promotes to rule.",
    ),
    # ── Delivery gate ─────────────────────────────────────
    "delivery.gate.max_iterations": TunableSpec(
        target="delivery.gate.max_iterations",
        preset=5,
        kind="continuous",
        vmin=1.0,
        vmax=20.0,
        source="concinno.delivery.gate.DeliveryGate (line ~193)",
        note="WIREDO iteration cap before gate denies.",
    ),
    # ── FieldRead token budgets ───────────────────────────
    "field_read.handoff_budget": TunableSpec(
        target="field_read.handoff_budget",
        preset=200,
        kind="continuous",
        vmin=50.0,
        vmax=1000.0,
        source="concinno.field_read.FieldReadConfig (line ~861)",
        note="Handoff extraction token budget.",
    ),
    "field_read.memory_budget": TunableSpec(
        target="field_read.memory_budget",
        preset=150,
        kind="continuous",
        vmin=50.0,
        vmax=800.0,
        source="concinno.field_read.FieldReadConfig (line ~862)",
        note="Memory extraction token budget.",
    ),
    "field_read.compress_breakeven_tokens": _resolve_field_read_compress_breakeven(),
    # ── Few-shot retrieval ────────────────────────────────
    "fewshot.min_token_len": TunableSpec(
        target="fewshot.min_token_len",
        preset=3,
        kind="continuous",
        vmin=1.0,
        vmax=10.0,
        source="concinno.fewshot.FewshotBank (line ~108)",
        note="Minimum token length for few-shot index building.",
    ),
    # ── Parallel dispatch spawn depth ─────────────────────
    "parallel_dispatch.max_fork_depth": TunableSpec(
        target="parallel_dispatch.max_fork_depth",
        preset=5,
        kind="continuous",
        vmin=0.0,
        vmax=10.0,
        source="concinno.agent.parallel_dispatch (line ~102)",
        note="Max fork chain depth (root=0).",
    ),
    # ── GAIA meta-router arm (discrete) ───────────────────
    "gaia.meta_arm": TunableSpec(
        target="gaia.meta_arm",
        preset="SAS",
        kind="discrete",
        choices=("SAS", "MAS", "hybrid"),
        source="concinno.skills.public.agent.gaia_ziq.select_arm",
        note="Per-task arm selection when meta-router observes enough runs.",
    ),
    # ── Judge arm (discrete) ──────────────────────────────
    "judge.arm": TunableSpec(
        target="judge.arm",
        preset="haiku",
        kind="discrete",
        choices=("self", "haiku", "opus"),
        source="GAIA / Sancio judge layer (not a single line)",
        note=(
            "External judge model. Opus is strongest but $40/event; Haiku is "
            "the default baseline; self is the reward-hacking-prone option "
            "that MEMORY #27 already flags."
        ),
    ),
    # ── Boolean feature flag ──────────────────────────────
    "escalation.enable_few_shot": TunableSpec(
        target="escalation.enable_few_shot",
        preset=True,
        kind="boolean",
        source="concinno.escalation (opt-in enrichment)",
        note="Whether to inject few-shot examples into escalation prompt.",
    ),
    # ── CBUA SOTA-borrow gap-fill (2026-04-27) ────────────
    "reflexion.max_words": TunableSpec(
        target="reflexion.max_words",
        preset=80,
        kind="continuous",
        vmin=30.0,
        vmax=200.0,
        source="concinno.guards.reflexion_guard.ReflexionGuard",
        note="Word cap on the synthesised why_failed narrative.",
    ),
    "reflexion.injection_ttl_calls": TunableSpec(
        target="reflexion.injection_ttl_calls",
        preset=2,
        kind="continuous",
        vmin=1.0,
        vmax=5.0,
        source="concinno.guards.reflexion_guard.ReflexionGuard",
        note=(
            "How many subsequent PreToolUse calls replay the narrative "
            "before it expires."
        ),
    ),
    "tot.max_branches": TunableSpec(
        target="tot.max_branches",
        preset=3,
        kind="continuous",
        vmin=1.0,
        vmax=5.0,
        source="concinno.cognitive.tot_branch_explorer.plan_branches",
        note="Hard cap on Tree-of-Thought parallel branches.",
    ),
    "tot.convergence_pct": TunableSpec(
        target="tot.convergence_pct",
        preset=0.5,
        kind="continuous",
        vmin=0.3,
        vmax=0.7,
        source="concinno.cognitive.tot_branch_explorer.plan_branches",
        note=(
            "Reasoning-budget fraction above which ToT branching is "
            "force-converged."
        ),
    ),
    "action_phase.summary_interval": TunableSpec(
        target="action_phase.summary_interval",
        preset=10,
        kind="continuous",
        vmin=5.0,
        vmax=30.0,
        source="concinno.guards.action_phase_signal_guard.ActionPhaseSignalGuard",
        note=(
            "Tool-call count between OODA/PDCA/ReAct phase-distribution "
            "advisory summaries."
        ),
    ),
}


def register(spec: TunableSpec, *, overwrite: bool = False) -> None:
    """Register a new tunable target into ``TUNABLE_REGISTRY``.

    Use this for module-import-time registration of tunable arms owned
    by feature modules (e.g. ``redblue_green_dispatch_guard`` registers
    its 8 axis-weight / verdict-threshold arms on import).

    Args:
        spec: Fully-formed :class:`TunableSpec` to add.
        overwrite: If ``False`` (default) and the target already exists,
            raise :class:`KeyError`. If ``True``, replace the existing
            entry (mostly useful for tests / hot-reload).
    """
    if not overwrite and spec.target in TUNABLE_REGISTRY:
        raise KeyError(
            f"Tunable target '{spec.target}' is already registered. "
            f"Pass overwrite=True to replace.",
        )
    TUNABLE_REGISTRY[spec.target] = spec


def list_targets() -> list[str]:
    """Return registered tunable target identifiers, sorted alphabetically."""
    return sorted(TUNABLE_REGISTRY.keys())


def describe(target: str) -> TunableSpec:
    """Return the declarative spec for a target.

    Raises:
        KeyError: If ``target`` is not in the registry.
    """
    if target not in TUNABLE_REGISTRY:
        raise KeyError(
            f"Unknown tunable target '{target}'. "
            f"Known: {', '.join(list_targets())}",
        )
    return TUNABLE_REGISTRY[target]


_tuner_cache: dict[str, ZIQAutoTuner] = {}


def get_tuner(
    target: str,
    *,
    tunable_threshold: int = 300,
    full_threshold: int = 500,
    store_dir: Optional[Path] = None,
    auto_persist: bool = True,
    fresh: bool = False,
) -> ZIQAutoTuner:
    """Factory: return (and memoize) a ``ZIQAutoTuner`` for a registered target.

    Args:
        target: One of the keys in ``TUNABLE_REGISTRY``.
        tunable_threshold: Override the default 300.
        full_threshold: Override the default 500.
        store_dir: Override persistence directory (useful for tests).
        auto_persist: Whether observations are appended to disk.
        fresh: If True, bypass cache and create a new tuner instance.

    Returns:
        Cached ``ZIQAutoTuner`` instance for this target. Callers can hold the
        reference and call ``.record()`` / ``.suggest()`` across requests;
        the cache means all callers of the same target share the tuner state.
    """
    if not fresh and target in _tuner_cache:
        return _tuner_cache[target]

    spec = describe(target)
    choices: Optional[Iterable[Any]] = (
        list(spec.choices) if spec.choices else None
    )
    tuner = ZIQAutoTuner(
        target=spec.target.replace(".", "_"),
        preset=spec.preset,
        kind=spec.kind,
        choices=choices,
        vmin=spec.vmin,
        vmax=spec.vmax,
        tunable_threshold=tunable_threshold,
        full_threshold=full_threshold,
        store_dir=store_dir,
        auto_persist=auto_persist,
    )
    if not fresh:
        _tuner_cache[target] = tuner
    return tuner


def clear_cache() -> None:
    """Drop memoized tuners. Primarily for tests."""
    _tuner_cache.clear()


__all__ = [
    "TUNABLE_REGISTRY",
    "TunableSpec",
    "clear_cache",
    "describe",
    "get_tuner",
    "list_targets",
    "register",
]
