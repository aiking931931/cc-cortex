"""Tests for concinno.observability.token_audit (W3, 4.5.0).

Cleanroom — covers core ``TokenAudit`` recorder, ``OverheadTracker``
analyser, ``ArchiveAdvisor`` ZIQ FTRL learner, and the ``concinno
token-audit`` CLI subcommand. ≥25 tests.

Every test isolates ``Path.home()`` via ``monkeypatch.setenv("HOME",
tmp_path)`` (Windows: ``USERPROFILE``) so the audit jsonl + advisor
state files never escape ``tmp_path``.
"""

from __future__ import annotations

import argparse
import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from concinno.observability.token_audit import (
    ArchiveAdvisor,
    ArchiveCandidate,
    TokenAudit,
)
from concinno.observability.token_audit.archive_advisor import (
    DEFAULT_ARCHIVE_DAYS_THRESHOLD,
    DEFAULT_ARCHIVE_RETENTION_DAYS,
    DEFAULT_FTRL_ALPHA,
)
from concinno.observability.token_audit.audit import (
    DEFAULT_SYSTEM_PROMPT_FLOOR_TOKENS,
    get_audit,
    reset_audit,
)
from concinno.observability.token_audit.cli import (
    cmd_advisor,
    cmd_summary,
)
from concinno.observability.token_audit.overhead_tracker import (
    OverheadTracker,
    breakdown_from_summary,
    render_summary,
    summarize_session,
)


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Force ``Path.home()`` to ``tmp_path`` for every test."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    # Reset module-level singleton between tests.
    reset_audit()


@pytest.fixture
def audit(tmp_path: Path) -> TokenAudit:
    return TokenAudit(
        session_id="test-session-1",
        enabled=True,
        audit_dir=tmp_path / "audit",
    )


# ─── Recorder tests ───────────────────────────────────────────────


def test_record_skill_load_accumulates(audit: TokenAudit) -> None:
    audit.record_skill_load("foo", l2_chars=400, frontmatter_chars=80)
    audit.record_skill_load("bar", l2_chars=200, frontmatter_chars=40)
    summary = audit.session_summary()
    assert summary.skills_loaded == 2
    # 480 + 240 = 720 chars at 4 chars/tok ≈ 180 tok
    assert summary.skills_total_overhead == 180
    assert "foo" in summary.skills_breakdown
    assert "bar" in summary.skills_breakdown


def test_record_subagent_accumulates(audit: TokenAudit) -> None:
    audit.record_subagent(brief_chars=4000, return_chars=2000)
    audit.record_subagent(brief_chars=4000, return_chars=2000)
    summary = audit.session_summary()
    assert summary.subagents_spawned == 2
    # 12000 chars / 4 = 3000 tok
    assert summary.subagents_total_overhead == 3000


def test_record_mcp_tool_accumulates(audit: TokenAudit) -> None:
    audit.record_mcp_tool("server-a", "tool-1", schema_chars=400)
    audit.record_mcp_tool("server-a", "tool-2", schema_chars=200)
    audit.record_mcp_tool("server-b", "tool-1", schema_chars=600)
    summary = audit.session_summary()
    assert summary.mcp_tools_loaded == 3
    # 1200 chars / 4 = 300 tok
    assert summary.mcp_total_overhead == 300


def test_session_summary_aggregates_correctly(audit: TokenAudit) -> None:
    audit.record_skill_load("foo", l2_chars=400, frontmatter_chars=80)
    audit.record_subagent(brief_chars=4000, return_chars=2000)
    audit.record_mcp_tool("srv", "t", schema_chars=200)
    summary = audit.session_summary()
    # floor 14000 + skills 120 + sub 1500 + mcp 50 = 15670 tok
    assert summary.total_estimated_tokens == (
        DEFAULT_SYSTEM_PROMPT_FLOOR_TOKENS + 120 + 1500 + 50
    )


def test_session_summary_jsonl_written_on_stop(audit: TokenAudit) -> None:
    audit.record_skill_load("foo", l2_chars=400)
    audit.end_session()
    expected = audit.get_default_audit_dir()  # not used
    assert expected  # silence linters
    jsonl = audit._audit_dir / f"token_audit_{audit.session_id}.jsonl"
    assert jsonl.exists()
    payload = json.loads(jsonl.read_text(encoding="utf-8").strip())
    assert payload["session_id"] == audit.session_id
    assert payload["skills_loaded"] == 1


def test_session_summary_includes_system_floor(audit: TokenAudit) -> None:
    summary = audit.session_summary()
    assert summary.system_prompt_floor == DEFAULT_SYSTEM_PROMPT_FLOOR_TOKENS


def test_concurrent_record_thread_safe(audit: TokenAudit) -> None:
    def worker(prefix: str) -> None:
        for i in range(50):
            audit.record_skill_load(
                f"{prefix}-{i}", l2_chars=100, frontmatter_chars=20,
            )

    threads = [
        threading.Thread(target=worker, args=(f"thread{i}",))
        for i in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    summary = audit.session_summary()
    assert summary.skills_loaded == 200


def test_disabled_records_noop(tmp_path: Path) -> None:
    a = TokenAudit(
        session_id="off",
        enabled=False,
        audit_dir=tmp_path / "audit",
    )
    a.record_skill_load("foo", l2_chars=10000, frontmatter_chars=1000)
    a.record_subagent(brief_chars=10000, return_chars=10000)
    a.record_mcp_tool("s", "t", schema_chars=10000)
    summary = a.session_summary(flush=True)
    assert summary.enabled is False
    assert summary.skills_loaded == 0
    assert summary.total_estimated_tokens == 0
    assert not (tmp_path / "audit").exists()


def test_audit_jsonl_path_uses_pathlib_home(audit: TokenAudit) -> None:
    default = audit.get_default_audit_dir()
    assert default == Path.home() / ".concinno" / "audit"


def test_summary_resilient_to_missing_dir(tmp_path: Path) -> None:
    a = TokenAudit(
        session_id="sx",
        enabled=True,
        audit_dir=tmp_path / "nope" / "deeper",
    )
    a.record_skill_load("foo", l2_chars=400)
    # Should create the dir on flush, not raise.
    summary = a.end_session()
    assert summary.skills_loaded == 1
    assert (tmp_path / "nope" / "deeper").exists()


def test_session_id_uniqueness(tmp_path: Path) -> None:
    a = TokenAudit(audit_dir=tmp_path / "a")
    b = TokenAudit(audit_dir=tmp_path / "b")
    assert a.session_id != b.session_id


def test_record_after_stop_warns(audit: TokenAudit, capsys) -> None:
    audit.record_skill_load("foo", l2_chars=100)
    audit.end_session()
    audit.record_skill_load("bar", l2_chars=100)
    err = capsys.readouterr().err
    assert "after end_session" in err


def test_overhead_per_skill_breakdown(audit: TokenAudit) -> None:
    audit.record_skill_load("foo", l2_chars=400, frontmatter_chars=0)
    audit.record_skill_load("bar", l2_chars=800, frontmatter_chars=0)
    tracker = OverheadTracker(audit, top_n_skills=2)
    breakdown = tracker.breakdown()
    assert "bar" in breakdown.skills_top
    assert breakdown.skills_top["bar"] == 200
    assert breakdown.skills_top["foo"] == 100


def test_overhead_per_mcp_breakdown(audit: TokenAudit) -> None:
    audit.record_mcp_tool("srv", "tool-x", schema_chars=400)
    audit.record_mcp_tool("srv", "tool-y", schema_chars=200)
    tracker = OverheadTracker(audit)
    breakdown = tracker.breakdown()
    assert "srv:tool-x" in breakdown.mcp_top
    assert breakdown.mcp_top["srv:tool-x"] == 100


def test_subagent_brief_return_overhead(audit: TokenAudit) -> None:
    audit.record_subagent(brief_chars=2000, return_chars=2000)
    tracker = OverheadTracker(audit)
    breakdown = tracker.breakdown()
    # 4000 / 4 = 1000 tok
    assert breakdown.subagents_total == 1000
    assert breakdown.subagents_count == 1


def test_render_summary_text(audit: TokenAudit) -> None:
    audit.record_skill_load("foo", l2_chars=400)
    audit.record_subagent(brief_chars=2000, return_chars=2000)
    text = render_summary(audit)
    assert "Token audit" in text
    assert "foo" in text
    assert "sub-agents" in text


def test_summarize_session_helper(audit: TokenAudit) -> None:
    audit.record_skill_load("foo", l2_chars=400)
    breakdown = summarize_session(audit, top_n_skills=1)
    assert breakdown.skills_total > 0
    assert "foo" in breakdown.skills_top


def test_breakdown_from_summary_matches_tracker(audit: TokenAudit) -> None:
    audit.record_skill_load("foo", l2_chars=400)
    audit.record_skill_load("bar", l2_chars=800)
    summary = audit.session_summary()
    bd1 = breakdown_from_summary(summary, top_n_skills=1)
    bd2 = OverheadTracker(audit, top_n_skills=1).breakdown()
    assert bd1.skills_top == bd2.skills_top
    assert bd1.grand_total == bd2.grand_total


def test_get_audit_singleton_returns_same_instance() -> None:
    a1 = get_audit(session_id="s1")
    a2 = get_audit()
    assert a1 is a2


def test_get_audit_toggle_enabled_reconstructs() -> None:
    a1 = get_audit(session_id="s1", enabled=True)
    a2 = get_audit(enabled=False)
    assert a1 is not a2
    assert a2.enabled is False


def test_tail_buffer_recorded(audit: TokenAudit) -> None:
    audit.record_tail_buffer(8000)
    summary = audit.session_summary()
    assert summary.tail_buffer == 2000  # 8000/4


def test_tool_search_flag_round_trips(audit: TokenAudit) -> None:
    audit.set_tool_search_enabled(True)
    summary = audit.session_summary()
    assert summary.tool_search_enabled is True


# ─── ArchiveAdvisor tests ─────────────────────────────────────────


def _seed_state(
    advisor: ArchiveAdvisor,
    skill: str,
    days_ago: float,
    *,
    sessions: int = 0,
    weight: float = 0.0,
) -> None:
    """Helper: write a synthetic state record for ``skill`` last-used N days ago."""
    state = advisor._load_state()
    last_used_dt = datetime.now(tz=timezone.utc) - timedelta(days=days_ago)
    state[skill] = {
        "last_used": last_used_dt.isoformat(),
        "count": 1,
        "sessions_loaded_in": [f"s{i}" for i in range(sessions)],
        "ftrl_weight": weight,
    }
    advisor._save_state(state)


def test_archive_advisor_30d_unused_flagged() -> None:
    advisor = ArchiveAdvisor()
    _seed_state(advisor, "old-skill", days_ago=45.0)
    candidates = advisor.suggest_archives()
    names = [c.skill for c in candidates]
    assert "old-skill" in names


def test_archive_advisor_recently_used_skipped() -> None:
    advisor = ArchiveAdvisor()
    _seed_state(advisor, "fresh-skill", days_ago=2.0)
    candidates = advisor.suggest_archives()
    names = [c.skill for c in candidates]
    assert "fresh-skill" not in names


def test_archive_no_hard_delete_only_move(tmp_path: Path) -> None:
    advisor = ArchiveAdvisor(archive_root=tmp_path / "archive")
    skill_dir = tmp_path / "skills" / "to-archive"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("placeholder", encoding="utf-8")
    _seed_state(advisor, "to-archive", days_ago=45.0)
    target = advisor.accept("to-archive", skill_dir=skill_dir)
    assert target is not None
    assert target.exists()  # moved
    assert not skill_dir.exists()  # source moved away (NOT deleted)


def test_archive_retention_90d() -> None:
    advisor = ArchiveAdvisor(retention_days=DEFAULT_ARCHIVE_RETENTION_DAYS)
    assert advisor._retention_days == 90


def test_archive_ftrl_reward_on_user_accept() -> None:
    advisor = ArchiveAdvisor(ftrl_alpha=DEFAULT_FTRL_ALPHA)
    _seed_state(advisor, "x", days_ago=45.0, weight=0.0)
    advisor.accept("x")
    state = advisor._load_state()
    assert state["x"]["ftrl_weight"] > 0.0


def test_archive_ftrl_penalty_on_user_reject() -> None:
    advisor = ArchiveAdvisor(ftrl_alpha=DEFAULT_FTRL_ALPHA)
    _seed_state(advisor, "x", days_ago=45.0, weight=0.0)
    advisor.reject("x")
    state = advisor._load_state()
    assert state["x"]["ftrl_weight"] < 0.0


def test_archive_user_decision_recorded() -> None:
    advisor = ArchiveAdvisor()
    _seed_state(advisor, "x", days_ago=45.0)
    advisor.accept("x")
    state = advisor._load_state()
    assert state["x"]["last_decision"] == "accept"
    advisor.reject("x")
    state = advisor._load_state()
    assert state["x"]["last_decision"] == "reject"


def test_archive_threshold_default_30_days() -> None:
    assert DEFAULT_ARCHIVE_DAYS_THRESHOLD == 30


def test_archive_record_invocation_resets_clock() -> None:
    advisor = ArchiveAdvisor()
    _seed_state(advisor, "x", days_ago=60.0)
    candidates_before = advisor.suggest_archives()
    assert any(c.skill == "x" for c in candidates_before)
    advisor.record_invocation("x", session_id="now-session")
    candidates_after = advisor.suggest_archives()
    assert not any(c.skill == "x" for c in candidates_after)


def test_archive_score_gate_skips_low_score() -> None:
    advisor = ArchiveAdvisor()
    # 31 days unused (just past 30-day threshold) + 9 sessions of soft
    # evidence in a 10-session window: day=min(1, 31/30)=1.0 ×
    # soft=(1 - 0.4*0.9)=0.64 × ftrl=1.0 ≈ 0.64. Tweak: use days=31
    # and 10 sessions → soft = 1 - 0.4*1 = 0.6 → score 0.6 (right at
    # gate, but the gate is ``<`` so equal still admits). Use 31 days
    # unused with deep soft evidence (sessions=10) AND a negative
    # ftrl_weight from past rejection so the modifier shaves ~5% more
    # off — that puts score below 0.6.
    _seed_state(
        advisor,
        "borderline",
        days_ago=31.0,
        sessions=10,
        weight=-1.0,  # one prior rejection
    )
    candidates = advisor.suggest_archives()
    assert all(c.skill != "borderline" for c in candidates)


def test_archive_candidate_has_soft_evidence() -> None:
    advisor = ArchiveAdvisor()
    _seed_state(advisor, "soft", days_ago=45.0, sessions=3)
    candidates = advisor.suggest_archives()
    target = next(c for c in candidates if c.skill == "soft")
    assert "sessions_loaded_in_recent" in target.soft_evidence


def test_archive_candidate_dataclass() -> None:
    c = ArchiveCandidate(
        skill="x",
        days_unused=45.0,
        last_used="2026-01-01T00:00:00+00:00",
        score=0.9,
    )
    assert c.to_dict()["skill"] == "x"


# ─── CLI tests ────────────────────────────────────────────────────


def test_cli_summary_command_no_records(capsys) -> None:
    args = argparse.Namespace(format="text", session=None)
    rc = cmd_summary(args)
    assert rc == 0


def test_cli_summary_command_with_record(audit: TokenAudit, capsys) -> None:
    audit.record_skill_load("foo", l2_chars=400)
    audit.end_session()
    # Move audit jsonl to canonical location for CLI
    canonical = Path.home() / ".concinno" / "audit"
    canonical.mkdir(parents=True, exist_ok=True)
    src = audit._audit_dir / f"token_audit_{audit.session_id}.jsonl"
    dst = canonical / f"token_audit_{audit.session_id}.jsonl"
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    args = argparse.Namespace(
        format="text",
        session=audit.session_id,
    )
    rc = cmd_summary(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert audit.session_id in out


def test_cli_summary_json(audit: TokenAudit, capsys) -> None:
    audit.record_skill_load("foo", l2_chars=400)
    audit.end_session()
    canonical = Path.home() / ".concinno" / "audit"
    canonical.mkdir(parents=True, exist_ok=True)
    src = audit._audit_dir / f"token_audit_{audit.session_id}.jsonl"
    dst = canonical / f"token_audit_{audit.session_id}.jsonl"
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    args = argparse.Namespace(
        format="json",
        session=audit.session_id,
    )
    cmd_summary(args)
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["session_id"] == audit.session_id


def test_cli_advisor_command_empty(capsys) -> None:
    args = argparse.Namespace(
        format="text",
        days=30,
        accept=None,
        reject=None,
    )
    rc = cmd_advisor(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "no archive candidates" in out


def test_cli_advisor_accept(capsys) -> None:
    advisor = ArchiveAdvisor()
    _seed_state(advisor, "x", days_ago=45.0)
    args = argparse.Namespace(
        format="text",
        days=30,
        accept="x",
        reject=None,
    )
    rc = cmd_advisor(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "accept" in out


def test_cli_advisor_reject_json(capsys) -> None:
    advisor = ArchiveAdvisor()
    _seed_state(advisor, "y", days_ago=45.0)
    args = argparse.Namespace(
        format="json",
        days=30,
        accept=None,
        reject="y",
    )
    cmd_advisor(args)
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["action"] == "reject"
