# SPDX-FileCopyrightText: 2025 Nous Research
# SPDX-License-Identifier: MIT

"""SPS x FTRL posterior approval.

Ported from concinno.approval_mode at commit
20bab6c8cb35006453bcb662afb4844831ea6427.

Patent-verified novel per MEMORY #4l (ZIQ posterior P proportional to
SPS x FTRL, ICML 2026 GRPO is closest prior, no prior factor).

Lyceum-idiomatic surface
------------------------
The upstream Concinno module has 614 LoC across mode-resolution,
config persistence, env var handling, CLI helpers, and the SPS x
FTRL kernel. This port extracts the **scoring kernel** so Lyceum's
existing ``approvals.mode=smart`` LLM risk assessor can consult the
posterior as one signal among many.

The math
--------
For an action with structural prior ``sps`` (a function of
``blast_radius``: low=0.10, medium=0.50, high=0.90) and an FTRL
Beta(alpha, beta) state for the per-feature bucket::

    ftrl_proceed_prob = alpha / (alpha + beta)
    posterior_proceed = (1.0 - sps) * ftrl_proceed_prob
    should_ask         = posterior_proceed < threshold   # default 0.50

Cold-start (Jeffreys 1/1) override
----------------------------------
A pure ``Beta(1, 1)`` prior gives ``ftrl_proceed_prob = 0.5`` which
collapses to a coin-flip on the very first call. For high-blast
operations a 50/50 routing toss is unsafe — so we force
``should_ask=True`` whenever the bucket has zero observations. The
operator's first answer trains the FTRL, and subsequent calls follow
the regular posterior-vs-threshold rule.

Outcome ledger
--------------
:func:`record_outcome` calls :mod:`lyceum.governance.outcome_store` to
append a JSONL record at ``~/.lyceum/governance/ziq_outcomes.jsonl``
(NOT ``~/.concinno/`` — Lyceum owns its own state dir). The store is
a separate module so other Lyceum subsystems (curator timing /
char_cap / cache_ttl) can append outcomes without depending on the
approval kernel.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from concinno._lyceum_vendor.governance.outcome_store import record_outcome as _store_outcome

__all__ = [
    "BLAST_RADIUS_LOW",
    "BLAST_RADIUS_MEDIUM",
    "BLAST_RADIUS_HIGH",
    "ApprovalMode",
    "ApprovalState",
    "ApprovalConfig",
    "ApprovalDecision",
    "compute_sps_score",
    "smart_decision",
    "decide",
    "decide_with_config",
    "record_outcome",
    "record_outcome_with_config",
    "load_config",
    "save_config",
    "current_mode",
    "describe_current_config",
    "DEFAULT_THRESHOLD",
    "DEFAULT_CONFIG_PATH",
    "MODE_ENV_VAR",
]


# ─── Constants ────────────────────────────────────────────────────


BLAST_RADIUS_LOW = "low"
BLAST_RADIUS_MEDIUM = "medium"
BLAST_RADIUS_HIGH = "high"

_VALID_RADII = frozenset(
    {BLAST_RADIUS_LOW, BLAST_RADIUS_MEDIUM, BLAST_RADIUS_HIGH}
)

_SPS_BY_RADIUS: dict[str, float] = {
    BLAST_RADIUS_LOW: 0.10,
    BLAST_RADIUS_MEDIUM: 0.50,
    BLAST_RADIUS_HIGH: 0.90,
}

DEFAULT_THRESHOLD = 0.50
_THRESHOLD_ENV = "LYCEUM_APPROVAL_THRESHOLD"

_FEATURE_NAME = "lyceum.governance.smart_approval"

# Path-aware config helpers (Wave 2.7-H K7 port).
# DEFAULT_CONFIG_PATH targets ~/.lyceum/. Concinno-side shim binds the
# same helpers to ~/.concinno/approval_mode.json by passing the
# ``path=`` arg, so two parallel persistence stores coexist without
# merge.
DEFAULT_CONFIG_PATH = Path.home() / ".lyceum" / "approval_mode.json"
MODE_ENV_VAR = "LYCEUM_APPROVAL_MODE"


# ─── Enum ─────────────────────────────────────────────────────────


class ApprovalMode(str, Enum):
    """Three operator-selectable approval routing modes."""

    MANUAL = "manual"
    SMART = "smart"
    OFF = "off"

    @classmethod
    def from_raw(cls, raw: object) -> "ApprovalMode":
        """Parse a loose user-supplied value; unknown -> SMART."""
        if isinstance(raw, str):
            normalized = raw.strip().lower()
            for mode in cls:
                if mode.value == normalized:
                    return mode
        return cls.SMART


# ─── State + decision dataclasses ─────────────────────────────────


@dataclass(frozen=True)
class ApprovalState:
    """Beta(alpha, beta) posterior over operator-said-proceed.

    Attributes:
        alpha: Successes (operator clicked "proceed").
        beta:  Failures (operator clicked "ask" / cancelled).
    """

    alpha: float = 1.0
    beta: float = 1.0

    def proceed_probability(self) -> float:
        """Posterior mean of the Beta(alpha, beta) distribution."""
        denom = self.alpha + self.beta
        if denom <= 0:
            return 0.5
        return self.alpha / denom

    @property
    def is_jeffreys(self) -> bool:
        """True iff this is the un-trained Beta(1, 1) prior."""
        return self.alpha == 1.0 and self.beta == 1.0


@dataclass(frozen=True)
class ApprovalConfig:
    """Resolved approval-mode snapshot.

    Wave 2.7-H K7 port — Lyceum-side counterpart to
    ``concinno.approval_mode.ApprovalConfig``. Persisted at
    ``~/.lyceum/approval_mode.json``. The Concinno shim
    binds the same shape to ``~/.concinno/approval_mode.json``
    (separate operator state, no migration).
    """

    mode: ApprovalMode = ApprovalMode.SMART
    ftrl: dict[str, ApprovalState] = field(default_factory=dict)
    source: str = "default"
    warnings: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ApprovalDecision:
    """Per-call routing answer."""

    should_ask: bool
    mode: ApprovalMode
    sps: float = 0.0
    ftrl_proceed_prob: float = 0.0
    threshold: float = DEFAULT_THRESHOLD
    reason: str = ""
    bucket: str = ""


# ─── SPS / threshold ──────────────────────────────────────────────


def compute_sps_score(blast_radius: str) -> float:
    """Return the SPS structural-prior weight for a blast radius.

    Args:
        blast_radius: One of low / medium / high (case-insensitive).
            Unknown values fall back to medium (the safest middle).

    Returns:
        Float in [0.0, 1.0]. Higher = "intuitively asks more".
    """
    key = (blast_radius or "").strip().lower()
    if key not in _VALID_RADII:
        key = BLAST_RADIUS_MEDIUM
    return _SPS_BY_RADIUS[key]


def _resolve_threshold(override: Optional[float] = None) -> float:
    """Caller arg > env var > DEFAULT_THRESHOLD."""
    if override is not None:
        return min(1.0, max(0.0, float(override)))
    raw = os.environ.get(_THRESHOLD_ENV, "").strip()
    if raw:
        try:
            value = float(raw)
            return min(1.0, max(0.0, value))
        except ValueError:
            pass
    return DEFAULT_THRESHOLD


def _bucket_key(blast_radius: str, tunable: Optional[str]) -> str:
    """Compose the FTRL state-store bucket key.

    Per-tunable buckets win over per-radius so operators can express
    "ask me about cache rebuilds, but never about branch deletions".
    """
    if tunable:
        return f"tunable:{tunable}"
    radius = (blast_radius or "").strip().lower()
    if radius not in _VALID_RADII:
        radius = BLAST_RADIUS_MEDIUM
    return f"blast:{radius}"


# ─── SPS x FTRL kernel ────────────────────────────────────────────


def smart_decision(
    blast_radius: str,
    *,
    state: Optional[ApprovalState] = None,
    tunable: Optional[str] = None,
    threshold: Optional[float] = None,
) -> ApprovalDecision:
    """SPS x FTRL posterior smart routing.

    Decision rule::

        posterior_proceed = (1 - sps) * ftrl_proceed_prob
        should_ask        = posterior_proceed < threshold

    Cold-start safety override: when ``state`` is None or its
    ``is_jeffreys`` (Beta(1, 1) un-trained), force ``should_ask=True``
    so the operator's first answer trains the FTRL.

    Args:
        blast_radius: ``low | medium | high``.
        state: Current FTRL Beta(alpha, beta) posterior. None = cold.
        tunable: Optional per-feature key for bucket naming.
        threshold: Override the posterior cutoff. None = env / default.
    """
    sps = compute_sps_score(blast_radius)
    bucket = _bucket_key(blast_radius, tunable)
    resolved_threshold = _resolve_threshold(threshold)

    if state is None or state.is_jeffreys:
        # Cold-start: pure Beta(1, 1) collapses to 50% which is a
        # coin-flip on whether to ask — unsafe for high-blast knobs.
        # Force ask so the operator's first answer trains the FTRL.
        cold_state = state or ApprovalState()
        ftrl_p = cold_state.proceed_probability()
        reason = (
            f"smart cold-start: no FTRL history for bucket={bucket!r}, "
            f"defaulting to ask "
            f"(sps={sps:.2f} ftrl_proceed_prob={ftrl_p:.3f} "
            f"threshold={resolved_threshold:.3f})"
        )
        return ApprovalDecision(
            should_ask=True,
            mode=ApprovalMode.SMART,
            sps=sps,
            ftrl_proceed_prob=ftrl_p,
            threshold=resolved_threshold,
            reason=reason,
            bucket=bucket,
        )

    ftrl_p = state.proceed_probability()
    posterior_proceed = (1.0 - sps) * ftrl_p
    should_ask = posterior_proceed < resolved_threshold
    reason = (
        f"smart routing: sps={sps:.2f} "
        f"ftrl_proceed_prob={ftrl_p:.3f} "
        f"posterior={posterior_proceed:.3f} "
        f"threshold={resolved_threshold:.3f} "
        f"=> should_ask={should_ask}"
    )
    return ApprovalDecision(
        should_ask=should_ask,
        mode=ApprovalMode.SMART,
        sps=sps,
        ftrl_proceed_prob=ftrl_p,
        threshold=resolved_threshold,
        reason=reason,
        bucket=bucket,
    )


def decide(
    mode: ApprovalMode,
    blast_radius: str,
    *,
    state: Optional[ApprovalState] = None,
    tunable: Optional[str] = None,
    threshold: Optional[float] = None,
) -> ApprovalDecision:
    """Top-level routing entry honouring the operator's mode switch.

    * ``manual`` -> always ask, no posterior maths.
    * ``smart``  -> SPS x FTRL via :func:`smart_decision`.
    * ``off``    -> never ask, no posterior maths.

    Note: ``off`` does NOT short-circuit the destruction-pattern
    blocklist (see :mod:`lyceum.sandbox.destruction_patterns`). Those
    patterns enforce data-deletion safety regardless of approval mode.
    """
    if mode is ApprovalMode.MANUAL:
        return ApprovalDecision(
            should_ask=True,
            mode=ApprovalMode.MANUAL,
            reason="manual mode: ask every time",
        )
    if mode is ApprovalMode.OFF:
        return ApprovalDecision(
            should_ask=False,
            mode=ApprovalMode.OFF,
            reason=(
                "off mode: never ask "
                "(destruction_patterns + release_authorization "
                "remain enforced separately)"
            ),
        )
    return smart_decision(
        blast_radius,
        state=state,
        tunable=tunable,
        threshold=threshold,
    )


# ─── FTRL outcome update (Beta-Bernoulli) ─────────────────────────


def update_state(state: ApprovalState, proceed: bool) -> ApprovalState:
    """Apply one Beta-Bernoulli update.

    proceed=True  -> alpha += 1.0  (operator authorised)
    proceed=False -> beta  += 1.0  (operator declined / corrected)
    """
    if proceed:
        return ApprovalState(alpha=state.alpha + 1.0, beta=state.beta)
    return ApprovalState(alpha=state.alpha, beta=state.beta + 1.0)


def record_outcome(
    blast_radius: str,
    proceed: bool,
    *,
    tunable: Optional[str] = None,
    decision_id: Optional[str] = None,
    source: str = "smart_approval",
) -> None:
    """Persist an outcome to the Lyceum ZIQ outcome store.

    Wraps :func:`lyceum.governance.outcome_store.record_outcome` so
    callers don't need to reach into the store directly. The
    ``feature`` key is fixed at ``lyceum.governance.smart_approval``;
    the ``arm`` is the bucket key (per-tunable or per-radius);
    ``outcome`` is 1.0 for proceed and 0.0 for ask/decline.
    """
    arm = _bucket_key(blast_radius, tunable)
    outcome = 1.0 if proceed else 0.0
    _store_outcome(
        feature=_FEATURE_NAME,
        arm=arm,
        outcome=outcome,
        source=source,
        decision_id=decision_id,
    )


# ─── Config persistence (Wave 2.7-H K7 port) ──────────────────────


def _coerce_state(raw: Any) -> ApprovalState:
    """Coerce a JSON dict into :class:`ApprovalState`."""
    if not isinstance(raw, dict):
        return ApprovalState()
    try:
        alpha = float(raw.get("alpha", 1.0))
        beta = float(raw.get("beta", 1.0))
    except (TypeError, ValueError):
        return ApprovalState()
    alpha = max(alpha, 1e-3)
    beta = max(beta, 1e-3)
    return ApprovalState(alpha=alpha, beta=beta)


def _coerce_ftrl(raw: Any) -> dict[str, ApprovalState]:
    """Coerce the ``ftrl`` JSON map into ``{bucket: ApprovalState}``."""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, ApprovalState] = {}
    for k, v in raw.items():
        if isinstance(k, str):
            out[k] = _coerce_state(v)
    return out


def _read_config_file(path: Path) -> tuple[dict[str, Any], Optional[str]]:
    """Read config JSON. Returns (data, warning_or_None). Fail-open."""
    try:
        if not path.is_file():
            return {}, None
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return {}, f"{path} is not a JSON object; ignoring"
        return data, None
    except json.JSONDecodeError:
        return {}, f"{path} is malformed JSON; ignoring"
    except OSError:
        return {}, None


def load_config(
    path: Optional[Path] = None,
    *,
    explicit_mode: Optional[ApprovalMode] = None,
    mode_env_var: str = MODE_ENV_VAR,
) -> ApprovalConfig:
    """Load + resolve the approval config from file + env.

    Args:
        path: Override the default config file path. Concinno shim
            passes ``~/.concinno/approval_mode.json`` here.
        explicit_mode: Bypasses every other source — useful for
            one-shot caller overrides.
        mode_env_var: Environment variable that overrides ``mode``.
            Concinno shim sets to ``CONCINNO_APPROVAL_MODE``.

    Returns:
        Resolved :class:`ApprovalConfig`. Always succeeds; malformed
        sources surface as ``warnings`` and the default is used.
    """
    p = path or DEFAULT_CONFIG_PATH
    warnings: list[str] = []
    sources: list[str] = []

    mode = ApprovalMode.SMART
    ftrl: dict[str, ApprovalState] = {}

    file_data, file_warning = _read_config_file(p)
    if file_warning:
        warnings.append(file_warning)
    if file_data:
        if "mode" in file_data:
            mode = ApprovalMode.from_raw(file_data.get("mode"))
        ftrl = _coerce_ftrl(file_data.get("ftrl"))
        sources.append("file")

    env_mode = os.environ.get(mode_env_var)
    if env_mode is not None:
        mode = ApprovalMode.from_raw(env_mode)
        sources.append(f"env:{mode_env_var}")

    if explicit_mode is not None:
        mode = explicit_mode
        sources.append("explicit")

    return ApprovalConfig(
        mode=mode,
        ftrl=ftrl,
        source="+".join(sources) if sources else "default",
        warnings=tuple(warnings),
    )


def save_config(
    config: ApprovalConfig, *, path: Optional[Path] = None
) -> None:
    """Persist :class:`ApprovalConfig` to JSON.

    Mode + FTRL state are written; ``source`` and ``warnings`` are
    derived fields and intentionally NOT persisted.
    """
    p = path or DEFAULT_CONFIG_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "mode": config.mode.value,
        "ftrl": {
            bucket: {"alpha": state.alpha, "beta": state.beta}
            for bucket, state in config.ftrl.items()
        },
    }
    p.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def current_mode(path: Optional[Path] = None) -> ApprovalMode:
    """Return the active mode (one-line CLI helper)."""
    return load_config(path).mode


def describe_current_config(path: Optional[Path] = None) -> str:
    """Return a short human-readable summary."""
    p = path or DEFAULT_CONFIG_PATH
    cfg = load_config(p)
    lines = [
        f"mode={cfg.mode.value}",
        f"source={cfg.source}",
        f"config_file={p} ({'present' if p.is_file() else 'absent'})",
        f"threshold={_resolve_threshold():.3f}",
    ]
    if cfg.ftrl:
        lines.append("ftrl:")
        for bucket in sorted(cfg.ftrl):
            st = cfg.ftrl[bucket]
            prob = st.proceed_probability()
            lines.append(
                f"  - {bucket}: alpha={st.alpha:.2f} "
                f"beta={st.beta:.2f} proceed_prob={prob:.3f}"
            )
    if cfg.warnings:
        lines.append("warnings:")
        for w in cfg.warnings:
            lines.append(f"  - {w}")
    return "\n".join(lines)


def decide_with_config(
    blast_radius: str,
    *,
    config: Optional[ApprovalConfig] = None,
    tunable: Optional[str] = None,
    path: Optional[Path] = None,
) -> ApprovalDecision:
    """Concinno-compatible decide: load config + apply mode dispatch.

    Mirrors the original ``concinno.approval_mode.decide`` signature
    so callers don't churn during the substrate migration.
    """
    cfg = config if config is not None else load_config(path)
    bucket = _bucket_key(blast_radius, tunable)
    state = cfg.ftrl.get(bucket)
    return decide(
        cfg.mode,
        blast_radius,
        state=state,
        tunable=tunable,
    )


def record_outcome_with_config(
    blast_radius: str,
    proceed: bool,
    *,
    tunable: Optional[str] = None,
    config: Optional[ApprovalConfig] = None,
    path: Optional[Path] = None,
) -> ApprovalConfig:
    """Concinno-compatible outcome recorder: mutate FTRL + persist.

    Updates the per-bucket Beta posterior in ``config.ftrl`` and
    persists to the same path the config was loaded from. Returns
    the post-update :class:`ApprovalConfig` so callers can re-use it
    without re-loading.
    """
    cfg = config if config is not None else load_config(path)
    bucket = _bucket_key(blast_radius, tunable)
    prior = cfg.ftrl.get(bucket, ApprovalState())
    new_state = update_state(prior, proceed)
    new_ftrl = dict(cfg.ftrl)
    new_ftrl[bucket] = new_state
    new_cfg = ApprovalConfig(
        mode=cfg.mode,
        ftrl=new_ftrl,
        source=cfg.source,
        warnings=cfg.warnings,
    )
    try:
        save_config(new_cfg, path=path)
    except OSError:
        pass

    # Also append to the ZIQ outcome jsonl ledger (existing behavior).
    record_outcome(
        blast_radius,
        proceed,
        tunable=tunable,
    )
    return new_cfg
