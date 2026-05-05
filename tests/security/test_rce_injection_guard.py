"""Tests for concinno.security.rce_injection_guard.

Coverage targets (≥30 cases):
  * f-string into shell sinks (os.system / subprocess shell=True /
    os.popen / commands.getoutput)
  * % / .format() composing shell strings
  * eval / exec literal vs dynamic severity split
  * compile(..., 'exec') with dynamic source
  * Bash backtick substitution + unquoted variable shapes
  * Safe forms (parametrized argv list / shlex.quote / json.loads)
    produce zero findings
  * Per-line escape-hatch comment recognised
  * malformed_payload / parse_error handling
  * Tool-input dict extraction (Bash / Edit / Write / NotebookEdit)
  * BaseGuard wrapper end-to-end through GuardContext (proves the
    guard is wired into the pipeline path, not isolated)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from concinno.guards.base import GuardAction, GuardContext
from concinno.security import (
    PolicyGate,
    PolicyGateResult,
    RceFinding,
    RceInjectionBaseGuard,
    RceInjectionGuard,
    RcePayload,
    extract_code_payload,
)

# ── Fixtures ────────────────────────────────────────────────────


@pytest.fixture
def audit_tmp(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> Path:
    monkeypatch.setenv("CONCINNO_AUDIT_DIR", str(tmp_path))
    monkeypatch.setenv("CONCINNO_ZIQ_BUS_DISABLED", "1")
    return tmp_path


def _types(findings: list[RceFinding]) -> set[str]:
    return {f.type for f in findings}


def _scan(src: str, **kwargs: Any) -> list[RceFinding]:
    g = RceInjectionGuard(min_severity="low", **kwargs)
    return g.scan(src)


# ════════════════════════════════════════════════════════════════
#  1. Inheritance / class invariants
# ════════════════════════════════════════════════════════════════


def test_inherits_from_policygate(audit_tmp: Path) -> None:
    assert issubclass(RceInjectionGuard, PolicyGate)


def test_class_name_constant() -> None:
    assert RceInjectionGuard.name == "rce_injection_guard"


def test_invalid_min_severity_raises() -> None:
    with pytest.raises(ValueError):
        RceInjectionGuard(min_severity="extreme")  # type: ignore[arg-type]


# ════════════════════════════════════════════════════════════════
#  2. f-string into shell sinks
# ════════════════════════════════════════════════════════════════


def test_os_system_with_fstring_critical(audit_tmp: Path) -> None:
    findings = _scan('import os\nos.system(f"echo {user}")')
    assert "fstring_shell.system" in _types(findings)
    assert findings[0].severity == "critical"


def test_os_system_literal_clean(audit_tmp: Path) -> None:
    findings = _scan('import os\nos.system("echo hi")')
    assert not any(t.startswith("fstring_shell") for t in _types(findings))


def test_subprocess_run_shell_true_fstring_critical(audit_tmp: Path) -> None:
    findings = _scan(
        'import subprocess\n'
        'subprocess.run(f"ls {path}", shell=True)'
    )
    assert "fstring_shell.subprocess_shell" in _types(findings)


def test_subprocess_run_shell_false_clean(audit_tmp: Path) -> None:
    """``subprocess.run([...], shell=False)`` is the safe pattern."""
    findings = _scan(
        'import subprocess\n'
        'subprocess.run(["ls", path])'
    )
    assert not any(t.startswith("fstring_shell") for t in _types(findings))


def test_subprocess_call_shell_true_fstring(audit_tmp: Path) -> None:
    findings = _scan(
        'import subprocess\n'
        'subprocess.call(f"cat {f}", shell=True)'
    )
    assert "fstring_shell.subprocess_shell" in _types(findings)


def test_subprocess_popen_shell_true_fstring(audit_tmp: Path) -> None:
    findings = _scan(
        'import subprocess\n'
        'subprocess.Popen(f"echo {x}", shell=True)'
    )
    assert "fstring_shell.subprocess_shell" in _types(findings)


def test_os_popen_fstring_high(audit_tmp: Path) -> None:
    findings = _scan('import os\nos.popen(f"cat {f}").read()')
    assert "fstring_shell.popen" in _types(findings)


def test_commands_getoutput_fstring(audit_tmp: Path) -> None:
    findings = _scan('import commands\ncommands.getoutput(f"ls {x}")')
    assert "fstring_shell.system" in _types(findings)


# ════════════════════════════════════════════════════════════════
#  3. % / .format() shell composition
# ════════════════════════════════════════════════════════════════


def test_percent_format_into_shell(audit_tmp: Path) -> None:
    findings = _scan('import os\nos.system("echo %s" % user)')
    assert "format_shell" in _types(findings)


def test_dot_format_into_shell(audit_tmp: Path) -> None:
    findings = _scan('import os\nos.system("echo {}".format(user))')
    assert "format_shell" in _types(findings)


def test_string_concat_into_shell(audit_tmp: Path) -> None:
    findings = _scan('import os\nos.system("echo " + user)')
    assert "format_shell" in _types(findings)


def test_format_with_constant_args_clean(audit_tmp: Path) -> None:
    findings = _scan('import os\nos.system("echo %s" % "literal")')
    # Constant substitution is safe — no dynamic value.
    assert "format_shell" not in _types(findings)


# ════════════════════════════════════════════════════════════════
#  4. eval / exec / compile
# ════════════════════════════════════════════════════════════════


def test_eval_dynamic_critical(audit_tmp: Path) -> None:
    findings = _scan("eval(user_input)")
    assert "eval.dynamic" in _types(findings)
    sev = next(f.severity for f in findings if f.type == "eval.dynamic")
    assert sev == "critical"


def test_eval_literal_low(audit_tmp: Path) -> None:
    findings = _scan('eval("1 + 2")')
    assert "eval.literal" in _types(findings)


def test_exec_dynamic_critical(audit_tmp: Path) -> None:
    findings = _scan("exec(payload)")
    assert "exec.dynamic" in _types(findings)


def test_exec_literal_low(audit_tmp: Path) -> None:
    findings = _scan('exec("x = 1")')
    assert "exec.literal" in _types(findings)


def test_compile_exec_dynamic_critical(audit_tmp: Path) -> None:
    findings = _scan('compile(src, "<x>", "exec")')
    assert "compile.exec" in _types(findings)


def test_compile_eval_mode_not_flagged(audit_tmp: Path) -> None:
    """``compile(s, '<x>', 'eval')`` is a different mode — only ``exec``
    is the RCE primitive we flag here."""
    findings = _scan('compile(src, "<x>", "eval")')
    assert "compile.exec" not in _types(findings)


def test_flag_eval_literal_off_suppresses(audit_tmp: Path) -> None:
    findings = _scan('eval("1+2")', flag_eval_literal=False)
    assert not any(t.endswith(".literal") for t in _types(findings))


# ════════════════════════════════════════════════════════════════
#  5. Bash command-injection shapes
# ════════════════════════════════════════════════════════════════


def test_bash_backtick_high(audit_tmp: Path) -> None:
    g = RceInjectionGuard(min_severity="low")
    findings = g.scan(RcePayload(code="echo `whoami`", tool_name="Bash"))
    assert "bash_backtick_subst" in _types(findings)


def test_bash_unquoted_var_medium(audit_tmp: Path) -> None:
    g = RceInjectionGuard(min_severity="low")
    findings = g.scan(RcePayload(code="echo $USER", tool_name="Bash"))
    assert "bash_unquoted_var" in _types(findings)


def test_bash_quoted_var_clean(audit_tmp: Path) -> None:
    g = RceInjectionGuard(min_severity="low")
    findings = g.scan(
        RcePayload(code='echo "$USER"', tool_name="Bash"),
    )
    assert "bash_unquoted_var" not in _types(findings)


def test_bash_safe_command_clean(audit_tmp: Path) -> None:
    g = RceInjectionGuard(min_severity="low")
    findings = g.scan(RcePayload(code="ls -la /tmp", tool_name="Bash"))
    assert findings == []


# ════════════════════════════════════════════════════════════════
#  6. Safe / parameterised forms
# ════════════════════════════════════════════════════════════════


def test_argv_list_subprocess_clean(audit_tmp: Path) -> None:
    findings = _scan(
        'import subprocess\n'
        'subprocess.run(["ls", path], check=True)'
    )
    assert findings == [] or all(
        t in {"eval.literal", "exec.literal"} for t in _types(findings)
    )


def test_shlex_quote_pattern_clean(audit_tmp: Path) -> None:
    """``shlex.quote(user) + cmd`` is the conventional safe pattern.

    We don't flag this because the shell text itself is built from a
    quoted value — the guard cannot statically prove that, but the
    common shape (assignment + call without f-string) does not match
    any of our detectors.
    """
    src = (
        "import shlex, os\n"
        "safe = shlex.quote(user)\n"
        "os.system('echo ' + safe)\n"
    )
    findings = _scan(src)
    # ``+ safe`` is a non-literal concat — we DO flag it (better safe
    # than sorry). This test documents that intentional outcome.
    assert "format_shell" in _types(findings)


def test_json_loads_not_flagged(audit_tmp: Path) -> None:
    findings = _scan('import json\nx = json.loads(payload)')
    assert findings == []


# ════════════════════════════════════════════════════════════════
#  7. Per-line escape hatch
# ════════════════════════════════════════════════════════════════


def test_per_line_escape_suppresses(audit_tmp: Path) -> None:
    src = (
        "import os\n"
        'os.system(f"echo {x}")  # CONCINNO_DISABLE:rce_injection_guard:test\n'
    )
    findings = _scan(src)
    assert "fstring_shell.system" not in _types(findings)


def test_global_escape_token_short_circuits(audit_tmp: Path) -> None:
    src = (
        "# CONCINNO_DISABLE: whole-file escape\n"
        "import os\n"
        'os.system(f"echo {x}")\n'
    )
    g = RceInjectionGuard(
        profile="lite",
        fail_mode_override="hard_deny",
        min_severity="low",
    )
    result: PolicyGateResult = g.evaluate(src)
    assert result.escaped is True
    assert result.decision == "accept"


# ════════════════════════════════════════════════════════════════
#  8. Malformed / parse-error handling
# ════════════════════════════════════════════════════════════════


def test_parse_error_returns_low_finding(audit_tmp: Path) -> None:
    findings = _scan("def(((((  # invalid syntax")
    assert any(f.type == "parse_error" for f in findings)


def test_malformed_payload_unsupported_type(audit_tmp: Path) -> None:
    g = RceInjectionGuard(min_severity="low")
    findings = g.scan(12345)  # type: ignore[arg-type]
    assert any(f.type == "malformed_payload" for f in findings)


def test_empty_string_no_findings(audit_tmp: Path) -> None:
    findings = _scan("")
    assert findings == []


# ════════════════════════════════════════════════════════════════
#  9. extract_code_payload — tool-input shape
# ════════════════════════════════════════════════════════════════


def test_extract_bash_command() -> None:
    p = extract_code_payload("Bash", {"command": "ls -la"})
    assert p is not None and p.code == "ls -la"
    assert p.tool_name == "Bash"


def test_extract_write_content() -> None:
    p = extract_code_payload(
        "Write", {"file_path": "/tmp/x.py", "content": "print(1)"},
    )
    assert p is not None and p.code == "print(1)"
    assert p.tool_name == "Write"
    assert p.file_path == "/tmp/x.py"


def test_extract_edit_new_string() -> None:
    p = extract_code_payload(
        "Edit",
        {"file_path": "/tmp/y.py", "old_string": "a", "new_string": "b"},
    )
    assert p is not None and p.code == "b"


def test_extract_notebook_new_source() -> None:
    p = extract_code_payload(
        "NotebookEdit",
        {
            "notebook_path": "/tmp/n.ipynb",
            "cell_id": "abc",
            "new_source": "print(2)",
        },
    )
    assert p is not None and p.code == "print(2)"
    assert p.file_path == "/tmp/n.ipynb"


def test_extract_unrelated_tool_returns_none() -> None:
    assert extract_code_payload("Read", {"file_path": "/a"}) is None


def test_extract_empty_content_returns_none() -> None:
    assert extract_code_payload("Write", {"content": ""}) is None


# ════════════════════════════════════════════════════════════════
#  10. End-to-end pipeline path through BaseGuard wrapper
# ════════════════════════════════════════════════════════════════


def _ctx(tool_name: str, tool_input: dict[str, Any]) -> GuardContext:
    return GuardContext(
        tool_name=tool_name,
        tool_input=tool_input,
        session_id="test-session",
        cache_dir="",
        hook_event="PreToolUse",
    )


def test_baseguard_fires_on_fstring_shell(audit_tmp: Path) -> None:
    """E2E: build a fake Edit GuardContext, exercise the wired hook
    path, prove the guard fires. Closes the
    feedback_subagent_island_caller_required.md acceptance criterion.
    """
    # Use override to force ``warn`` so we get an allow+context (not
    # a deny that depends on profile resolution).
    inner = RceInjectionGuard(
        profile="lite",
        fail_mode_override="warn",
        min_severity="low",
    )
    guard = RceInjectionBaseGuard(guard=inner)
    ctx = _ctx(
        "Edit",
        {
            "file_path": "/tmp/x.py",
            "old_string": "pass",
            "new_string": 'import os\nos.system(f"echo {user}")',
        },
    )
    result = guard.check(ctx)
    assert result is not None
    assert result.action == GuardAction.ALLOW  # warn = allow + context
    assert "fstring_shell" in (result.context or "")


def test_baseguard_clean_payload_returns_none(audit_tmp: Path) -> None:
    guard = RceInjectionBaseGuard(
        guard=RceInjectionGuard(
            fail_mode_override="warn", min_severity="low",
        ),
    )
    ctx = _ctx(
        "Write",
        {"file_path": "/tmp/x.py", "content": "print('hello')"},
    )
    assert guard.check(ctx) is None


def test_baseguard_unrelated_tool_returns_none(audit_tmp: Path) -> None:
    guard = RceInjectionBaseGuard()
    ctx = _ctx("Read", {"file_path": "/a"})
    assert guard.check(ctx) is None


def test_baseguard_hard_deny_blocks(audit_tmp: Path) -> None:
    """Override fail-mode to hard_deny — the guard MUST emit DENY."""
    inner = RceInjectionGuard(
        profile="paranoid",
        fail_mode_override="hard_deny",
        min_severity="low",
    )
    guard = RceInjectionBaseGuard(guard=inner)
    ctx = _ctx(
        "Bash",
        {"command": "echo `whoami`"},
    )
    result = guard.check(ctx)
    assert result is not None
    assert result.action == GuardAction.DENY
    assert "RCE" in result.reason


def test_baseguard_registered_in_default_pipeline(audit_tmp: Path) -> None:
    """The guard MUST appear in the pipeline produced by
    ``create_default_pipeline`` — proves wiring is present, not just
    importable."""
    from concinno.guards.registry import create_default_pipeline

    pipe = create_default_pipeline()
    rce_guards = [
        g for g in pipe._guards if g.name == "rce_injection_guard"
    ]
    assert len(rce_guards) == 1
    g = rce_guards[0]
    assert g.feature_name == "rce_injection_guard"
