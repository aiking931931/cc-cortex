"""Tests for cc_cortex.guards.cbua_pipeline_guard — PostToolUse CBUA enforcement.

Covers the behavioural signals the guard persists across ticks via
StateStore:
  - edit/read/agent counting
  - B1/C1/U1/WIREDO marker detection from tool input
  - polling streak suppression
  - A4 ask-user violation early-exit
  - delivery keyword one-shot WIREDO reminder
  - simple complexity skip
  - behavioural silent-ack when reads >= edits
  - _is_delivery_command segment-split semantics
  - _generate_reminder missing-signal text content
"""

from __future__ import annotations  # noqa: I001

from pathlib import Path

import pytest

from cc_cortex.core.state_store import StateStore
from cc_cortex.guards.base import GuardContext
from cc_cortex.guards.cbua_pipeline_guard import (
    CbuaPipelineGuard,
    _is_delivery_command,
)


# ── Helpers ──────────────────────────────────────────────────


_NAMESPACE = "cbua_pipeline"
_C0_NAMESPACE = "c0_route"


def _ctx(
    tmp_path: Path,
    *,
    tool_name: str = "Edit",
    tool_input: dict | None = None,
    tool_result: str = "",
    session_id: str = "sess-test",
) -> GuardContext:
    return GuardContext(
        tool_name=tool_name,
        tool_input=tool_input if tool_input is not None else {"file_path": "x.py"},
        session_id=session_id,
        cache_dir=str(tmp_path),
        hook_event="PostToolUse",
        tool_result=tool_result,
    )


def _preseed_complexity(
    tmp_path: Path,
    complexity: str,
    *,
    redteam_required: bool = False,
    session_id: str = "sess-test",
) -> None:
    """Seed C0Router state so `_classify` picks it up on first call."""
    store = StateStore(str(tmp_path))
    store.write(
        _C0_NAMESPACE,
        session_id,
        {
            "complexity": complexity,
            "prompt_budget": 800,
            "guard_level": "normal",
            "signals": {},
            "escalation_reason": "",
            "redteam_required": redteam_required,
            "a2a_suggested": False,
        },
    )


def _state(tmp_path: Path, session_id: str = "sess-test") -> dict:
    return StateStore(str(tmp_path)).read(_NAMESPACE, session_id, default={})


def _run(
    guard: CbuaPipelineGuard,
    tmp_path: Path,
    *,
    tool_name: str = "Edit",
    tool_input: dict | None = None,
    tool_result: str = "",
    session_id: str = "sess-test",
):
    return guard.on_post_tool(
        _ctx(
            tmp_path,
            tool_name=tool_name,
            tool_input=tool_input,
            tool_result=tool_result,
            session_id=session_id,
        ),
    )


@pytest.fixture
def guard() -> CbuaPipelineGuard:
    return CbuaPipelineGuard()


# ── 1. Basic plumbing ─────────────────────────────────────────


class TestPlumbing:
    def test_check_is_noop(self, guard, tmp_path):
        """PreToolUse path returns None regardless of inputs."""
        assert guard.check(_ctx(tmp_path)) is None

    def test_missing_cache_dir_returns_none(self, guard, tmp_path):
        """No cache_dir → guard cannot persist, return None."""
        ctx = GuardContext(
            tool_name="Edit",
            tool_input={"file_path": "x.py"},
            session_id="sess",
            cache_dir="",
            hook_event="PostToolUse",
        )
        assert guard.on_post_tool(ctx) is None

    def test_simple_complexity_skip(self, guard, tmp_path):
        """Simple tasks don't track state and never emit reminders."""
        _preseed_complexity(tmp_path, "simple")
        for _ in range(5):
            result = _run(guard, tmp_path)
        assert result is None
        state = _state(tmp_path)
        # Simple branch returns early before incrementing edit_count.
        assert state.get("edit_count", 0) == 0


# ── 2. Behavioural counters ───────────────────────────────────


class TestCounters:
    def test_edit_count_increments(self, guard, tmp_path):
        _preseed_complexity(tmp_path, "complicated")
        for _ in range(2):
            _run(guard, tmp_path)
        assert _state(tmp_path)["edit_count"] == 2

    def test_read_count_increments(self, guard, tmp_path):
        _preseed_complexity(tmp_path, "complicated")
        _run(guard, tmp_path, tool_name="Read", tool_input={"file_path": "x.py"})
        _run(guard, tmp_path, tool_name="Grep", tool_input={"pattern": "foo"})
        _run(guard, tmp_path, tool_name="Glob", tool_input={"pattern": "*.py"})
        assert _state(tmp_path)["read_count"] == 3

    def test_agent_count_increments(self, guard, tmp_path):
        _preseed_complexity(tmp_path, "complicated")
        _run(
            guard, tmp_path,
            tool_name="Agent",
            tool_input={"description": "child task", "prompt": "do thing"},
        )
        assert _state(tmp_path)["agent_count"] == 1

    def test_notebook_edit_counts_as_edit(self, guard, tmp_path):
        _preseed_complexity(tmp_path, "complicated")
        _run(
            guard, tmp_path,
            tool_name="NotebookEdit",
            tool_input={"notebook_path": "x.ipynb", "new_source": "print(1)"},
        )
        assert _state(tmp_path)["edit_count"] == 1


# ── 3. Marker detection ───────────────────────────────────────


class TestMarkerDetection:
    def test_b1_marker_silences_reminder(self, guard, tmp_path):
        """Text containing '根因.*甜蜜點.*策略' marks b1_shown."""
        _preseed_complexity(tmp_path, "complicated")
        payload = "B1 分析：根因=x → 甜蜜點=y → 策略=z"
        _run(
            guard, tmp_path,
            tool_input={"file_path": "x.py", "new_string": payload},
        )
        assert _state(tmp_path).get("b1_shown") is True

    def test_c1_marker_detected(self, guard, tmp_path):
        _preseed_complexity(tmp_path, "complex")
        payload = "情報盤點：我知道 X，我不知道 Y，我假設 Z 成立"
        _run(
            guard, tmp_path,
            tool_input={"file_path": "x.py", "new_string": payload},
        )
        assert _state(tmp_path).get("c1_shown") is True

    def test_u1_marker_detected(self, guard, tmp_path):
        _preseed_complexity(tmp_path, "complex")
        payload = "反例：當 input 為空時會炸"
        _run(
            guard, tmp_path,
            tool_input={"file_path": "x.py", "new_string": payload},
        )
        assert _state(tmp_path).get("u1_shown") is True

    def test_wiredo_marker_detected(self, guard, tmp_path):
        _preseed_complexity(tmp_path, "complicated")
        payload = "WIREDO 六維檢查 Wired ✓ Inherited ✓"
        _run(
            guard, tmp_path,
            tool_input={"file_path": "x.py", "new_string": payload},
        )
        assert _state(tmp_path).get("wiredo_shown") is True

    def test_marker_in_tool_result_is_detected(self, guard, tmp_path):
        """Scanner must look at tool_result, not just tool_input."""
        _preseed_complexity(tmp_path, "complex")
        _run(
            guard, tmp_path,
            tool_input={"file_path": "x.py"},
            tool_result="我知道 A / 我不知道 B / 我假設 C",
        )
        assert _state(tmp_path).get("c1_shown") is True

    def test_scan_text_cap_truncates_not_skips(self, guard, tmp_path):
        """Long values are truncated to cap, not skipped entirely.

        Regression test: the permanent false-positive where a very long
        Edit `new_string` made markers invisible — root cause was
        `len(v) < 2000` skip. Now truncates to _SCAN_TEXT_CAP.
        """
        _preseed_complexity(tmp_path, "complicated")
        prefix = "根因=x → 甜蜜點=y → 策略=z\n"
        huge = prefix + ("# filler\n" * 2000)
        _run(
            guard, tmp_path,
            tool_input={"file_path": "x.py", "new_string": huge},
        )
        assert _state(tmp_path).get("b1_shown") is True

    def test_dichotomy_detected_chinese(self, guard, tmp_path):
        """二選一 pattern marks dichotomy_seen."""
        _preseed_complexity(tmp_path, "complicated")
        payload = "這個選擇是二選一：保留或改寫"
        _run(
            guard, tmp_path,
            tool_input={"file_path": "x.py", "new_string": payload},
        )
        assert _state(tmp_path).get("dichotomy_seen") is True

    def test_dichotomy_detected_english(self, guard, tmp_path):
        _preseed_complexity(tmp_path, "complicated")
        payload = "either A or B — we need to keep or switch"
        _run(
            guard, tmp_path,
            tool_input={"file_path": "x.py", "new_string": payload},
        )
        assert _state(tmp_path).get("dichotomy_seen") is True

    def test_integrative_silences_dichotomy(self, guard, tmp_path):
        """A+B / 共存 / dual-mode marks integrative_shown."""
        _preseed_complexity(tmp_path, "complicated")
        payload = "dual-mode framework: zero-shot + non-zero-shot 共存"
        _run(
            guard, tmp_path,
            tool_input={"file_path": "x.py", "new_string": payload},
        )
        assert _state(tmp_path).get("integrative_shown") is True


# ── 4. Behavioural silent ack ─────────────────────────────────


class TestBehaviouralSilentAck:
    def test_reads_ge_edits_silences_b1(self, guard, tmp_path):
        """When read_count ≥ edit_count ≥ 3, B1 reminder is auto-ack'd."""
        _preseed_complexity(tmp_path, "complicated")
        # 3 edits
        for _ in range(3):
            _run(guard, tmp_path)
        # 3 reads
        for _ in range(3):
            _run(guard, tmp_path, tool_name="Read", tool_input={"file_path": "x.py"})
        assert _state(tmp_path).get("b1_shown") is True

    def test_edits_without_reads_does_not_silence(self, guard, tmp_path):
        """Edit-only sessions stay loud."""
        _preseed_complexity(tmp_path, "complicated")
        for _ in range(3):
            _run(guard, tmp_path)
        assert _state(tmp_path).get("b1_shown") is not True


# ── 5. Reminder generation ────────────────────────────────────


class TestReminderGeneration:
    def test_no_missing_returns_none(self):
        reminder = CbuaPipelineGuard._generate_reminder(
            state={"edit_count": 0},
            complexity="complicated",
            redteam_required=False,
        )
        assert reminder is None

    def test_b1_fires_at_3_edits_complicated(self):
        reminder = CbuaPipelineGuard._generate_reminder(
            state={"edit_count": 3, "b1_shown": False},
            complexity="complicated",
            redteam_required=False,
        )
        assert reminder is not None
        assert "B1" in reminder.context
        assert reminder.advisory is True

    def test_b1_silenced_when_shown(self):
        reminder = CbuaPipelineGuard._generate_reminder(
            state={"edit_count": 3, "b1_shown": True},
            complexity="complicated",
            redteam_required=False,
        )
        assert reminder is None

    def test_c1_only_in_complex(self):
        # 5 edits in complicated → no C1
        reminder = CbuaPipelineGuard._generate_reminder(
            state={"edit_count": 5, "b1_shown": True, "c1_shown": False},
            complexity="complicated",
            redteam_required=False,
        )
        assert reminder is None

        reminder = CbuaPipelineGuard._generate_reminder(
            state={"edit_count": 5, "b1_shown": True, "c1_shown": False},
            complexity="complex",
            redteam_required=False,
        )
        assert reminder is not None
        assert "C1" in reminder.context

    def test_u1_only_in_complex(self):
        reminder = CbuaPipelineGuard._generate_reminder(
            state={
                "edit_count": 8,
                "b1_shown": True,
                "c1_shown": True,
                "u1_shown": False,
            },
            complexity="complex",
            redteam_required=False,
        )
        assert reminder is not None
        assert "U1" in reminder.context

    def test_dichotomy_reminder_fires(self):
        reminder = CbuaPipelineGuard._generate_reminder(
            state={
                "edit_count": 3,
                "b1_shown": True,
                "dichotomy_seen": True,
                "integrative_shown": False,
            },
            complexity="complicated",
            redteam_required=False,
        )
        assert reminder is not None
        assert "Dichotomy" in reminder.context

    def test_dichotomy_silenced_by_integrative(self):
        reminder = CbuaPipelineGuard._generate_reminder(
            state={
                "edit_count": 3,
                "b1_shown": True,
                "dichotomy_seen": True,
                "integrative_shown": True,
            },
            complexity="complicated",
            redteam_required=False,
        )
        assert reminder is None

    def test_a5_redteam_requires_flag_and_10_edits(self):
        # Not required → no fire
        reminder = CbuaPipelineGuard._generate_reminder(
            state={
                "edit_count": 10,
                "b1_shown": True,
                "c1_shown": True,
                "u1_shown": True,
            },
            complexity="complex",
            redteam_required=False,
        )
        assert reminder is None

        # Required + dispatched → no fire
        reminder = CbuaPipelineGuard._generate_reminder(
            state={
                "edit_count": 10,
                "b1_shown": True,
                "c1_shown": True,
                "u1_shown": True,
                "redteam_dispatched": True,
            },
            complexity="complex",
            redteam_required=True,
        )
        assert reminder is None

        # Required + not dispatched + 10 edits → fire
        reminder = CbuaPipelineGuard._generate_reminder(
            state={
                "edit_count": 10,
                "b1_shown": True,
                "c1_shown": True,
                "u1_shown": True,
            },
            complexity="complex",
            redteam_required=True,
        )
        assert reminder is not None
        assert "A5" in reminder.context
        assert "紅隊" in reminder.context

    def test_wiredo_fires_on_just_fired_flag(self):
        reminder = CbuaPipelineGuard._generate_reminder(
            state={"edit_count": 1, "b1_shown": True, "wiredo_just_fired": True},
            complexity="complicated",
            redteam_required=False,
        )
        assert reminder is not None
        assert "WIREDO" in reminder.context
        assert "D 維" in reminder.context

    def test_severity_escalates_with_missing_count(self):
        # 1 missing → ⚠
        r = CbuaPipelineGuard._generate_reminder(
            state={"edit_count": 3, "b1_shown": False},
            complexity="complicated",
            redteam_required=False,
        )
        assert r is not None
        assert r.context.startswith("⚠")

        # 3 missing → ⛔
        r = CbuaPipelineGuard._generate_reminder(
            state={
                "edit_count": 10,
                "b1_shown": False,
                "c1_shown": False,
                "u1_shown": False,
            },
            complexity="complex",
            redteam_required=True,
        )
        assert r is not None
        assert r.context.startswith("⛔")


# ── 6. Polling detection ──────────────────────────────────────


class TestPolling:
    def test_same_bash_3x_suppresses_reminder(self, guard, tmp_path):
        """≥3 identical Bash prefixes with no Edit/Write → skip reminder."""
        _preseed_complexity(tmp_path, "complicated")
        # Build edit_count up so reminder WOULD fire otherwise
        for _ in range(3):
            _run(guard, tmp_path)
        assert _state(tmp_path)["edit_count"] == 3

        # First Bash — streak=0 (sig differs from empty), still should fire
        # Actually first bash sets last_sig but streak=0. We need 3 repeats.
        cmd = "pytest tests/foo.py"
        # Tick 1: sets last_sig, streak=0
        _run(guard, tmp_path, tool_name="Bash", tool_input={"command": cmd})
        # Tick 2 → polling_streak becomes 1
        _run(guard, tmp_path, tool_name="Bash", tool_input={"command": cmd})
        # Tick 3 → polling_streak becomes 2
        _run(guard, tmp_path, tool_name="Bash", tool_input={"command": cmd})
        # Tick 4 → polling_streak becomes 3 → suppressed
        r4 = _run(guard, tmp_path, tool_name="Bash", tool_input={"command": cmd})
        assert _state(tmp_path)["polling_streak"] >= 3
        assert r4 is None

    def test_edit_resets_polling_streak(self, guard, tmp_path):
        _preseed_complexity(tmp_path, "complicated")
        cmd = "pytest tests/foo.py"
        for _ in range(4):
            _run(guard, tmp_path, tool_name="Bash", tool_input={"command": cmd})
        assert _state(tmp_path)["polling_streak"] >= 3

        _run(guard, tmp_path)  # Edit
        assert _state(tmp_path)["polling_streak"] == 0

    def test_different_bash_sig_breaks_streak(self, guard, tmp_path):
        _preseed_complexity(tmp_path, "complicated")
        _run(guard, tmp_path, tool_name="Bash", tool_input={"command": "ls -la"})
        _run(guard, tmp_path, tool_name="Bash", tool_input={"command": "ls -la"})
        # Different sig
        _run(guard, tmp_path, tool_name="Bash", tool_input={"command": "pwd"})
        assert _state(tmp_path)["polling_streak"] == 0


# ── 7. A4 ask violation ───────────────────────────────────────


class TestA4AskViolation:
    def test_ask_pattern_in_agent_prompt_triggers_advisory(self, guard, tmp_path):
        _preseed_complexity(tmp_path, "complicated")
        result = _run(
            guard, tmp_path,
            tool_name="Agent",
            tool_input={"description": "child", "prompt": "要做嗎？"},
        )
        assert result is not None
        assert result.advisory is True
        assert "A4" in result.context
        assert _state(tmp_path).get("ask_violations") == 1

    def test_ask_pattern_outside_agent_tool_ignored(self, guard, tmp_path):
        """A4 ask check is gated to Agent tool only."""
        _preseed_complexity(tmp_path, "complicated")
        _run(
            guard, tmp_path,
            tool_name="Edit",
            tool_input={"file_path": "x.py", "new_string": "# 要做嗎"},
        )
        assert _state(tmp_path).get("ask_violations", 0) == 0


# ── 8. WIREDO one-shot delivery ───────────────────────────────


class TestWiredoOneShot:
    def test_delivery_keyword_triggers_wiredo(self, guard, tmp_path):
        _preseed_complexity(tmp_path, "complicated")
        result = _run(
            guard, tmp_path,
            tool_name="Bash",
            tool_input={"command": "git commit -m 'feat: ship it'"},
        )
        assert _state(tmp_path).get("wiredo_reminded") is True
        assert result is not None
        assert "WIREDO" in result.context

    def test_wiredo_fires_once_only(self, guard, tmp_path):
        _preseed_complexity(tmp_path, "complicated")
        _run(
            guard, tmp_path,
            tool_name="Bash",
            tool_input={"command": "git commit -m 'x'"},
        )
        # Second delivery command should NOT re-fire
        result = _run(
            guard, tmp_path,
            tool_name="Bash",
            tool_input={"command": "git push origin main"},
        )
        state = _state(tmp_path)
        assert state.get("wiredo_reminded") is True
        assert state.get("wiredo_just_fired") is False
        if result is not None:
            assert "WIREDO" not in result.context

    def test_wiredo_fires_at_20_edits_without_delivery(self, guard, tmp_path):
        _preseed_complexity(tmp_path, "complicated")
        for _ in range(20):
            _run(guard, tmp_path)
        assert _state(tmp_path).get("wiredo_reminded") is True

    def test_docstring_text_does_not_trigger_delivery(self, guard, tmp_path):
        """完成/done/release in tool text must NOT trigger delivery flag.

        Only shell delivery verbs on Bash input trigger. This was the
        「事前查證六維 亂七八糟」false-positive fix.
        """
        _preseed_complexity(tmp_path, "complicated")
        _run(
            guard, tmp_path,
            tool_input={
                "file_path": "README.md",
                "new_string": "# Release notes\nShip it! 完成交付 done release",
            },
        )
        assert _state(tmp_path).get("delivery_keyword_seen", False) is False
        assert _state(tmp_path).get("wiredo_just_fired", False) is False


# ── 9. _is_delivery_command ───────────────────────────────────


class TestIsDeliveryCommand:
    @pytest.mark.parametrize(
        "cmd,expected",
        [
            ("", False),
            ("ls -la", False),
            ("pytest tests/", False),
            ("python -m build", True),
            ("git commit -m 'wip'", True),
            ("git push origin main", True),
            ("gh pr create --fill", True),
            ("gh release create v1.0", True),
            ("twine upload dist/*", True),
            ("npm publish", True),
            ("cargo publish", True),
            ("docker push myimage:latest", True),
            ("kubectl apply -f deploy.yaml", True),
            # Dry-run exemptions
            ("git commit --dry-run", False),
            ("git push --dry-run", False),
            # Compound command: any segment matches
            ("pytest && git commit -m 'x'", True),
            ("ls; gh pr create", True),
            # Meta-command with quoted delivery verb: NOT matched (leading token is python)
            ("python -c \"import os; os.system('git commit -m x')\"", False),
            # Segment split on newline
            ("pytest\ngit push", True),
        ],
    )
    def test_delivery_patterns(self, cmd, expected):
        assert _is_delivery_command(cmd) is expected


# ── 10. Cross-session isolation ───────────────────────────────


class TestSessionIsolation:
    def test_state_per_session(self, guard, tmp_path):
        _preseed_complexity(tmp_path, "complicated", session_id="sess-a")
        _preseed_complexity(tmp_path, "complicated", session_id="sess-b")
        for _ in range(3):
            _run(guard, tmp_path, session_id="sess-a")
        _run(guard, tmp_path, session_id="sess-b")
        assert _state(tmp_path, "sess-a")["edit_count"] == 3
        assert _state(tmp_path, "sess-b")["edit_count"] == 1
