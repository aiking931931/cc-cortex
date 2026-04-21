"""concinno.gaia_meta_router — Per-task SAS / MAS / hybrid arm selector.

@module gaia_meta_router
@responsibility Route each GAIA-shaped task to SAS (single-agent ReAct),
    MAS (1-layer parallel subagent), or hybrid (MAS + external Opus judge)
    using SPS (structural prior from question features) + FTRL (outcome
    online-learning per arm). Breaks the Tran&Kiela 2026 "SAS >= MAS at
    token-matched budget" attack by picking the right arm per task
    instead of committing to one family across the whole run.
@dependencies concinno.ziq_autotuner (optional), stdlib
@exports select_arm, record_arm_outcome, sps_arm_scores, ArmFTRL, ARMS, Arm

Design rationale (Plan Part 5 ⭐ ZIQ meta-router key insight)::

    Level 1 + single-tool signal  → SAS  (reasoning-dense, MAS overhead waste)
    Level 2 + file + multi-tool   → MAS  (coordination-dense, SAS saturates)
    Level 3 + long-horizon + cross-domain → hybrid (MAS + external judge)
    Token budget < 20% remaining  → force SAS (MAS spawn overhead starves)

This module lives at the ``concinno`` package root (not under
``skills/public/agent/``) because it is a generic meta-router consumed by
callers in both the packaged ``gaia_agent`` skill and other agent loops
(e.g. Sancio's fork_context driver). It is intentionally independent of
``gaia_ziq.py``'s buff-stack FTRL: arms are mutually exclusive while buffs
stack, so their state lives in separate files.

Persistence path ``$HOME/.concinno/gaia_arm_ftrl.json`` — override with
``$CONCINNO_GAIA_ARM_STORE`` for tests.
"""

from __future__ import annotations

import json
import math
import os
import threading
from pathlib import Path
from typing import Any, Literal

ARMS: tuple[str, str, str] = ("SAS", "MAS", "hybrid")
Arm = Literal["SAS", "MAS", "hybrid"]

# SPS features that push toward MAS (multi-tool coordination).
_MAS_SIGNALS: tuple[str, ...] = (
    "compare", "contrast", "both", "and then", "after that",
    "multiple", "several", "each of", "respectively",
    "according to", "cross-reference", "combine", "merge",
)

# SPS features that push toward hybrid (long-horizon, multi-hop).
_HYBRID_SIGNALS: tuple[str, ...] = (
    "step by step", "plan", "strategy", "decompose",
    "phase", "stage", "iterate", "synthesize",
)


def _default_store_path() -> Path:
    override = os.environ.get("CONCINNO_GAIA_ARM_STORE", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".concinno" / "gaia_arm_ftrl.json"


class ArmFTRL:
    """Per-arm FTRL-Proximal tracker for SAS/MAS/hybrid outcomes.

    Separate from per-buff ``FTRLv2`` in ``gaia_ziq`` because arm selection
    has different semantics: buffs stack additively, arms are mutually
    exclusive. Cold start returns 0.5 for each arm.
    """

    def __init__(self, lr: float = 0.2) -> None:
        self.z: dict[str, float] = {a: 0.0 for a in ARMS}
        self.n: dict[str, float] = {a: 0.0 for a in ARMS}
        self.lr = lr

    def gate(self, arm: str) -> float:
        """Return [0.01, 0.99] preference for arm. 0.5 at cold start.

        Monotonic in accumulated reward: rewards >= 0.5 push gate above 0.5,
        rewards < 0.5 push it below. Intentionally distinct from the
        ``sign * z`` quota-limiter pattern used by ``gaia_ziq.FTRLv2`` for
        buff activation, because arm selection is preference-based not
        budget-based.
        """
        if self.n.get(arm, 0.0) == 0.0:
            return 0.5
        denom = self.lr + math.sqrt(self.n[arm])
        return max(0.01, min(0.99, 0.5 - self.z[arm] / denom))

    def update(self, arm: str, reward: float) -> None:
        """Update arm FTRL state with scalar reward in [0, 1]."""
        g = -(reward - 0.5)
        self.z[arm] = self.z.get(arm, 0.0) + g
        self.n[arm] = self.n.get(arm, 0.0) + g * g

    def save(self, path: Path | None = None) -> None:
        p = path or _default_store_path()
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("w", encoding="utf-8") as f:
                json.dump({"z": self.z, "n": self.n}, f)
        except OSError:
            pass

    def load(self, path: Path | None = None) -> None:
        p = path or _default_store_path()
        if not p.exists():
            return
        try:
            with p.open("r", encoding="utf-8") as f:
                d = json.load(f)
            for a in ARMS:
                self.z[a] = float(d.get("z", {}).get(a, 0.0))
                self.n[a] = float(d.get("n", {}).get(a, 0.0))
        except (OSError, json.JSONDecodeError, ValueError):
            # Corrupt state -> fresh start is safer than propagating garbage.
            pass


def sps_arm_scores(
    question: str,
    level: str = "1",
    file_name: str = "",
    qtype: str | None = None,
) -> dict[str, float]:
    """Structural prior over arms from task features.

    Returns raw non-normalized scores in [1.0, inf). Higher = stronger
    structural reason to pick that arm. Uniform prior starts at 1.0 per arm;
    signals add to the base.

    Signals:
        - Level 1, short Q, no file → SAS
        - Level 2, file present, multi-tool hint → MAS
        - Level 3 OR very long Q OR hybrid keywords → hybrid
        - qtype override: research/visual → MAS; factual/calculation → SAS
    """
    q = question.lower()
    qlen = len(question)
    scores: dict[str, float] = {a: 1.0 for a in ARMS}

    # Level signal --------------------------------------------------
    if level == "1":
        scores["SAS"] += 1.2
    elif level == "2":
        scores["MAS"] += 1.0
        scores["SAS"] += 0.3
    elif level == "3":
        scores["hybrid"] += 1.5
        scores["MAS"] += 0.5

    # Length signal -------------------------------------------------
    if qlen < 120:
        scores["SAS"] += 0.6
    elif qlen < 300:
        scores["SAS"] += 0.2
        scores["MAS"] += 0.3
    else:
        scores["hybrid"] += 0.8
        scores["MAS"] += 0.4

    # File attachment signal ---------------------------------------
    if file_name:
        scores["MAS"] += 0.5  # file + Q = multi-source synthesis

    # MAS keyword signals ------------------------------------------
    mas_hits = sum(1 for kw in _MAS_SIGNALS if kw in q)
    if mas_hits > 0:
        scores["MAS"] += 0.4 * mas_hits
    hybrid_hits = sum(1 for kw in _HYBRID_SIGNALS if kw in q)
    if hybrid_hits > 0:
        scores["hybrid"] += 0.4 * hybrid_hits

    # qtype override (from classify_question in gaia_agent) --------
    if qtype == "research":
        scores["MAS"] += 0.6
        scores["hybrid"] += 0.4
    elif qtype == "calculation":
        scores["SAS"] += 0.5
    elif qtype == "file_analysis":
        scores["MAS"] += 0.3
    elif qtype == "factual":
        scores["SAS"] += 0.7
    elif qtype == "visual":
        scores["SAS"] += 0.4

    return scores


def select_arm(
    task: dict[str, Any],
    budget: int = 10_000,
    context: dict[str, Any] | None = None,
    router: ArmFTRL | None = None,
    budget_floor_fraction: float = 0.20,
) -> Arm:
    """Meta-router: choose SAS / MAS / hybrid for this task.

    Args:
        task: GAIA-shaped dict with at least ``Question`` or ``question``;
            optional ``Level`` or ``level``, ``file_name``, ``qtype``.
        budget: Per-task token budget in tokens. When remaining budget drops
            below ``budget_floor_fraction`` of this, we force SAS.
        context: Optional state carrier with keys ``remaining_budget`` (int)
            and ``prev_arm`` (str) for hysteresis. Unknown keys ignored.
        router: Optional ``ArmFTRL`` for online posterior. If None, uses the
            loaded-from-disk singleton.
        budget_floor_fraction: Fraction of ``budget`` below which we force SAS.
            Default 0.20 per plan ("Token 預算剩 <20% → SAS fallback").

    Returns:
        One of "SAS", "MAS", "hybrid".
    """
    ctx = context or {}
    question = task.get("Question") or task.get("question") or ""
    level = str(task.get("Level") or task.get("level") or "1")
    file_name = task.get("file_name", "") or ""
    qtype = task.get("qtype")

    # Hard fallback: low budget -> SAS -----------------------------
    remaining = ctx.get("remaining_budget")
    if (
        isinstance(remaining, (int, float))
        and budget > 0
        and remaining / budget < budget_floor_fraction
    ):
        return "SAS"

    # posterior = SPS x FTRL(gate) ---------------------------------
    sps = sps_arm_scores(question, level=level, file_name=file_name, qtype=qtype)
    if router is None:
        router = _default_router()
    posterior: dict[str, float] = {}
    for arm in ARMS:
        posterior[arm] = sps.get(arm, 1.0) * router.gate(arm)

    # Hysteresis: keep prev_arm if within 5% of winner -------------
    prev_arm = ctx.get("prev_arm")
    if prev_arm in ARMS:
        best_arm = max(posterior, key=posterior.__getitem__)
        best_score = posterior[best_arm]
        if best_score > 0 and posterior[prev_arm] / best_score >= 0.95:
            return prev_arm  # type: ignore[return-value]

    winner = max(posterior, key=posterior.__getitem__)
    return winner  # type: ignore[return-value]


_router_singleton: ArmFTRL | None = None
_router_lock = threading.Lock()


def _default_router() -> ArmFTRL:
    """Lazily load the singleton ``ArmFTRL`` from disk."""
    global _router_singleton
    if _router_singleton is None:
        with _router_lock:
            if _router_singleton is None:
                r = ArmFTRL()
                r.load()
                _router_singleton = r
    return _router_singleton


def reset_default_router() -> None:
    """Drop the memoized singleton. Primarily for tests."""
    global _router_singleton
    with _router_lock:
        _router_singleton = None


def record_arm_outcome(arm: str, reward: float, persist: bool = True) -> None:
    """Record (arm, reward) feedback to update the meta-router.

    Args:
        arm: One of ``"SAS"``, ``"MAS"``, ``"hybrid"``. Unknown arms silently
            ignored so upstream typos never crash a benchmark run.
        reward: Outcome in [0, 1]; 1 = task succeeded, 0 = task failed.
            Values outside [0, 1] are clamped.
        persist: Whether to save the router state to disk.
    """
    if arm not in ARMS:
        return
    router = _default_router()
    router.update(arm, max(0.0, min(1.0, float(reward))))
    if persist:
        router.save()


__all__ = [
    "ARMS",
    "Arm",
    "ArmFTRL",
    "record_arm_outcome",
    "reset_default_router",
    "select_arm",
    "sps_arm_scores",
]
