"""Tests for 2.7.0 F4 — AskUserQuestion toast hook.

User reported twice in one session that AskUserQuestion dialogs block
silently; they'd wait 10+ minutes before noticing. The hook wraps
``concinno.core.notify.show_toast`` so the operator sees a Windows
toast the instant Claude opens an AskUser prompt.

These tests pin:
* Matcher: only ``AskUserQuestion`` fires the toast.
* Body: first 60 chars of the question appear verbatim.
* Fallback: missing question field still emits a toast.
* Fail-open: notify exception → hook still ALLOWs.
* Hook protocol: main() always writes a valid hook JSON response.
"""

from __future__ import annotations

import io
from unittest.mock import patch

from concinno.hooks.ask_user_toast import (
    _extract_question_preview,
    main,
    maybe_show_ask_user_toast,
)


# ── matcher ───────────────────────────────────────────────────


def test_non_ask_user_tool_skips_toast():
    hook_data = {"tool_name": "Edit", "tool_input": {"file_path": "x.py"}}
    with patch(
        "concinno.core.notify.show_toast", return_value=True,
    ) as mock_toast:
        assert maybe_show_ask_user_toast(hook_data) is False
        assert mock_toast.call_count == 0


def test_ask_user_question_triggers_toast():
    hook_data = {
        "tool_name": "AskUserQuestion",
        "tool_input": {"question": "Shall we ship 2.7.0 now?"},
    }
    with patch(
        "concinno.core.notify.show_toast", return_value=True,
    ) as mock_toast:
        assert maybe_show_ask_user_toast(hook_data) is True
        assert mock_toast.call_count == 1
        _, kwargs = mock_toast.call_args
        # Title must be the fixed string so Windows coalesces them.
        assert "Claude" in kwargs["title"]
        # Body must include the first chars of the question.
        assert "Shall we ship 2.7.0 now?" in kwargs["message"]


# ── preview extraction ────────────────────────────────────────


def test_extract_prefers_question_key():
    assert _extract_question_preview(
        {"question": "what's the color?", "prompt": "wrong"},
    ) == "what's the color?"


def test_extract_falls_back_to_prompt_key():
    assert (
        _extract_question_preview({"prompt": "do the thing"})
        == "do the thing"
    )


def test_extract_truncates_long_question():
    long = "x" * 500
    preview = _extract_question_preview({"question": long})
    assert len(preview) <= 60


def test_extract_handles_nested_questions_list():
    data = {"questions": [{"question": "nested q"}]}
    assert _extract_question_preview(data) == "nested q"


def test_extract_handles_empty_input():
    # Empty dict → falls back to str(empty dict)[:60].
    # The important guarantee: no exception.
    out = _extract_question_preview({})
    assert isinstance(out, str)


def test_extract_non_dict_returns_empty():
    assert _extract_question_preview("not a dict") == ""
    assert _extract_question_preview(None) == ""


# ── fail-open contract ────────────────────────────────────────


def test_notify_crash_is_failopen():
    hook_data = {
        "tool_name": "AskUserQuestion",
        "tool_input": {"question": "x"},
    }
    with patch(
        "concinno.core.notify.show_toast",
        side_effect=RuntimeError("notify broken"),
    ):
        # Must NOT raise. Returns False because toast failed.
        assert maybe_show_ask_user_toast(hook_data) is False


def test_notify_import_missing_is_failopen():
    """A broken install (notify module unloadable) must not break hook."""
    import sys as _sys

    hook_data = {
        "tool_name": "AskUserQuestion",
        "tool_input": {"question": "x"},
    }
    # Simulate the import failing by poisoning sys.modules.
    with patch.dict(_sys.modules, {"concinno.core.notify": None}):
        # Hook has its own fail-safe.
        assert maybe_show_ask_user_toast(hook_data) is False


# ── main() hook protocol ──────────────────────────────────────


def test_main_always_writes_allow_decision():
    hook_data = {
        "tool_name": "AskUserQuestion",
        "tool_input": {"question": "Proceed?"},
    }
    buf = io.StringIO()
    with patch(
        "concinno.core.notify.show_toast", return_value=True,
    ), patch("sys.stdout", buf):
        main(hook_data)
    import json

    out = json.loads(buf.getvalue())
    assert out == {"permissionDecision": "allow"}


def test_main_empty_hook_data_still_allows():
    buf = io.StringIO()
    with patch("sys.stdout", buf):
        main({})
    import json

    out = json.loads(buf.getvalue())
    assert out == {"permissionDecision": "allow"}


def test_main_non_ask_user_tool_does_not_toast_but_allows():
    hook_data = {"tool_name": "Edit", "tool_input": {}}
    buf = io.StringIO()
    with patch(
        "concinno.core.notify.show_toast", return_value=True,
    ) as mock_toast, patch("sys.stdout", buf):
        main(hook_data)
    assert mock_toast.call_count == 0
    import json

    out = json.loads(buf.getvalue())
    assert out == {"permissionDecision": "allow"}
