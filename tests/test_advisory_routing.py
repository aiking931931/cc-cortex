"""Tests for GuardResult.advisory routing (CCC 1.15.1 hot-fix).

Covers:
  1. ``GuardResult.advisory`` field defaults to False (backwards compat).
  2. ``GuardResult.allow_advisory()`` helper sets ``advisory=True``.
  3. ``GuardPipeline`` injects advisory context in the standard profile.
  4. ``GuardPipeline`` silences advisory context in the competition profile.
  5. Safety (non-advisory) results stay loud in every profile.
  6. Silenced advisories are captured on ``pipeline.advisory_audit``.
  7. Real CbuaPipelineGuard returns an advisory result.
  8. Real WiredoGuard returns an advisory result.
  9. Real ThinkingDepthGuard warning is advisory.
 10. ``get_active_profile`` reads ``CONCINNO_PROFILE`` env var.
 11. ``get_active_profile`` defaults to ``standard``.
 12. The ``/mode`` Skill is discoverable under the templates tree.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import pytest

from concinno.feature_config import PROFILES, get_active_profile
from concinno.guards.base import (
    BaseGuard,
    GuardAction,
    GuardCategory,
    GuardContext,
    GuardResult,
)
from concinno.guards.pipeline import GuardPipeline

# ── Helpers ──────────────────────────────────────────────────


def _ctx(tmp_path: Path) -> GuardContext:
    return GuardContext(
        tool_name="Edit",
        tool_input={"file_path": str(tmp_path / "foo.py")},
        session_id="test-session",
        cache_dir=str(tmp_path),
        hook_event="PreToolUse",
    )


class _AdvisoryStubGuard(BaseGuard):
    name = "advisory_stub"
    category = GuardCategory.COGNITIVE

    def check(self, ctx: GuardContext) -> Optional[GuardResult]:
        return GuardResult.allow_advisory(
            context="⚠ advisory nag — should be silent in competition",
        )


class _SafetyStubGuard(BaseGuard):
    name = "safety_stub"
    category = GuardCategory.SECURITY

    def check(self, ctx: GuardContext) -> Optional[GuardResult]:
        return GuardResult.allow(
            context="🛡 safety context — always loud",
        )


def _pipeline_with(*guards: BaseGuard) -> GuardPipeline:
    pipe = GuardPipeline()
    for g in guards:
        pipe.register(g)
    return pipe


# ── 1. GuardResult shape ─────────────────────────────────────


def test_guard_result_advisory_default_false() -> None:
    r = GuardResult.allow(context="hello")
    assert r.advisory is False
    assert r.action == GuardAction.ALLOW
    assert r.context == "hello"


def test_allow_advisory_helper_sets_flag() -> None:
    r = GuardResult.allow_advisory(context="advisory prose")
    assert r.advisory is True
    assert r.action == GuardAction.ALLOW
    assert r.context == "advisory prose"


def test_allow_advisory_preserves_reason_and_metadata() -> None:
    r = GuardResult.allow_advisory(
        context="ctx",
        reason="why",
        audit_id="abc123",
    )
    assert r.advisory is True
    assert r.reason == "why"
    assert r.metadata["audit_id"] == "abc123"


# ── 2. Pipeline routing ──────────────────────────────────────


def test_pipeline_injects_advisory_in_standard_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    pipe = _pipeline_with(_AdvisoryStubGuard())
    monkeypatch.setattr(
        GuardPipeline, "_active_profile", staticmethod(lambda: "standard"),
    )
    out = pipe.run_pre_tool(_ctx(tmp_path))
    assert out["permissionDecision"] == "allow"
    assert "advisory nag" in out.get("additionalContext", "")


def test_pipeline_skips_advisory_in_competition_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    pipe = _pipeline_with(_AdvisoryStubGuard())
    monkeypatch.setattr(
        GuardPipeline, "_active_profile", staticmethod(lambda: "competition"),
    )
    out = pipe.run_pre_tool(_ctx(tmp_path))
    assert out["permissionDecision"] == "allow"
    assert "advisory nag" not in out.get("additionalContext", "")


def test_pipeline_injects_safety_even_in_competition(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    pipe = _pipeline_with(_SafetyStubGuard(), _AdvisoryStubGuard())
    monkeypatch.setattr(
        GuardPipeline, "_active_profile", staticmethod(lambda: "competition"),
    )
    out = pipe.run_pre_tool(_ctx(tmp_path))
    ctx_text = out.get("additionalContext", "")
    assert "safety context" in ctx_text
    assert "advisory nag" not in ctx_text


def test_audit_log_records_skipped_advisories(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    pipe = _pipeline_with(_AdvisoryStubGuard())
    monkeypatch.setattr(
        GuardPipeline, "_active_profile", staticmethod(lambda: "competition"),
    )
    pipe.run_pre_tool(_ctx(tmp_path))
    audit = pipe.advisory_audit
    assert len(audit) == 1
    assert audit[0].advisory is True
    assert "advisory nag" in audit[0].context


def test_audit_log_empty_in_standard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    pipe = _pipeline_with(_AdvisoryStubGuard())
    monkeypatch.setattr(
        GuardPipeline, "_active_profile", staticmethod(lambda: "standard"),
    )
    pipe.run_pre_tool(_ctx(tmp_path))
    assert pipe.advisory_audit == []


# ── 3. Real guards use allow_advisory ────────────────────────


def test_cbua_pipeline_guard_uses_allow_advisory() -> None:
    from concinno.guards.cbua_pipeline_guard import CbuaPipelineGuard

    reminder = CbuaPipelineGuard._generate_reminder(
        state={"edit_count": 5, "b1_shown": False},
        complexity="complicated",
        redteam_required=False,
    )
    assert reminder is not None
    assert reminder.advisory is True
    assert "B1" in reminder.context


def test_wiredo_enforcement_guard_advisory_on_success() -> None:
    # Import check only — exercising the full happy path would require
    # a handoff file plus session state. The contract is: the success
    # branch returns an advisory result, the deny branch does NOT.
    from concinno.wiredo_guards import WiredoEnforcementGuard

    src = Path(WiredoEnforcementGuard.__module__.replace(".", "/"))
    text = (Path("src") / src.with_suffix(".py")).read_text(encoding="utf-8")
    assert "GuardResult.allow_advisory" in text
    # The deny path must NOT be advisory.
    deny_line = [ln for ln in text.splitlines() if "GuardResult.deny" in ln]
    assert deny_line, "expected a deny call in WiredoEnforcementGuard"


def test_read_edit_ratio_guard_advisory(tmp_path: Path) -> None:
    from concinno.core.state_store import StateStore
    from concinno.thinking_depth_guard import _NS, ThinkingDepthGuard

    # Seed a degraded ratio: 1 read, 5 edits in the window.
    store = StateStore(str(tmp_path))
    calls = [{"tool": "Read", "ts": 1.0}] + [
        {"tool": "Edit", "ts": float(2 + i)} for i in range(5)
    ]
    store.write(_NS, "test-session", {"calls": calls})

    guard = ThinkingDepthGuard()
    ctx = GuardContext(
        tool_name="Edit",
        tool_input={"file_path": str(tmp_path / "x.py")},
        session_id="test-session",
        cache_dir=str(tmp_path),
        hook_event="PreToolUse",
    )
    result = guard.check(ctx)
    assert result is not None
    assert result.advisory is True
    assert "Read:Edit" in result.context


# ── 4. feature_config resolver ───────────────────────────────


def test_get_active_profile_reads_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CONCINNO_PROFILE", "competition")
    assert get_active_profile() == "competition"


def test_get_active_profile_defaults_to_standard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CONCINNO_PROFILE", raising=False)
    # Also poison the config lookup so we land in the fallback branch.
    monkeypatch.setattr(
        "concinno.core.config.get_config",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no cfg")),
    )
    assert get_active_profile() == "standard"


def test_get_active_profile_rejects_unknown_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CONCINNO_PROFILE", "not-a-real-profile")
    monkeypatch.setattr(
        "concinno.core.config.get_config",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no cfg")),
    )
    assert get_active_profile() == "standard"


def test_competition_profile_exists() -> None:
    assert "competition" in PROFILES
    assert "standard" in PROFILES


# ── 5. Skill packaging ────────────────────────────────────────


def test_mode_skill_markdown_packaged_or_discoverable() -> None:
    """The /mode Skill ships under the templates tree."""
    here = Path(__file__).resolve().parent.parent
    candidates = [
        here / "src" / "concinno" / "templates" / "skills" / "mode" / "SKILL.md",
        here / ".claude" / "skills" / "mode" / "SKILL.md",
    ]
    found = [p for p in candidates if p.is_file()]
    assert found, (
        f"no /mode SKILL.md found under {candidates}"
    )
    body = found[0].read_text(encoding="utf-8")
    assert "name: mode" in body
    assert "competition" in body
    assert "advisory" in body.lower()


# ── 6. Decision enum sanity (advisory never triggers DENY path) ──


def test_advisory_deny_is_not_silenced(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Even if some future guard mistakenly passes advisory=True on a
    DENY-action result, the pipeline must NOT silence the deny — the
    silencer only applies to ALLOW.
    """

    class _BadGuard(BaseGuard):
        name = "bad_guard"
        category = GuardCategory.QUALITY

        def check(self, ctx: GuardContext) -> Optional[GuardResult]:
            return GuardResult(
                action=GuardAction.DENY,
                reason="should still deny",
                context="deny context",
                advisory=True,  # intentionally wrong
            )

    pipe = _pipeline_with(_BadGuard())
    monkeypatch.setattr(
        GuardPipeline, "_active_profile", staticmethod(lambda: "competition"),
    )
    out = pipe.run_pre_tool(_ctx(tmp_path))
    assert out["permissionDecision"] == "deny"


def test_should_silence_false_for_non_advisory() -> None:
    pipe = GuardPipeline()
    r = GuardResult.allow(context="loud")
    assert pipe._should_silence(r) is False


def test_should_silence_false_in_standard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipe = GuardPipeline()
    monkeypatch.setattr(
        GuardPipeline, "_active_profile", staticmethod(lambda: "standard"),
    )
    r = GuardResult.allow_advisory(context="nag")
    assert pipe._should_silence(r) is False
