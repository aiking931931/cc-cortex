"""Tests for cc_cortex.sentinel module."""

from __future__ import annotations

import os

from cc_cortex.sentinel import (
    DEFAULT_THRESHOLDS,
    check,
    gate_consecutive_fail,
    gate_sentinel,
    infer_success,
    record_outcome,
)

# ── Helpers ──────────────────────────────────────────────


def _call(state_dir, sid, tool, path="", extra=None, **kwargs):
    """Shortcut for calling check()."""
    tool_input = {"file_path": path}
    if extra:
        tool_input.update(extra)
    return check(sid, tool, tool_input, str(state_dir), **kwargs)


# ── check() basics ───────────────────────────────────────


def test_check_returns_none_for_empty_session_id(tmp_path):
    result = check("", "Edit", {"file_path": "x.py"}, str(tmp_path))
    assert result is None


def test_check_no_warnings_on_first_call(tmp_path):
    result = _call(tmp_path, "sess1", "Edit", "/a.py")
    assert result is None


# ── Repeat detection ─────────────────────────────────────


def test_check_repeat_warning(tmp_path):
    """After N same-tool same-file writes, get a repeat warning."""
    sid = "repeat_test"
    threshold = DEFAULT_THRESHOLDS["repeat"]  # 3

    for _ in range(threshold - 1):
        _call(tmp_path, sid, "Edit", "/a.py")

    result = _call(tmp_path, sid, "Edit", "/a.py")
    assert result is not None
    types = [w["type"] for w in result]
    assert "repeat" in types


def test_check_repeat_force_warning(tmp_path):
    """After repeat_force threshold, get a repeat_force warning."""
    sid = "force_test"
    threshold = DEFAULT_THRESHOLDS["repeat_force"]  # 5

    for _ in range(threshold - 1):
        _call(tmp_path, sid, "Edit", "/b.py")

    result = _call(tmp_path, sid, "Edit", "/b.py")
    assert result is not None
    types = [w["type"] for w in result]
    assert "repeat_force" in types


# ── Stagnation detection ─────────────────────────────────


def test_check_stagnation_on_identical_edit_sigs(tmp_path):
    sid = "stale_test"
    stale_th = DEFAULT_THRESHOLDS["stale"]  # 2

    edit_input = {
        "file_path": "/c.py",
        "old_string": "foo",
        "new_string": "bar",
    }
    for _ in range(stale_th):
        result = check(sid, "Edit", edit_input, str(tmp_path))

    assert result is not None
    types = [w["type"] for w in result]
    assert "stagnation" in types


# ── Analysis paralysis ───────────────────────────────────


def test_check_paralysis_after_consecutive_reads(tmp_path):
    sid = "paralysis_test"
    threshold = DEFAULT_THRESHOLDS["paralysis"]  # 7

    for _ in range(threshold - 1):
        _call(tmp_path, sid, "Read", "/d.py")

    result = _call(tmp_path, sid, "Read", "/d.py")
    assert result is not None
    types = [w["type"] for w in result]
    assert "paralysis_start" in types


def test_check_paralysis_end_on_write_after_paralysis(tmp_path):
    sid = "paralysis_end_test"
    threshold = DEFAULT_THRESHOLDS["paralysis"]

    # Trigger paralysis
    for _ in range(threshold):
        _call(tmp_path, sid, "Read", "/e.py")

    # Switch to write → should get paralysis_end
    result = _call(tmp_path, sid, "Edit", "/e.py")
    assert result is not None
    types = [w["type"] for w in result]
    assert "paralysis_end" in types


# ── Scope detection ──────────────────────────────────────


def test_check_scope_warning_many_files(tmp_path):
    sid = "scope_test"
    scope_th = DEFAULT_THRESHOLDS["scope"]  # 10

    for i in range(scope_th):
        _call(tmp_path, sid, "Edit", f"/file_{i}.py")

    # The Nth call should trigger scope warning
    result = _call(tmp_path, sid, "Edit", f"/file_{scope_th}.py")
    # At least one call in the sequence should have scope warning
    # Re-check: scope looks at all calls in window
    assert result is not None
    types = [w["type"] for w in result]
    assert "scope" in types


# ── Split detection ──────────────────────────────────────


def test_check_split_warning(tmp_path):
    sid = "split_test"
    split_th = DEFAULT_THRESHOLDS["split"]  # 4

    for i in range(split_th - 1):
        _call(tmp_path, sid, "Edit", f"/split_{i}.py")

    result = _call(tmp_path, sid, "Edit", f"/split_{split_th - 1}.py")
    assert result is not None
    types = [w["type"] for w in result]
    assert "split" in types


# ── Custom thresholds ────────────────────────────────────


def test_custom_thresholds_override(tmp_path):
    sid = "custom_th"
    # Set repeat to 2 (lower than default 3)
    custom = {"repeat": 2}

    _call(tmp_path, sid, "Edit", "/ct.py", thresholds=custom)
    result = _call(tmp_path, sid, "Edit", "/ct.py", thresholds=custom)
    assert result is not None
    types = [w["type"] for w in result]
    assert "repeat" in types


# ── gate_sentinel ────────────────────────────────────────


def test_gate_sentinel_returns_none_for_read_tools(tmp_path):
    result = gate_sentinel("sess1", "Read", {"file_path": "/a.py"}, str(tmp_path))
    assert result is None


def test_gate_sentinel_returns_none_below_max_repeats(tmp_path):
    sid = "gate_below"
    state_dir = str(tmp_path)

    # Record 2 calls (below default max_repeats=5)
    for _ in range(2):
        _call(tmp_path, sid, "Edit", "/g.py")

    result = gate_sentinel(sid, "Edit", {"file_path": "/g.py"}, state_dir)
    assert result is None


def test_gate_sentinel_returns_deny_at_max_repeats(tmp_path):
    sid = "gate_deny"
    state_dir = str(tmp_path)

    # Record max_repeats calls via check()
    for _ in range(5):
        _call(tmp_path, sid, "Edit", "/h.py")

    result = gate_sentinel(sid, "Edit", {"file_path": "/h.py"}, state_dir)
    assert result is not None
    assert result["permissionDecision"] == "deny"
    assert "Sentinel Gate" in result["reason"]


def test_gate_sentinel_returns_none_for_empty_session_id(tmp_path):
    result = gate_sentinel("", "Edit", {"file_path": "/a.py"}, str(tmp_path))
    assert result is None


# ── State persistence ────────────────────────────────────


# ── Bash retry detection ────────────────────────────────


def test_check_bash_retry_warning(tmp_path):
    """After N similar Bash commands, get a bash_retry warning."""
    sid = "bash_retry_test"
    threshold = DEFAULT_THRESHOLDS["bash_retry"]  # 3

    cmd = "npm run build --production"
    for _ in range(threshold - 1):
        _call(
            tmp_path, sid, "Bash", "",
            extra={"command": cmd},
        )

    result = _call(
        tmp_path, sid, "Bash", "",
        extra={"command": cmd},
    )
    assert result is not None
    types = [w["type"] for w in result]
    assert "bash_retry" in types


def test_check_bash_retry_no_warning_different_cmds(tmp_path):
    """Different Bash commands should not trigger bash_retry."""
    sid = "bash_diff_test"

    _call(tmp_path, sid, "Bash", "", extra={"command": "ls"})
    _call(tmp_path, sid, "Bash", "", extra={"command": "pwd"})
    result = _call(
        tmp_path, sid, "Bash", "",
        extra={"command": "cat x"},
    )
    assert result is None


# ── Consecutive failure detection ────────────────────────


def test_check_consecutive_fail_warning(tmp_path):
    """After N consecutive failures, get a consecutive_fail warning."""
    sid = "fail_test"
    threshold = DEFAULT_THRESHOLDS["consecutive_fail"]  # 3

    for _ in range(threshold - 1):
        _call(tmp_path, sid, "Bash", "", extra={"command": "make"}, success=False)

    result = _call(
        tmp_path, sid, "Bash", "",
        extra={"command": "make build"},
        success=False,
    )
    assert result is not None
    types = [w["type"] for w in result]
    assert "consecutive_fail" in types


def test_check_consecutive_fail_reset_on_success(tmp_path):
    """A successful call resets the consecutive failure counter."""
    sid = "fail_reset_test"

    _call(tmp_path, sid, "Bash", "", extra={"command": "make"}, success=False)
    _call(tmp_path, sid, "Bash", "", extra={"command": "make"}, success=False)
    # Success resets
    _call(tmp_path, sid, "Edit", "/x.py", success=True)
    # Two more failures — below threshold of 3
    _call(tmp_path, sid, "Bash", "", extra={"command": "make"}, success=False)
    result = _call(
        tmp_path, sid, "Bash", "",
        extra={"command": "make"},
        success=False,
    )
    # Only 2 consecutive fails after the success, below threshold
    if result:
        types = [w["type"] for w in result]
        assert "consecutive_fail" not in types


def test_check_no_consecutive_fail_without_success_flag(tmp_path):
    """Without success=False, no consecutive_fail warning."""
    sid = "no_flag_test"

    for _ in range(5):
        _call(tmp_path, sid, "Bash", "", extra={"command": "make"})

    # success not passed → no consecutive_fail check
    result = _call(tmp_path, sid, "Bash", "", extra={"command": "make"})
    if result:
        types = [w["type"] for w in result]
        assert "consecutive_fail" not in types


# ── State persistence ────────────────────────────────────


def test_state_persistence_across_calls(tmp_path):
    from cc_cortex.core.state_store import StateStore
    sid = "persist_test"
    state_dir = str(tmp_path)

    _call(tmp_path, sid, "Edit", "/p.py")
    _call(tmp_path, sid, "Edit", "/p.py")

    # Read via the public StateStore API instead of hardcoding the
    # filename. The previous `{sid[:8]}.json` shape broke when
    # state_store switched to a 16-hex blake2b digest for collision
    # safety.
    state = StateStore(state_dir).read("sentinel", sid, default=None)
    assert state is not None

    assert len(state["calls"]) == 2
    assert all(c["tool"] == "Edit" for c in state["calls"])
    assert all(c["path"] == "/p.py" for c in state["calls"])


# ── infer_success ───────────────────────────────────────


def test_infer_success_edit_ok():
    assert infer_success("Edit", "Applied edit successfully") is True


def test_infer_success_edit_not_found():
    assert infer_success("Edit", "old_string not found in file") is False


def test_infer_success_edit_not_unique():
    assert infer_success("Edit", "Match is not unique in file") is False


def test_infer_success_bash_ok():
    assert infer_success("Bash", "total 42\ndrwxr-xr-x") is True


def test_infer_success_bash_cmd_not_found():
    assert infer_success("Bash", "foobar: command not found") is False


def test_infer_success_bash_no_such_file():
    assert infer_success("Bash", "cat: /x: No such file or directory") is False


def test_infer_success_unknown_tool():
    assert infer_success("Agent", "some output") is None


def test_infer_success_empty_result():
    assert infer_success("Edit", "") is None


# ── record_outcome ──────────────────────────────────────


def test_record_outcome_writes_state(tmp_path):
    from cc_cortex.core.state_store import StateStore
    sid = "rec_test"
    record_outcome(sid, "Edit", {"file_path": "/a.py"}, str(tmp_path), success=True)
    state = StateStore(str(tmp_path)).read("sentinel", sid, default=None)
    assert state is not None
    assert len(state["calls"]) == 1
    assert state["calls"][0]["ok"] is True


def test_record_outcome_appends(tmp_path):
    from cc_cortex.core.state_store import StateStore
    sid = "rec_append"
    d = str(tmp_path)
    record_outcome(sid, "Edit", {"file_path": "/a.py"}, d, success=True)
    record_outcome(sid, "Bash", {"command": "ls"}, d, success=False)
    state = StateStore(d).read("sentinel", sid, default=None)
    assert state is not None
    assert len(state["calls"]) == 2
    assert state["calls"][0]["ok"] is True
    assert state["calls"][1]["ok"] is False


def test_record_outcome_skips_empty_session(tmp_path):
    record_outcome("", "Edit", {"file_path": "/a.py"}, str(tmp_path), success=True)
    assert not os.listdir(str(tmp_path))


# ── gate_consecutive_fail ───────────────────────────────


def test_gate_consec_fail_no_state(tmp_path):
    result = gate_consecutive_fail("sess1", str(tmp_path))
    assert result is None


def test_gate_consec_fail_below_threshold(tmp_path):
    sid = "gcf_below"
    d = str(tmp_path)
    record_outcome(sid, "Bash", {"command": "x"}, d, success=False)
    record_outcome(sid, "Bash", {"command": "x"}, d, success=False)
    result = gate_consecutive_fail(sid, d, max_fails=3)
    assert result is None


def test_gate_consec_fail_at_threshold(tmp_path):
    sid = "gcf_deny"
    d = str(tmp_path)
    for _ in range(3):
        record_outcome(sid, "Bash", {"command": "x"}, d, success=False)
    result = gate_consecutive_fail(sid, d, max_fails=3)
    assert result is not None
    assert result["permissionDecision"] == "deny"
    assert "three strikes" in result["reason"].lower() or "consecutive" in result["reason"].lower()


def test_gate_consec_fail_reset_by_success(tmp_path):
    sid = "gcf_reset"
    d = str(tmp_path)
    record_outcome(sid, "Bash", {"command": "x"}, d, success=False)
    record_outcome(sid, "Bash", {"command": "x"}, d, success=False)
    record_outcome(sid, "Edit", {"file_path": "/a.py"}, d, success=True)
    record_outcome(sid, "Bash", {"command": "x"}, d, success=False)
    record_outcome(sid, "Bash", {"command": "x"}, d, success=False)
    result = gate_consecutive_fail(sid, d, max_fails=3)
    assert result is None  # Only 2 fails after success


def test_gate_consec_fail_ignores_unknown(tmp_path):
    sid = "gcf_unk"
    d = str(tmp_path)
    record_outcome(sid, "Bash", {"command": "x"}, d, success=False)
    record_outcome(sid, "Agent", {}, d, success=None)  # unknown
    record_outcome(sid, "Bash", {"command": "x"}, d, success=False)
    # 2 fails with 1 unknown in between — unknown doesn't count or break
    result = gate_consecutive_fail(sid, d, max_fails=3)
    assert result is None  # only 2 counted


def test_gate_consec_fail_empty_session(tmp_path):
    result = gate_consecutive_fail("", str(tmp_path))
    assert result is None


# ── Three Strikes: same-problem signature tracking ─────


def test_three_strikes_same_sig_triggers(tmp_path):
    """Same error signature 3 times → deny with Skill/WebSearch instruction."""
    sid = "ts_sig"
    d = str(tmp_path)
    err = "Error: old_string not found in file"
    for _ in range(3):
        record_outcome(sid, "Edit", {"file_path": "/a.py"}, d,
                       success=False, tool_result=err)
    result = gate_consecutive_fail(sid, d, max_fails=3)
    assert result is not None
    assert result["permissionDecision"] == "deny"
    assert "edit:old_string_not_found" in result["reason"]
    assert "Skill" in result.get("additionalContext", "")
    assert "WebSearch" in result.get("additionalContext", "")


def test_three_strikes_different_sigs_no_trigger(tmp_path):
    """Different error signatures don't accumulate — each counts separately."""
    sid = "ts_diff"
    d = str(tmp_path)
    record_outcome(sid, "Edit", {"file_path": "/a.py"}, d,
                   success=False, tool_result="old_string not found in file")
    record_outcome(sid, "Edit", {"file_path": "/a.py"}, d,
                   success=False, tool_result="old_string is not unique")
    record_outcome(sid, "Edit", {"file_path": "/a.py"}, d,
                   success=False, tool_result="old_string not found in file")
    # 2x old_string_not_found + 1x not_unique = neither hits 3
    result = gate_consecutive_fail(sid, d, max_fails=3)
    assert result is None


def test_three_strikes_success_resets_sig(tmp_path):
    """Success clears all sig counters — fixed problem doesn't carry over."""
    sid = "ts_reset"
    d = str(tmp_path)
    err = "Error: old_string not found in file"
    record_outcome(sid, "Edit", {"file_path": "/a.py"}, d,
                   success=False, tool_result=err)
    record_outcome(sid, "Edit", {"file_path": "/a.py"}, d,
                   success=False, tool_result=err)
    # Fix it
    record_outcome(sid, "Edit", {"file_path": "/a.py"}, d, success=True)
    # New failure — count starts fresh
    record_outcome(sid, "Edit", {"file_path": "/a.py"}, d,
                   success=False, tool_result=err)
    result = gate_consecutive_fail(sid, d, max_fails=3)
    assert result is None  # Only 1 fail after success


def test_three_strikes_bash_tsc_sig(tmp_path):
    """TypeScript error signature extraction works."""
    sid = "ts_tsc"
    d = str(tmp_path)
    err = "src/index.ts(42,5): error TS2339: Property 'foo' does not exist"
    for _ in range(3):
        record_outcome(sid, "Bash", {"command": "tsc"}, d,
                       success=False, tool_result=err)
    result = gate_consecutive_fail(sid, d, max_fails=3)
    assert result is not None
    assert "TS2339" in result["reason"]


def test_three_strikes_bash_python_sig(tmp_path):
    """Python error signature extraction works."""
    sid = "ts_py"
    d = str(tmp_path)
    err = "Traceback...\nTypeError: cannot unpack non-iterable NoneType"
    for _ in range(3):
        record_outcome(sid, "Bash", {"command": "python x.py"}, d,
                       success=False, tool_result=err)
    result = gate_consecutive_fail(sid, d, max_fails=3)
    assert result is not None
    assert "TypeError" in result["reason"]
