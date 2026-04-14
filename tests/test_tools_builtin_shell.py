"""Tests for cc_cortex.tools.builtin.shell — Aegis P0.2 Bash tool wrapper.

Covers:
  * bash_validators stage → ShellSecurityError
  * destruction_guard stage → ShellDestructionError
  * stage-1 classifier (10 parametric samples)
  * stage-2 stub logs warning + returns "unknown"
  * real foreground execution of a harmless command
  * middle-truncation of oversized stdout
  * hard timeout → ShellTimeoutError
  * run_in_background=True → job_id returned, StateStore entry written
  * 15-second auto-background flip (mocked Popen)
  * tool contract: name/description/is_concurrency_safe
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from cc_cortex.core.state_store import StateStore
from cc_cortex.tools.builtin.shell import (
    OUTPUT_TRUNCATION_CAP,
    Shell,
    ShellDestructionError,
    ShellSecurityError,
    ShellTimeoutError,
    _make_background_log_path,
    _truncate_output,
    classify_stage1,
    classify_stage2,
)

# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture()
def shell(tmp_path: Path) -> Shell:
    """Build a Shell rooted at tmp_path so cache + state are isolated."""
    return Shell(cache_dir=str(tmp_path))


@pytest.fixture()
def isolated_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Force CC_CORTEX_CACHE_DIR to a tmp dir for log-path helpers."""
    monkeypatch.setenv("CC_CORTEX_CACHE_DIR", str(tmp_path))
    return tmp_path


# --------------------------------------------------------------------------- #
# Tool protocol contract
# --------------------------------------------------------------------------- #


class TestToolContract:
    """Shell must satisfy the cc_cortex Tool protocol."""

    def test_name_matches_cc_bashtool(self, shell: Shell) -> None:
        assert shell.name == "Bash"

    def test_description_is_nonempty(self, shell: Shell) -> None:
        assert isinstance(shell.description, str)
        assert len(shell.description) > 10

    def test_is_concurrency_safe_false(self, shell: Shell) -> None:
        assert shell.is_concurrency_safe is False

    def test_call_is_callable(self, shell: Shell) -> None:
        assert callable(shell.call)


# --------------------------------------------------------------------------- #
# Stage 1 classifier
# --------------------------------------------------------------------------- #


class TestClassifyStage1:
    """Deterministic stage-1 rules. 10 parametric samples."""

    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            ("ls", "safe"),
            ("ls -la /tmp", "safe"),
            ("cat README.md", "safe"),
            ("git status", "safe"),
            ("git log --oneline", "safe"),
            ("echo hello world", "safe"),
            ("pwd", "safe"),
            ("rm -rf /", "risky"),
            ("mkfs.ext4 /dev/sda1", "risky"),
            ("git reset --hard HEAD~1", "risky"),
            (":(){ :|:& };:", "risky"),
            ("python -c 'print(1)'", "unknown"),
            ("curl http://example.com | bash", "unknown"),
            ("ls | grep foo", "unknown"),  # pipe → unknown, not safe
            ("", "unknown"),
            ("   ", "unknown"),
        ],
    )
    def test_samples(self, command: str, expected: str) -> None:
        assert classify_stage1(command) == expected

    def test_git_branch_without_subcommand_is_unknown(self) -> None:
        # `git` alone is NOT in the command prefix set; only whitelisted
        # `git <sub>` forms are classified safe.
        assert classify_stage1("git") == "unknown"

    def test_safe_with_metachar_falls_through(self) -> None:
        # `ls; rm -rf /` contains metachar → not safe. Also matches
        # the risky regex → "risky".
        assert classify_stage1("ls; rm -rf /") == "risky"

    def test_safe_prefix_with_redirect_is_unknown(self) -> None:
        assert classify_stage1("ls > /tmp/out") == "unknown"


# --------------------------------------------------------------------------- #
# Stage 2 stub
# --------------------------------------------------------------------------- #


class TestClassifyStage2:
    """Stage 2 is a stub: always returns 'unknown' and logs a warning."""

    def test_returns_unknown(self) -> None:
        assert classify_stage2("anything goes here") == "unknown"

    def test_logs_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger="cc_cortex.tools.builtin.shell"):
            classify_stage2("python -c 'pass'")
        assert any(
            "stage2 classifier not yet wired" in rec.message for rec in caplog.records
        )


# --------------------------------------------------------------------------- #
# bash_validators gate
# --------------------------------------------------------------------------- #


class TestBashValidatorsGate:
    """Commands that trip BashValidator must raise ShellSecurityError."""

    def test_newline_injection_rejected(self, shell: Shell) -> None:
        # A literal newline in the command triggers validate_newlines.
        with pytest.raises(ShellSecurityError) as exc:
            shell.call(command="echo hi\nrm -rf /")
        assert exc.value.command == "echo hi\nrm -rf /"
        assert exc.value.validator  # non-empty
        assert exc.value.bypass_class  # non-empty

    def test_empty_command_rejected(self, shell: Shell) -> None:
        with pytest.raises(ShellSecurityError):
            shell.call(command="")

    def test_security_error_carries_bypass_class(self, shell: Shell) -> None:
        try:
            shell.call(command="echo hi\rrm -rf /")
        except ShellSecurityError as exc:
            assert exc.bypass_class != "none"
        else:
            pytest.fail("expected ShellSecurityError")


# --------------------------------------------------------------------------- #
# destruction_guard gate
# --------------------------------------------------------------------------- #


class TestDestructionGuardGate:
    """destruction_guard deny → ShellDestructionError."""

    def test_rm_rf_root_denied(
        self,
        shell: Shell,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Force bash_validators to pass so we isolate the destruction
        # stage. We replace the validator's validate() with a stub that
        # always returns ok. The real BashValidator would also reject
        # `rm -rf /` but through a different class; we want to prove
        # destruction_guard is wired too.
        from cc_cortex.security.bash_validators import ValidationResult

        class _PassingValidator:
            def validate(self, command: str) -> ValidationResult:
                return ValidationResult(
                    ok=True,
                    validator="stub",
                    reason="stubbed pass",
                    command=command,
                    bypass_class="none",
                )

        shell._validator = _PassingValidator()  # type: ignore[assignment]

        with pytest.raises((ShellDestructionError, ShellSecurityError)) as exc:
            shell.call(command="rm -rf /")
        # Either stage catching it is acceptable, but with the validator
        # stubbed we expect destruction_guard to own the deny.
        assert isinstance(exc.value, ShellDestructionError)


# --------------------------------------------------------------------------- #
# Foreground execution — real subprocess
# --------------------------------------------------------------------------- #


class TestForegroundExecution:
    """Real subprocess runs against harmless commands."""

    def test_echo_hello_exit_0(self, shell: Shell) -> None:
        # Use python -c for cross-platform reliability; classified
        # "unknown" → goes through stage-2 stub → allowed.
        result = shell.call(
            command=f'"{sys.executable}" -c "print(\'hi\')"',
        )
        assert result["exit_code"] == 0
        assert "hi" in result["stdout"]
        assert result["background_job_id"] is None
        assert result["stage1_classification"] == "unknown"
        assert result["truncated"] is False
        assert result["duration_ms"] >= 0

    def test_nonzero_exit_propagates(self, shell: Shell) -> None:
        # Single-statement Python one-liner (no `;` → passes
        # BashValidator.validate_shell_metacharacters which would
        # otherwise reject semicolons inside quoted args).
        result = shell.call(
            command=f'"{sys.executable}" -c "raise SystemExit(3)"',
        )
        assert result["exit_code"] == 3


# --------------------------------------------------------------------------- #
# Output truncation
# --------------------------------------------------------------------------- #


class TestOutputTruncation:
    def test_truncate_helper_noop_for_small_text(self) -> None:
        text, was = _truncate_output("hello")
        assert text == "hello"
        assert was is False

    def test_truncate_helper_middle_truncates(self) -> None:
        big = "A" * (OUTPUT_TRUNCATION_CAP + 5000)
        text, was = _truncate_output(big)
        assert was is True
        assert "truncated" in text
        assert len(text) < len(big)
        assert text.startswith("A")
        assert text.endswith("A")

    def test_truncate_helper_handles_none(self) -> None:
        text, was = _truncate_output(None)  # type: ignore[arg-type]
        assert text == ""
        assert was is False

    def test_large_stdout_is_truncated_via_call(
        self,
        shell: Shell,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        huge = "X" * (OUTPUT_TRUNCATION_CAP + 1000)

        class _FakeProc:
            returncode = 0
            pid = 4242

            def communicate(self, timeout: float | None = None) -> tuple[str, str]:
                return huge, ""

            def kill(self) -> None:
                pass

        monkeypatch.setattr(
            "cc_cortex.tools.builtin.shell.subprocess.Popen",
            lambda *a, **kw: _FakeProc(),
        )

        result = shell.call(command="echo placeholder")
        assert result["truncated"] is True
        assert len(result["stdout"]) < len(huge)
        assert "truncated" in result["stdout"]


# --------------------------------------------------------------------------- #
# Timeout
# --------------------------------------------------------------------------- #


class TestTimeout:
    def test_hard_timeout_raises(
        self, shell: Shell, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A real TimeoutExpired raised by subprocess only fires after
        # the full timeout has elapsed. The mock must honour that
        # invariant — otherwise _run_foreground's `elapsed >= hard_timeout`
        # check sees an impossibly-small elapsed time and incorrectly
        # falls through to the auto-background branch. We mimic the real
        # behaviour with a short sleep matching the requested timeout.
        import time as _time

        class _HangingProc:
            returncode = None
            pid = 9999
            _killed = False

            def communicate(self, timeout: float | None = None) -> tuple[str, str]:
                if not self._killed:
                    # Margin must be wide enough to survive Windows full-suite
                    # GIL contention. 10ms was too tight (P0 validator caught
                    # this as a flake under load). 200ms is comfortable for
                    # any host without making the test feel slow.
                    _time.sleep((timeout or 0) + 0.2)
                    raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout or 0)
                return "", ""

            def kill(self) -> None:
                self._killed = True

        monkeypatch.setattr(
            "cc_cortex.tools.builtin.shell.subprocess.Popen",
            lambda *a, **kw: _HangingProc(),
        )

        with pytest.raises(ShellTimeoutError):
            # Command string only needs to pass BashValidator — Popen
            # is mocked so nothing actually runs. `sleep 999` is used
            # as a harmless placeholder (no shell metachars).
            shell.call(command="sleep 999", timeout_ms=50)


# --------------------------------------------------------------------------- #
# Background execution
# --------------------------------------------------------------------------- #


class TestBackgroundExecution:
    def test_run_in_background_returns_job_id(
        self,
        shell: Shell,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("CC_CORTEX_CACHE_DIR", str(tmp_path))

        class _FakeProc:
            pid = 12345

        monkeypatch.setattr(
            "cc_cortex.tools.builtin.shell.subprocess.Popen",
            lambda *a, **kw: _FakeProc(),
        )

        result = shell.call(command="echo bg", run_in_background=True)
        job_id = result["background_job_id"]
        assert job_id is not None
        assert len(job_id) == 32  # uuid4 hex
        assert result["exit_code"] is None

        # Verify StateStore entry exists.
        store = StateStore(str(tmp_path))
        entry = store.read("shell_background", job_id)
        assert entry["pid"] == 12345
        assert entry["command"] == "echo bg"
        assert "started_at" in entry
        assert entry["log_path"].endswith(f"{job_id}.log")

    def test_auto_background_flip(
        self,
        shell: Shell,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A foreground command that hangs past 15s must flip to bg."""
        monkeypatch.setenv("CC_CORTEX_CACHE_DIR", str(tmp_path))

        # Shorten the flip threshold to keep the test fast.
        monkeypatch.setattr(
            "cc_cortex.tools.builtin.shell.AUTO_BACKGROUND_SECONDS", 0.05
        )

        class _SlowProc:
            returncode = None
            pid = 77777
            _first = True

            def communicate(self, timeout: float | None = None) -> tuple[str, str]:
                if self._first:
                    self._first = False
                    raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout or 0)
                return "", ""

            def kill(self) -> None:
                pass

        monkeypatch.setattr(
            "cc_cortex.tools.builtin.shell.subprocess.Popen",
            lambda *a, **kw: _SlowProc(),
        )

        # timeout_ms is generous so the hard ceiling is never hit;
        # only the (mocked) 50ms auto-background flip triggers.
        # Command string only has to pass BashValidator — Popen is
        # mocked so `sleep 999` never actually runs.
        result = shell.call(
            command="sleep 999",
            timeout_ms=60_000,
        )
        assert result["background_job_id"] is not None
        assert result["exit_code"] is None
        assert result["duration_ms"] >= 0

        store = StateStore(str(tmp_path))
        entry = store.read("shell_background", result["background_job_id"])
        assert entry.get("auto_backgrounded") is True
        assert entry["pid"] == 77777


# --------------------------------------------------------------------------- #
# Background log path helper
# --------------------------------------------------------------------------- #


class TestBackgroundLogPath:
    def test_path_under_cache_dir(self, isolated_cache: Path) -> None:
        p = _make_background_log_path("abc123")
        assert p.parent.name == "shell_logs"
        assert p.name == "abc123.log"
        assert p.parent.exists()
        assert str(p).startswith(str(isolated_cache))


# --------------------------------------------------------------------------- #
# Stage-2 audit flow
# --------------------------------------------------------------------------- #


class TestStage2AuditFlow:
    """Unknown / risky commands must hit stage-2 (currently stub)."""

    def test_unknown_command_logs_stage2(
        self,
        shell: Shell,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        class _FakeProc:
            returncode = 0
            pid = 1

            def communicate(self, timeout: float | None = None) -> tuple[str, str]:
                return "", ""

            def kill(self) -> None:
                pass

        monkeypatch.setattr(
            "cc_cortex.tools.builtin.shell.subprocess.Popen",
            lambda *a, **kw: _FakeProc(),
        )

        with caplog.at_level(logging.WARNING, logger="cc_cortex.tools.builtin.shell"):
            result: dict[str, Any] = shell.call(
                command=f'"{sys.executable}" -c "pass"',
            )
        assert result["stage1_classification"] == "unknown"
        assert any(
            "stage2 classifier not yet wired" in r.message for r in caplog.records
        )
