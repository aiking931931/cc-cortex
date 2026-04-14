"""Tests for cc_cortex.window_guard — Console window flash prevention."""

from __future__ import annotations

from cc_cortex.window_guard import check


class TestWindowGuardCheck:
    # ── Should block ──────────────────────────────────

    def test_blocks_schtasks(self):
        result = check("Bash", {"command": "schtasks /create /tn test"})
        assert result is not None
        assert result["permissionDecision"] == "deny"
        assert result["cmd"] == "schtasks"

    def test_blocks_reg(self):
        result = check("Bash", {"command": "reg query HKLM\\Software"})
        assert result is not None
        assert result["cmd"] == "reg"

    def test_blocks_wmic(self):
        result = check("Bash", {"command": "wmic process list brief"})
        assert result is not None
        assert result["cmd"] == "wmic"

    def test_blocks_netsh(self):
        result = check("Bash", {"command": "netsh advfirewall show"})
        assert result is not None
        assert result["cmd"] == "netsh"

    def test_blocks_certutil(self):
        result = check("Bash", {"command": "certutil -hashfile x.exe SHA256"})
        assert result is not None
        assert result["cmd"] == "certutil"

    def test_blocks_msiexec(self):
        result = check("Bash", {"command": "msiexec /i package.msi"})
        assert result is not None
        assert result["cmd"] == "msiexec"

    def test_blocks_case_insensitive(self):
        result = check("Bash", {"command": "SCHTASKS /query"})
        assert result is not None

    # ── Should pass ───────────────────────────────────

    def test_non_bash_tool_passes(self):
        assert check("Read", {"file_path": "schtasks.txt"}) is None

    def test_empty_command_passes(self):
        assert check("Bash", {"command": ""}) is None

    def test_safe_command_passes(self):
        assert check("Bash", {"command": "ls -la"}) is None

    def test_git_command_passes(self):
        assert check("Bash", {"command": "git status"}) is None

    def test_python_command_passes(self):
        assert check("Bash", {"command": "python -m pytest"}) is None

    # ── Hidden wrapper bypass ─────────────────────────

    def test_run_hidden_pyw_bypasses(self):
        assert check("Bash", {"command": "python run-hidden.pyw schtasks /query"}) is None

    def test_run_hidden_ps1_bypasses(self):
        assert check("Bash", {"command": "pwsh run-hidden-utf8.ps1 reg query"}) is None

    def test_run_hidden_function_bypasses(self):
        assert check("Bash", {"command": "Run-Hidden schtasks /create"}) is None

    # ── Custom message ────────────────────────────────

    def test_custom_message(self):
        result = check("Bash", {"command": "schtasks /query"}, message="blocked: {cmd}")
        assert result["reason"] == "blocked: schtasks"

    # ── Edge cases ────────────────────────────────────

    def test_non_dict_input(self):
        assert check("Bash", "not a dict") is None

    def test_no_command_key(self):
        assert check("Bash", {"other": "value"}) is None
