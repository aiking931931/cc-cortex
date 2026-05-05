"""Tests for the 3.1.3 opt-out wiring fix on stop-gate modules.

Background: each module was hard-coded to enforce, but
``rules/L1/switches.md`` (and several FEATURE_META entries elsewhere
in this codebase) advertised a per-feature toggle. The 2026-04-26
wiring audit confirmed the toggles were never read; these tests pin
the fix so the docs and the code can no longer drift apart silently.

Note: ``excuse_scanner``, ``sedimentation_gate``, and
``git_size_monitor`` were removed in the 4.6.0 KILL 10 cleanup wave;
their per-feature opt-out tests live with their git history.
"""
from __future__ import annotations

import pytest

# ── handoff_claim_guard ─────────────────────────────────────────────


def test_handoff_claim_guard_env_opt_out(monkeypatch):
    """Opt-out skips the git scan even when text contains a claim."""
    monkeypatch.setenv("CONCINNO_HANDOFF_CLAIM_GUARD_DISABLED", "1")

    from concinno.handoff_claim_guard import on_stop

    hook_data = {
        "session_id": "abc123",
        "messages": [
            {"role": "assistant", "content": "已寫入交接"},
        ],
    }
    assert on_stop(hook_data) is None


# ── FEATURE_META coverage ───────────────────────────────────────────


@pytest.mark.parametrize(
    "feature_name",
    [
        "release_authorization",
        "publish_scan_guard",
        "semver_gate",
        "handoff_claim_guard",
    ],
)
def test_feature_meta_entry_exists(feature_name):
    """Each module's opt-out toggle must be advertised in FEATURE_META so
    ``concinno features get <name>`` and the GUI can show / flip it."""
    from concinno.feature_config import FEATURE_META

    meta = FEATURE_META.get(feature_name)
    assert meta is not None, (
        f"FEATURE_META is missing {feature_name!r} — "
        "switches.md / docs would drift from runtime again"
    )
    # Schema must contain the bare-minimum keys used by other audits.
    assert "category" in meta
    assert "description" in meta
    assert "ziq_autotunable" in meta
    assert "cosmetic" in meta
