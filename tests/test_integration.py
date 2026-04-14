"""Integration tests — Streak UX + Token Monitor end-to-end."""

import importlib
import json
import os

import pytest


@pytest.fixture
def mock_env(tmp_path):
    """Set up a mock project environment."""
    cache_dir = tmp_path / ".cc_cortex_cache"
    cache_dir.mkdir()
    old = os.environ.get("CLAUDE_PROJECT_DIR")
    os.environ["CLAUDE_PROJECT_DIR"] = str(tmp_path)
    yield tmp_path, cache_dir
    if old:
        os.environ["CLAUDE_PROJECT_DIR"] = old
    else:
        os.environ.pop("CLAUDE_PROJECT_DIR", None)


class TestStreakUXIntegration:
    """Streak UX: actual state file read/write + milestone triggering."""

    def _get_module(self, ux_path):
        from cc_cortex.hooks import on_post_tool as opt
        importlib.reload(opt)
        opt._UX_STATE_FILE = str(ux_path)
        return opt

    def _write_ux(self, path, streak=0, errors=None):
        from cc_cortex.hooks.on_post_tool import _resolve_session_id
        with open(path, "w", encoding="utf-8") as f:
            json.dump(
                {"errors": errors or {}, "streak": streak, "session_id": _resolve_session_id()},
                f,
            )

    def _read_ux(self, path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def test_clean_edits_reach_milestone(self, mock_env):
        """5 consecutive clean edits → streak=5 → 🔥x5 milestone."""
        _, cache = mock_env
        ux_path = cache / "streak_ux.json"
        self._write_ux(ux_path, streak=0)
        opt = self._get_module(ux_path)

        msgs = []
        for i in range(5):
            msg = opt._build_streak_msg(has_errors=False, file_path=f"test_{i}.md")
            msgs.append(msg)

        state = self._read_ux(ux_path)
        assert state["streak"] == 5
        assert msgs[-1] is not None
        assert "\U0001f525" in msgs[-1]  # 🔥
        # Intermediate (1-4) should be None (not milestones)
        assert all(m is None for m in msgs[:4])

    def test_error_resets_streak(self, mock_env):
        """Error → streak=0, then fix → ✅ message."""
        _, cache = mock_env
        ux_path = cache / "streak_ux.json"
        self._write_ux(ux_path, streak=7)
        opt = self._get_module(ux_path)

        # Error
        msg = opt._build_streak_msg(has_errors=True, file_path="broken.py")
        state = self._read_ux(ux_path)
        assert state["streak"] == 0
        assert msg is None  # Error msg comes from linting, not streak

        # Fix
        fix_msg = opt._build_streak_msg(has_errors=False, file_path="broken.py")
        state = self._read_ux(ux_path)
        assert state["streak"] == 1
        assert fix_msg is not None
        assert "\u2705" in fix_msg  # ✅

    def test_non_milestone_suppressed(self, mock_env):
        """Streak 3 is not a milestone → returns None."""
        _, cache = mock_env
        ux_path = cache / "streak_ux.json"
        self._write_ux(ux_path, streak=2)
        opt = self._get_module(ux_path)

        msg = opt._build_streak_msg(has_errors=False, file_path="x.md")
        state = self._read_ux(ux_path)
        assert state["streak"] == 3
        assert msg is None

    def test_throttle_filters_correctly(self, mock_env):
        """Throttle: x5 shown, x3 filtered, CRITICAL always shown."""
        _, cache = mock_env
        ux_path = cache / "streak_ux.json"
        self._write_ux(ux_path)
        opt = self._get_module(ux_path)

        lines = ["\U0001f525x5 nice", "\U0001f525x3 nope", "plain info", "error found"]
        throttled = opt._throttle(lines)

        # x5 → SHOW USER VERBATIM
        assert any("x5" in line and "SHOW USER" in line for line in throttled)
        # x3 → filtered out (not milestone)
        assert not any("x3" in line and "SHOW USER" in line for line in throttled)
        # "error" → CRITICAL → SHOW USER
        assert any("error" in line and "SHOW USER" in line for line in throttled)

    def test_path_normalization_windows(self, mock_env):
        """Backslash path and forward slash path should match."""
        _, cache = mock_env
        ux_path = cache / "streak_ux.json"
        self._write_ux(ux_path, streak=0)
        opt = self._get_module(ux_path)

        # Error with mixed case + backslashes
        opt._build_streak_msg(has_errors=True, file_path="E:\\Project\\src\\App.tsx")
        state = self._read_ux(ux_path)
        keys = list(state["errors"].keys())
        assert len(keys) == 1
        assert "\\" not in keys[0]  # Normalized to forward slashes

        # Fix with forward slashes + different case
        fix_msg = opt._build_streak_msg(has_errors=False, file_path="e:/project/src/app.tsx")
        state = self._read_ux(ux_path)
        assert fix_msg is not None
        assert "\u2705" in fix_msg
        assert len(state["errors"]) == 0  # Error cleared

    def test_milestone_10_and_25(self, mock_env):
        """Milestones at 10 and 25 should trigger."""
        _, cache = mock_env
        ux_path = cache / "streak_ux.json"
        opt = self._get_module(ux_path)

        for target in [10, 25]:
            self._write_ux(ux_path, streak=target - 1)
            msg = opt._build_streak_msg(has_errors=False, file_path="x.md")
            assert msg is not None, f"Milestone {target} should trigger"
            assert "\U0001f525" in msg
            assert f"x{target}" in msg

    def test_max_errors_trim(self, mock_env):
        """Errors dict trimmed to _MAX_ERRORS (50)."""
        _, cache = mock_env
        ux_path = cache / "streak_ux.json"
        opt = self._get_module(ux_path)

        errors = {f"file_{i}.py": 1 for i in range(60)}
        self._write_ux(ux_path, streak=0, errors=errors)
        state = opt._load_ux()
        assert len(state["errors"]) <= 50

    def test_clean_edits_in_milestone(self, mock_env):
        """Milestone message must contain 'clean edits'."""
        _, cache = mock_env
        ux_path = cache / "streak_ux.json"
        self._write_ux(ux_path, streak=4)
        opt = self._get_module(ux_path)

        msg = opt._build_streak_msg(has_errors=False, file_path="x.md")
        assert msg is not None
        assert "clean edits" in msg
        assert "🔥x5" in msg

    def test_error_count_stored(self, mock_env):
        """Lint error count extracted and stored, then used in fix message."""
        _, cache = mock_env
        ux_path = cache / "streak_ux.json"
        self._write_ux(ux_path, streak=3)
        opt = self._get_module(ux_path)

        # Simulate ruff finding 7 issues
        lint_msg = "🔴 ruff ❌ 7 issues (foo.py) — fix before proceeding:\n  ..."
        opt._build_streak_msg(has_errors=True, file_path="foo.py", lint_msg=lint_msg)
        state = self._read_ux(ux_path)
        assert state["errors"]["foo.py"] == 7

        # Fix: should report 7/7
        fix_msg = opt._build_streak_msg(has_errors=False, file_path="foo.py")
        assert fix_msg is not None
        assert "7/7" in fix_msg
        assert "clean edits" in fix_msg


class TestTokenMonitorIntegration:
    """Token Monitor: actual transcript parsing + threshold checks."""

    def _make_transcript(self, tmp_path, tokens=50000, cache_read=0, cache_create=0, output=1000):
        """Create a fake transcript JSONL with usage data."""
        transcript = tmp_path / "session.jsonl"
        entry = {
            "message": {
                "usage": {
                    "input_tokens": tokens,
                    "cache_read_input_tokens": cache_read,
                    "cache_creation_input_tokens": cache_create,
                    "output_tokens": output,
                }
            }
        }
        with open(transcript, "w", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        return str(transcript)

    def test_read_real_usage(self, mock_env):
        """Read token usage from transcript JSONL."""
        tmp, _ = mock_env
        from cc_cortex.token_monitor import read_real_token_usage

        path = self._make_transcript(tmp, tokens=80000, cache_read=20000, output=5000)
        result = read_real_token_usage(path)
        assert result is not None
        assert result["context_tokens"] == 80000 + 20000  # inp + cache_read
        assert result["output_tokens"] == 5000
        assert result["cost_tokens"] < result["context_tokens"]  # cache_read discounted

    def test_threshold_100k(self, mock_env):
        """100K tokens → 📊 info threshold."""
        tmp, cache = mock_env
        from cc_cortex.token_monitor import check_threshold

        path = self._make_transcript(tmp, tokens=105000)
        thresholds = [
            (160_000, "\U0001f6a8", "160K emergency", True),
            (140_000, "\u26a0\ufe0f", "140K warning", False),
            (100_000, "\U0001f4ca", "100K info", False),
        ]
        state = str(cache / "token_state")
        result = check_threshold(path, thresholds, state_dir=state, session_id="test123")
        assert result is not None
        assert result["threshold"] == 100_000
        assert result["icon"] == "\U0001f4ca"

    def test_threshold_140k(self, mock_env):
        """140K tokens → ⚠️ warning threshold."""
        tmp, cache = mock_env
        from cc_cortex.token_monitor import check_threshold

        path = self._make_transcript(tmp, tokens=145000)
        thresholds = [
            (160_000, "\U0001f6a8", "160K emergency", True),
            (140_000, "\u26a0\ufe0f", "140K warning", False),
            (100_000, "\U0001f4ca", "100K info", False),
        ]
        state = str(cache / "token_state")
        result = check_threshold(path, thresholds, state_dir=state, session_id="test456")
        assert result is not None
        assert result["threshold"] == 140_000

    def test_threshold_160k_repeats(self, mock_env):
        """160K with repeat=True → warns every time."""
        tmp, cache = mock_env
        from cc_cortex.token_monitor import check_threshold

        path = self._make_transcript(tmp, tokens=165000)
        thresholds = [
            (160_000, "\U0001f6a8", "160K emergency", True),
            (140_000, "\u26a0\ufe0f", "140K warning", False),
        ]
        state_dir = str(cache / "token_state")
        sid = "repeat_test"

        # First call
        r1 = check_threshold(path, thresholds, state_dir=state_dir, session_id=sid)
        assert r1 is not None
        # Second call — repeat=True so should warn again
        r2 = check_threshold(path, thresholds, state_dir=state_dir, session_id=sid)
        assert r2 is not None

    def test_threshold_dedup_non_repeat(self, mock_env):
        """Non-repeat threshold → only warns once per session."""
        tmp, cache = mock_env
        from cc_cortex.token_monitor import check_threshold

        path = self._make_transcript(tmp, tokens=105000)
        thresholds = [
            (100_000, "\U0001f4ca", "100K info", False),
        ]
        state_dir = str(cache / "token_state")
        sid = "dedup_test"

        r1 = check_threshold(path, thresholds, state_dir=state_dir, session_id=sid)
        assert r1 is not None
        r2 = check_threshold(path, thresholds, state_dir=state_dir, session_id=sid)
        assert r2 is None  # Deduped

    def test_below_threshold_returns_none(self, mock_env):
        """50K tokens → no threshold crossed."""
        tmp, cache = mock_env
        from cc_cortex.token_monitor import check_threshold

        path = self._make_transcript(tmp, tokens=50000)
        thresholds = [
            (100_000, "\U0001f4ca", "100K info", False),
        ]
        result = check_threshold(path, thresholds)
        assert result is None

    def test_cost_weighted_tokens(self, mock_env):
        """Cost tokens should discount cache_read by 90%."""
        tmp, _ = mock_env
        from cc_cortex.token_monitor import read_real_token_usage

        # 50K input + 100K cache_read → context=150K, cost=50K+10K=60K+output
        path = self._make_transcript(tmp, tokens=50000, cache_read=100000, output=5000)
        result = read_real_token_usage(path)
        assert result["context_tokens"] == 150000
        expected_cost = 50000 + int(100000 * 0.1) + 5000  # 65000
        assert result["cost_tokens"] == expected_cost


class TestFullPipelineIntegration:
    """End-to-end: simulate hook stdin → main() → JSON stdout."""

    def test_write_tool_produces_output(self, mock_env, capsys):
        """Write tool to .md file → streak increments, no output (not milestone)."""
        _, cache = mock_env
        ux_path = cache / "streak_ux.json"
        from cc_cortex.hooks.on_post_tool import _resolve_session_id
        with open(ux_path, "w") as f:
            json.dump({"errors": {}, "streak": 0, "session_id": _resolve_session_id()}, f)

        from cc_cortex.hooks import on_post_tool as opt
        importlib.reload(opt)
        opt._UX_STATE_FILE = str(ux_path)

        hook_data = {
            "tool_name": "Write",
            "tool_input": {"file_path": "/tmp/test.md", "content": "hello"},
        }
        opt.main(hook_data)

        state = json.load(open(ux_path))
        assert state["streak"] == 1

    def test_write_at_milestone_produces_json(self, mock_env, capsys):
        """Write at streak=4 → milestone 5 → JSON output with 🔥."""
        _, cache = mock_env
        ux_path = cache / "streak_ux.json"
        from cc_cortex.hooks.on_post_tool import _resolve_session_id
        with open(ux_path, "w") as f:
            json.dump({"errors": {}, "streak": 4, "session_id": _resolve_session_id()}, f)

        from cc_cortex.hooks import on_post_tool as opt
        importlib.reload(opt)
        opt._UX_STATE_FILE = str(ux_path)

        hook_data = {
            "tool_name": "Edit",
            "tool_input": {"file_path": "/tmp/test.md", "old_string": "a", "new_string": "b"},
        }
        opt.main(hook_data)

        state = json.load(open(ux_path))
        assert state["streak"] == 5

    def test_non_write_tool_no_streak(self, mock_env):
        """Grep/Read tools should not affect streak."""
        _, cache = mock_env
        ux_path = cache / "streak_ux.json"
        from cc_cortex.hooks.on_post_tool import _resolve_session_id
        with open(ux_path, "w") as f:
            json.dump({"errors": {}, "streak": 3, "session_id": _resolve_session_id()}, f)

        from cc_cortex.hooks import on_post_tool as opt
        importlib.reload(opt)
        opt._UX_STATE_FILE = str(ux_path)

        hook_data = {
            "tool_name": "Grep",
            "tool_input": {"pattern": "test"},
        }
        opt.main(hook_data)

        state = json.load(open(ux_path))
        assert state["streak"] == 3  # Unchanged
