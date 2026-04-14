"""Adaptive thresholds — dynamically adjust sentinel/guard thresholds.

@module cognitive.thresholds
@responsibility Learn from session history, adjust thresholds within bounds
@dependencies cognitive._base, cognitive.session_profile
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional

from ._base import (
    DEFAULT_THRESHOLDS,
    THRESHOLD_BOUNDS,
    THRESHOLDS_FILE,
    cognitive_dir,
    read_json,
    write_json,
)
from .session_profile import SessionProfile


class AdaptiveThresholds:
    """Dynamically adjusts sentinel/guard thresholds based on session history.

    Instead of static thresholds, learns from user behavior patterns:
    - Users who write lots of files → raise scope_creep threshold
    - Sessions with heavy reading → raise paralysis threshold
    - Frequent corrections on a threshold → loosen it

    All adjustments are bounded by THRESHOLD_BOUNDS for safety.
    """

    def __init__(self, base_dir: Optional[str] = None):
        self._dir = cognitive_dir(base_dir)
        self._path = os.path.join(self._dir, THRESHOLDS_FILE)

    def get(self, key: str) -> int:
        """Get current adaptive threshold value."""
        data = read_json(self._path)
        thresholds = data.get("thresholds", {})
        return thresholds.get(key, DEFAULT_THRESHOLDS.get(key, 0))

    def get_all(self) -> dict[str, int]:
        """Get all current thresholds (adaptive merged with defaults)."""
        result = dict(DEFAULT_THRESHOLDS)
        data = read_json(self._path)
        result.update(data.get("thresholds", {}))
        return result

    def adjust(self, key: str, delta: int, reason: str = "") -> int:
        """Adjust a threshold by delta, respecting bounds.

        Args:
            key: Threshold key.
            delta: Amount to adjust (positive = loosen, negative = tighten).
            reason: Why the adjustment was made.

        Returns:
            New threshold value.
        """
        data = read_json(self._path)
        thresholds = data.get("thresholds", {})
        history = data.get("adjustment_history", [])

        current = thresholds.get(key, DEFAULT_THRESHOLDS.get(key, 0))
        new_val = current + delta

        # Apply bounds
        if key in THRESHOLD_BOUNDS:
            lo, hi = THRESHOLD_BOUNDS[key]
            new_val = max(lo, min(hi, new_val))

        thresholds[key] = new_val
        history.append(
            {
                "key": key,
                "old": current,
                "new": new_val,
                "delta": delta,
                "reason": reason[:200],
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        )

        # Keep history manageable
        if len(history) > 200:
            history = history[-200:]

        data["thresholds"] = thresholds
        data["adjustment_history"] = history
        data["last_updated"] = datetime.now(timezone.utc).isoformat()
        write_json(self._path, data)
        return new_val

    def learn_from_profiles(self, base_dir: Optional[str] = None) -> list[str]:
        """Analyze session profiles and adjust thresholds.

        Returns list of adjustments made.
        """
        profiles = SessionProfile.load_history(base_dir or self._dir, limit=30)
        if len(profiles) < 5:
            return []  # Not enough data

        adjustments: list[str] = []

        # 1. Average files touched → adjust scope threshold
        avg_files = sum(p.get("files_touched_count", 0) for p in profiles) / len(profiles)
        current_scope = self.get("sentinel_scope")
        scope_max = THRESHOLD_BOUNDS["sentinel_scope"][1]
        if avg_files > current_scope * 0.8 and current_scope < scope_max:
            new_val = self.adjust(
                "sentinel_scope",
                2,
                f"avg_files={avg_files:.0f}",
            )
            adjustments.append(
                f"sentinel_scope: {current_scope} → {new_val} (avg files: {avg_files:.0f})"
            )

        # 2. Average read/write ratio → adjust paralysis threshold
        ratios = [p.get("read_write_ratio", 1.0) for p in profiles]
        avg_ratio = sum(ratios) / len(ratios)
        current_paralysis = self.get("sentinel_paralysis")
        par_max = THRESHOLD_BOUNDS["sentinel_paralysis"][1]
        if avg_ratio > 3.0 and current_paralysis < par_max:
            new_val = self.adjust(
                "sentinel_paralysis",
                2,
                f"avg_rw_ratio={avg_ratio:.1f}",
            )
            adjustments.append(
                f"sentinel_paralysis: {current_paralysis} → {new_val} (ratio: {avg_ratio:.1f})"
            )

        # 3. Session type patterns → adjust tidy thresholds
        type_dist = SessionProfile.get_type_distribution(base_dir or self._dir, limit=30)
        if type_dist.get("refactor", 0) + type_dist.get("feature", 0) > len(profiles) * 0.6:
            current_tidy = self.get("tidy_code_lines")
            if current_tidy < THRESHOLD_BOUNDS["tidy_code_lines"][1]:
                new_val = self.adjust("tidy_code_lines", 5, "heavy refactor/feature sessions")
                adjustments.append(f"tidy_code_lines: {current_tidy} → {new_val}")

        return adjustments

    def reset(self, key: Optional[str] = None) -> None:
        """Reset threshold(s) to defaults."""
        data = read_json(self._path)
        thresholds = data.get("thresholds", {})
        if key:
            if key in thresholds:
                del thresholds[key]
        else:
            thresholds = {}
        data["thresholds"] = thresholds
        write_json(self._path, data)

    def status(self) -> dict:
        """Get threshold status: current values, defaults, deviations."""
        current = self.get_all()
        result = {}
        for key, val in current.items():
            default = DEFAULT_THRESHOLDS.get(key, val)
            result[key] = {
                "current": val,
                "default": default,
                "deviation": val - default,
                "bounds": THRESHOLD_BOUNDS.get(key, (None, None)),
            }
        return result
