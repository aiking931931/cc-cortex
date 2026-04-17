"""Tests for concinno.stop_guard.detect_silent_turn_end.

Root cause: MEMORY #15 handoff cognitive desync + WIREDO-D violation.
After last mutating tool call, assistant must emit a summary; silent
turn end forces the user to grep the diff.
"""

from __future__ import annotations

import pytest

from concinno.stop_guard import (
    _classify_bash,
    _classify_tool_call,
    _find_last_mutation,
    detect_silent_turn_end,
    on_stop,
)

# ---------------------------------------------------------------------------
# Helper: build a fake transcript
# ---------------------------------------------------------------------------


def _msg_assistant_tool(name: str, tool_input: dict | None = None) -> dict:
    return {
        "role": "assistant",
        "content": [
            {
                "type": "tool_use",
                "name": name,
                "id": f"tool_{name}",
                "input": tool_input or {},
            },
        ],
    }


def _msg_tool_result(output: str = "ok") -> dict:
    return {"role": "tool", "content": output}


def _msg_assistant_text(text: str) -> dict:
    return {"role": "assistant", "content": text}


def _msg_user(text: str) -> dict:
    return {"role": "user", "content": text}


# ---------------------------------------------------------------------------
# _classify_bash — unit tests for the command classifier
# ---------------------------------------------------------------------------


class TestClassifyBash:
    def test_git_status_is_inspective(self):
        assert _classify_bash("git status") == "inspective"

    def test_git_log_is_inspective(self):
        assert _classify_bash("git log --oneline -n 5") == "inspective"

    def test_git_commit_is_mutating(self):
        assert _classify_bash("git commit -m 'foo'") == "mutating"

    def test_git_push_is_mutating(self):
        assert _classify_bash("git push origin main") == "mutating"

    def test_git_add_is_mutating(self):
        assert _classify_bash("git add -A") == "mutating"

    def test_git_tag_is_mutating(self):
        assert _classify_bash("git tag v1.17.4") == "mutating"

    def test_ls_is_inspective(self):
        assert _classify_bash("ls -la") == "inspective"

    def test_cat_is_inspective(self):
        assert _classify_bash("cat README.md") == "inspective"

    def test_pytest_is_inspective(self):
        assert _classify_bash("pytest -q") == "inspective"

    def test_twine_upload_is_mutating(self):
        assert _classify_bash("twine upload dist/*") == "mutating"

    def test_pip_install_is_mutating(self):
        assert _classify_bash("pip install foo") == "mutating"

    def test_rm_is_mutating(self):
        assert _classify_bash("rm -rf build/") == "mutating"

    def test_gh_pr_create_is_mutating(self):
        assert _classify_bash("gh pr create --title x") == "mutating"

    def test_redirect_is_mutating(self):
        assert _classify_bash("echo foo > bar.txt") == "mutating"

    def test_chained_inspective_then_mutation_is_mutating(self):
        assert _classify_bash("git status && git commit -m x") == "mutating"

    def test_empty_command_is_inspective(self):
        assert _classify_bash("") == "inspective"


# ---------------------------------------------------------------------------
# _classify_tool_call
# ---------------------------------------------------------------------------


class TestClassifyToolCall:
    def test_write_is_mutating(self):
        assert _classify_tool_call("Write", {}) == "mutating"

    def test_edit_is_mutating(self):
        assert _classify_tool_call("Edit", {}) == "mutating"

    def test_multiedit_is_mutating(self):
        assert _classify_tool_call("MultiEdit", {}) == "mutating"

    def test_read_is_inspective(self):
        assert _classify_tool_call("Read", {}) == "inspective"

    def test_grep_is_inspective(self):
        assert _classify_tool_call("Grep", {}) == "inspective"

    def test_todowrite_is_inspective(self):
        # TodoWrite is self-tracking, not user-visible state
        assert _classify_tool_call("TodoWrite", {}) == "inspective"

    def test_bash_commit_is_mutating(self):
        assert _classify_tool_call(
            "Bash", {"command": "git commit -m x"},
        ) == "mutating"

    def test_bash_status_is_inspective(self):
        assert _classify_tool_call(
            "Bash", {"command": "git status"},
        ) == "inspective"

    def test_unknown_tool_defaults_inspective(self):
        assert _classify_tool_call("SomeFutureTool", {}) == "inspective"


# ---------------------------------------------------------------------------
# detect_silent_turn_end — the main detector
# ---------------------------------------------------------------------------


class TestDetectSilentTurnEnd:
    def test_no_messages_is_silent_but_not_fired(self):
        fired, _ = detect_silent_turn_end({})
        assert not fired

    def test_only_inspective_tools_pass(self):
        data = {"messages": [
            _msg_assistant_tool("Read", {"file_path": "/a.py"}),
            _msg_tool_result(),
            _msg_assistant_tool("Grep", {"pattern": "foo"}),
            _msg_tool_result(),
            # silent end is fine — no mutation happened
        ]}
        fired, _ = detect_silent_turn_end(data)
        assert not fired

    def test_edit_with_long_final_text_passes(self):
        data = {"messages": [
            _msg_user("please edit"),
            _msg_assistant_tool("Edit", {"file_path": "/a.py"}),
            _msg_tool_result(),
            _msg_assistant_text(
                "Done. Rewrote foo() to handle None inputs. "
                "Ran pytest — 12/12 green. Next: add integration test.",
            ),
        ]}
        fired, _ = detect_silent_turn_end(data)
        assert not fired

    def test_edit_without_final_text_fires(self):
        data = {"messages": [
            _msg_user("edit it"),
            _msg_assistant_tool("Edit", {"file_path": "/a.py"}),
            _msg_tool_result(),
            # silent end
        ]}
        fired, msg = detect_silent_turn_end(data)
        assert fired
        assert "Edit" in msg
        assert "silent_turn_guard" in msg

    def test_multiple_commits_with_short_text_fires(self):
        data = {"messages": [
            _msg_assistant_tool("Bash", {"command": "git commit -m 'a'"}),
            _msg_tool_result(),
            _msg_assistant_tool("Bash", {"command": "git commit -m 'b'"}),
            _msg_tool_result(),
            _msg_assistant_text("done"),  # 4 chars, below 30
        ]}
        fired, msg = detect_silent_turn_end(data)
        assert fired
        assert "git commit" in msg or "Bash" in msg

    def test_git_status_only_passes(self):
        data = {"messages": [
            _msg_assistant_tool("Bash", {"command": "git status"}),
            _msg_tool_result(),
            # silent — but only inspection, no mutation
        ]}
        fired, _ = detect_silent_turn_end(data)
        assert not fired

    def test_git_push_silent_end_fires_with_push_in_msg(self):
        data = {"messages": [
            _msg_assistant_tool(
                "Bash", {"command": "git push origin main"},
            ),
            _msg_tool_result(),
        ]}
        fired, msg = detect_silent_turn_end(data)
        assert fired
        assert "push" in msg

    def test_write_and_edit_with_1000_char_summary_passes(self):
        summary = "A" * 1000
        data = {"messages": [
            _msg_assistant_tool("Write", {"file_path": "/a.py"}),
            _msg_tool_result(),
            _msg_assistant_tool("Edit", {"file_path": "/b.py"}),
            _msg_tool_result(),
            _msg_assistant_text(summary),
        ]}
        fired, _ = detect_silent_turn_end(data)
        assert not fired

    def test_env_disable_suppresses_warning(self, monkeypatch):
        monkeypatch.setenv("CCC_SILENT_TURN_GUARD", "0")
        data = {"messages": [
            _msg_assistant_tool("Edit", {"file_path": "/a.py"}),
            _msg_tool_result(),
            # perfect violation but guard disabled
        ]}
        fired, _ = detect_silent_turn_end(data)
        assert not fired

    def test_env_threshold_tuned_higher(self, monkeypatch):
        monkeypatch.setenv("CCC_SILENT_TURN_MIN_CHARS", "100")
        data = {"messages": [
            _msg_assistant_tool("Edit", {"file_path": "/a.py"}),
            _msg_tool_result(),
            _msg_assistant_text("X" * 50),  # below new threshold
        ]}
        fired, _ = detect_silent_turn_end(data)
        assert fired

    def test_env_threshold_tuned_higher_but_over_threshold_passes(
        self, monkeypatch,
    ):
        monkeypatch.setenv("CCC_SILENT_TURN_MIN_CHARS", "100")
        data = {"messages": [
            _msg_assistant_tool("Edit", {"file_path": "/a.py"}),
            _msg_tool_result(),
            _msg_assistant_text("X" * 150),
        ]}
        fired, _ = detect_silent_turn_end(data)
        assert not fired

    def test_todowrite_only_passes(self):
        data = {"messages": [
            _msg_assistant_tool("TodoWrite", {"todos": []}),
            _msg_tool_result(),
            # silent — TodoWrite is self-tracking
        ]}
        fired, _ = detect_silent_turn_end(data)
        assert not fired

    def test_write_then_inspective_then_silent_still_fires(self):
        """Last mutation lives mid-turn; trailing Reads don't rescue it."""
        data = {"messages": [
            _msg_assistant_tool("Write", {"file_path": "/a.py"}),
            _msg_tool_result(),
            _msg_assistant_tool("Read", {"file_path": "/a.py"}),
            _msg_tool_result(),
        ]}
        fired, _ = detect_silent_turn_end(data)
        assert fired

    def test_whitespace_only_final_text_fires(self):
        data = {"messages": [
            _msg_assistant_tool("Edit", {"file_path": "/a.py"}),
            _msg_tool_result(),
            _msg_assistant_text("   \n\n  \t  "),
        ]}
        fired, _ = detect_silent_turn_end(data)
        assert fired


# ---------------------------------------------------------------------------
# _find_last_mutation — transcript scanning
# ---------------------------------------------------------------------------


class TestFindLastMutation:
    def test_none_when_no_mutations(self):
        data = {"messages": [
            _msg_assistant_tool("Read", {}),
            _msg_assistant_tool("Grep", {}),
        ]}
        assert _find_last_mutation(data) is None

    def test_picks_last_of_many(self):
        data = {"messages": [
            _msg_assistant_tool("Edit", {"file_path": "/first.py"}),
            _msg_tool_result(),
            _msg_assistant_tool("Write", {"file_path": "/second.py"}),
            _msg_tool_result(),
        ]}
        result = _find_last_mutation(data)
        assert result is not None
        _, name, tool_input = result
        assert name == "Write"
        assert tool_input.get("file_path") == "/second.py"


# ---------------------------------------------------------------------------
# on_stop integration — stderr side-effect
# ---------------------------------------------------------------------------


class TestOnStopSilentTurnIntegration:
    def test_on_stop_emits_stderr_when_silent(self, capsys, tmp_path,
                                              monkeypatch):
        state_file = str(tmp_path / "block.json")
        monkeypatch.setattr(
            "concinno.stop_guard._BLOCK_STATE_PATH", state_file,
        )
        data = {
            "session_id": "silent-test",
            "messages": [
                _msg_assistant_tool("Edit", {"file_path": "/a.py"}),
                _msg_tool_result(),
                # silent end
            ],
        }
        on_stop(data)
        captured = capsys.readouterr()
        assert "silent_turn_guard" in captured.err

    def test_on_stop_no_stderr_when_long_text(self, capsys, tmp_path,
                                              monkeypatch):
        state_file = str(tmp_path / "block.json")
        monkeypatch.setattr(
            "concinno.stop_guard._BLOCK_STATE_PATH", state_file,
        )
        data = {
            "session_id": "silent-test-2",
            "messages": [
                _msg_assistant_tool("Edit", {"file_path": "/a.py"}),
                _msg_tool_result(),
                _msg_assistant_text(
                    "Edit applied to /a.py — function refactored, "
                    "tests green, committed as c0ffee12.",
                ),
            ],
        }
        on_stop(data)
        captured = capsys.readouterr()
        assert "silent_turn_guard" not in captured.err

    def test_on_stop_disabled_env_no_stderr(self, capsys, tmp_path,
                                            monkeypatch):
        state_file = str(tmp_path / "block.json")
        monkeypatch.setattr(
            "concinno.stop_guard._BLOCK_STATE_PATH", state_file,
        )
        monkeypatch.setenv("CCC_SILENT_TURN_GUARD", "0")
        data = {
            "session_id": "silent-test-3",
            "messages": [
                _msg_assistant_tool("Edit", {"file_path": "/a.py"}),
                _msg_tool_result(),
            ],
        }
        on_stop(data)
        captured = capsys.readouterr()
        assert "silent_turn_guard" not in captured.err


# ---------------------------------------------------------------------------
# Regression: env var parsing corner cases
# ---------------------------------------------------------------------------


class TestEnvParsing:
    @pytest.mark.parametrize("bad_value", ["", "abc", "-5", "1.5"])
    def test_min_chars_invalid_falls_back_to_default(
        self, monkeypatch, bad_value,
    ):
        monkeypatch.setenv("CCC_SILENT_TURN_MIN_CHARS", bad_value)
        # 29 < 30 default, should fire
        data = {"messages": [
            _msg_assistant_tool("Edit", {"file_path": "/a.py"}),
            _msg_tool_result(),
            _msg_assistant_text("X" * 29),
        ]}
        fired, _ = detect_silent_turn_end(data)
        assert fired

    def test_guard_default_enabled(self, monkeypatch):
        # Ensure no leftover env var from other tests
        monkeypatch.delenv("CCC_SILENT_TURN_GUARD", raising=False)
        data = {"messages": [
            _msg_assistant_tool("Edit", {"file_path": "/a.py"}),
            _msg_tool_result(),
        ]}
        fired, _ = detect_silent_turn_end(data)
        assert fired
