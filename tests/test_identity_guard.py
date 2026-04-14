"""Tests for cc_cortex.identity_guard — identity configuration protection."""

from __future__ import annotations

import re

import pytest

from cc_cortex.identity_guard import (
    check,
    classify_bash_identity,
    is_identity_file,
)

# ── is_identity_file ──────────────────────────────────────────────


class TestIsIdentityFile:
    """Test identity file detection."""

    @pytest.mark.parametrize("path", [
        "CLAUDE.md",
        "/project/CLAUDE.md",
        "E:/Cursor/CLAUDE.md",
        "C:\\Users\\user\\project\\CLAUDE.md",
    ])
    def test_claude_md(self, path: str):
        protected, reason = is_identity_file(path)
        assert protected is True
        assert "claude.md" in reason

    @pytest.mark.parametrize("path", [
        "settings.json",
        "/home/user/.claude/settings.json",
        "E:/project/.claude/settings.json",
    ])
    def test_settings_json(self, path: str):
        protected, reason = is_identity_file(path)
        assert protected is True
        assert "settings.json" in reason

    @pytest.mark.parametrize("path", [
        "settings.local.json",
        "/project/.claude/settings.local.json",
    ])
    def test_settings_local_json(self, path: str):
        protected, reason = is_identity_file(path)
        assert protected is True
        assert "settings.local.json" in reason

    @pytest.mark.parametrize("path", [
        "/project/.claude/rules/10-core.md",
        "E:\\Cursor\\.claude\\rules\\22-autonomous-mode.md",
        ".claude/rules/my-rule.md",
    ])
    def test_rules_md(self, path: str):
        protected, reason = is_identity_file(path)
        assert protected is True
        assert "identity config" in reason

    @pytest.mark.parametrize("path", [
        "/project/.claude/hooks/config.json",
        ".claude/hooks/destruction_guard_config.json",
        ".claude/hooks/schedule_config.yaml",
        ".claude/hooks/my_config.yml",
    ])
    def test_hook_configs(self, path: str):
        protected, reason = is_identity_file(path)
        assert protected is True
        assert "identity config" in reason

    @pytest.mark.parametrize("path", [
        "/project/src/main.py",
        "/project/README.md",
        "/project/.claude/hooks/on-stop.py",  # Python hook, not config
        "/project/package.json",
        "/project/data/settings.csv",
        "",
    ])
    def test_not_protected(self, path: str):
        protected, _ = is_identity_file(path)
        assert protected is False

    def test_extra_basenames(self):
        protected, reason = is_identity_file(
            "/project/MY_CONFIG.md",
            extra_basenames=frozenset(["my_config.md"]),
        )
        assert protected is True
        assert "my_config.md" in reason

    def test_extra_patterns(self):
        custom = re.compile(r"[/\\]\.myapp[/\\]config\.toml$", re.IGNORECASE)
        protected, _ = is_identity_file(
            "/project/.myapp/config.toml",
            extra_patterns=[custom],
        )
        assert protected is True

    def test_none_path(self):
        protected, _ = is_identity_file("")
        assert protected is False


# ── classify_bash_identity ────────────────────────────────────────


class TestClassifyBashIdentity:
    """Test Bash command classification for identity file modification."""

    @pytest.mark.parametrize("cmd", [
        "echo 'new content' > CLAUDE.md",
        "cat template.md > CLAUDE.md",
        "echo '{}' > settings.json",
        "echo 'rule' >> settings.local.json",
        "printf 'x' > CLAUDE.md",
        "tee CLAUDE.md < input.txt",
    ])
    def test_redirect_to_identity(self, cmd: str):
        dangerous, reason = classify_bash_identity(cmd)
        assert dangerous is True
        assert "redirect" in reason

    @pytest.mark.parametrize("cmd", [
        "sed -i 's/old/new/' CLAUDE.md",
        "sed -i.bak 's/x/y/' settings.json",
        "awk -i inplace '{print}' settings.local.json",
    ])
    def test_inplace_edit(self, cmd: str):
        dangerous, reason = classify_bash_identity(cmd)
        assert dangerous is True
        assert "in-place" in reason

    @pytest.mark.parametrize("cmd", [
        "rm CLAUDE.md",
        "rm -f settings.json",
        "del CLAUDE.md",
        "Remove-Item settings.json",
        "rm -rf .claude/rules/",
    ])
    def test_delete_identity(self, cmd: str):
        dangerous, reason = classify_bash_identity(cmd)
        assert dangerous is True
        assert "delete" in reason

    @pytest.mark.parametrize("cmd", [
        "mv CLAUDE.md CLAUDE.md.bak",
        "move settings.json settings.json.old",
        "ren settings.json old.json",
        "Rename-Item CLAUDE.md backup.md",
    ])
    def test_rename_move_identity(self, cmd: str):
        dangerous, reason = classify_bash_identity(cmd)
        assert dangerous is True
        assert "rename" in reason or "move" in reason

    @pytest.mark.parametrize("cmd", [
        "cat CLAUDE.md",          # reading is fine
        "grep 'pattern' CLAUDE.md",  # searching is fine
        "echo 'hello world'",     # no identity file
        "rm -rf node_modules",    # not identity
        "git push origin main",   # not identity
        "python script.py",
        "",
    ])
    def test_safe_commands(self, cmd: str):
        dangerous, _ = classify_bash_identity(cmd)
        assert dangerous is False


# ── check (main entry) ───────────────────────────────────────────


class TestCheck:
    """Test the main check() entry point."""

    # ── Edit/Write deny ──

    def test_edit_claude_md_deny(self):
        result = check("Edit", {"file_path": "/project/CLAUDE.md"})
        assert result is not None
        assert result["permissionDecision"] == "deny"
        assert "Identity Guard" in result["reason"]
        assert "CLAUDE.md" in result["reason"]

    def test_write_settings_deny(self):
        result = check("Write", {"file_path": "/project/.claude/settings.json"})
        assert result is not None
        assert result["permissionDecision"] == "deny"

    def test_write_rules_deny(self):
        result = check("Write", {"file_path": "/project/.claude/rules/10-core.md"})
        assert result is not None
        assert result["permissionDecision"] == "deny"

    def test_edit_hook_config_deny(self):
        result = check("Edit", {
            "file_path": "/project/.claude/hooks/destruction_guard_config.json",
        })
        assert result is not None
        assert result["permissionDecision"] == "deny"

    def test_write_settings_local_deny(self):
        result = check("Write", {"file_path": ".claude/settings.local.json"})
        assert result is not None
        assert result["permissionDecision"] == "deny"

    # ── Edit/Write allow ──

    def test_edit_normal_file_allow(self):
        result = check("Edit", {"file_path": "/project/src/main.py"})
        assert result is None

    def test_write_normal_file_allow(self):
        result = check("Write", {"file_path": "/project/README.md"})
        assert result is None

    def test_edit_hook_script_allow(self):
        """Python hook scripts are not identity config."""
        result = check("Edit", {"file_path": "/project/.claude/hooks/on-stop.py"})
        assert result is None

    # ── Bash deny ──

    def test_bash_redirect_claude_md_deny(self):
        result = check("Bash", {"command": "echo 'new' > CLAUDE.md"})
        assert result is not None
        assert result["permissionDecision"] == "deny"
        assert "Identity Guard" in result["reason"]

    def test_bash_sed_settings_deny(self):
        result = check("Bash", {"command": "sed -i 's/old/new/' settings.json"})
        assert result is not None
        assert result["permissionDecision"] == "deny"

    def test_bash_rm_rules_deny(self):
        result = check("Bash", {"command": "rm -rf .claude/rules/"})
        assert result is not None
        assert result["permissionDecision"] == "deny"

    # ── Bash allow ──

    def test_bash_read_claude_md_allow(self):
        result = check("Bash", {"command": "cat CLAUDE.md"})
        assert result is None

    def test_bash_normal_command_allow(self):
        result = check("Bash", {"command": "npm install express"})
        assert result is None

    # ── Other tools ──

    def test_read_tool_allow(self):
        """Read tool should never be blocked."""
        result = check("Read", {"file_path": "/project/CLAUDE.md"})
        assert result is None

    def test_agent_tool_allow(self):
        result = check("Agent", {"prompt": "do something"})
        assert result is None

    def test_glob_tool_allow(self):
        result = check("Glob", {"pattern": "CLAUDE.md"})
        assert result is None

    # ── Edge cases ──

    def test_empty_tool_input(self):
        result = check("Edit", {})
        assert result is None

    def test_non_dict_input(self):
        result = check("Edit", "not a dict")  # type: ignore
        assert result is None

    def test_notebook_path(self):
        """NotebookEdit with notebook_path should work too."""
        result = check("NotebookEdit", {"notebook_path": "/project/CLAUDE.md"})
        assert result is not None
        assert result["permissionDecision"] == "deny"

    def test_extra_basenames_in_check(self):
        result = check(
            "Edit",
            {"file_path": "/project/CUSTOM.md"},
            extra_basenames=frozenset(["custom.md"]),
        )
        assert result is not None
        assert result["permissionDecision"] == "deny"

    def test_extra_patterns_in_check(self):
        custom = re.compile(r"[/\\]\.myapp[/\\].*\.toml$", re.IGNORECASE)
        result = check(
            "Write",
            {"file_path": "/project/.myapp/config.toml"},
            extra_patterns=[custom],
        )
        assert result is not None
        assert result["permissionDecision"] == "deny"

    def test_deny_has_additional_context(self):
        result = check("Edit", {"file_path": "/project/CLAUDE.md"})
        assert result is not None
        assert "additionalContext" in result
        assert "identity" in result["additionalContext"].lower()

    def test_bash_deny_has_identity_context(self):
        result = check("Bash", {"command": "echo 'x' > CLAUDE.md"})
        assert result is not None
        assert "additionalContext" in result
        assert "identity" in result["additionalContext"].lower()

    def test_bash_long_command_still_denied(self):
        long_cmd = "echo '" + "x" * 200 + "' > CLAUDE.md"
        result = check("Bash", {"command": long_cmd})
        assert result is not None
        assert "identity" in result["additionalContext"].lower()
