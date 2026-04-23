"""concinno.ziq_autotune_loop — Outcome-driven preset cascade updater.

@module ziq_autotune_loop
@responsibility Implement the narrower-scope v4 autotune loop that
    walks :data:`concinno.feature_config.FEATURE_META`, skips entries
    flagged ``cosmetic`` or ``ziq_autotunable=False``, asks
    :class:`concinno.ziq_autotuner.ZIQAutoTuner` for a current
    suggestion, and — when the delta is significant — writes the new
    value back into the active preset's ``summary`` via
    :func:`concinno.preset_cascade.set_preset_value`.
@dependencies
    concinno.feature_config, concinno.ziq_autotune_registry,
    concinno.ziq_autotuner, concinno.preset_cascade
@exports AuditEntry, ZIQAutoTuneLoop, significant_delta

Design notes:

* The loop is **passive** — it does not collect outcome signal itself.
  :meth:`ZIQAutoTuneLoop.tick` takes a ``context`` dict that callers
  fill with whatever per-turn / per-session telemetry is available.
  Missing signal → skip the target (the tuner's own history is the
  long-term memory).

* **Non-destructive** — ``ziq_autotunable=False`` and ``cosmetic=True``
  are hard exclusions. A target with both missing falls back to the
  registry's own record: only FEATURE_META keys that match a
  registered ``TUNABLE_REGISTRY`` entry will ever be touched.

* **Audit JSONL** at ``~/.concinno/ziq_autotune_log.jsonl`` — one line
  per write-back, append-only, never edited. Callers can tail it to
  see what the loop is changing.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_LOG = logging.getLogger("concinno.ziq_autotune_loop")


@dataclass
class AuditEntry:
    """One write-back audit record for the JSONL log."""

    ts: str  # ISO-8601 UTC
    key: str  # dotted feature.param
    old: Any
    new: Any
    reason: str
    alpha_t: float
    origin: tuple[str, ...]

    def as_json_line(self) -> str:
        data = asdict(self)
        data["origin"] = list(self.origin)
        return json.dumps(data, ensure_ascii=False, default=_json_default)


def _json_default(o: Any) -> Any:
    """Fallback encoder for types pydantic/json don't handle natively."""
    if isinstance(o, (set, frozenset)):
        return list(o)
    return str(o)


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def significant_delta(new: Any, old: Any, min_frac: float = 0.05) -> bool:
    """Return True when ``new`` is different enough from ``old`` to write.

    * Identical values → False.
    * Different types → True (type change is always significant).
    * Numeric → ``|new - old| / max(|old|, 1)`` must meet ``min_frac``.
    * Bool / str / anything else → True when not equal.
    """
    if new == old:
        return False
    if type(new) is not type(old):
        return True
    if isinstance(new, (int, float)) and isinstance(old, (int, float)):
        denom = max(abs(float(old)), 1.0)
        return abs(float(new) - float(old)) / denom >= min_frac
    return True


def _default_log_path() -> Path:
    """JSONL audit log path (override via ``CONCINNO_ZIQ_AUTOTUNE_LOG``)."""
    override = os.environ.get("CONCINNO_ZIQ_AUTOTUNE_LOG", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".concinno" / "ziq_autotune_log.jsonl"


def _split_dotted(dotted: str) -> tuple[str, str]:
    feature, _, param = dotted.partition(".")
    return feature, param if param else "enabled"


class ZIQAutoTuneLoop:
    """Outcome-driven cascade updater — one tick per caller invocation.

    Args:
        log_path: Override JSONL audit path. Default
            ``~/.concinno/ziq_autotune_log.jsonl``.
        min_delta_frac: Minimum fractional change to trigger a
            write-back for numeric targets. Default 0.05 (5%).
    """

    def __init__(
        self,
        *,
        log_path: Path | None = None,
        min_delta_frac: float = 0.05,
    ) -> None:
        self._log_path = log_path or _default_log_path()
        self._min_delta_frac = float(min_delta_frac)

    # -- Public API --------------------------------------------------

    def tick(self, context: dict[str, Any] | None = None) -> list[AuditEntry]:
        """Walk FEATURE_META ∩ TUNABLE_REGISTRY and propagate ZIQ updates.

        Args:
            context: Opaque telemetry dict. Reserved for future per-tick
                scoring; the v4 loop itself does not consume context and
                relies on each tuner's own accumulated history.

        Returns:
            List of :class:`AuditEntry` for every key whose preset value
            was rewritten this tick.
        """
        _ = context  # reserved
        try:
            from concinno.feature_config import FEATURE_META
            from concinno.preset_cascade import (
                get_active_preset,
                get_effective_value,
                set_preset_value,
            )
            from concinno.ziq_autotune_registry import (
                TUNABLE_REGISTRY,
                get_tuner,
            )
        except Exception as exc:
            _LOG.warning("ziq_autotune_loop tick imports failed: %s", exc)
            return []

        active_preset = get_active_preset()
        changes: list[AuditEntry] = []

        # TUNABLE_REGISTRY ids mirror source paths ("delivery.gate.max_iterations")
        # while FEATURE_META keys use a flat name ("delivery_gate"). Build a
        # lookup that matches a (feature, param) pair to either form — either
        # "feature.param" or any registry id whose trailing segment equals param
        # and prefix equals feature-with-dots.
        def _resolve_registry_id(feature: str, param: str) -> str | None:
            direct = f"{feature}.{param}"
            if direct in TUNABLE_REGISTRY:
                return direct
            flat_feat = feature.replace("_", "")
            for tid in TUNABLE_REGISTRY:
                # Registry ids use dots; compare ending and flat-feature prefix.
                if not tid.endswith("." + param):
                    continue
                head = tid.rsplit(".", 1)[0]
                if head.replace(".", "").replace("_", "") == flat_feat:
                    return tid
            return None

        for feature, meta in FEATURE_META.items():
            # ziq_autotunable / cosmetic — honest honoring.
            if not meta.get("ziq_autotunable", False):
                continue
            if meta.get("cosmetic", False):
                continue
            # Walk declared params + the implicit ``enabled`` key.
            param_keys: list[str] = ["enabled"]
            param_keys.extend(meta.get("params", {}).keys())
            for param in param_keys:
                dotted = f"{feature}.{param}"
                registry_id = _resolve_registry_id(feature, param)
                if registry_id is None:
                    continue
                try:
                    tuner = get_tuner(registry_id)
                except Exception as exc:
                    _LOG.warning("tuner %s load failed: %s", registry_id, exc)
                    continue
                if tuner.current_regime() == "preset":
                    continue
                try:
                    suggested = tuner.suggest()
                except Exception as exc:
                    _LOG.warning("tuner %s suggest failed: %s", dotted, exc)
                    continue
                current_value, current_origin = get_effective_value(dotted)
                if not significant_delta(
                    suggested, current_value, self._min_delta_frac
                ):
                    continue
                ok = set_preset_value(
                    active_preset,
                    dotted,
                    suggested,
                    origin=("ziq", "autotune", tuner.current_regime()),
                )
                if not ok:
                    _LOG.warning(
                        "set_preset_value rejected %s -> %s (preset=%s)",
                        dotted, suggested, active_preset,
                    )
                    continue
                alpha = _estimate_alpha_t(tuner)
                entry = AuditEntry(
                    ts=_now_iso(),
                    key=dotted,
                    old=current_value,
                    new=suggested,
                    reason=f"ziq regime={tuner.current_regime()}",
                    alpha_t=alpha,
                    origin=("ziq", "autotune", tuner.current_regime()),
                )
                self._append_log(entry)
                changes.append(entry)
        return changes

    # -- Internals ---------------------------------------------------

    def _append_log(self, entry: AuditEntry) -> None:
        try:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            with self._log_path.open("a", encoding="utf-8") as fh:
                fh.write(entry.as_json_line() + "\n")
        except OSError as exc:
            _LOG.warning("ziq_autotune_log append failed: %s", exc)


def _estimate_alpha_t(tuner: Any) -> float:
    """Rough alpha_t estimate from the tuner's observation count.

    Returns 1.0 while in the ``preset`` regime (purely conservative),
    shrinks linearly through the ``conservative`` regime, and approaches
    0.0 in ``full``.
    """
    try:
        n = int(getattr(tuner, "n", 0))
        t = int(getattr(tuner, "tunable_threshold", 300))
        f = int(getattr(tuner, "full_threshold", 500))
    except Exception:
        return 0.5
    if n <= t:
        return 1.0
    if n >= f:
        return 0.0
    # Linear interpolation in the conservative band.
    return max(0.0, min(1.0, 1.0 - (n - t) / max(f - t, 1)))


__all__ = [
    "AuditEntry",
    "ZIQAutoTuneLoop",
    "significant_delta",
]
