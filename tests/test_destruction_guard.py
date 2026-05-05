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
    DestructionBlockedError,
    block_message,
    check_confirmed,
    classify_bash,
    classify_write,
    cleanup_backups,
    confirm_with_options,
    destruction_gate,
    evaluate,
    is_reason_valid_r4,
    list_backups,
    set_pin,
    split_commands,
    suggest_safer_alternative,
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

    # 2026-04-18 regression: git gc/reflog/filter-repo/bfg patterns.
    # squash-path silent failure root cause — these operations rewrite
    # history irreversibly and MUST stay R3 gated with a reason keyword.
    @pytest.mark.parametrize("cmd", [
        "git gc --prune=now",
        "git gc --prune=all",
        "git gc --prune=2.weeks.ago",
        "git gc --aggressive",
        "git gc --aggressive --prune=now",
        "git reflog expire --all --expire=now",
        "git prune --expire=now",
        "git filter-branch --force --tree-filter 'rm -f secret'",
        "git filter-repo --invert-paths --path secret.txt",
        "bfg --strip-blobs-bigger-than 100M",
        "bfg --delete-files '*.key'",
    ])
    def test_r3_git_history_rewrite(self, cmd: str):
        """New R3 patterns for irreversible git/bfg rewrites.

        All must trigger R3 (needs reason keyword, not just confirmation).
        Denies must stand without #DESTROY_CONFIRMED:<reason>.
        """
        risk, _ = classify_bash(cmd)
        assert risk == R3, f"expected R3 for {cmd!r}, got R{risk}"

    @pytest.mark.parametrize("cmd", [
        "git gc --prune=now #DESTROY_CONFIRMED:migrate to new remote",
        "git gc --aggressive #DESTROY_CONFIRMED:decommission large pack",
        "bfg --strip-blobs-bigger-than 100M "
        "#DESTROY_CONFIRMED:redact large PII blobs",
        "git filter-repo --invert-paths --path secret.txt "
        "#DESTROY_CONFIRMED:migrate after secret leak",
        "git reflog expire --all --expire=now "
        "#DESTROY_CONFIRMED:archive old branches",
    ])
    def test_r3_git_history_rewrite_allowed_with_reason(self, cmd: str):
        """Reason keyword >3 chars unlocks R3 commands."""
        result = evaluate("Bash", {"command": cmd})
        assert result["permissionDecision"] == "allow", (
            f"expected allow with reason, got {result}"
        )

    def test_r3_git_gc_denied_without_reason(self):
        """R3 denies when only #DESTROY_CONFIRMED with no reason attached."""
        result = evaluate(
            "Bash",
            {"command": "git gc --prune=now #DESTROY_CONFIRMED"},
        )
        assert result["permissionDecision"] == "deny"

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


# ─── confirm_with_options (P1 #5) ────────────────────────────────


class TestConfirmWithOptions:
    """AskUserQuestion template builder."""

    def test_two_options(self):
        tpl = confirm_with_options(
            "Proceed?",
            [
                {"label": "abort", "description": "stop"},
                {"label": "proceed", "description": "go anyway"},
            ],
        )
        assert tpl["question"] == "Proceed?"
        assert len(tpl["options"]) == 2
        assert tpl["default"] == "abort"
        # preview defaults to None when omitted
        assert tpl["options"][0]["preview"] is None

    def test_four_options_with_previews(self):
        tpl = confirm_with_options(
            "Choose",
            [
                {"label": "a", "description": "desc a", "preview": "cmd a"},
                {"label": "b", "description": "desc b", "preview": "cmd b"},
                {"label": "c", "description": "desc c", "preview": "cmd c"},
                {"label": "d", "description": "desc d", "preview": "cmd d"},
            ],
            default="a",
        )
        assert len(tpl["options"]) == 4
        assert tpl["default"] == "a"
        assert tpl["options"][2]["preview"] == "cmd c"

    def test_out_of_range_raises(self):
        with pytest.raises(ValueError, match="2-4"):
            confirm_with_options("?", [{"label": "x", "description": "y"}])
        with pytest.raises(ValueError, match="2-4"):
            confirm_with_options(
                "?",
                [
                    {"label": f"x{i}", "description": "y"}
                    for i in range(5)
                ],
            )

    def test_missing_label_raises(self):
        with pytest.raises(ValueError, match="label"):
            confirm_with_options(
                "?",
                [
                    {"description": "missing label"},
                    {"label": "ok", "description": "ok"},
                ],
            )


# ─── suggest_safer_alternative (P1 #6) ───────────────────────────


class TestSaferAlternative:
    """Lookup table for destructive-command alternatives."""

    @pytest.mark.parametrize("cmd,expected_label_contains", [
        ("rm -f path/to/app.lock", "python unlink"),
        ("git reset --hard HEAD~3", "git stash push"),
        ("git push --force origin main", "force-with-lease"),
        ("git gc --prune=now", "git gc --auto"),
        ("twine upload dist/*", "skip-existing"),
        ("DROP TABLE users;", "rename to deprecated"),
        ("kubectl delete namespace prod", "snapshot resources"),
        ("docker system prune -a", "no -a"),
        ("aws s3 rb s3://my-bucket --force", "versioning"),
        ("git filter-repo --invert-paths --path x.txt", "clone fresh"),
    ])
    def test_matches_expected_alternative(
        self, cmd: str, expected_label_contains: str,
    ):
        alts = suggest_safer_alternative(cmd)
        assert alts is not None, f"no alternative for {cmd!r}"
        assert any(
            expected_label_contains in a["label"]
            for a in alts
        ), f"expected label containing {expected_label_contains!r}: {alts}"

    def test_no_match_returns_none(self):
        # rm -rf dir is R2 but no dedicated safer hint registered for
        # bare dir paths — still None.
        assert suggest_safer_alternative("ls -la") is None
        assert suggest_safer_alternative("") is None
        assert suggest_safer_alternative("echo hello") is None

    def test_rm_rf_dir_gets_soft_delete(self):
        alts = suggest_safer_alternative("rm -rf build/")
        assert alts is not None
        assert any("soft delete" in a["label"] for a in alts)

    def test_returned_list_is_shallow_copy(self):
        """Caller mutations must not leak into the registry."""
        alts = suggest_safer_alternative("git gc --prune=now")
        assert alts is not None
        original_len = len(alts)
        alts[0]["label"] = "MUTATED"
        # Re-query — registry still pristine.
        alts2 = suggest_safer_alternative("git gc --prune=now")
        assert alts2 is not None
        assert len(alts2) == original_len
        assert "MUTATED" not in [a["label"] for a in alts2]


# ─── evaluate() now injects ask_user_question_template ───────────


class TestEvaluateTemplate:
    def test_r3_deny_includes_template(self):
        result = evaluate(
            "Bash", {"command": "git gc --prune=now"},
        )
        assert result["permissionDecision"] == "deny"
        extras = result.get("additionalContext", {})
        assert isinstance(extras, dict)
        tpl = extras.get("ask_user_question_template")
        assert tpl is not None
        labels = [o["label"] for o in tpl["options"]]
        assert "abort" in labels
        # At least one label references the safer alt or proceed path
        assert any("proceed" in lbl or "gc --auto" in lbl for lbl in labels)
        assert tpl["default"] == "abort"
        assert 2 <= len(tpl["options"]) <= 4

    def test_r3_deny_includes_safer_alternatives(self):
        result = evaluate(
            "Bash", {"command": "git push --force origin main"},
        )
        assert result["permissionDecision"] == "deny"
        extras = result["additionalContext"]
        assert "safer_alternatives" in extras
        alts = extras["safer_alternatives"]
        assert any("force-with-lease" in a["command"] for a in alts)

    def test_r2_deny_has_no_additional_context(self):
        # R2 without confirmation blocks but no ask-user template
        # (R2 is mild enough that the existing block_message suffices).
        result = evaluate("Bash", {"command": "rm -rf src/"})
        assert result["permissionDecision"] == "deny"
        # additionalContext may be absent for R2 — that's expected.
        assert "additionalContext" not in result


# ─── destruction_gate decorator (P1 #7) ─────────────────────────


class TestDestructionGate:
    """The decorator must block direct calls but pass through hook ctx."""

    def test_direct_call_without_reason_raises(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.delenv("CONCINNO_INLINE_SQUASH", raising=False)
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)

        @destruction_gate(risk="R3", op_name="squash_auto_commits")
        def danger():
            return "ran"

        with pytest.raises(DestructionBlockedError):
            danger()

    def test_direct_call_with_valid_reason_passes(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.delenv("CONCINNO_INLINE_SQUASH", raising=False)
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)

        @destruction_gate(risk="R3", op_name="squash_auto_commits")
        def danger():
            return "ran"

        assert danger(reason="migrate repo to new server") == "ran"

    def test_direct_call_with_bogus_reason_raises(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.delenv("CONCINNO_INLINE_SQUASH", raising=False)
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)

        @destruction_gate(risk="R3", op_name="squash_auto_commits")
        def danger():
            return "ran"

        with pytest.raises(DestructionBlockedError):
            danger(reason="because I said so")

    def test_hook_context_passes(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/tmp/test")
        monkeypatch.setenv("CONCINNO_INLINE_SQUASH", "1")

        @destruction_gate(risk="R3", op_name="squash_auto_commits")
        def danger():
            return "ran"

        assert danger() == "ran"

    def test_unknown_op_always_requires_reason(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/tmp/test")

        @destruction_gate(risk="R2", op_name="bogus_op_not_in_map")
        def danger():
            return "ran"

        # no escape env flag maps to this op → gate fires
        with pytest.raises(DestructionBlockedError):
            danger()
        # ...unless reason passed
        assert danger(reason="decommission legacy pipeline") == "ran"

    def test_decorator_preserves_name_and_doc(self):
        @destruction_gate(risk="R2", op_name="prune")
        def danger():
            """danger docs"""
            return 1

        assert danger.__name__ == "danger"
        assert "danger docs" in (danger.__doc__ or "")
        assert hasattr(danger, "__wrapped__")


# ─── audit log rotation ──────────────────────────────────────────


class TestAuditRotation:
    def test_rotation_creates_gz_archive(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        import gzip

        from concinno import destruction_guard as dg

        # Point audit path at tmp_path
        fake_log = tmp_path / "destruction_audit.log"
        monkeypatch.setattr(dg, "_audit_log_path", lambda: fake_log)
        # Pre-fill with > 10 MB garbage
        fake_log.write_text("x" * (11 * 1024 * 1024))
        # Trigger rotation
        dg.audit("touch trigger", R0, "allow", "rotation test")
        archive = fake_log.with_name("destruction_audit.log.1.gz")
        assert archive.exists(), "expected rotated archive"
        with gzip.open(archive, "rb") as f:
            head = f.read(100)
            assert b"x" in head
        # New log has the fresh entry only
        fresh = fake_log.read_text(encoding="utf-8")
        assert "rotation test" in fresh
