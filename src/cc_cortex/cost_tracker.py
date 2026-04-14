"""cc_cortex.cost_tracker -- Per-session token and cost tracking.

@module cost_tracker
@responsibility Track token consumption, estimate cost, enforce budget
@dependencies cc_cortex.core.state_store
@exports CostTracker
"""

from __future__ import annotations

from cc_cortex.core.state_store import StateStore

_NAMESPACE = "cost_tracker"

# Approximate pricing (USD per token)
_INPUT_PRICE_PER_TOKEN = 3.0 / 1_000_000   # $3 / 1M tokens
_OUTPUT_PRICE_PER_TOKEN = 15.0 / 1_000_000  # $15 / 1M tokens


class CostTracker:
    """Track token consumption and estimated cost per session.

    Adds budget ceiling and alert mechanisms on top of raw token counts.
    """

    def __init__(
        self,
        cache_dir: str,
        session_id: str,
        budget_usd: float = 5.0,
    ) -> None:
        self._store = StateStore(cache_dir)
        self._session_id = session_id
        self._budget_usd = budget_usd

    def _read(self) -> dict:
        return self._store.read(_NAMESPACE, self._session_id, default={})

    def _write(self, data: dict) -> None:
        self._store.write(_NAMESPACE, self._session_id, data)

    def record(self, input_tokens: int, output_tokens: int) -> None:
        """Record token usage for current operation."""
        data = self._read()
        data["total_input"] = data.get("total_input", 0) + input_tokens
        data["total_output"] = data.get("total_output", 0) + output_tokens
        self._write(data)

    def update_snapshot(
        self,
        cumulative_input: int,
        cumulative_output: int,
    ) -> tuple[int, int]:
        """Feed a cumulative token snapshot, record the positive delta.

        Callers (e.g. PostToolUse hooks) observe the running total of
        context/output tokens from the API ``usage`` field. This helper
        computes the delta against the last snapshot we stored and feeds
        that delta to :meth:`record`. A monotonic-reset scenario
        (snapshot smaller than prior — autocompact, model switch) is
        treated as "start fresh from here", recording zero for this tick.

        Returns:
            ``(delta_input, delta_output)`` that was actually recorded.
        """
        data = self._read()
        prior_in = data.get("snapshot_input", 0)
        prior_out = data.get("snapshot_output", 0)

        if cumulative_input < prior_in or cumulative_output < prior_out:
            # Reset detected — don't record spurious negative delta
            delta_in = delta_out = 0
        else:
            delta_in = cumulative_input - prior_in
            delta_out = cumulative_output - prior_out
            data["total_input"] = data.get("total_input", 0) + delta_in
            data["total_output"] = data.get("total_output", 0) + delta_out

        data["snapshot_input"] = cumulative_input
        data["snapshot_output"] = cumulative_output
        self._write(data)
        return delta_in, delta_out

    def stats(self) -> dict:
        """Return usage statistics.

        Returns:
            Dict with keys: total_input, total_output, estimated_usd,
            budget_usd, percent_used.
        """
        data = self._read()
        total_in = data.get("total_input", 0)
        total_out = data.get("total_output", 0)
        estimated = (
            total_in * _INPUT_PRICE_PER_TOKEN
            + total_out * _OUTPUT_PRICE_PER_TOKEN
        )
        percent = round(estimated / self._budget_usd * 100, 1) if self._budget_usd > 0 else 0.0
        return {
            "total_input": total_in,
            "total_output": total_out,
            "estimated_usd": round(estimated, 6),
            "budget_usd": self._budget_usd,
            "percent_used": percent,
        }

    def is_over_budget(self) -> bool:
        """Whether estimated cost exceeds budget."""
        return self.stats()["percent_used"] >= 100.0

    def alert_message(self) -> str | None:
        """Return alert if >80% budget used, None otherwise."""
        s = self.stats()
        if s["percent_used"] >= 80.0:
            return (
                f"Cost alert: ${s['estimated_usd']:.3f} / "
                f"${s['budget_usd']:.2f} ({s['percent_used']}% used)"
            )
        return None
