"""concinno.ziq_autotuner — Generic ZIQ hyperparameter auto-tuner.

@module ziq_autotuner
@responsibility Route tunable hyperparameters through a three-regime
    cold-to-warm gradient driven by (value, outcome) history. Lets any
    hardcoded threshold / magic-number / boolean choice in the cognitive
    layer be promoted to ZIQ-governed adaptive tuning without changing
    callers that only want the preset default.
@dependencies stdlib only (json, math, os, pathlib, threading)
@exports ZIQAutoTuner, AutoTuneObservation, AutoTuneRegime, is_autotune_enabled

Design
------
Each *tunable target* (e.g. ``routing_threshold``, ``spawn_depth_cap``)
owns one ``ZIQAutoTuner`` instance. Callers log (value_used, outcome)
pairs via ``record()`` and ask ``suggest()`` for the next value. The
suggested value depends on the sample count ``n``:

* ``n < tunable_threshold`` (default **300**) → preset (hardcoded best)
* ``tunable_threshold <= n < full_threshold`` (default **500**)
    → **conservative** tune — small learning rate + large prior weight
      so early signal does not swing the recommendation.
* ``n >= full_threshold`` → **full** FTRL-Proximal tune.

This matches the user's 2026-04-21 directive (Plan Part 10 Session E):

    "所有能調動的參數能選擇的，只要數量大於 300 或 500 都能 ZIQ 自己 CBUA 最佳解"

Opt-in via ``CONCINNO_ZIQ_AUTOTUNE=1`` env var. Default off preserves
backward compatibility — callers that never set the env keep receiving
the preset even after ``record()`` observations accumulate.

Persistence
-----------
Each target persists its observation log as append-only JSONL at
``$HOME/.concinno/ziq_tuners/<target>.jsonl``. The file is self-healing:
a partially written record is ignored on load. No schema migration is
needed; fields are read with ``.get()``.

Numerical target types
----------------------
* ``continuous``: values are real numbers inside ``(vmin, vmax)``.
  Conservative tune mixes the preset with the outcome-weighted sample
  mean; full tune uses FTRL-Proximal gradient descent on the mean
  estimate.
* ``discrete``: values live in a finite ``choices`` list. Conservative
  tune returns the preset unless one arm has >=2x the sample count AND
  strictly higher mean outcome; full tune uses FTRL per-arm weights with
  deterministic greedy pick at ``suggest()``.
* ``boolean``: special-case of ``discrete`` with ``choices=[False, True]``.

Invariants
----------
* ``record()`` is idempotent on disk corruption (skips malformed lines).
* ``suggest()`` always returns a value whose type matches ``preset``.
* Empty history always returns preset — never None.
"""

from __future__ import annotations

import json
import math
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Literal, Optional

AutoTuneRegime = Literal["preset", "conservative", "full"]
TargetKind = Literal["continuous", "discrete", "boolean"]


def is_autotune_enabled() -> bool:
    """Return True unless ``CONCINNO_ZIQ_AUTOTUNE`` is explicitly disabled.

    2026-05-10 cold-start unblock: previously defaulted to **disabled**
    (only enabled when env was set to a truthy value), which combined
    with the 300-sample ``tunable_threshold`` meant 4-arm bandits with
    only 1 arm observed (N=93) collapsed to ``regime='preset'`` — the
    tuner produced zero exploration in production.

    New default behaviour:

    * ``CONCINNO_ZIQ_AUTOTUNE`` unset or truthy (``1``/``true``/``yes``/``on``)
      → **enabled**
    * ``CONCINNO_ZIQ_AUTOTUNE`` set to ``0``/``false``/``no``/``off``/``disabled``
      → disabled (explicit opt-out preserved for ablation / red-team)
    * Any other non-empty value → enabled (unrecognised values fall
      through to the default-on policy rather than silently disabling)

    Rationale: the tuner records (value, outcome) pairs unconditionally
    via ``record()``; the only effect of disabling is that ``suggest()``
    returns the preset even when the regime would otherwise be
    conservative / full. Defaulting to enabled means the FTRL signal
    starts contributing as soon as the threshold is crossed, instead
    of requiring a one-time env-var ritual that no production caller
    ever performed.
    """
    raw = os.environ.get("CONCINNO_ZIQ_AUTOTUNE", "").strip().lower()
    if raw in {"0", "false", "no", "off", "disabled"}:
        return False
    return True


def _default_store_dir() -> Path:
    """Storage directory for persistent tuner state.

    ``$CONCINNO_ZIQ_TUNER_DIR`` overrides for tests; else ``~/.concinno/ziq_tuners``.
    """
    override = os.environ.get("CONCINNO_ZIQ_TUNER_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".concinno" / "ziq_tuners"


@dataclass(frozen=True)
class AutoTuneObservation:
    """One recorded (value, outcome) sample."""

    value: Any
    outcome: float
    context: dict[str, Any] = field(default_factory=dict)


@dataclass
class _FTRLArm:
    """FTRL-Proximal bookkeeping for a single arm / value estimate."""

    z: float = 0.0
    n: float = 0.0
    reward_sum: float = 0.0
    reward_count: int = 0

    def update(self, reward: float, lr: float) -> None:
        # Center reward around 0.5 so positive outcomes pull toward 'increase'.
        gradient = -(reward - 0.5)
        sigma = (math.sqrt(self.n + gradient * gradient) - math.sqrt(self.n)) / max(
            lr, 1e-9,
        )
        self.z += gradient - sigma * self._weight()
        self.n += gradient * gradient
        self.reward_sum += reward
        self.reward_count += 1

    def _weight(self) -> float:
        if self.n <= 0:
            return 0.0
        return -self.z / (1.0 + math.sqrt(self.n))

    def weight(self) -> float:
        return self._weight()

    def mean_reward(self) -> float:
        if self.reward_count == 0:
            return 0.5
        return self.reward_sum / self.reward_count


class ZIQAutoTuner:
    """Adaptive tuner for one hyperparameter target.

    Args:
        target: Short identifier used as filename stem. Must be filesystem-safe;
            caller is responsible for sanitizing. Examples: ``"routing_threshold"``,
            ``"spawn_depth_cap"``.
        preset: Baseline value returned while ``n < tunable_threshold`` or when
            auto-tune is disabled. Type determines the default ``kind``.
        kind: One of ``"continuous"``, ``"discrete"``, ``"boolean"``. If omitted,
            inferred from ``preset`` type and ``choices``.
        choices: For ``discrete`` kind, the finite list of allowed values. Ignored
            for ``continuous``.
        vmin, vmax: For ``continuous`` kind, hard clamps on the suggested value.
        tunable_threshold: Sample count at which conservative tuning starts.
            Default **300** per user directive.
        full_threshold: Sample count at which full FTRL tuning starts.
            Default **500** per user directive.
        conservative_lr: FTRL learning rate in the conservative regime. Small
            so early signal does not overshoot.
        full_lr: FTRL learning rate in the full regime.
        store_dir: Override directory for persistence. Default ``~/.concinno/ziq_tuners``.
        auto_persist: When True, ``record()`` appends each observation to disk.
            Default True; tests can pass False for pure in-memory use.

    Thread-safety
    -------------
    ``record()`` and ``suggest()`` are guarded by a per-instance lock. Concurrent
    callers on the same target see consistent state; callers on different
    targets run independently.
    """

    def __init__(
        self,
        target: str,
        preset: Any,
        *,
        kind: Optional[TargetKind] = None,
        choices: Optional[Iterable[Any]] = None,
        vmin: Optional[float] = None,
        vmax: Optional[float] = None,
        tunable_threshold: int = 50,
        full_threshold: int = 500,
        conservative_lr: float = 0.05,
        full_lr: float = 0.3,
        cold_start_explore_pct: float = 0.05,
        store_dir: Optional[Path] = None,
        auto_persist: bool = True,
    ) -> None:
        if tunable_threshold < 0 or full_threshold < tunable_threshold:
            raise ValueError(
                "thresholds must satisfy 0 <= tunable_threshold <= full_threshold",
            )
        self.target = target
        self.preset = preset
        self.tunable_threshold = tunable_threshold
        self.full_threshold = full_threshold
        self.conservative_lr = conservative_lr
        self.full_lr = full_lr
        self.auto_persist = auto_persist
        # 2026-05-10 cold-start exploration: every Nth observation in
        # the conservative regime, force a random discrete arm pick so
        # under-observed arms accumulate samples instead of starving.
        # Continuous tuners ignore this knob.
        self.cold_start_explore_pct = max(
            0.0, min(0.5, float(cold_start_explore_pct)),
        )

        self._choices_list: Optional[list[Any]] = (
            list(choices) if choices is not None else None
        )
        self.kind: TargetKind = kind or self._infer_kind(preset, self._choices_list)

        if self.kind == "boolean":
            self._choices_list = [False, True]
        if self.kind == "discrete" and not self._choices_list:
            raise ValueError(
                f"discrete target '{target}' requires non-empty choices list",
            )
        if self.kind == "continuous":
            self.vmin = float(vmin) if vmin is not None else -math.inf
            self.vmax = float(vmax) if vmax is not None else math.inf
            if self.vmin > self.vmax:
                raise ValueError("vmin must be <= vmax")
        else:
            self.vmin = -math.inf
            self.vmax = math.inf

        self._lock = threading.RLock()
        self._observations: list[AutoTuneObservation] = []
        self._arms: dict[Any, _FTRLArm] = {}
        self._cont_arm = _FTRLArm()  # aggregate arm for continuous target
        self._cont_mean_estimate: float = (
            float(preset) if self.kind == "continuous" else 0.0
        )
        self._store_dir = store_dir or _default_store_dir()

        self._load_from_disk()

    # -- Public API --------------------------------------------------

    @property
    def n(self) -> int:
        """Number of observations recorded (memory + disk-loaded)."""
        return len(self._observations)

    def current_regime(self) -> AutoTuneRegime:
        """Which regime ``suggest()`` will use right now.

        Auto-tune disabled collapses all regimes to ``"preset"``.
        """
        if not is_autotune_enabled():
            return "preset"
        if self.n < self.tunable_threshold:
            return "preset"
        if self.n < self.full_threshold:
            return "conservative"
        return "full"

    def record(
        self,
        value: Any,
        outcome: float,
        context: Optional[dict[str, Any]] = None,
    ) -> None:
        """Log one (value, outcome) sample.

        Args:
            value: The value actually used on this request.
            outcome: Scalar reward in [0, 1]. 1 = full success, 0 = failure.
                Values outside [0, 1] are clamped with a one-line warning-free
                floor/ceiling (this stays silent by design -- callers stream).
            context: Optional dict carried for audit / future analysis. Not
                consumed by the tuner's decisions today.
        """
        outcome_f = max(0.0, min(1.0, float(outcome)))
        ctx = dict(context) if context else {}
        obs = AutoTuneObservation(value=value, outcome=outcome_f, context=ctx)

        with self._lock:
            self._observations.append(obs)
            arm = self._arm_for(value)
            lr = self.conservative_lr if self.n < self.full_threshold else self.full_lr
            # Capture FTRL weight pair around the update so we can emit
            # one row per ``record()`` call to the shared ZIQ FTRL state
            # log (``~/.concinno/ziq_state/<feature>_ftrl.jsonl``). The
            # arm's ``weight()`` reflects the FTRL-Proximal estimate
            # before/after the gradient step; that is the value the
            # downstream FTRL loop (``concinno.ziq.persist.load_ftrl_state``
            # + posterior bookkeeping) wants to observe.
            weight_before = arm.weight()
            arm.update(outcome_f, lr)
            weight_after = arm.weight()
            arm_key = self._key(value)

            if self.kind == "continuous":
                self._update_continuous_estimate()

            if self.auto_persist:
                self._append_to_disk(obs)

        # Emit the FTRL outcome event OUTSIDE the lock so disk I/O
        # never serialises concurrent ``record()`` calls on the same
        # tuner. ``_emit_ftrl_outcome`` is best-effort and silently
        # tolerates persistence layer absence (lazy import) or disk
        # failure — telemetry must not break the cognitive layer.
        self._emit_ftrl_outcome(
            arm_key=arm_key,
            outcome=outcome_f,
            weight_before=weight_before,
            weight_after=weight_after,
            context=ctx,
        )

    def suggest(self, context: Optional[dict[str, Any]] = None) -> Any:
        """Return the currently best-guess value.

        ``context`` is accepted for future per-context routing but is unused in
        v1 -- the tuner is context-agnostic. Callers that want context-aware
        selection should keep a separate tuner per context bucket.
        """
        _ = context  # reserved for future use
        with self._lock:
            regime = self.current_regime()
            # 2026-05-10 cold-start exploration applies in BOTH preset
            # and conservative regimes for discrete targets — without
            # this, a 4-arm bandit whose preset arm gets all the
            # external traffic stays at N_other=0 forever and never
            # reaches the threshold. ``self.n % stride == 0`` triggers
            # at most floor(self.n * cold_start_explore_pct) times in
            # ``self.n`` calls, which is the requested fraction.
            if (
                regime in ("preset", "conservative")
                and self.kind in ("discrete", "boolean")
                and self._choices_list is not None
                and self.cold_start_explore_pct > 0.0
                and self.n > 0
                and is_autotune_enabled()
            ):
                stride = max(1, int(round(1.0 / self.cold_start_explore_pct)))
                if self.n % stride == 0:
                    least = self._least_observed_choice()
                    # Skip exploration when the least-observed arm is
                    # already the preset (no information gain).
                    if least is not None and least != self.preset:
                        return least
            if regime == "preset":
                return self.preset
            if self.kind == "continuous":
                if regime == "conservative":
                    # Shrink toward preset: 70% preset + 30% estimate.
                    return max(
                        self.vmin,
                        min(
                            self.vmax,
                            0.7 * float(self.preset) + 0.3 * self._cont_mean_estimate,
                        ),
                    )
                return self._cont_mean_estimate
            # discrete / boolean. Cold-start exploration was already
            # applied above (covers preset+conservative regimes). The
            # remaining branch picks based on bandit posterior.
            arms = self._arms_sorted_by_reward()
            if not arms:
                return self.preset
            if regime == "conservative":
                return self._conservative_discrete_pick(arms)
            return self._full_discrete_pick(arms)

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-serializable snapshot for diagnostics / tests."""
        with self._lock:
            return {
                "target": self.target,
                "preset": self.preset,
                "kind": self.kind,
                "n": self.n,
                "regime": self.current_regime(),
                "tunable_threshold": self.tunable_threshold,
                "full_threshold": self.full_threshold,
                "suggestion": self.suggest(),
                "arm_rewards": {
                    self._key(v): arm.mean_reward() for v, arm in self._arms.items()
                },
                "arm_counts": {
                    self._key(v): arm.reward_count for v, arm in self._arms.items()
                },
                "continuous_mean_estimate": (
                    self._cont_mean_estimate if self.kind == "continuous" else None
                ),
            }

    # -- Internals ---------------------------------------------------

    def _update_continuous_estimate(self) -> None:
        """Rebuild the outcome-weighted mean anchored by preset.

        ``prior_weight`` shrinks as samples accrue so cold start stays near
        the preset and full regime trusts empirical data.
        """
        prior_weight = max(1, max(0, self.full_threshold - self.n))
        weighted_sum = float(self.preset) * prior_weight + sum(
            float(o.value) * (0.5 + o.outcome) for o in self._observations
        )
        sample_weight = prior_weight + sum(
            0.5 + o.outcome for o in self._observations
        )
        est = weighted_sum / max(sample_weight, 1.0)
        self._cont_mean_estimate = max(self.vmin, min(self.vmax, est))

    @staticmethod
    def _infer_kind(
        preset: Any, choices: Optional[list[Any]],
    ) -> TargetKind:
        if isinstance(preset, bool):
            return "boolean"
        if choices is not None and len(choices) > 0:
            return "discrete"
        if isinstance(preset, (int, float)):
            return "continuous"
        return "discrete"

    def _arm_for(self, value: Any) -> _FTRLArm:
        if self.kind == "continuous":
            return self._cont_arm
        key = self._key(value)
        if key not in self._arms:
            self._arms[key] = _FTRLArm()
        return self._arms[key]

    @staticmethod
    def _key(value: Any) -> Any:
        """Hashable key for arm lookup. Avoids float-key collisions."""
        if isinstance(value, float):
            return round(value, 6)
        return value

    def _arms_sorted_by_reward(self) -> list[tuple[Any, _FTRLArm]]:
        return sorted(
            self._arms.items(),
            key=lambda kv: (kv[1].mean_reward(), kv[1].reward_count),
            reverse=True,
        )

    def _least_observed_choice(self) -> Optional[Any]:
        """Return the choice with the lowest reward_count.

        Used by the 2026-05-10 cold-start exploration path. When the
        target has declared ``choices`` and one of them has never been
        observed (``reward_count == 0``), prefer that one — it gives
        the bandit its first signal on the under-explored arm. When
        all arms have at least one observation, return the one with
        the smallest count. Ties broken by ``choices`` declaration
        order so behaviour is deterministic across runs.

        Returns ``None`` for boolean / unbounded discrete targets.
        """
        if not self._choices_list:
            return None
        # Find the choice with the lowest observed count (zero counts
        # for never-observed arms are best, since we want to fill the
        # gaps in the bandit's information).
        best: Optional[Any] = None
        best_count: float = math.inf
        for c in self._choices_list:
            arm = self._arms.get(self._key(c))
            count = float(arm.reward_count) if arm is not None else 0.0
            if count < best_count:
                best_count = count
                best = c
        return best

    def _conservative_discrete_pick(
        self, arms: list[tuple[Any, _FTRLArm]],
    ) -> Any:
        """Only flip away from preset when evidence is strong.

        Strong = best arm has >=2x samples of preset's arm AND strictly higher
        mean reward. Otherwise keep the preset. Preset never observed is
        treated as the most conservative case — keep the declared default.
        """
        best_key, best_arm = arms[0]
        preset_arm = self._arms.get(self._key(self.preset))
        if preset_arm is None:
            # Preset never observed; do not flip in conservative regime. Full
            # regime will override this via _full_discrete_pick if signal
            # accumulates past full_threshold.
            return self.preset
        if best_arm.mean_reward() <= preset_arm.mean_reward():
            return self.preset
        if best_arm.reward_count < 2 * max(preset_arm.reward_count, 1):
            return self.preset
        return self._unkey(best_key)

    def _full_discrete_pick(
        self, arms: list[tuple[Any, _FTRLArm]],
    ) -> Any:
        """Full FTRL regime: greedy pick on mean reward.

        We intentionally do not inject exploration noise; the cognitive layer
        handles exploration at a higher level (ZIQ feature router decides
        whether to even consult the tuner). Deterministic pick keeps unit tests
        reproducible.
        """
        best_key, _ = arms[0]
        return self._unkey(best_key)

    def _unkey(self, key: Any) -> Any:
        """Inverse of ``_key`` -- recover the canonical representative value."""
        if self.kind == "boolean":
            return bool(key)
        if self._choices_list:
            # Prefer exact match against declared choices so returned value
            # compares equal to what callers passed in (important for enums /
            # string literals).
            for c in self._choices_list:
                if self._key(c) == key:
                    return c
        return key

    # -- Persistence -------------------------------------------------

    def _target_path(self) -> Path:
        return self._store_dir / f"{self.target}.jsonl"

    def _append_to_disk(self, obs: AutoTuneObservation) -> None:
        try:
            self._store_dir.mkdir(parents=True, exist_ok=True)
            rec = {
                "value": obs.value,
                "outcome": obs.outcome,
                "context": obs.context,
            }
            with self._target_path().open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            # Storage hiccup should never break the cognitive layer.
            # Silently degrade to in-memory only for this observation.
            pass

    def _emit_ftrl_outcome(
        self,
        *,
        arm_key: Any,
        outcome: float,
        weight_before: float,
        weight_after: float,
        context: dict[str, Any],
    ) -> None:
        """Append one FTRL outcome event to the shared ZIQ state log.

        Bridges the per-target observation log (``ziq_tuners/<target>.jsonl``,
        kept for cold-load replay) onto the cross-feature FTRL trail at
        ``ziq_state/<target>_ftrl.jsonl``. The latter is the source the
        downstream FTRL posterior + ``load_ftrl_state`` consume — without
        this emit, ``ZIQAutoTuner.record()`` is invisible to the rest of
        ZIQ even when callers wire it correctly.

        Schema mirrors :func:`concinno.ziq.persist.record_ftrl_update`
        exactly: ``{ts, feature, key, weight_before, weight_after,
        signal, posterior_components}``. ``signal`` is the outcome
        centred to ``[-1, 1]`` (matching the existing
        ``agent_invariants_ftrl.jsonl`` event from the 2026-05-07
        smoke-real fixture) so downstream consumers can subtract 0.5
        and double without re-centring.

        Best-effort by contract:
            * Lazy import of :mod:`concinno.ziq.persist` so a stripped
              install (or test that monkeypatches the persist module
              out) does not crash the tuner.
            * Every failure path swallows the exception — the tuner's
              in-memory state remains the source of truth for the
              running process and the unit tests assert that.

        Args:
            arm_key: Hashable arm key as returned by ``self._key()``.
                Serialised via ``str(...)`` because the persist layer
                requires a string key (filesystem-safe + jsonl-stable).
            outcome: The clamped reward in ``[0, 1]`` that drove the
                FTRL update.
            weight_before: Arm's FTRL weight estimate immediately
                before ``arm.update()``.
            weight_after: Arm's FTRL weight estimate immediately after
                ``arm.update()``.
            context: Caller-supplied audit dict copied into
                ``posterior_components`` so downstream consumers can
                trace why a particular update fired.
        """
        if not self.auto_persist:
            return
        try:
            # Lazy import — keeps the autotuner module importable even
            # when the persist sub-module is stubbed out (e.g.
            # ``CONCINNO_ZIQ_PERSIST_DISABLED=1`` integration tests).
            from concinno.ziq import persist as _persist  # noqa: PLC0415
        except ImportError:
            return
        # Centre [0, 1] → [-1, 1] so the on-disk signal matches the
        # convention used by the rest of ZIQ (positive = reward,
        # negative = penalty). Done as ``(outcome - 0.5) * 2`` to
        # preserve exact 0 / ±1 endpoints without floating-point drift
        # at the boundaries.
        signal = (float(outcome) - 0.5) * 2.0
        try:
            _persist.record_ftrl_update(
                self.target,
                str(arm_key),
                weight_before=float(weight_before),
                weight_after=float(weight_after),
                signal=signal,
                posterior_components=context or {},
            )
        except Exception:  # noqa: BLE001
            # The persist layer is itself try/except'd around the disk
            # call, so the only way this raises is a programmer error
            # in the schema. We still swallow — telemetry is best-
            # effort by contract.
            return

    def _load_from_disk(self) -> None:
        path = self._target_path()
        if not path.exists():
            return
        try:
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    value = rec.get("value")
                    outcome = rec.get("outcome", 0.5)
                    context = rec.get("context", {}) or {}
                    obs = AutoTuneObservation(
                        value=value, outcome=float(outcome), context=context,
                    )
                    self._observations.append(obs)
                    arm = self._arm_for(value)
                    lr = (
                        self.conservative_lr
                        if len(self._observations) < self.full_threshold
                        else self.full_lr
                    )
                    arm.update(float(outcome), lr)
            # Rebuild the continuous mean-estimate once from the full
            # reloaded observation set. Doing it inside the per-line loop
            # would cost O(n^2); doing it here is O(n) and matches the
            # post-record() invariant.
            if self.kind == "continuous" and self._observations:
                self._update_continuous_estimate()
        except OSError:
            # If the file is unreadable, start from clean memory state.
            self._observations.clear()
            self._arms.clear()


__all__ = [
    "AutoTuneObservation",
    "AutoTuneRegime",
    "TargetKind",
    "ZIQAutoTuner",
    "is_autotune_enabled",
]
