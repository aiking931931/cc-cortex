"""Tests for cc_cortex.git_safety — Dangerous git operation detection."""

from __future__ import annotations

from cc_cortex.git_safety import check


class TestGitSafetyCheck:
    # ── Should block ──────────────────────────────────

    def test_blocks_force_push(self):
        r = check("Bash", {"command": "git push --force origin main"})
        assert r is not None
        assert r["permissionDecision"] == "deny"
        assert r["cmd"] == "force push"

    def test_blocks_reset_hard(self):
        r = check("Bash", {"command": "git reset --hard HEAD~3"})
        assert r is not None
        assert r["cmd"] == "reset --hard"

    def test_blocks_clean_f(self):
        r = check("Bash", {"command": "git clean -fd"})
        assert r is not None
        assert r["cmd"] == "clean -f"

    def test_blocks_checkout_dot(self):
        r = check("Bash", {"command": "git checkout -- ."})
        assert r is not None
        assert r["cmd"] == "checkout -- ."

    def test_blocks_branch_D(self):
        r = check("Bash", {"command": "git branch -D feature/old"})
        assert r is not None
        assert r["cmd"] == "branch -D"

    def test_blocks_rebase_i(self):
        r = check("Bash", {"command": "git rebase -i HEAD~5"})
        assert r is not None
        assert r["cmd"] == "rebase -i (interactive)"

    def test_blocks_push_to_main(self):
        r = check("Bash", {"command": "git push origin main"})
        assert r is not None
        assert "main" in r["cmd"].lower() or "push" in r["reason"].lower()

    # ── Should pass ───────────────────────────────────

    def test_force_with_lease_ok(self):
        assert check("Bash", {"command": "git push --force-with-lease origin feat"}) is None

    def test_normal_push_ok(self):
        assert check("Bash", {"command": "git push origin feature/x"}) is None

    def test_normal_reset_ok(self):
        assert check("Bash", {"command": "git reset HEAD file.py"}) is None

    def test_branch_d_lowercase_ok(self):
        assert check("Bash", {"command": "git branch -d merged-branch"}) is None

    def test_non_bash_tool(self):
        assert check("Read", {"file_path": "x"}) is None

    def test_non_git_command(self):
        assert check("Bash", {"command": "ls -la"}) is None

    def test_empty_command(self):
        assert check("Bash", {"command": ""}) is None

    def test_git_status_ok(self):
        assert check("Bash", {"command": "git status"}) is None

    def test_git_log_ok(self):
        assert check("Bash", {"command": "git log --oneline -10"}) is None

    def test_non_dict_input(self):
        assert check("Bash", "not a dict") is None
