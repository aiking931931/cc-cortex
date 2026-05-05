"""Live-Opus E2E test for ``RedBlueGreenDispatchGuard.review`` (4.2.4).

Closes Red 2's R2.1 (4.2.3 PM review): existing tests cover parsers
and ``_decide_verdict`` matrix, but none invoked ``review`` end-to-end
with a real model. L0 鐵律 #3 D-dim violation. Skipped by default;
runs only when ``ANTHROPIC_API_KEY`` set AND ``pytest -m live`` given.
"""

from __future__ import annotations

import json
import os

import pytest

from concinno.guards.redblue_green_dispatch_guard import (
    Axis,
    Radius,
    RedBlueGreenDispatchGuard,
    Verdict,
)

pytestmark = pytest.mark.live


class _AnthropicOpusDispatcher:
    """Real Opus dispatcher — Protocol-compatible with ``AgentDispatcher``."""

    def __init__(self, model: str | None = None) -> None:
        import anthropic

        self._client = anthropic.Anthropic()
        # Match concinno.escalation env-override convention.
        self._model = model or os.environ.get(
            "CONCINNO_OPUS_MODEL", "claude-opus-4-7",
        )

    def dispatch(
        self,
        prompt: str,
        *,
        model: str = "opus",  # noqa: ARG002 — Protocol parity
        role: str,  # noqa: ARG002
    ) -> str:
        resp = self._client.messages.create(
            model=self._model,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        chunks: list[str] = []
        for block in resp.content:
            text = getattr(block, "text", None)
            if text:
                chunks.append(text)
        return "".join(chunks)


@pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="live test requires ANTHROPIC_API_KEY env var",
)
def test_rbg_review_end_to_end_medium_radius() -> None:
    """One real-Opus E2E happy path covering R2.1's gap.

    Medium radius dispatches 1 red (no blue, no green) which bounds
    API spend (~1024 max tokens × 1 call). Verifies a real
    :class:`ReviewVerdict` returns with a valid 5-state verdict and
    all 5 axes from ``rules/L1/redteam.md`` show up across findings.
    """
    guard = RedBlueGreenDispatchGuard()
    verdict = guard.review(
        decision_context=(
            "Refactor a 50-LOC helper into 2 functions for clarity. "
            "No external callers. Pure cosmetic split."
        ),
        radius=Radius.MEDIUM,
        dispatcher=_AnthropicOpusDispatcher(),
        original_intent="Improve readability of a small utility helper.",
    )

    assert verdict is not None
    assert verdict.radius == Radius.MEDIUM
    assert verdict.spawn_count == 1

    valid_states = {
        Verdict.ACCEPT, Verdict.ACCEPT_DOWNGRADE,
        Verdict.REJECT, Verdict.HOLD, Verdict.REQUERY,
    }
    assert verdict.verdict in valid_states, (
        f"unexpected verdict state: {verdict.verdict!r}"
    )

    all_findings = list(verdict.findings_accepted) + [
        f for f, _ in verdict.findings_rejected_framing
    ]
    seen_axes = {f.axis for f in all_findings}
    expected_axes = {
        Axis.REAL_DONE, Axis.WIRED, Axis.FUNCTIONAL,
        Axis.AI_CAPABILITY, Axis.UX_FRICTION,
    }
    missing = expected_axes - seen_axes
    assert not missing, (
        f"missing axes from live red review: "
        f"{sorted(a.value for a in missing)} "
        f"(seen: {sorted(a.value for a in seen_axes)}; "
        f"dump={json.dumps([f.axis.value for f in all_findings])})"
    )
