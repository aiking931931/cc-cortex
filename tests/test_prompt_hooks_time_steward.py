"""Tests for ``concinno.time_steward`` (DAG-aware time scheduling hook).

Test plan numbering matches the brief in the dispatch:

    1-3.  Each of capabilities 1 / 2 / 3 fires when conditions met.
    4-6.  Each does NOT fire when conditions not met (false-positive guard).
    7.    Feature flag ``time_steward.enabled=False`` → all 6 disabled.
    8.    Cooldown: capability 3 fires once per 3 turns max.
    9.    State file concurrent-write safety.
    10.   Multilingual phrase detection (zh + en).
    11.   Idle phrase NOT match if in user message (only main agent's turns).
    12-13. Budget tracker × 1.5 → fires; × 2 → fires stronger.
    14.   Re-triage fires on completion event but not on result-processing turn.
    15.   Pre-spawn contention check: overlap → warn; no overlap → silent.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from concinno.time_steward import (
    CAP_BUDGET_TRACKER,
    CAP_CANCEL_RESTART,
    CAP_DAG_VISUALIZER,
    CAP_IDLE_DETECTION,
    CAP_PRE_SPAWN_CONTENTION,
    CAP_RETRIAGE_ON_COMPLETE,
    SubagentRecord,
    TimeSteward,
    _detect_idle_phrase,
    _extract_estimated_minutes,
    _extract_files_from_brief,
    _last_n_agent_turns,
    _summarise_brief,
    register_subagent_complete,
    register_subagent_spawn,
    run_time_steward,
)

# ── Shared fixtures ────────────────────────────────────────


@pytest.fixture
def steward(tmp_path: Path) -> TimeSteward:
    """Return a TimeSteward writing to a tmp_path-scoped state dir."""
    return TimeSteward(state_dir=tmp_path / "state")


def _seed_active_agent(
    steward: TimeSteward,
    *,
    agent_id: str = "agent-001",
    minutes_ago: float = 0.0,
    est_minutes: int = 0,
    brief_summary: str = "",
    files_touched: list[str] | None = None,
) -> SubagentRecord:
    rec = SubagentRecord(
        id=agent_id,
        spawned_at=time.time() - minutes_ago * 60.0,
        est_minutes=est_minutes,
        brief_summary=brief_summary,
        files_touched=list(files_touched or []),
    )
    steward._upsert_record(rec)
    return rec


# ── Test 1 — capability 1 (DAG visualiser) fires ──────────


class TestCapability1DagVisualizer:
    def test_dag_visualizer_fires_when_called(self, steward: TimeSteward):
        result = steward.advise_pre_spawn_dag()
        assert result["inject"]
        assert "⬜ DAG" in result["inject"]
        assert "parallel" in result["inject"].lower()
        assert result["metadata"]["capability"] == CAP_DAG_VISUALIZER

    def test_dag_visualizer_inject_under_budget(self, steward: TimeSteward):
        result = steward.advise_pre_spawn_dag()
        assert len(result["inject"]) <= 480


# ── Test 2 — capability 2 (pre-spawn contention) fires ────


class TestCapability2ContentionCheck:
    def test_contention_warns_when_active_agent_present(
        self, steward: TimeSteward
    ):
        _seed_active_agent(
            steward,
            agent_id="bg-1",
            brief_summary="kb_handoff redesign",
            files_touched=["src/concinno/handoff_engine.py"],
        )
        result = steward.advise_pre_spawn_contention()
        assert result["inject"]
        assert "background sub-agent" in result["inject"]
        assert result["metadata"]["capability"] == CAP_PRE_SPAWN_CONTENTION
        assert result["metadata"]["active_count"] == 1


# ── Test 3 — capability 3 (idle detection) fires ──────────


class TestCapability3IdleDetection:
    def test_idle_fires_with_active_agent_and_idle_phrase_zh(
        self, steward: TimeSteward
    ):
        _seed_active_agent(steward)
        result = steward.advise_idle(
            agent_recent_turns=["先等子代理回來再決定"],
            session_id="s1",
            turn_index=10,
        )
        assert result["inject"]
        assert "Idle waiting detected" in result["inject"]
        assert result["metadata"]["capability"] == CAP_IDLE_DETECTION

    def test_idle_fires_with_active_agent_and_idle_phrase_en(
        self, steward: TimeSteward
    ):
        _seed_active_agent(steward)
        result = steward.advise_idle(
            agent_recent_turns=["I'll wait for sub-agent to finish"],
            session_id="s2",
            turn_index=10,
        )
        assert result["inject"]
        assert result["metadata"]["capability"] == CAP_IDLE_DETECTION


# ── Tests 4-6 — false-positive guards ─────────────────────


class TestFalsePositiveGuards:
    def test_dag_visualizer_no_op_when_disabled(self, steward, monkeypatch):
        monkeypatch.setattr(
            "concinno.time_steward._feature_enabled", lambda *a, **k: False
        )
        assert steward.advise_pre_spawn_dag() == {}

    def test_contention_silent_when_no_active_agents(self, steward):
        assert steward.advise_pre_spawn_contention() == {}

    def test_idle_silent_when_no_idle_phrase(self, steward):
        _seed_active_agent(steward)
        result = steward.advise_idle(
            agent_recent_turns=["Working on the next task now."],
            session_id="s-no-idle",
            turn_index=5,
        )
        assert result == {}

    def test_idle_silent_when_no_active_agents(self, steward):
        # Even with idle phrase, no active sub-agents = no advisory.
        result = steward.advise_idle(
            agent_recent_turns=["waiting for sub-agent"],
            session_id="s-no-active",
            turn_index=5,
        )
        assert result == {}


# ── Test 7 — feature flag disables all six ────────────────


class TestFeatureFlagDisablesAll:
    def test_all_capabilities_silent_when_flag_off(
        self, steward: TimeSteward, monkeypatch
    ):
        monkeypatch.setattr(
            "concinno.time_steward._feature_enabled", lambda *a, **k: False
        )
        # Seed registry so naïve "no agents = silent" doesn't mask the flag.
        _seed_active_agent(
            steward,
            agent_id="x",
            minutes_ago=120.0,
            est_minutes=10,
            files_touched=["a.py"],
        )

        assert steward.advise_pre_spawn_dag() == {}
        assert steward.advise_pre_spawn_contention(["a.py"]) == {}
        assert (
            steward.advise_idle(
                agent_recent_turns=["wait for sub-agent"],
                session_id="s",
                turn_index=999,
            )
            == {}
        )
        assert steward.advise_budget() == {}
        assert (
            steward.advise_retriage(completed_agent_id="x") == {}
        )
        # And cancel-restart (capability 6) routes through advise_budget.


# ── Test 8 — idle cool-down ───────────────────────────────


class TestIdleCooldown:
    def test_idle_cooldown_swallows_repeats_within_window(
        self, steward: TimeSteward
    ):
        _seed_active_agent(steward)
        first = steward.advise_idle(
            agent_recent_turns=["wait for sub-agent"],
            session_id="cool",
            turn_index=10,
        )
        second = steward.advise_idle(
            agent_recent_turns=["wait for sub-agent"],
            session_id="cool",
            turn_index=11,
        )
        third = steward.advise_idle(
            agent_recent_turns=["wait for sub-agent"],
            session_id="cool",
            turn_index=12,
        )
        assert first["inject"]
        assert second == {}  # within 3-turn window
        assert third == {}  # still within window

    def test_idle_fires_again_after_cooldown_expires(self, steward):
        _seed_active_agent(steward)
        first = steward.advise_idle(
            agent_recent_turns=["wait for sub-agent"],
            session_id="cool2",
            turn_index=10,
        )
        fourth = steward.advise_idle(
            agent_recent_turns=["wait for sub-agent"],
            session_id="cool2",
            turn_index=13,  # 13 - 10 == 3 == COOLDOWN_TURNS
        )
        assert first["inject"]
        assert fourth["inject"]


# ── Test 9 — concurrent write safety ──────────────────────


class TestConcurrentWriteSafety:
    def test_parallel_upserts_do_not_corrupt_registry(
        self, steward: TimeSteward
    ):
        # Spawn 8 threads each registering a distinct sub-agent.
        ids = [f"par-{i:02d}" for i in range(8)]

        def _spawn(agent_id: str) -> None:
            register_subagent_spawn(
                agent_id,
                brief="test 30 min",
                state_dir=steward.state_dir,
            )

        threads = [threading.Thread(target=_spawn, args=(i,)) for i in ids]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        records = steward._load_registry()
        seen_ids = {r.id for r in records}
        # Atomic write semantics mean some upserts may overwrite each
        # other (last-writer-wins), but the file MUST remain valid JSON
        # and contain at least one tracked record.
        assert len(records) >= 1
        assert seen_ids.issubset(set(ids))
        # Re-read the raw file to confirm valid JSON.
        raw = (steward.state_dir / "active_subagents.json").read_text("utf-8")
        parsed = json.loads(raw)
        assert "agents" in parsed
        assert isinstance(parsed["agents"], list)


# ── Test 10 — multilingual phrase detection ───────────────


class TestMultilingualPhraseDetection:
    @pytest.mark.parametrize(
        "phrase,expected",
        [
            ("等子代理回來", True),
            ("等待子代理完成", True),
            ("waiting for sub-agent", True),
            ("I'll hold on for the background agent.", True),
            ("standing by for delegate to finish", True),
            # negatives
            ("just doing other work now", False),
            ("等等我", False),  # vague "wait" without sub-agent noun
            ("subagent", False),  # noun alone, no wait verb
        ],
    )
    def test_idle_phrase_detector(self, phrase: str, expected: bool):
        assert _detect_idle_phrase(phrase) is expected


# ── Test 11 — only agent's own turns count ────────────────


class TestOnlyAgentTurnsCount:
    def test_idle_phrase_in_user_message_does_not_match(
        self, steward: TimeSteward
    ):
        _seed_active_agent(steward)
        # Caller is responsible for filtering: the public API
        # (run_time_steward / advise_idle) takes
        # ``agent_recent_turns`` — caller must not pass user msgs.
        # Verify _last_n_agent_turns extracts only role==assistant.
        transcript: list[dict[str, Any]] = [
            {"role": "user", "content": "等子代理回來"},
            {"role": "assistant", "content": "Doing useful work."},
        ]
        agent_turns = _last_n_agent_turns(transcript, n=3)
        assert agent_turns == ["Doing useful work."]
        # Now feed those turns to advise_idle: no idle phrase → no fire.
        result = steward.advise_idle(
            agent_recent_turns=agent_turns,
            session_id="s11",
            turn_index=20,
        )
        assert result == {}


# ── Tests 12-13 — budget tracker ──────────────────────────


class TestBudgetTracker:
    def test_warns_at_one_point_five_x_estimate(self, steward: TimeSteward):
        # est=10min, age=16min  → 1.6× → warn (not yet ×2)
        _seed_active_agent(
            steward,
            agent_id="slow-1",
            est_minutes=10,
            minutes_ago=16.0,
            brief_summary="slow agent",
        )
        result = steward.advise_budget()
        assert result["inject"]
        assert result["metadata"]["capability"] == CAP_BUDGET_TRACKER
        assert "likely stuck" in result["inject"]

    def test_cancels_at_two_x_estimate(self, steward: TimeSteward):
        # est=10min, age=21min → ≥ ×2 → cancel-restart
        _seed_active_agent(
            steward,
            agent_id="slow-2",
            est_minutes=10,
            minutes_ago=21.0,
            brief_summary="really stuck agent",
        )
        result = steward.advise_budget()
        assert result["inject"]
        assert result["metadata"]["capability"] == CAP_CANCEL_RESTART
        assert "×2 over" in result["inject"]
        assert "TaskStop" in result["inject"]

    def test_no_estimate_no_advisory(self, steward: TimeSteward):
        # est=0 → cannot judge; skip silently.
        _seed_active_agent(
            steward,
            agent_id="unbudgeted",
            est_minutes=0,
            minutes_ago=300.0,
        )
        assert steward.advise_budget() == {}


# ── Test 14 — re-triage on completion ─────────────────────


class TestRetriageOnComplete:
    def test_retriage_fires_on_completion(self, steward: TimeSteward):
        result = steward.advise_retriage(completed_agent_id="done-001")
        assert result["inject"]
        assert "Re-evaluate" in result["inject"]
        assert result["metadata"]["capability"] == CAP_RETRIAGE_ON_COMPLETE

    def test_retriage_silent_during_result_processing(
        self, steward: TimeSteward
    ):
        result = steward.advise_retriage(
            completed_agent_id="done-001",
            agent_currently_processing_result=True,
        )
        assert result == {}

    def test_retriage_silent_on_empty_id(self, steward: TimeSteward):
        assert steward.advise_retriage(completed_agent_id="") == {}


# ── Test 15 — pre-spawn contention overlap vs no-overlap ──


class TestContentionOverlapVsNoOverlap:
    def test_overlap_warns_about_specific_files(self, steward: TimeSteward):
        _seed_active_agent(
            steward,
            agent_id="bg-overlap",
            files_touched=["src/concinno/feature_config.py"],
            brief_summary="touches feature_config",
        )
        result = steward.advise_pre_spawn_contention(
            new_files_touched=["src/concinno/feature_config.py", "other.md"]
        )
        assert result["inject"]
        assert "overlaps" in result["inject"]
        assert "feature_config.py" in result["inject"]
        assert result["metadata"]["overlap"] == [
            "src/concinno/feature_config.py"
        ]

    def test_no_overlap_silent_inject(self, steward: TimeSteward):
        _seed_active_agent(
            steward,
            agent_id="bg-disjoint",
            files_touched=["src/concinno/handoff_engine.py"],
        )
        result = steward.advise_pre_spawn_contention(
            new_files_touched=["src/concinno/time_steward.py"]
        )
        # When caller supplies scope and there is no overlap, advisory
        # body is empty (caller may still log metadata).
        assert result["inject"] == ""
        assert result["metadata"]["overlap"] == []

    def test_no_scope_supplied_falls_through_to_generic_warning(
        self, steward: TimeSteward
    ):
        _seed_active_agent(
            steward,
            agent_id="bg-no-scope",
            files_touched=["a.py"],
            brief_summary="some work",
        )
        result = steward.advise_pre_spawn_contention()
        assert result["inject"]
        assert "State which files" in result["inject"]


# ── Spawn-brief parser unit tests (helpers) ───────────────


class TestSpawnBriefParsing:
    @pytest.mark.parametrize(
        "brief,expected_minutes",
        [
            ("should take 30-60 min", 60),
            ("estimate ~5 min", 5),
            ("about 10 minutes", 10),
            ("1-2 hr", 120),
            ("2 hours", 120),
            ("應該需要 30 分鐘", 30),
            ("no time mentioned", 0),
            ("", 0),
        ],
    )
    def test_extract_estimated_minutes(
        self, brief: str, expected_minutes: int
    ):
        assert _extract_estimated_minutes(brief) == expected_minutes

    def test_summarise_brief_truncates(self):
        summary = _summarise_brief("x" * 200)
        assert len(summary) <= 80

    def test_extract_files_from_brief(self):
        brief = "Will touch `src/foo.py` and `tests/bar.py` plus README.md"
        # README.md is NOT in backticks → should not be picked up
        assert _extract_files_from_brief(brief) == [
            "src/foo.py",
            "tests/bar.py",
        ]


# ── Public spawn / complete API ───────────────────────────


class TestRegisterSpawnComplete:
    def test_spawn_then_complete_round_trip(self, tmp_path: Path):
        sd = tmp_path / "state"
        rec = register_subagent_spawn(
            "agent-xyz",
            brief="should take 30 min — touches `src/a.py`",
            state_dir=sd,
        )
        assert rec.id == "agent-xyz"
        assert rec.est_minutes == 30
        assert "src/a.py" in rec.files_touched

        steward = TimeSteward(state_dir=sd)
        active = steward._active_records()
        assert len(active) == 1
        assert active[0].id == "agent-xyz"

        removed = register_subagent_complete("agent-xyz", state_dir=sd)
        assert removed is not None
        assert removed.id == "agent-xyz"
        assert steward._active_records() == []

    def test_spawn_with_empty_id_is_noop(self, tmp_path: Path):
        sd = tmp_path / "state"
        rec = register_subagent_spawn("", state_dir=sd)
        assert rec.id == ""
        assert TimeSteward(state_dir=sd)._active_records() == []

    def test_complete_unknown_id_returns_none(self, tmp_path: Path):
        sd = tmp_path / "state"
        assert register_subagent_complete("never-spawned", state_dir=sd) is None


# ── Top-level orchestrator ────────────────────────────────


class TestRunTimeStewardOrchestrator:
    def test_returns_empty_when_disabled(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "concinno.time_steward._feature_enabled", lambda *a, **k: False
        )
        result = run_time_steward(
            agent_recent_turns=["wait for sub-agent"],
            session_id="s",
            turn_index=1,
            state_dir=tmp_path / "state",
        )
        assert result == {}

    def test_budget_advisory_wins_over_idle(self, tmp_path: Path):
        sd = tmp_path / "state"
        steward = TimeSteward(state_dir=sd)
        # Seed a stuck agent past ×2 — should produce cancel-restart
        # in preference to any idle inject.
        _seed_active_agent(
            steward,
            agent_id="stuck",
            est_minutes=10,
            minutes_ago=21.0,
        )
        result = run_time_steward(
            agent_recent_turns=["waiting for sub-agent"],
            session_id="orch",
            turn_index=10,
            state_dir=sd,
        )
        assert result["metadata"]["capability"] == CAP_CANCEL_RESTART

    def test_idle_advisory_when_no_budget_alarm(self, tmp_path: Path):
        sd = tmp_path / "state"
        steward = TimeSteward(state_dir=sd)
        # No estimate → no budget alarm; but active agent + idle phrase fires.
        _seed_active_agent(steward, agent_id="quiet", est_minutes=0)
        result = run_time_steward(
            agent_recent_turns=["waiting for sub-agent"],
            session_id="orch2",
            turn_index=10,
            state_dir=sd,
        )
        assert result["metadata"]["capability"] == CAP_IDLE_DETECTION


# ── Test 16-21 — capability 7 (polling watchdog) ──────────


from concinno.time_steward import (  # noqa: E402
    CAP_POLLING_WATCHDOG,
    POLL_STATUS_FILENAME,
)


def _write_poll_status(
    state_dir: Path,
    *,
    pod_status: str = "RUNNING",
    age_seconds: float = 0.0,
    extra: dict | None = None,
) -> Path:
    """Write a poll_status.json with controlled mtime offset."""
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / POLL_STATUS_FILENAME
    payload: dict[str, Any] = {"pod_status": pod_status}
    if extra:
        payload.update(extra)
    path.write_text(json.dumps(payload), encoding="utf-8")
    if age_seconds > 0:
        ts = time.time() - age_seconds
        import os as _os
        _os.utime(path, (ts, ts))
    return path


class TestCapability7PollingWatchdog:
    def test_fires_when_active_subagent_stale_and_poll_stale(
        self, tmp_path: Path,
    ):
        sd = tmp_path / "state"
        steward = TimeSteward(state_dir=sd)
        _seed_active_agent(steward, agent_id="long-running", minutes_ago=15)
        # Stale poll_status (older than default 10 min threshold).
        _write_poll_status(sd, pod_status="RUNNING", age_seconds=20 * 60)
        result = steward.advise_polling_watchdog(
            session_id="watch1", turn_index=0,
        )
        assert result.get("inject")
        assert result["metadata"]["capability"] == CAP_POLLING_WATCHDOG
        assert result["metadata"]["active_count"] == 1

    def test_no_fire_when_poll_fresh(self, tmp_path: Path):
        sd = tmp_path / "state"
        steward = TimeSteward(state_dir=sd)
        _seed_active_agent(steward, agent_id="long-running", minutes_ago=15)
        # Fresh poll_status.
        _write_poll_status(sd, pod_status="RUNNING", age_seconds=0)
        result = steward.advise_polling_watchdog(
            session_id="watch2", turn_index=0,
        )
        assert result == {}

    def test_no_fire_when_no_active_subagent(self, tmp_path: Path):
        sd = tmp_path / "state"
        steward = TimeSteward(state_dir=sd)
        # No subagent registered → graceful no-fire even with stale poll.
        _write_poll_status(sd, pod_status="RUNNING", age_seconds=20 * 60)
        result = steward.advise_polling_watchdog(
            session_id="watch3", turn_index=0,
        )
        assert result == {}

    def test_pod_exited_triggers_stronger_inject(self, tmp_path: Path):
        sd = tmp_path / "state"
        steward = TimeSteward(state_dir=sd)
        _seed_active_agent(steward, agent_id="dead-pod", minutes_ago=15)
        # Even a fresh poll fires because pod_status != RUNNING.
        _write_poll_status(sd, pod_status="EXITED", age_seconds=0)
        result = steward.advise_polling_watchdog(
            session_id="watch4", turn_index=0,
        )
        assert result.get("inject")
        # Stronger variant must mention immediate resume / EXITED.
        text = result["inject"]
        assert "EXITED" in text or "exited" in text.lower()
        assert "resume" in text.lower()
        assert result["metadata"]["pod_status"] == "EXITED"

    def test_malformed_poll_status_does_not_crash(self, tmp_path: Path):
        sd = tmp_path / "state"
        sd.mkdir(parents=True, exist_ok=True)
        steward = TimeSteward(state_dir=sd)
        _seed_active_agent(steward, agent_id="long-running", minutes_ago=15)
        # Write garbage JSON.
        (sd / POLL_STATUS_FILENAME).write_text(
            "not-valid-json-{{",
            encoding="utf-8",
        )
        # Make it appear stale.
        ts = time.time() - 20 * 60
        import os as _os
        _os.utime(sd / POLL_STATUS_FILENAME, (ts, ts))
        result = steward.advise_polling_watchdog(
            session_id="watch5", turn_index=0,
        )
        # Garbage = stale-equivalent → fires, never raises.
        assert result.get("inject")

    def test_state_dir_absent_no_fire(self, tmp_path: Path):
        # state_dir does not exist on disk at all.
        sd = tmp_path / "no-such-dir"
        steward = TimeSteward(state_dir=sd)
        result = steward.advise_polling_watchdog(
            session_id="watch6", turn_index=0,
        )
        assert result == {}

    def test_cooldown_blocks_repeat_within_window(self, tmp_path: Path):
        sd = tmp_path / "state"
        steward = TimeSteward(state_dir=sd)
        _seed_active_agent(steward, agent_id="long-running", minutes_ago=15)
        _write_poll_status(sd, pod_status="RUNNING", age_seconds=20 * 60)
        # First call fires.
        r1 = steward.advise_polling_watchdog(
            session_id="watch-cd", turn_index=0,
        )
        assert r1.get("inject")
        # Within cooldown window + same poll_mtime → silent.
        r2 = steward.advise_polling_watchdog(
            session_id="watch-cd", turn_index=1,
        )
        assert r2 == {}

    def test_run_time_steward_routes_through_polling_watchdog(
        self, tmp_path: Path,
    ):
        sd = tmp_path / "state"
        steward = TimeSteward(state_dir=sd)
        _seed_active_agent(
            steward, agent_id="long-running", minutes_ago=15, est_minutes=0,
        )
        _write_poll_status(sd, pod_status="EXITED", age_seconds=0)
        result = run_time_steward(
            agent_recent_turns=["doing other things"],
            session_id="orch-poll", turn_index=0, state_dir=sd,
        )
        assert result.get("inject")
        assert result["metadata"]["capability"] == CAP_POLLING_WATCHDOG
