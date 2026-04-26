"""Tests for the 3.1.3 opt-out wiring fix on excuse_scanner /
sedimentation_gate / handoff_claim_guard / git_size_monitor.

Background: each of those modules was hard-coded to enforce, but
``rules/L1/switches.md`` (and several FEATURE_META entries elsewhere
in this codebase) advertised a per-feature toggle. The 2026-04-26
wiring audit confirmed the toggles were never read; these tests pin
the fix so the docs and the code can no longer drift apart silently.
"""
from __future__ import annotations

from pathlib import Path

import pytest


def _temp_project_with_pack(tmp_path: Path) -> Path:
    """Create a project root containing an empty .git/objects/pack/ dir.

    The pack-byte total is monkey-patched at the call site so we don't
    actually allocate multi-GB sparse files (which fails on CI runners
    with tight tmpfs budgets).
    """
    git_dir = tmp_path / ".git" / "objects" / "pack"
    git_dir.mkdir(parents=True)
    return tmp_path


def _patch_pack_bytes(monkeypatch, total_bytes: int) -> None:
    """Force ``_pack_bytes`` to report ``total_bytes`` regardless of
    actual filesystem state."""
    import concinno.git_size_monitor as gsm

    monkeypatch.setattr(gsm, "_pack_bytes", lambda _git_dir: total_bytes)


# ── git_size_monitor ────────────────────────────────────────────────


def test_git_size_monitor_default_warns(tmp_path, monkeypatch):
    """Without opt-out, a 6 GB pack-byte total triggers a warning string."""
    project = _temp_project_with_pack(tmp_path)
    monkeypatch.delenv("CONCINNO_GIT_SIZE_MONITOR_DISABLED", raising=False)
    monkeypatch.delenv("CC_GIT_HEALTH_DISABLED", raising=False)
    _patch_pack_bytes(monkeypatch, int(6 * 1024 ** 3))

    from concinno.git_size_monitor import check_git_size

    msg = check_git_size(str(project))
    assert msg is not None
    assert "git_size_monitor" in msg


def test_git_size_monitor_env_opt_out(tmp_path, monkeypatch):
    """``CONCINNO_GIT_SIZE_MONITOR_DISABLED=1`` suppresses the warning."""
    project = _temp_project_with_pack(tmp_path)
    monkeypatch.setenv("CONCINNO_GIT_SIZE_MONITOR_DISABLED", "1")
    _patch_pack_bytes(monkeypatch, int(6 * 1024 ** 3))

    from concinno.git_size_monitor import check_git_size

    assert check_git_size(str(project)) is None


def test_git_size_monitor_legacy_alias(tmp_path, monkeypatch):
    """The legacy ``CC_GIT_HEALTH_DISABLED`` alias (documented in
    switches.md row #24) is honoured for backward compatibility."""
    project = _temp_project_with_pack(tmp_path)
    monkeypatch.delenv("CONCINNO_GIT_SIZE_MONITOR_DISABLED", raising=False)
    monkeypatch.setenv("CC_GIT_HEALTH_DISABLED", "true")
    _patch_pack_bytes(monkeypatch, int(6 * 1024 ** 3))

    from concinno.git_size_monitor import check_git_size

    assert check_git_size(str(project)) is None


# ── excuse_scanner ──────────────────────────────────────────────────


def _hook_data_with_excuse() -> dict:
    return {
        "messages": [
            {
                "role": "assistant",
                "content": "這個 lint 不是我造成的，先跳過。",
            },
        ],
    }


def test_excuse_scanner_default_blocks(monkeypatch):
    monkeypatch.delenv("CONCINNO_EXCUSE_SCANNER_DISABLED", raising=False)

    from concinno.excuse_scanner import on_stop

    assert (on_stop(_hook_data_with_excuse()) or "").startswith("EXCUSE_BLOCK:")


def test_excuse_scanner_env_opt_out(monkeypatch):
    monkeypatch.setenv("CONCINNO_EXCUSE_SCANNER_DISABLED", "1")

    from concinno.excuse_scanner import on_stop

    assert on_stop(_hook_data_with_excuse()) is None


# ── sedimentation_gate ──────────────────────────────────────────────


def test_sedimentation_gate_env_opt_out(monkeypatch, tmp_path):
    """Even when corrections-without-sedimentation conditions exist,
    the env opt-out short-circuits to None."""
    monkeypatch.setenv("CONCINNO_SEDIMENTATION_GATE_DISABLED", "1")
    monkeypatch.setenv("CC_SESSION_ID", "anything")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))

    from concinno.sedimentation_gate import on_stop

    assert on_stop({}) is None


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
        "excuse_scanner",
        "sedimentation_gate",
        "handoff_claim_guard",
        "git_size_monitor",
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
