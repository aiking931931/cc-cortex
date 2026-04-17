"""Tests for concinno.destruction_guard — risk classification, backup, CLI."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from concinno.destruction_guard import (
    R0,
    R1,
    R2,
    R3,
    R4,
    block_message,
    check_confirmed,
    classify_bash,
    classify_write,
    cleanup_backups,
    evaluate,
    is_reason_valid_r4,
    list_backups,
    set_pin,
    split_commands,
)

# ─── classify_bash ───────────────────────────────────────────────

class TestClassifyBash:
    """Test Bash command risk classification."""

    @pytest.mark.parametrize("cmd", [
        "ls -la",
        "git status",
        "echo hello",
        "python --version",
        "rm --dry-run foo",
        "terraform plan",
        "rm test.tmp",
    ])
    def test_r0_safe(self, cmd: str):
        risk, _ = classify_bash(cmd)
        assert risk == R0

    @pytest.mark.parametrize("cmd", [
        "rm -rf node_modules",
        "rm -rf dist",
        "rm -rf __pycache__",
        "rm -rf .pytest_cache",
        "git stash drop",
        "pip cache purge",
        "npm cache clean",
    ])
    def test_r1_low_risk(self, cmd: str):
        risk, _ = classify_bash(cmd)
        assert risk == R1

    @pytest.mark.parametrize("cmd", [
        "rm -rf src/",
        "rm -rf my_project",
        "del /S important",
        "DROP TABLE users",
        "TRUNCATE TABLE orders",
        "git branch -D feature",
        "docker rm container1",
        "pip uninstall requests",
    ])
    def test_r2_medium_risk(self, cmd: str):
        risk, _ = classify_bash(cmd)
        assert risk == R2

    @pytest.mark.parametrize("cmd", [
        "terraform destroy",
        "git push --force",
        "git push -f origin main",
        "git reset --hard HEAD~5",
        "DROP DATABASE production",
        "docker system prune -a",
        "rm -rf /var/data",
        "rm -rf .",
        "kubectl delete namespace staging",
    ])
    def test_r3_high_risk(self, cmd: str):
        risk, _ = classify_bash(cmd)
        assert risk == R3

    @pytest.mark.parametrize("cmd", [
        "rm -rf / ",
        "rm -rf /*",
        "rm -rf ~/",
        "mkfs.ext4 /dev/sda1",
        "dd if=/dev/zero of=/dev/sda",
        "rm -rf /Windows",
    ])
    def test_r4_forbidden(self, cmd: str):
        risk, _ = classify_bash(cmd)
        assert risk == R4


class TestEchoStripping:
    """Echo/printf content must not trigger false positives."""

    def test_echo_rm_no_trigger(self):
        cmd = 'echo "rm -rf /" > log.txt'
        risk, _ = classify_bash(cmd)
        assert risk == R0

    def test_printf_drop_no_trigger(self):
        cmd = "printf 'DROP TABLE users' > query.sql"
        risk, _ = classify_bash(cmd)
        assert risk == R0

    def test_echo_pipe_still_checks_pipe(self):
        cmd = 'echo "hello" | rm -rf src/'
        risk, _ = classify_bash(cmd)
        assert risk == R2


class TestSplitCommands:
    def test_basic_split(self):
        assert split_commands("a && b || c ; d") == ["a", "b", "c", "d"]

    def test_pipe_split(self):
        assert split_commands("a | b | c") == ["a", "b", "c"]


# ─── classify_write ──────────────────────────────────────────────

class TestClassifyWrite:
    def test_nonexistent_file(self):
        risk, _ = classify_write("/nonexistent/path.txt", "content")
        assert risk == R0

    def test_empty_overwrite(self, tmp_path: Path):
        f = tmp_path / "data.json"
        f.write_text("x" * 1000)
        risk, reason = classify_write(str(f), "")
        assert risk == R3
        assert "Empty overwrite" in reason

    def test_massive_reduction(self, tmp_path: Path):
        f = tmp_path / "big.py"
        f.write_text("x" * 5000)
        risk, reason = classify_write(str(f), "x" * 100)
        assert risk == R2
        assert "reduction" in reason

    def test_normal_write(self, tmp_path: Path):
        f = tmp_path / "small.txt"
        f.write_text("hello")
        risk, _ = classify_write(str(f), "hello world")
        assert risk == R0


# ─── Confirmation ────────────────────────────────────────────────

class TestConfirmation:
    def test_no_marker(self):
        confirmed, reason = check_confirmed("rm -rf src/")
        assert not confirmed

    def test_simple_marker(self):
        confirmed, reason = check_confirmed("rm -rf src/ #DESTROY_CONFIRMED")
        assert confirmed
        assert reason == ""

    def test_marker_with_reason(self):
        confirmed, reason = check_confirmed("rm -rf src/ #DESTROY_CONFIRMED:migrating to new repo")
        assert confirmed
        assert reason == "migrating to new repo"

    def test_r4_valid_reason(self):
        assert is_reason_valid_r4("migrate to new infra")
        assert is_reason_valid_r4("廢棄舊系統")
        assert is_reason_valid_r4("fermeture du service")

    def test_r4_invalid_reason(self):
        assert not is_reason_valid_r4("just because")
        assert not is_reason_valid_r4("test")


# ─── evaluate (hook entry point) ────────────────────────────────

class TestEvaluate:
    def test_safe_bash(self):
        result = evaluate("Bash", {"command": "ls -la"})
        assert result["permissionDecision"] == "allow"

    def test_blocked_bash(self):
        result = evaluate("Bash", {"command": "rm -rf src/"})
        assert result["permissionDecision"] == "deny"

    def test_unknown_tool(self):
        result = evaluate("Read", {"file_path": "/etc/passwd"})
        assert result["permissionDecision"] == "allow"

    def test_disabled(self):
        with patch("concinno.destruction_guard.load_config",
                   return_value={"enabled": False}):
            result = evaluate("Bash", {"command": "rm -rf /"})
            assert result["permissionDecision"] == "allow"

    def test_r1_allowed(self):
        result = evaluate("Bash", {"command": "rm -rf node_modules"})
        assert result["permissionDecision"] == "allow"

    def test_r2_confirmed(self):
        result = evaluate("Bash", {"command": "rm -rf src/ #DESTROY_CONFIRMED"})
        assert result["permissionDecision"] == "allow"

    def test_r3_needs_reason(self):
        result = evaluate("Bash", {"command": "git push --force #DESTROY_CONFIRMED"})
        assert result["permissionDecision"] == "deny"

    def test_r3_with_reason(self):
        result = evaluate("Bash", {
            "command": "git push --force #DESTROY_CONFIRMED:hotfix deploy",
        })
        assert result["permissionDecision"] == "allow"

    def test_r4_not_downgraded_with_confirmed_tag(self):
        """Bug fix: R4 commands must stay R4 even with #DESTROY_CONFIRMED appended.

        Previously, $ anchored R4 patterns (e.g. `rm -rf / $`) would fail to
        match when #DESTROY_CONFIRMED:reason was appended, causing the command
        to be downgraded to R2 and bypassed with any confirmation.
        """
        # Without tag — should be R4
        risk_bare, _ = classify_bash("rm -rf / ")
        assert risk_bare == R4

        # With invalid reason — must still be R4 and denied
        result = evaluate("Bash", {
            "command": "rm -rf / #DESTROY_CONFIRMED:just because",
        })
        assert result["permissionDecision"] == "deny"

        # With valid R4 reason — allowed
        result = evaluate("Bash", {
            "command": "rm -rf / #DESTROY_CONFIRMED:migrate to new infra",
        })
        assert result["permissionDecision"] == "allow"

    def test_r4_home_not_downgraded_with_confirmed_tag(self):
        """Same bug for rm -rf ~/ pattern."""
        risk_bare, _ = classify_bash("rm -rf ~/")
        assert risk_bare == R4

        result = evaluate("Bash", {
            "command": "rm -rf ~/ #DESTROY_CONFIRMED:random excuse",
        })
        assert result["permissionDecision"] == "deny"


# ─── block_message ───────────────────────────────────────────────

class TestBlockMessage:
    def test_r2_message(self):
        msg = block_message(R2, "rm -rf src/", "Medium risk")
        assert "MEDIUM RISK" in msg
        assert "AskUserQuestion" in msg

    def test_r3_message(self):
        msg = block_message(R3, "git push --force", "High risk")
        assert "HIGH RISK" in msg
        assert "reason" in msg.lower()

    def test_r4_message(self):
        msg = block_message(R4, "rm -rf /", "Catastrophic")
        assert "CATASTROPHIC" in msg
        assert "keyword" in msg.lower()


# ─── Backup CLI functions ────────────────────────────────────────

class TestBackupCLI:
    def test_list_empty(self, tmp_path: Path):
        with patch("concinno.destruction_guard._backup_dir", return_value=tmp_path / "none"):
            assert "No backups" in list_backups()

    def test_list_with_backups(self, tmp_path: Path):
        bid = "20260310_120000_abc123"
        bdir = tmp_path / bid
        bdir.mkdir()
        (bdir / "manifest.json").write_text(json.dumps({
            "id": bid, "timestamp": "2026-03-10T12:00:00",
            "type": "auto", "targets": ["/tmp/foo"], "pinned": False,
        }))
        with patch("concinno.destruction_guard._backup_dir", return_value=tmp_path):
            result = list_backups()
            assert bid in result

    def test_pin_unpin(self, tmp_path: Path):
        bid = "test_backup"
        bdir = tmp_path / bid
        bdir.mkdir()
        (bdir / "manifest.json").write_text(json.dumps({
            "id": bid, "pinned": False,
        }))
        with patch("concinno.destruction_guard._backup_dir", return_value=tmp_path):
            result = set_pin(bid, True)
            assert "Pinned" in result
            data = json.loads((bdir / "manifest.json").read_text())
            assert data["pinned"] is True

    def test_cleanup(self, tmp_path: Path):
        # Create an old backup
        bid = "old_backup"
        bdir = tmp_path / bid
        bdir.mkdir()
        (bdir / "manifest.json").write_text(json.dumps({
            "id": bid, "timestamp": "2020-01-01T00:00:00",
            "pinned": False,
        }))
        with patch("concinno.destruction_guard._backup_dir", return_value=tmp_path):
            result = cleanup_backups(keep_days=1)
            assert "1" in result
            assert not bdir.exists()

    def test_cleanup_skips_pinned(self, tmp_path: Path):
        bid = "pinned_backup"
        bdir = tmp_path / bid
        bdir.mkdir()
        (bdir / "manifest.json").write_text(json.dumps({
            "id": bid, "timestamp": "2020-01-01T00:00:00",
            "pinned": True,
        }))
        with patch("concinno.destruction_guard._backup_dir", return_value=tmp_path):
            cleanup_backups(keep_days=1)
            assert bdir.exists()
