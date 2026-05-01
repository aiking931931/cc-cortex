"""concinno.approval_mode — operator-tunable approval routing (Lyceum substrate shim).

@module approval_mode
@responsibility Operator-tunable approval routing. Substrate kernel
    (SPS x FTRL posterior + cold-start safety + Beta-Bernoulli update +
    config persistence) lives in
    ``lyceum.governance.smart_approval_ziq`` since Wave 2.7-H. Concinno
    keeps this thin shim so existing callers
    (``from concinno.approval_mode import decide``) don't churn while
    the canonical implementation stays Lyceum-side and is available to
    non-Concinno harnesses.

    The shim binds the Lyceum substrate to Concinno's persistent state:
    ``~/.concinno/approval_mode.json`` (NOT ``~/.lyceum/...``) and
    ``CONCINNO_APPROVAL_MODE`` env var (NOT ``LYCEUM_APPROVAL_MODE``).
    Operators with both Concinno and Lyceum installed thus have two
    independent approval mode states — by design, no merge.

@dependencies lyceum.governance.smart_approval_ziq
@exports
    - :class:`ApprovalMode`, :class:`ApprovalState`,
      :class:`ApprovalConfig`, :class:`ApprovalDecision`
    - :func:`load_config`, :func:`save_config`
    - :func:`decide`, :func:`record_outcome`
    - :func:`compute_sps_score`, :func:`current_mode`,
      :func:`describe_current_config`
    - :data:`BLAST_RADIUS_LOW`, :data:`BLAST_RADIUS_MEDIUM`,
      :data:`BLAST_RADIUS_HIGH`

Wave 2.7-H (2026-05-02) — port note:
    See ``_AI_BRAIN/05_Planning/2026-05-02-lyceum-api-surface-audit.md``
    §2 K7 for the Option C audit decision. Substrate move keeps the
    public symbol set 1:1 with the legacy Concinno API; the shim
    re-binds Lyceum's path-aware helpers to ``~/.concinno/...`` so
    operator state survives the migration.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from lyceum.governance.smart_approval_ziq import (  # noqa: F401 — public API
    BLAST_RADIUS_HIGH,
    BLAST_RADIUS_LOW,
    BLAST_RADIUS_MEDIUM,
    DEFAULT_THRESHOLD as _DEFAULT_THRESHOLD,
    ApprovalConfig,
    ApprovalDecision,
    ApprovalMode,
    ApprovalState,
    compute_sps_score,
)
from lyceum.governance.smart_approval_ziq import (  # noqa: F401 — test introspection
    _bucket_key,
)
from lyceum.governance.smart_approval_ziq import (
    decide_with_config as _lyceum_decide,
)
from lyceum.governance.smart_approval_ziq import (
    describe_current_config as _lyceum_describe,
)
from lyceum.governance.smart_approval_ziq import (
    load_config as _lyceum_load,
)
from lyceum.governance.smart_approval_ziq import (
    record_outcome_with_config as _lyceum_record,
)
from lyceum.governance.smart_approval_ziq import (
    save_config as _lyceum_save,
)

# ── Concinno-side persistent state binding ──────────────────────────


def _config_path() -> Path:
    """Concinno-side approval mode state lives here (NOT ~/.lyceum/)."""
    return Path.home() / ".concinno" / "approval_mode.json"


_MODE_ENV = "CONCINNO_APPROVAL_MODE"
# Lyceum substrate reads this env name internally; Concinno re-exports
# the same identifier so tests / operators using
# ``CONCINNO_APPROVAL_THRESHOLD`` historically still need to set
# ``LYCEUM_APPROVAL_THRESHOLD`` post-Wave-2.7-H. The shim itself does
# not double-read.
_THRESHOLD_ENV = "LYCEUM_APPROVAL_THRESHOLD"


# ── Concinno-shaped public API (delegates to Lyceum substrate) ─────


def load_config(
    path: Optional[Path] = None,
    *,
    explicit_mode: Optional[ApprovalMode] = None,
) -> ApprovalConfig:
    """Load + resolve the approval config.

    Concinno binding: defaults path to ``~/.concinno/approval_mode.json``
    and env var to ``CONCINNO_APPROVAL_MODE``. Lyceum substrate handles
    the actual JSON parsing + warning collection.
    """
    return _lyceum_load(
        path or _config_path(),
        explicit_mode=explicit_mode,
        mode_env_var=_MODE_ENV,
    )


def save_config(config: ApprovalConfig, *, path: Optional[Path] = None) -> None:
    """Persist :class:`ApprovalConfig` to ``~/.concinno/approval_mode.json``."""
    _lyceum_save(config, path=path or _config_path())


def decide(
    blast_radius: str,
    *,
    tunable: Optional[str] = None,
    config: Optional[ApprovalConfig] = None,
) -> ApprovalDecision:
    """Concinno-shaped decide — delegates to Lyceum substrate.

    Mirrors the original ``concinno.approval_mode.decide`` signature so
    every existing caller (``cli/approval_mode_cmd.py``,
    ``release_authorization.py``) keeps compiling. The OFF-mode
    ``reason`` text is rewritten to mention ``destruction_guard`` (the
    Concinno-side name) instead of the substrate's ``destruction_patterns``
    so operators see the module they actually have on disk.
    """
    decision = _lyceum_decide(
        blast_radius,
        config=config,
        tunable=tunable,
        path=_config_path(),
    )
    if decision.mode is ApprovalMode.OFF:
        # Substrate refers to the Lyceum module name; Concinno operators
        # know the gate by ``destruction_guard``. Rewrite for clarity.
        rewritten = decision.reason.replace(
            "destruction_patterns", "destruction_guard"
        )
        if rewritten != decision.reason:
            return ApprovalDecision(
                should_ask=decision.should_ask,
                mode=decision.mode,
                sps=decision.sps,
                ftrl_proceed_prob=decision.ftrl_proceed_prob,
                threshold=decision.threshold,
                reason=rewritten,
                bucket=decision.bucket,
            )
    return decision


def record_outcome(
    blast_radius: str,
    proceed: bool,
    *,
    tunable: Optional[str] = None,
    config: Optional[ApprovalConfig] = None,
) -> ApprovalConfig:
    """Update FTRL posterior for a bucket and persist Concinno-side.

    Always persists to ``~/.concinno/approval_mode.json`` regardless
    of whether ``config`` was passed in, mirroring the legacy behaviour.
    """
    return _lyceum_record(
        blast_radius,
        proceed,
        tunable=tunable,
        config=config,
        path=_config_path(),
    )


def current_mode() -> ApprovalMode:
    """Return the active mode (one-line CLI helper)."""
    return load_config().mode


def describe_current_config() -> str:
    """Return a short human-readable summary of Concinno-side approval state."""
    return _lyceum_describe(_config_path())


__all__ = [
    "ApprovalConfig",
    "ApprovalDecision",
    "ApprovalMode",
    "ApprovalState",
    "BLAST_RADIUS_HIGH",
    "BLAST_RADIUS_LOW",
    "BLAST_RADIUS_MEDIUM",
    "compute_sps_score",
    "current_mode",
    "decide",
    "describe_current_config",
    "load_config",
    "record_outcome",
    "save_config",
]
