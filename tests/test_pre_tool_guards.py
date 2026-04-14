"""Tests for cc_cortex.pre_tool_guards — BashGuard, PythonGuard, ReadFirst."""

from cc_cortex.guards.base import GuardContext
from cc_cortex.pre_tool_guards import (
    ReadBudgetGuard,
    check_bash,
    check_python_c,
    check_read_first,
    gate_ssh_interactive,
    gate_tool_redirect,
    log_read,
)

# ── BashGuard ──────────────────────────────────────────────────


class TestBashGuard:
    def test_long_running_server_warns(self):
        warns = check_bash({"command": "npm start"})
        assert len(warns) == 1
        assert "BashGuard" in warns[0]

    def test_long_running_with_background_ok(self):
        warns = check_bash({"command": "npm start", "run_in_background": True})
        assert warns == []

    def test_docker_compose_up_no_d_warns(self):
        warns = check_bash({"command": "docker compose up"})
        assert len(warns) == 1

    def test_docker_compose_up_d_ok(self):
        warns = check_bash({"command": "docker compose up -d"})
        assert warns == []

    def test_tail_f_warns(self):
        warns = check_bash({"command": "tail -f /var/log/syslog"})
        assert len(warns) == 1

    def test_normal_command_ok(self):
        warns = check_bash({"command": "ls -la"})
        assert warns == []

    def test_sleep_large_warns(self):
        warns = check_bash({"command": "sleep 60"})
        assert len(warns) == 1
        assert "BashGuard" in warns[0]

    def test_sleep_small_ok(self):
        warns = check_bash({"command": "sleep 5"})
        assert warns == []

    def test_sleep_large_with_background_ok(self):
        warns = check_bash({"command": "sleep 60", "run_in_background": True})
        assert warns == []

    def test_while_true_warns(self):
        warns = check_bash({"command": "while true; do echo hi; done"})
        assert len(warns) == 1

    def test_uvicorn_warns(self):
        warns = check_bash({"command": "uvicorn app:main"})
        assert len(warns) == 1


# ── PythonGuard ────────────────────────────────────────────────


class TestPythonGuard:
    def test_short_python_c_ok(self):
        assert check_python_c({"command": "python -c 'print(1)'"}) is None

    def test_long_python_c_warns(self):
        cmd = "python -c '\n".join(["line"] * 6) + "'"
        result = check_python_c({"command": cmd})
        assert result is not None
        assert "PythonGuard" in result

    def test_non_python_ok(self):
        assert check_python_c({"command": "node -e 'console.log(1)'"}) is None


# ── SSHGuard ──────────────────────────────────────────────────


class TestSSHGuard:
    def test_ssh_command_blocked(self):
        result = gate_ssh_interactive({"command": "ssh root@5.104.83.69 'ls'"})
        assert result is not None
        assert result["permissionDecision"] == "deny"

    def test_scp_command_blocked(self):
        result = gate_ssh_interactive({"command": "scp file.txt root@host:/tmp/"})
        assert result is not None
        assert result["permissionDecision"] == "deny"

    def test_ssh_in_chain_blocked(self):
        result = gate_ssh_interactive({"command": "echo hi && ssh vps 'docker ps'"})
        assert result is not None

    def test_paramiko_script_allowed(self):
        result = gate_ssh_interactive({"command": "python upload_media.py"})
        assert result is None

    def test_python_deploy_allowed(self):
        result = gate_ssh_interactive({"command": "python deploy.py"})
        assert result is None

    def test_normal_command_allowed(self):
        result = gate_ssh_interactive({"command": "ls -la"})
        assert result is None

    def test_curl_allowed(self):
        result = gate_ssh_interactive({"command": "curl -s https://example.com"})
        assert result is None


# ── ReadFirst ──────────────────────────────────────────────────


class TestReadFirst:
    def test_new_file_ok(self, tmp_path):
        """Writing a non-existent file should not warn."""
        cache = str(tmp_path / "cache")
        result = check_read_first(
            str(tmp_path / "nonexistent.py"), cache, "sess1234"
        )
        assert result is None

    def test_existing_file_without_read_warns(self, tmp_path):
        """Editing an existing file without reading first should warn."""
        target = tmp_path / "existing.py"
        target.write_text("# code")
        cache = str(tmp_path / "cache")
        result = check_read_first(str(target), cache, "sess1234")
        assert result is not None
        assert "ReadFirst" in result

    def test_log_read_then_edit_ok(self, tmp_path):
        """After log_read, editing should not warn."""
        target = tmp_path / "existing.py"
        target.write_text("# code")
        cache = str(tmp_path / "cache")
        sid = "sess1234"

        log_read(str(target), cache, sid)
        result = check_read_first(str(target), cache, sid)
        assert result is None

    def test_json_file_skip(self, tmp_path):
        """JSON files should not trigger ReadFirst warnings."""
        target = tmp_path / "config.json"
        target.write_text("{}")
        cache = str(tmp_path / "cache")
        result = check_read_first(str(target), cache, "sess1234")
        assert result is None

    def test_lock_file_skip(self, tmp_path):
        """Lock files should not trigger ReadFirst warnings."""
        target = tmp_path / "package-lock.json"
        target.write_text("{}")
        cache = str(tmp_path / "cache")
        result = check_read_first(str(target), cache, "sess1234")
        assert result is None

    def test_log_file_skip(self, tmp_path):
        """Log files should not trigger ReadFirst warnings."""
        target = tmp_path / "app.log"
        target.write_text("log")
        cache = str(tmp_path / "cache")
        result = check_read_first(str(target), cache, "sess1234")
        assert result is None

    def test_different_session_still_warns(self, tmp_path):
        """Reading in session A should not clear warning for session B."""
        target = tmp_path / "existing.py"
        target.write_text("# code")
        cache = str(tmp_path / "cache")

        log_read(str(target), cache, "sessAAAA")
        result = check_read_first(str(target), cache, "sessBBBB")
        assert result is not None


# ── ToolRedirectGuard ─────────────────────────────────────────


class TestToolRedirect:
    def test_simple_grep_denied(self):
        r = gate_tool_redirect({"command": "grep 'pattern' src/main.py"})
        assert r is not None
        assert "Grep" in r.get("additionalContext", "")

    def test_simple_rg_denied(self):
        r = gate_tool_redirect({"command": "rg 'TODO' src/"})
        assert r is not None

    def test_simple_cat_denied(self):
        r = gate_tool_redirect({"command": "cat src/main.py"})
        assert r is not None
        assert "Read" in r.get("additionalContext", "")

    def test_simple_head_denied(self):
        r = gate_tool_redirect({"command": "head -20 file.txt"})
        assert r is not None

    def test_simple_find_denied(self):
        r = gate_tool_redirect({"command": "find . -name '*.py'"})
        assert r is not None
        assert "Glob" in r.get("additionalContext", "")

    def test_simple_sed_denied(self):
        r = gate_tool_redirect({"command": "sed 's/old/new/g' file.py"})
        assert r is not None
        assert "Edit" in r.get("additionalContext", "")

    def test_echo_redirect_denied(self):
        r = gate_tool_redirect({"command": "echo 'hello' > out.txt"})
        assert r is not None
        assert "Write" in r.get("additionalContext", "")

    def test_piped_grep_allowed(self):
        r = gate_tool_redirect({"command": "git log | grep 'fix'"})
        assert r is None

    def test_complex_chain_allowed(self):
        r = gate_tool_redirect({"command": "cat file.txt && wc -l"})
        assert r is None

    def test_empty_command_allowed(self):
        assert gate_tool_redirect({"command": ""}) is None

    def test_normal_bash_allowed(self):
        assert gate_tool_redirect({"command": "npm install"}) is None

    def test_git_command_allowed(self):
        assert gate_tool_redirect({"command": "git status"}) is None


# ── ReadBudgetGuard ───────────────────────────────────────────


class TestReadBudgetGuard:
    def _make_ctx(self, tool_name: str) -> GuardContext:
        return GuardContext(
            tool_name=tool_name,
            tool_input={},
            session_id="test",
            cache_dir="/tmp",
            hook_event="pre_tool_use",
        )

    def test_no_trigger_under_threshold(self):
        guard = ReadBudgetGuard()
        for _ in range(7):
            r = guard.check(self._make_ctx("Read"))
            assert r is None

    def test_triggers_at_threshold(self):
        guard = ReadBudgetGuard()
        for _ in range(7):
            guard.check(self._make_ctx("Read"))
        r = guard.check(self._make_ctx("Read"))  # 8th
        assert r is not None
        assert "ReadBudget" in (r.context or "")

    def test_resets_on_edit(self):
        guard = ReadBudgetGuard()
        for _ in range(6):
            guard.check(self._make_ctx("Read"))
        guard.check(self._make_ctx("Edit"))  # Reset
        for _ in range(7):
            r = guard.check(self._make_ctx("Read"))
            assert r is None

    def test_resets_on_bash(self):
        guard = ReadBudgetGuard()
        for _ in range(7):
            guard.check(self._make_ctx("Read"))
        guard.check(self._make_ctx("Bash"))  # Reset
        r = guard.check(self._make_ctx("Read"))
        assert r is None

    def test_continues_triggering_after_threshold(self):
        guard = ReadBudgetGuard()
        for _ in range(9):
            guard.check(self._make_ctx("Read"))
        r = guard.check(self._make_ctx("Read"))  # 10th
        assert r is not None
