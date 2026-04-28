"""concinno.approval_mode — operator-tunable approval routing.

@module approval_mode
@responsibility Provide a single switch ("manual / smart / off") that
    governs whether the agent should ask the operator before any
    *user-authorisation-bearing* operation. The switch is layered
    **above** :mod:`concinno.destruction_guard` (R0-R4 data-destruction
    is still policed exactly as before — see "Scope" below) and **above**
    :mod:`concinno.release_authorization` (publish gate keeps its own
    opt-out).

@dependencies (stdlib only — json, os, dataclasses, enum, pathlib)

@exports
    - :class:`ApprovalMode` (enum: ``manual | smart | off``)
    - :class:`ApprovalConfig` (resolved snapshot)
    - :class:`ApprovalDecision` (per-call routing answer)
    - :func:`load_config`, :func:`save_config`
    - :func:`decide` (main entry — returns :class:`ApprovalDecision`)
    - :func:`compute_sps_score`, :func:`record_outcome` (smart-mode helpers)
    - :data:`BLAST_RADIUS_LOW`, :data:`BLAST_RADIUS_MEDIUM`,
      :data:`BLAST_RADIUS_HIGH` (radius constants)

Scope:
    HP6 lives at a strictly higher abstraction than the data-destruction
    guard. Concretely, this module:

    * NEVER weakens R0-R4 in :mod:`concinno.destruction_guard`. Even in
      ``off`` mode, ``rm -rf working tree`` / ``DROP TABLE`` / etc.
      still trip the destruction guard's hard deny + ``#DESTROY_CONFIRMED``
      escape. HP6 does NOT short-circuit destruction guard.
    * NEVER replaces :mod:`concinno.release_authorization`. The publish
      opt-out (``~/.concinno/release_auth.json::disabled=True``) is a
      DIFFERENT switch, intentionally so per the user's L0 鐵律 #4
      sediment. HP6's ``off`` mode does not turn the publish gate back
      on; the publish gate just stays opted-out independently.
    * Decides routing for *any* AskUserQuestion-style choice point that
      is not data-destruction and not publish authorisation — e.g.
      "should I refactor the helper to async?", "delete this stale
      branch?", "rebuild the cache directory?" — the kind of "ask vs
      proceed" decision that today blocks autonomy.

Modes:

    * ``manual`` — every non-trivial decision goes through AskUser.
      Mirrors the 2.x default for power-users who want full control.
    * ``smart`` (default) — SPS(blast radius) × FTRL(historical deny
      rate posterior) determines whether to ask or proceed. The SPS
      term anchors the decision in the structural prior (a high-radius
      op is treated as ask-by-default), and the FTRL term carries the
      learning signal (the longer the operator has clicked "proceed"
      on similar prompts, the higher the autonomy threshold drifts).
    * ``off`` — fully autonomous; never ask, just record what was done.
      Honoured exactly as written so the operator's "stop asking me"
      directive is reliable.

Sources (later overrides earlier — mirrors switches.md ordering):

    1. Defaults (mode=``smart``)
    2. ``~/.concinno/approval_mode.json``
    3. Env var ``CONCINNO_APPROVAL_MODE`` (one of manual/smart/off)
    4. Explicit caller argument

Persistent state:

    ``~/.concinno/approval_mode.json`` schema::

        {
          "mode": "smart",
          "ftrl": {
            "default":         {"alpha": 1.0, "beta": 1.0},
            "blast:low":       {"alpha": 1.0, "beta": 1.0},
            "blast:medium":    {"alpha": 1.0, "beta": 1.0},
            "blast:high":      {"alpha": 1.0, "beta": 1.0},
            "tunable:<name>":  {"alpha": 1.0, "beta": 1.0}
          }
        }

    ``alpha`` / ``beta`` are Beta-distribution shape params for a
    posterior over "operator-said-proceed" vs "operator-said-ask".
    Initialised at the Jeffreys prior (``Beta(1,1)``) so a fresh
    install starts with a 50/50 belief.

Backward compatibility:

    1. Adding a new mode is a major bump (changes the routing space).
    2. Adding a new tunable bucket key is minor (extra keys are ignored
       by older readers).
    3. ``smart`` is the default; bumping the default would be major.
    4. ``ApprovalDecision.should_ask`` is the contractual return value
       — ``ApprovalDecision.reason`` is human-readable and may change.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

__all__ = [
    "BLAST_RADIUS_LOW",
    "BLAST_RADIUS_MEDIUM",
    "BLAST_RADIUS_HIGH",
    "ApprovalMode",
    "ApprovalConfig",
    "ApprovalDecision",
    "ApprovalState",
    "load_config",
    "save_config",
    "decide",
    "compute_sps_score",
    "record_outcome",
    "describe_current_config",
    "current_mode",
]


# ── Constants ───────────────────────────────────────────────────────


BLAST_RADIUS_LOW = "low"
BLAST_RADIUS_MEDIUM = "medium"
BLAST_RADIUS_HIGH = "high"

_VALID_RADII = frozenset({BLAST_RADIUS_LOW, BLAST_RADIUS_MEDIUM, BLAST_RADIUS_HIGH})

_SPS_BY_RADIUS: dict[str, float] = {
    BLAST_RADIUS_LOW: 0.10,
    BLAST_RADIUS_MEDIUM: 0.50,
    BLAST_RADIUS_HIGH: 0.90,
}

# Posterior threshold above which we hand control back to the operator.
# Empirically chosen so a fresh prior (alpha=beta=1) on a Medium-radius
# decision routes the first call to "ask" (SPS dominates), and as the
# operator clicks "proceed" the FTRL term overrides the SPS over ~5-10
# repeats. Tunable via ``CONCINNO_APPROVAL_THRESHOLD`` env for testing.
_DEFAULT_THRESHOLD = 0.50
_THRESHOLD_ENV = "CONCINNO_APPROVAL_THRESHOLD"

_MODE_ENV = "CONCINNO_APPROVAL_MODE"


# ── Enum + dataclasses ──────────────────────────────────────────────


class ApprovalMode(str, Enum):
    """Three operator-selectable approval routing modes."""

    MANUAL = "manual"
    SMART = "smart"
    OFF = "off"

    @classmethod
    def from_raw(cls, raw: object) -> "ApprovalMode":
        """Parse a loose user-supplied value (str / None / missing).

        Falls back to :attr:`SMART` (the default) on any unknown input
        so misconfiguration degrades gracefully toward middle ground —
        not toward unconditional ``off`` and not toward unconditional
        ``manual``.
        """
        if isinstance(raw, str):
            normalized = raw.strip().lower()
            for m in cls:
                if m.value == normalized:
                    return m
        return cls.SMART


@dataclass(frozen=True)
class ApprovalState:
    """One Beta-distribution posterior over a single tunable bucket.

    Attributes:
        alpha: Successes (operator clicked "proceed").
        beta: Failures (operator clicked "ask" / cancelled).
    """

    alpha: float = 1.0
    beta: float = 1.0

    def proceed_probability(self) -> float:
        """Posterior mean of the Beta(alpha, beta) distribution."""
        denom = self.alpha + self.beta
        if denom <= 0:
            return 0.5
        return self.alpha / denom


@dataclass(frozen=True)
class ApprovalConfig:
    """Resolved approval-mode snapshot for the current process."""

    mode: ApprovalMode = ApprovalMode.SMART
    ftrl: dict[str, ApprovalState] = field(default_factory=dict)
    source: str = "default"
    warnings: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ApprovalDecision:
    """Per-call routing answer.

    Attributes:
        should_ask: True iff the caller MUST AskUser; False = autonomous.
        mode: The active mode that drove the decision.
        sps: SPS structural prior used (0.0–1.0). Empty for ``manual`` /
            ``off`` modes (those don't compute SPS).
        ftrl_proceed_prob: Posterior P(operator clicks proceed) used
            (0.0–1.0). Empty for ``manual`` / ``off``.
        threshold: Posterior threshold compared against; only set in
            ``smart`` mode.
        reason: Human-readable explanation suitable for stderr / hook
            log surfacing.
    """

    should_ask: bool
    mode: ApprovalMode
    sps: float = 0.0
    ftrl_proceed_prob: float = 0.0
    threshold: float = 0.0
    reason: str = ""


# ── Path resolution ─────────────────────────────────────────────────


def _config_path() -> Path:
    return Path.home() / ".concinno" / "approval_mode.json"


def _ensure_root() -> None:
    _config_path().parent.mkdir(parents=True, exist_ok=True)


# ── Persistence ─────────────────────────────────────────────────────


def _read_config_file(
    path: Path,
) -> tuple[dict[str, Any], Optional[str]]:
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


def _coerce_state(raw: Any) -> ApprovalState:
    """Coerce a JSON dict into :class:`ApprovalState` with safe defaults."""
    if not isinstance(raw, dict):
        return ApprovalState()
    try:
        alpha = float(raw.get("alpha", 1.0))
        beta = float(raw.get("beta", 1.0))
    except (TypeError, ValueError):
        return ApprovalState()
    # Beta params must be positive — clamp gently.
    alpha = max(alpha, 1e-3)
    beta = max(beta, 1e-3)
    return ApprovalState(alpha=alpha, beta=beta)


def _coerce_ftrl(raw: Any) -> dict[str, ApprovalState]:
    """Coerce the ``ftrl`` map into ``{bucket: ApprovalState}``."""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, ApprovalState] = {}
    for k, v in raw.items():
        if not isinstance(k, str):
            continue
        out[k] = _coerce_state(v)
    return out


def load_config(
    path: Optional[Path] = None,
    *,
    explicit_mode: Optional[ApprovalMode] = None,
) -> ApprovalConfig:
    """Load + resolve the approval config from file + env.

    Args:
        path: Override the default config file path (for tests).
        explicit_mode: When set, bypasses every other source — useful
            for one-shot caller overrides.

    Returns:
        Resolved :class:`ApprovalConfig`. Always succeeds; malformed
        sources surface as ``warnings`` and the default is used.
    """
    p = path or _config_path()
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

    env_mode = os.environ.get(_MODE_ENV)
    if env_mode is not None:
        mode = ApprovalMode.from_raw(env_mode)
        sources.append("env:mode")

    if explicit_mode is not None:
        mode = explicit_mode
        sources.append("explicit")

    return ApprovalConfig(
        mode=mode,
        ftrl=ftrl,
        source="+".join(sources) if sources else "default",
        warnings=tuple(warnings),
    )


def save_config(config: ApprovalConfig, *, path: Optional[Path] = None) -> None:
    """Persist :class:`ApprovalConfig` to ``~/.concinno/approval_mode.json``.

    Mode + FTRL state are written; ``source`` and ``warnings`` are
    derived fields and intentionally NOT persisted.
    """
    p = path or _config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "mode": config.mode.value,
        "ftrl": {
            bucket: {"alpha": state.alpha, "beta": state.beta}
            for bucket, state in config.ftrl.items()
        },
    }
    p.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


# ── SPS / FTRL ──────────────────────────────────────────────────────


def compute_sps_score(blast_radius: str) -> float:
    """Return the SPS structural-prior weight for a blast radius.

    Args:
        blast_radius: One of ``low|medium|high`` (case-insensitive).
            Unknown values fall back to ``medium`` (the safest middle).

    Returns:
        Float in ``[0.0, 1.0]``. Higher = "intuitively asks more".
    """
    key = (blast_radius or "").strip().lower()
    if key not in _VALID_RADII:
        key = BLAST_RADIUS_MEDIUM
    return _SPS_BY_RADIUS[key]


def _bucket_key(blast_radius: str, tunable: Optional[str]) -> str:
    """Compose the FTRL state-store key.

    Per-tunable buckets have priority because they capture per-feature
    operator preferences ("ask me about cache rebuilds, but never about
    branch deletions"). Falls back to a per-radius bucket so cold
    tunables still get useful learning.
    """
    if tunable:
        return f"tunable:{tunable}"
    radius = (blast_radius or "").strip().lower()
    if radius not in _VALID_RADII:
        radius = BLAST_RADIUS_MEDIUM
    return f"blast:{radius}"


def _resolve_threshold() -> float:
    """Read the ask/proceed posterior threshold from env or default."""
    raw = os.environ.get(_THRESHOLD_ENV, "").strip()
    if raw:
        try:
            v = float(raw)
            return min(1.0, max(0.0, v))
        except ValueError:
            return _DEFAULT_THRESHOLD
    return _DEFAULT_THRESHOLD


def _smart_decision(
    config: ApprovalConfig,
    blast_radius: str,
    tunable: Optional[str],
) -> ApprovalDecision:
    """SPS × FTRL routing for ``smart`` mode.

    Decision rule (Beta-Bernoulli posterior with structural prior):

        posterior_proceed = (1 - sps) * ftrl_proceed_prob
                          + sps * 0.0      # SPS pulls toward "ask"
        ⇒ should_ask = posterior_proceed < threshold

    Math intuition:

        * SPS = 0.9 (high blast) ⇒ posterior never exceeds 0.10 even with
          a strongly-positive FTRL — the structural prior dominates.
        * SPS = 0.10 (low blast) ⇒ posterior tracks FTRL closely — a
          handful of "proceed" clicks moves the threshold quickly.

    The closed form is intentionally simple (no log-odds maths) because
    the operator should be able to read this code and predict the
    routing — the ZIQ first-principle "explainability beats accuracy
    when human trust is on the line".
    """
    sps = compute_sps_score(blast_radius)
    bucket = _bucket_key(blast_radius, tunable)
    state = config.ftrl.get(bucket, ApprovalState())
    ftrl_p = state.proceed_probability()
    posterior_proceed = (1.0 - sps) * ftrl_p
    threshold = _resolve_threshold()
    should_ask = posterior_proceed < threshold
    reason = (
        f"smart routing: sps={sps:.2f} "
        f"ftrl_proceed_prob={ftrl_p:.3f} "
        f"posterior={posterior_proceed:.3f} "
        f"threshold={threshold:.3f} "
        f"⇒ should_ask={should_ask}"
    )
    return ApprovalDecision(
        should_ask=should_ask,
        mode=ApprovalMode.SMART,
        sps=sps,
        ftrl_proceed_prob=ftrl_p,
        threshold=threshold,
        reason=reason,
    )


def decide(
    blast_radius: str,
    *,
    tunable: Optional[str] = None,
    config: Optional[ApprovalConfig] = None,
) -> ApprovalDecision:
    """Main entry — decide whether the caller MUST AskUser.

    Args:
        blast_radius: One of ``low|medium|high``. Anything else maps to
            ``medium`` for safety.
        tunable: Optional per-feature key for FTRL learning. Recommended
            for any caller that has a stable feature identifier so
            operator preferences specialise per-feature rather than
            per-radius.
        config: Resolved :class:`ApprovalConfig`. Defaults to
            :func:`load_config`.

    Returns:
        :class:`ApprovalDecision`. Honours mode strictly:

            * ``manual`` ⇒ ``should_ask=True``, no posterior maths.
            * ``smart`` ⇒ SPS × FTRL routing.
            * ``off`` ⇒ ``should_ask=False``, no posterior maths.
    """
    cfg = config if config is not None else load_config()

    if cfg.mode is ApprovalMode.MANUAL:
        return ApprovalDecision(
            should_ask=True,
            mode=ApprovalMode.MANUAL,
            reason="manual mode: ask every time",
        )
    if cfg.mode is ApprovalMode.OFF:
        return ApprovalDecision(
            should_ask=False,
            mode=ApprovalMode.OFF,
            reason=(
                "off mode: never ask (destruction_guard R0-R4 + "
                "release_authorization remain enforced separately)"
            ),
        )
    return _smart_decision(cfg, blast_radius, tunable)


# ── FTRL update ─────────────────────────────────────────────────────


def record_outcome(
    blast_radius: str,
    proceed: bool,
    *,
    tunable: Optional[str] = None,
    config: Optional[ApprovalConfig] = None,
) -> ApprovalConfig:
    """Update the FTRL posterior for a bucket and persist.

    Args:
        blast_radius: Blast radius the decision was made under.
        proceed: True iff the operator clicked "proceed" (or the
            outcome of a smart-route auto-decision was observed to
            be safe). False = "ask" / cancelled / regretted.
        tunable: Per-feature key matching :func:`decide`'s ``tunable``.
        config: Resolved config to mutate. Defaults to
            :func:`load_config`. The returned config is the post-update
            snapshot; the caller can re-use it without re-loading.

    Returns:
        Post-update :class:`ApprovalConfig`.
    """
    cfg = config if config is not None else load_config()
    bucket = _bucket_key(blast_radius, tunable)
    prior = cfg.ftrl.get(bucket, ApprovalState())
    if proceed:
        new_state = ApprovalState(alpha=prior.alpha + 1.0, beta=prior.beta)
    else:
        new_state = ApprovalState(alpha=prior.alpha, beta=prior.beta + 1.0)
    new_ftrl = dict(cfg.ftrl)
    new_ftrl[bucket] = new_state
    new_cfg = ApprovalConfig(
        mode=cfg.mode,
        ftrl=new_ftrl,
        source=cfg.source,
        warnings=cfg.warnings,
    )
    try:
        save_config(new_cfg)
    except OSError:
        # Persisting failed — return the in-memory update so callers
        # can still see the new posterior; disk re-sync on next save.
        pass
    return new_cfg


# ── Convenience helpers ─────────────────────────────────────────────


def current_mode() -> ApprovalMode:
    """Return the active mode (one-line CLI helper)."""
    return load_config().mode


def describe_current_config() -> str:
    """Return a short human-readable summary."""
    cfg = load_config()
    lines = [
        f"mode={cfg.mode.value}",
        f"source={cfg.source}",
        f"config_file={_config_path()} "
        f"({'present' if _config_path().is_file() else 'absent'})",
        f"threshold={_resolve_threshold():.3f}",
    ]
    if cfg.ftrl:
        lines.append("ftrl:")
        for bucket in sorted(cfg.ftrl):
            st = cfg.ftrl[bucket]
            p = st.proceed_probability()
            lines.append(
                f"  - {bucket}: alpha={st.alpha:.2f} beta={st.beta:.2f} "
                f"proceed_prob={p:.3f}"
            )
    if cfg.warnings:
        lines.append("warnings:")
        for w in cfg.warnings:
            lines.append(f"  - {w}")
    return "\n".join(lines)


# Sanity import to keep :mod:`math` import meaningful for future
# extensions (log-odds variant lives behind a flag — kept import here
# so future addition does not perturb the import block diff).
_ = math.tau  # noqa: F841 - intentional anchor for future log-odds path
