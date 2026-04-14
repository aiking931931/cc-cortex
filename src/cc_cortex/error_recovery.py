"""cc_cortex.error_recovery -- Four-level error recovery + burst tracking.

@module error_recovery
@responsibility Track consecutive failures and determine recovery level.
    Also provides time-windowed burst detection for patch-loop heuristics
    (used by the PostToolUseFailure hook).
@dependencies cc_cortex.core.state_store
@exports ErrorRecovery
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from cc_cortex.core.state_store import StateStore

_NAMESPACE = "error_recovery"
_BURST_NAMESPACE = "tool_failure_burst"


class ErrorRecovery:
    """4-level error recovery: retry -> degrade -> escalate -> pause.

    Tracks consecutive failures per operation type.
    """

    LEVELS = ("retry", "degrade", "escalate", "pause")

    _THRESHOLDS = {
        "retry": (1, 2),
        "degrade": (3, 4),
        "escalate": (5, 6),
        # 7+ -> pause
    }

    _GUIDANCE = {
        "retry": "Retry the operation. Transient failure likely.",
        "degrade": (
            "Simplify the approach. "
            "Reduce scope or use a fallback strategy."
        ),
        "escalate": (
            "Flag for human review. "
            "Repeated failures indicate a systemic issue."
        ),
        "pause": (
            "Stop this operation. "
            "Too many failures -- manual intervention required."
        ),
    }

    def __init__(
        self,
        cache_dir: str,
        session_id: str,
        *,
        burst_window_minutes: int = 10,
        burst_history_cap: int = 200,
    ) -> None:
        self._store = StateStore(cache_dir)
        self._session_id = session_id
        self._burst_window_minutes = burst_window_minutes
        self._burst_history_cap = burst_history_cap

    def _read(self) -> dict:
        return self._store.read(_NAMESPACE, self._session_id, default={})

    def _write(self, data: dict) -> None:
        self._store.write(_NAMESPACE, self._session_id, data)

    @staticmethod
    def _classify(count: int) -> str:
        """Map failure count to recovery level."""
        if count <= 2:
            return "retry"
        if count <= 4:
            return "degrade"
        if count <= 6:
            return "escalate"
        return "pause"

    def record_failure(self, operation: str, error: str) -> str:
        """Record a failure and return the recovery level.

        Args:
            operation: Operation identifier (e.g. 'lint', 'build').
            error: Error description for audit.

        Returns:
            One of: "retry", "degrade", "escalate", "pause".
        """
        data = self._read()
        ops = data.setdefault("operations", {})
        entry = ops.setdefault(operation, {"count": 0, "errors": []})
        entry["count"] = entry.get("count", 0) + 1
        errors = entry.get("errors", [])
        errors.append(error[:200])
        # Keep last 10 errors to avoid state bloat
        entry["errors"] = errors[-10:]
        self._write(data)
        return self._classify(entry["count"])

    def record_success(self, operation: str) -> None:
        """Reset failure count for an operation."""
        data = self._read()
        ops = data.get("operations", {})
        if operation in ops:
            ops[operation] = {"count": 0, "errors": []}
            self._write(data)

    def recovery_action(self, level: str) -> str:
        """Return actionable guidance for each level."""
        return self._GUIDANCE.get(level, self._GUIDANCE["pause"])

    def status(self) -> dict:
        """Return all tracked operations and their failure counts."""
        data = self._read()
        ops = data.get("operations", {})
        return {
            op: {
                "count": info.get("count", 0),
                "level": self._classify(info.get("count", 0)),
                "last_error": (info.get("errors") or [""])[-1],
            }
            for op, info in ops.items()
        }

    # ── Burst tracking (time-windowed patch-loop detection) ──────

    def _burst_read(self) -> dict:
        return self._store.read(
            _BURST_NAMESPACE, self._session_id, default={"events": []},
        )

    def _burst_write(self, data: dict) -> None:
        self._store.write(_BURST_NAMESPACE, self._session_id, data)

    def record_burst(
        self,
        operation: str,
        category: str,
        *,
        now: datetime | None = None,
    ) -> tuple[int, int]:
        """Append a failure event and return (total_count, consecutive).

        ``consecutive`` counts events matching (operation, category)
        scanning history in reverse, stopping at the first mismatch OR at
        the first entry older than ``burst_window_minutes``. This is the
        B0 patch-loop heuristic migrated from the old ``on_post_tool_failure``
        JSONL scanner — the +1 fix-up is now inside the primitive so
        callers cannot be off by one.

        ``now`` is injectable for deterministic tests.

        Uses ``StateStore.read_modify_write`` so concurrent hook
        invocations cannot clobber the event list.
        """
        ts = now or datetime.now(timezone.utc)
        ts_iso = ts.isoformat()
        cutoff = ts - timedelta(minutes=self._burst_window_minutes)
        cap = self._burst_history_cap

        total_ref = [0]
        consecutive_ref = [0]

        def _apply(data: dict) -> dict:
            events = data.get("events") or []
            events.append({"ts": ts_iso, "op": operation, "cat": category})
            if len(events) > cap:
                events = events[-cap:]

            # Count total (operation+category match, any time)
            total = 0
            for e in events:
                if e.get("op") == operation and e.get("cat") == category:
                    total += 1

            # Count consecutive within window, scanning from newest.
            # Tolerate tz-naive / corrupt entries: skip bad rows instead
            # of raising TypeError (red team #1-H1 — a corrupt state file
            # would otherwise crash the PostToolUseFailure hook).
            consecutive = 0
            for e in reversed(events):
                raw_ts = str(e.get("ts", ""))
                try:
                    e_ts = datetime.fromisoformat(raw_ts)
                except (ValueError, TypeError):
                    continue
                if e_ts.tzinfo is None:
                    e_ts = e_ts.replace(tzinfo=timezone.utc)
                if e_ts < cutoff:
                    break
                if e.get("op") == operation and e.get("cat") == category:
                    consecutive += 1
                else:
                    break

            total_ref[0] = total
            consecutive_ref[0] = consecutive
            data["events"] = events
            return data

        self._store.read_modify_write(
            _BURST_NAMESPACE, self._session_id, _apply,
            default={"events": []},
        )
        return total_ref[0], consecutive_ref[0]

    def burst_status(
        self,
        operation: str,
        category: str,
        *,
        now: datetime | None = None,
    ) -> dict:
        """Return ``{'total': int, 'consecutive': int}`` without mutation.

        ``now`` is injectable so tests can freeze the wall clock (red team
        #1-M4 — CI flakiness on slow runners). Same naive-datetime
        tolerance as :meth:`record_burst`.
        """
        ts_now = now or datetime.now(timezone.utc)
        data = self._burst_read()
        events = data.get("events") or []
        cutoff = ts_now - timedelta(minutes=self._burst_window_minutes)
        total = 0
        for e in events:
            if e.get("op") == operation and e.get("cat") == category:
                total += 1
        consecutive = 0
        for e in reversed(events):
            raw_ts = str(e.get("ts", ""))
            try:
                e_ts = datetime.fromisoformat(raw_ts)
            except (ValueError, TypeError):
                continue
            if e_ts.tzinfo is None:
                e_ts = e_ts.replace(tzinfo=timezone.utc)
            if e_ts < cutoff:
                break
            if e.get("op") == operation and e.get("cat") == category:
                consecutive += 1
            else:
                break
        return {"total": total, "consecutive": consecutive}

    def clear_burst(self, operation: str | None = None) -> None:
        """Clear burst history. All events if ``operation`` is None,
        otherwise only events matching that operation."""
        data = self._burst_read()
        events = data.get("events") or []
        if operation is None:
            data["events"] = []
        else:
            data["events"] = [
                e for e in events if e.get("op") != operation
            ]
        self._burst_write(data)

    @classmethod
    def classify_burst(cls, consecutive: int, total: int) -> str:
        """Classify a burst into a policy verdict.

        Returns one of:
          - ``"escalate"`` — consecutive >= 2 (patch-loop detected)
          - ``"prescribe"`` — total >= 3 (recurring pattern)
          - ``"normal"``   — otherwise

        ``escalate`` takes priority when both thresholds are crossed.
        """
        if consecutive >= 2:
            return "escalate"
        if total >= 3:
            return "prescribe"
        return "normal"
