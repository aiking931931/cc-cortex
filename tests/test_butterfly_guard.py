"""Tests for concinno.butterfly_guard module."""

from __future__ import annotations

import time

from concinno.butterfly_guard import (
    ButterflyGuard,
    DiscoveredIssue,
    IssueLedger,
    detect_issues,
    is_clean_result,
    is_verify_command,
)
from concinno.guards.base import GuardContext  # noqa: I001

# ── helpers ──────────────────────────────────────────────


def _ctx(
    tool_name: str = "Bash",
    tool_input: dict | None = None,
    tool_result: str = "",
    session_id: str = "test-session",
    cache_dir: str = "",
    hook_event: str = "PreToolUse",
) -> GuardContext:
    return GuardContext(
        tool_name=tool_name,
        tool_input=tool_input or {},
        session_id=session_id,
        cache_dir=cache_dir,
        hook_event=hook_event,
        tool_result=tool_result,
    )


def _make_issue(
    file_path: str = "src/app.ts",
    category: str = "tsc",
    snippet: str = "error TS2304: Cannot find name 'foo'",
    fix_attempts: int = 0,
    resolved: bool = False,
    deferred: str = "",
) -> DiscoveredIssue:
    return DiscoveredIssue(
        id=f"bf-{int(time.time() * 1000)}",
        source_tool="Bash",
        file_path=file_path,
        error_snippet=snippet,
        category=category,
        discovered_at=time.time(),
        fix_attempts=fix_attempts,
        resolved=resolved,
        deferred_reason=deferred,
    )


# ── detect_issues ────────────────────────────────────────


def test_detect_tsc_error():
    output = "src/app.ts:10:5 - error TS2304: Cannot find name 'x'."
    issues = detect_issues("Bash", output)
    assert len(issues) >= 1
    assert issues[0].category == "tsc"


def test_detect_ruff_lint():
    output = "ruff check src/main.py:42:1: E302 expected 2 blank lines"
    issues = detect_issues("Bash", output, file_path="src/main.py")
    assert len(issues) >= 1
    assert issues[0].category == "lint"


def test_detect_test_failure_fail_keyword():
    output = "FAIL tests/test_foo.py::test_bar"
    issues = detect_issues("Bash", output)
    assert len(issues) >= 1
    assert issues[0].category == "test"


def test_detect_test_failure_count():
    output = "Tests: 3 failed, 7 passed"
    issues = detect_issues("Bash", output)
    assert len(issues) >= 1
    assert issues[0].category == "test"


def test_detect_build_error():
    output = "error[e0425]: cannot find value x"
    issues = detect_issues("Bash", output)
    assert len(issues) >= 1
    assert issues[0].category == "build"


def test_detect_runtime_traceback():
    output = "Traceback (most recent call last):\n  File 'x.py', line 1"
    issues = detect_issues("Bash", output)
    assert len(issues) >= 1
    assert issues[0].category == "runtime"


def test_detect_exit_code():
    output = "Process finished with exit code 1"
    issues = detect_issues("Bash", output)
    assert len(issues) >= 1


def test_ignores_noise_deprecation():
    output = "DeprecationWarning: something old"
    issues = detect_issues("Bash", output)
    assert len(issues) == 0


def test_ignores_noise_npm_warn():
    output = "npm warn deprecated glob@7.2.3"
    issues = detect_issues("Bash", output)
    assert len(issues) == 0


def test_ignores_error_handler_variable():
    output = "const error_handler = new ErrorHandler();"
    issues = detect_issues("Bash", output)
    assert len(issues) == 0


def test_clean_output_returns_empty():
    output = "All 42 tests passed in 1.2s"
    issues = detect_issues("Bash", output)
    assert len(issues) == 0


def test_one_issue_per_category():
    output = (
        "error TS2304: Cannot find 'a'\n"
        "error TS2304: Cannot find 'b'\n"
        "error TS2304: Cannot find 'c'\n"
    )
    issues = detect_issues("Bash", output)
    cats = [i.category for i in issues]
    assert cats.count("tsc") == 1


def test_extracts_file_path():
    output = "src/utils.ts:15:3 - error TS2304: Cannot find 'z'"
    issues = detect_issues("Bash", output)
    assert len(issues) >= 1
    assert "utils.ts" in issues[0].file_path


def test_empty_output_returns_empty():
    assert detect_issues("Bash", "") == []
    assert detect_issues("Bash", "hi") == []


# ── is_verify_command ────────────────────────────────────


def test_verify_recognises_ruff():
    assert is_verify_command("ruff check src/") is True


def test_verify_recognises_pytest():
    assert is_verify_command("pytest tests/ -v") is True


def test_verify_recognises_vitest():
    assert is_verify_command("npx vitest run") is True


def test_verify_recognises_jest():
    assert is_verify_command("jest --coverage") is True


def test_verify_recognises_tsc():
    assert is_verify_command("npx tsc --noEmit") is True


def test_verify_recognises_eslint():
    assert is_verify_command("eslint src/") is True


def test_verify_recognises_cargo_test():
    assert is_verify_command("cargo test") is True


def test_verify_recognises_go_test():
    assert is_verify_command("go test ./...") is True


def test_verify_rejects_non_verify():
    assert is_verify_command("git status") is False
    assert is_verify_command("echo hello") is False
    assert is_verify_command("cat file.py") is False


# ── is_clean_result ──────────────────────────────────────


def test_clean_all_checks_passed():
    assert is_clean_result("All checks passed") is True


def test_clean_zero_errors():
    assert is_clean_result("Found 0 errors in 10 files") is True


def test_clean_tests_passed():
    assert is_clean_result("Tests: 42 passed, 0 failed") is True


def test_clean_false_for_errors():
    assert is_clean_result("error TS2304: Cannot find 'x'") is False


def test_clean_false_for_empty():
    assert is_clean_result("") is False


# ── IssueLedger ──────────────────────────────────────────


def test_ledger_add_creates_issue(tmp_path):
    ledger = IssueLedger(str(tmp_path), "s1")
    issue = _make_issue()
    assert ledger.add(issue) is True
    assert len(ledger.open_issues()) == 1


def test_ledger_add_deduplicates(tmp_path):
    ledger = IssueLedger(str(tmp_path), "s1")
    i1 = _make_issue(file_path="a.ts", category="tsc")
    i2 = _make_issue(file_path="a.ts", category="tsc")
    assert ledger.add(i1) is True
    assert ledger.add(i2) is False
    assert len(ledger.open_issues()) == 1


def test_ledger_open_issues_filters(tmp_path):
    ledger = IssueLedger(str(tmp_path), "s1")
    ledger.add(_make_issue(file_path="a.ts", category="tsc"))
    ledger.add(_make_issue(
        file_path="b.ts", category="lint", resolved=True,
    ))
    # resolved issue added directly to internal list
    resolved = _make_issue(file_path="c.ts", category="test")
    resolved.resolved = True
    ledger.add(resolved)
    # only the unresolved one
    assert len(ledger.open_issues()) == 1


def test_ledger_resolve_for_file(tmp_path):
    ledger = IssueLedger(str(tmp_path), "s1")
    ledger.add(_make_issue(file_path="src/a.ts", category="tsc"))
    count = ledger.resolve_for_file("src/a.ts")
    assert count == 1
    assert len(ledger.open_issues()) == 0


def test_ledger_resolve_by_category(tmp_path):
    ledger = IssueLedger(str(tmp_path), "s1")
    ledger.add(_make_issue(file_path="a.ts", category="lint"))
    ledger.add(_make_issue(file_path="b.ts", category="lint"))
    # different file same category — second should add
    # Force add by making it unique
    ledger._issues[-1].file_path = "b.ts"
    count = ledger.resolve_by_category("lint")
    assert count >= 1
    assert len(ledger.open_issues()) == 0


def test_ledger_increment_fix_attempt(tmp_path):
    ledger = IssueLedger(str(tmp_path), "s1")
    ledger.add(_make_issue(file_path="src/x.ts", category="tsc"))
    ledger.increment_fix_attempt("src/x.ts")
    assert ledger.open_issues()[0].fix_attempts == 1


def test_ledger_get_stuck_issues(tmp_path):
    ledger = IssueLedger(str(tmp_path), "s1")
    issue = _make_issue(file_path="s.ts", category="tsc", fix_attempts=3)
    ledger.add(issue)
    stuck = ledger.get_stuck_issues()
    assert len(stuck) == 1


def test_ledger_defer_issue(tmp_path):
    ledger = IssueLedger(str(tmp_path), "s1")
    issue = _make_issue()
    ledger.add(issue)
    iid = ledger.open_issues()[0].id
    assert ledger.defer_issue(iid, "needs next session") is True
    assert len(ledger.open_issues()) == 0


def test_ledger_summary(tmp_path):
    ledger = IssueLedger(str(tmp_path), "s1")
    ledger.add(_make_issue(file_path="f.ts", category="tsc"))
    s = ledger.summary()
    assert "1 discovered issue" in s
    assert "tsc" in s


def test_ledger_handoff_block(tmp_path):
    ledger = IssueLedger(str(tmp_path), "s1")
    ledger.add(_make_issue(file_path="f.ts", category="tsc"))
    block = ledger.handoff_block()
    assert "Butterfly Effect" in block
    assert "open" in block


def test_ledger_cap_at_20(tmp_path):
    ledger = IssueLedger(str(tmp_path), "s1")
    for i in range(25):
        issue = _make_issue(
            file_path=f"file{i}.ts", category="tsc",
        )
        # Force unique id
        issue.id = f"bf-{i}"
        # Force unique category so dedup doesn't kick in
        issue.category = f"cat{i}"
        ledger.add(issue)
    assert len(ledger._issues) <= 20


def test_ledger_session_isolation(tmp_path):
    ledger1 = IssueLedger(str(tmp_path), "session-A")
    ledger1.add(_make_issue(file_path="a.ts", category="tsc"))
    assert len(ledger1.open_issues()) == 1

    ledger2 = IssueLedger(str(tmp_path), "session-B")
    assert len(ledger2.open_issues()) == 0


# ── ButterflyGuard.check (PreToolUse) ───────────────────


def test_check_none_when_no_issues(tmp_path):
    guard = ButterflyGuard()
    ctx = _ctx(tool_name="Write", cache_dir=str(tmp_path))
    assert guard.check(ctx) is None


def test_check_allows_investigation_tools(tmp_path):
    guard = ButterflyGuard()
    # Seed an issue so there are open issues
    ledger = IssueLedger(str(tmp_path), "test-session")
    ledger.add(_make_issue())
    guard._ledger = ledger

    for tool in ("Read", "Grep", "Glob", "Agent"):
        ctx = _ctx(tool_name=tool, cache_dir=str(tmp_path))
        assert guard.check(ctx) is None


def test_check_allows_edit_to_issue_file(tmp_path):
    guard = ButterflyGuard()
    ledger = IssueLedger(str(tmp_path), "test-session")
    ledger.add(_make_issue(file_path="src/app.ts"))
    guard._ledger = ledger

    ctx = _ctx(
        tool_name="Edit",
        tool_input={"file_path": "src/app.ts", "old_string": "x", "new_string": "y"},
        cache_dir=str(tmp_path),
    )
    assert guard.check(ctx) is None


def test_check_allows_bash_verify(tmp_path):
    guard = ButterflyGuard()
    ledger = IssueLedger(str(tmp_path), "test-session")
    ledger.add(_make_issue())
    guard._ledger = ledger

    ctx = _ctx(
        tool_name="Bash",
        tool_input={"command": "npx tsc --noEmit"},
        cache_dir=str(tmp_path),
    )
    assert guard.check(ctx) is None


def test_check_denies_write_to_other_file(tmp_path):
    guard = ButterflyGuard()
    ledger = IssueLedger(str(tmp_path), "test-session")
    ledger.add(_make_issue(file_path="src/app.ts"))
    guard._ledger = ledger

    ctx = _ctx(
        tool_name="Write",
        tool_input={"file_path": "src/other.ts", "content": "..."},
        cache_dir=str(tmp_path),
    )
    result = guard.check(ctx)
    assert result is not None
    assert result.action.value == "deny"


def test_check_denies_stuck_issues(tmp_path):
    guard = ButterflyGuard()
    ledger = IssueLedger(str(tmp_path), "test-session")
    ledger.add(_make_issue(
        file_path="stuck.ts", category="tsc", fix_attempts=3,
    ))
    guard._ledger = ledger

    ctx = _ctx(
        tool_name="Write",
        tool_input={"file_path": "stuck.ts", "content": "..."},
        cache_dir=str(tmp_path),
    )
    result = guard.check(ctx)
    assert result is not None
    assert result.action.value == "deny"
    assert "handoff" in result.reason.lower()


# ── ButterflyGuard.on_post_tool (PostToolUse) ────────────


def test_post_tool_detects_issues(tmp_path):
    guard = ButterflyGuard()
    ctx = _ctx(
        tool_name="Bash",
        tool_input={"command": "tsc"},
        tool_result="src/x.ts:5:1 - error TS2304: Cannot find 'y'",
        cache_dir=str(tmp_path),
        hook_event="PostToolUse",
    )
    result = guard.on_post_tool(ctx)
    assert result is not None
    assert "issue" in (result.context or "").lower()


def test_post_tool_resolves_on_clean_verify(tmp_path):
    guard = ButterflyGuard()
    # Seed an issue first
    ledger = IssueLedger(str(tmp_path), "test-session")
    ledger.add(_make_issue(file_path="a.ts", category="lint"))
    guard._ledger = ledger
    assert len(ledger.open_issues()) == 1

    ctx = _ctx(
        tool_name="Bash",
        tool_input={"command": "ruff check src/"},
        tool_result="All checks passed. No issues found.",
        cache_dir=str(tmp_path),
        hook_event="PostToolUse",
    )
    guard.on_post_tool(ctx)
    assert len(ledger.open_issues()) == 0


def test_post_tool_resolves_tsc_category(tmp_path):
    guard = ButterflyGuard()
    ledger = IssueLedger(str(tmp_path), "test-session")
    ledger.add(_make_issue(file_path="a.ts", category="tsc"))
    guard._ledger = ledger

    ctx = _ctx(
        tool_name="Bash",
        tool_input={"command": "npx tsc --noEmit"},
        tool_result="Found 0 errors.",
        cache_dir=str(tmp_path),
        hook_event="PostToolUse",
    )
    guard.on_post_tool(ctx)
    assert len(ledger.open_issues()) == 0


def test_post_tool_resolves_test_category(tmp_path):
    guard = ButterflyGuard()
    ledger = IssueLedger(str(tmp_path), "test-session")
    ledger.add(_make_issue(file_path="t.py", category="test"))
    guard._ledger = ledger

    ctx = _ctx(
        tool_name="Bash",
        tool_input={"command": "pytest tests/"},
        tool_result="All 10 tests passed",
        cache_dir=str(tmp_path),
        hook_event="PostToolUse",
    )
    guard.on_post_tool(ctx)
    assert len(ledger.open_issues()) == 0


def test_post_tool_none_for_empty_result(tmp_path):
    guard = ButterflyGuard()
    ctx = _ctx(
        tool_name="Bash",
        tool_input={"command": "echo hi"},
        tool_result="",
        cache_dir=str(tmp_path),
        hook_event="PostToolUse",
    )
    assert guard.on_post_tool(ctx) is None


# ── ButterflyGuard.on_stop ──────────────────────────────


def test_on_stop_returns_handoff_when_unresolved(tmp_path):
    guard = ButterflyGuard()
    ledger = IssueLedger(str(tmp_path), "test-session")
    ledger.add(_make_issue(file_path="f.ts", category="tsc"))
    guard._ledger = ledger

    ctx = _ctx(cache_dir=str(tmp_path), hook_event="Stop")
    result = guard.on_stop(ctx)
    assert result is not None
    assert "Butterfly Effect" in (result.context or "")


def test_on_stop_returns_none_when_all_resolved(tmp_path):
    guard = ButterflyGuard()
    ledger = IssueLedger(str(tmp_path), "test-session")
    guard._ledger = ledger

    ctx = _ctx(cache_dir=str(tmp_path), hook_event="Stop")
    assert guard.on_stop(ctx) is None
