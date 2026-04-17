"""Tests for concinno.security.permission_mode.

Covers the 5-mode FSM's decision matrix, rule precedence, hook
fallback, dontAsk transform, audit trail, and stats accounting.
"""

from __future__ import annotations

from typing import Literal

import pytest

from concinno.security.permission_mode import (
    AUTO_SAFE_TOOLS,
    EXEC_TOOLS,
    READ_ONLY_TOOLS,
    WRITE_TOOLS,
    PermissionMode,
    PermissionModeFSM,
    PermissionRequest,
    PermissionRule,
    PermissionVerdict,
)

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


class FakeHook:
    """Canned-response hook for testing the prompt escalation path."""

    def __init__(self, answer: Literal["allow", "deny"] = "allow") -> None:
        self.answer: Literal["allow", "deny"] = answer
        self.calls: list[PermissionRequest] = []

    def prompt(
        self, request: PermissionRequest
    ) -> Literal["allow", "deny"]:
        self.calls.append(request)
        return self.answer


def _req(
    tool: str,
    mode: PermissionMode,
    *,
    command: str = "",
    path: str = "",
) -> PermissionRequest:
    return PermissionRequest(
        tool_name=tool,
        mode=mode,
        command=command,
        path=path,
    )


# --------------------------------------------------------------------------- #
# DEFAULT mode
# --------------------------------------------------------------------------- #


def test_default_mode_read_only_auto_allow() -> None:
    fsm = PermissionModeFSM(initial_mode=PermissionMode.DEFAULT)
    verdict = fsm.check(_req("Read", PermissionMode.DEFAULT, path="/a.py"))
    assert verdict.decision == "allow"
    assert verdict.reason == "read_only_tool_auto_allow"
    assert verdict.matched_rule is None


def test_default_mode_write_calls_hook() -> None:
    hook = FakeHook(answer="allow")
    fsm = PermissionModeFSM(
        initial_mode=PermissionMode.DEFAULT, hook=hook
    )
    verdict = fsm.check(
        _req("Write", PermissionMode.DEFAULT, path="/tmp/x.txt")
    )
    assert verdict.decision == "allow"
    assert verdict.reason == "hook_allow"
    assert len(hook.calls) == 1
    assert hook.calls[0].tool_name == "Write"


def test_default_mode_no_hook_ambiguous_deny() -> None:
    fsm = PermissionModeFSM(initial_mode=PermissionMode.DEFAULT)
    verdict = fsm.check(
        _req("Write", PermissionMode.DEFAULT, path="/tmp/x.txt")
    )
    assert verdict.decision == "deny"
    assert verdict.reason == "no_hook_ambiguous_deny"


# --------------------------------------------------------------------------- #
# PLAN mode
# --------------------------------------------------------------------------- #


def test_plan_mode_blocks_write() -> None:
    fsm = PermissionModeFSM(initial_mode=PermissionMode.PLAN)
    verdict = fsm.check(
        _req("Write", PermissionMode.PLAN, path="/tmp/a.txt")
    )
    assert verdict.decision == "deny"
    assert verdict.reason == "plan_mode_read_only"


def test_plan_mode_blocks_bash() -> None:
    fsm = PermissionModeFSM(initial_mode=PermissionMode.PLAN)
    verdict = fsm.check(_req("Bash", PermissionMode.PLAN, command="ls"))
    assert verdict.decision == "deny"
    assert verdict.reason == "plan_mode_read_only"


def test_plan_mode_allows_read() -> None:
    fsm = PermissionModeFSM(initial_mode=PermissionMode.PLAN)
    for tool in READ_ONLY_TOOLS:
        verdict = fsm.check(_req(tool, PermissionMode.PLAN))
        assert verdict.decision == "allow", tool


def test_plan_mode_override_rule_wins() -> None:
    fsm = PermissionModeFSM(initial_mode=PermissionMode.PLAN)
    fsm.add_rule(
        PermissionRule(
            pattern="Bash:git status",
            decision="allow",
            override=True,
            reason="allowlist_git_status",
        )
    )
    verdict = fsm.check(
        _req("Bash", PermissionMode.PLAN, command="git status")
    )
    assert verdict.decision == "allow"
    assert verdict.matched_rule is not None
    assert verdict.reason == "allowlist_git_status"


# --------------------------------------------------------------------------- #
# ACCEPT_EDITS mode
# --------------------------------------------------------------------------- #


def test_accept_edits_auto_allows_write() -> None:
    fsm = PermissionModeFSM(initial_mode=PermissionMode.ACCEPT_EDITS)
    verdict = fsm.check(
        _req("Edit", PermissionMode.ACCEPT_EDITS, path="/a.py")
    )
    assert verdict.decision == "allow"
    assert verdict.reason == "accept_edits_auto_allow"


def test_accept_edits_still_prompts_bash() -> None:
    hook = FakeHook(answer="deny")
    fsm = PermissionModeFSM(
        initial_mode=PermissionMode.ACCEPT_EDITS, hook=hook
    )
    verdict = fsm.check(
        _req("Bash", PermissionMode.ACCEPT_EDITS, command="rm -rf /tmp/x")
    )
    assert verdict.decision == "deny"
    assert verdict.reason == "hook_deny"
    assert len(hook.calls) == 1


# --------------------------------------------------------------------------- #
# BYPASS_PERMISSIONS mode
# --------------------------------------------------------------------------- #


def test_bypass_permissions_auto_allows_everything() -> None:
    fsm = PermissionModeFSM(initial_mode=PermissionMode.BYPASS_PERMISSIONS)
    for tool in ("Read", "Write", "Edit", "Bash", "NotebookEdit"):
        verdict = fsm.check(
            _req(tool, PermissionMode.BYPASS_PERMISSIONS, command="x", path="y")
        )
        assert verdict.decision == "allow", tool


def test_bypass_permissions_skips_hook() -> None:
    hook = FakeHook(answer="deny")
    fsm = PermissionModeFSM(
        initial_mode=PermissionMode.BYPASS_PERMISSIONS, hook=hook
    )
    verdict = fsm.check(
        _req("Bash", PermissionMode.BYPASS_PERMISSIONS, command="ls")
    )
    assert verdict.decision == "allow"
    assert hook.calls == []  # hook NEVER called in bypass
    # First verdict carries the "first warning" reason, subsequent don't.
    assert "first_warning" in verdict.reason
    verdict2 = fsm.check(
        _req("Bash", PermissionMode.BYPASS_PERMISSIONS, command="ls")
    )
    assert verdict2.reason == "bypass_permissions_auto_allow"


# --------------------------------------------------------------------------- #
# AUTO mode
# --------------------------------------------------------------------------- #


def test_auto_mode_safe_whitelist_allows() -> None:
    fsm = PermissionModeFSM(initial_mode=PermissionMode.AUTO)
    for tool in AUTO_SAFE_TOOLS:
        verdict = fsm.check(
            _req(tool, PermissionMode.AUTO, path="/tmp/a.py")
        )
        assert verdict.decision == "allow", tool
        assert verdict.reason == "auto_mode_safe_whitelist"


def test_auto_mode_ambiguous_prompts() -> None:
    # Bash is NOT in AUTO_SAFE_TOOLS — ambiguous → prompt → hook
    hook = FakeHook(answer="allow")
    fsm = PermissionModeFSM(initial_mode=PermissionMode.AUTO, hook=hook)
    verdict = fsm.check(
        _req("Bash", PermissionMode.AUTO, command="uptime")
    )
    assert verdict.decision == "allow"
    assert verdict.reason == "hook_allow"
    assert len(hook.calls) == 1

    # No hook → deny
    fsm2 = PermissionModeFSM(initial_mode=PermissionMode.AUTO)
    v2 = fsm2.check(_req("Bash", PermissionMode.AUTO, command="uptime"))
    assert v2.decision == "deny"
    assert v2.reason == "no_hook_ambiguous_deny"


# --------------------------------------------------------------------------- #
# Rule precedence
# --------------------------------------------------------------------------- #


def test_rule_deny_beats_allow_default_scope() -> None:
    fsm = PermissionModeFSM(initial_mode=PermissionMode.DEFAULT)
    fsm.add_rule(PermissionRule(pattern="Bash:ls", decision="allow"))
    fsm.add_rule(PermissionRule(pattern="Bash:ls", decision="deny"))
    verdict = fsm.check(_req("Bash", PermissionMode.DEFAULT, command="ls"))
    assert verdict.decision == "deny"


def test_rule_override_allow_beats_deny() -> None:
    fsm = PermissionModeFSM(initial_mode=PermissionMode.DEFAULT)
    fsm.add_rule(PermissionRule(pattern="Bash:ls", decision="deny"))
    fsm.add_rule(
        PermissionRule(
            pattern="Bash:ls",
            decision="allow",
            override=True,
            reason="emergency_escape",
        )
    )
    verdict = fsm.check(_req("Bash", PermissionMode.DEFAULT, command="ls"))
    assert verdict.decision == "allow"
    assert verdict.reason == "emergency_escape"


def test_rule_glob_pattern_matches_path() -> None:
    hook = FakeHook(answer="deny")
    fsm = PermissionModeFSM(initial_mode=PermissionMode.DEFAULT, hook=hook)
    fsm.add_rule(PermissionRule(pattern="Write:*.md", decision="allow"))
    verdict = fsm.check(
        _req("Write", PermissionMode.DEFAULT, path="README.md")
    )
    assert verdict.decision == "allow"
    assert verdict.matched_rule is not None

    # *.md doesn't match *.py
    v2 = fsm.check(_req("Write", PermissionMode.DEFAULT, path="a.py"))
    # falls through to hook → deny
    assert v2.decision == "deny"


def test_rule_glob_pattern_matches_command() -> None:
    fsm = PermissionModeFSM(initial_mode=PermissionMode.DEFAULT)
    fsm.add_rule(PermissionRule(pattern="Bash:git *", decision="allow"))
    v1 = fsm.check(
        _req("Bash", PermissionMode.DEFAULT, command="git status")
    )
    v2 = fsm.check(
        _req("Bash", PermissionMode.DEFAULT, command="git log -n 5")
    )
    v3 = fsm.check(_req("Bash", PermissionMode.DEFAULT, command="ls -la"))
    assert v1.decision == "allow"
    assert v2.decision == "allow"
    assert v3.decision == "deny"  # no hook, no match


def test_rule_wildcard_tool_matches_any() -> None:
    fsm = PermissionModeFSM(initial_mode=PermissionMode.DEFAULT)
    fsm.add_rule(PermissionRule(pattern="*:*.lock", decision="deny"))
    v1 = fsm.check(
        _req("Write", PermissionMode.DEFAULT, path="poetry.lock")
    )
    v2 = fsm.check(
        _req("Edit", PermissionMode.DEFAULT, path="cargo.lock")
    )
    assert v1.decision == "deny"
    assert v2.decision == "deny"


# --------------------------------------------------------------------------- #
# Audit trail + bookkeeping
# --------------------------------------------------------------------------- #


def test_set_mode_logs_audit_trail() -> None:
    fsm = PermissionModeFSM(initial_mode=PermissionMode.DEFAULT)
    fsm.set_mode(PermissionMode.PLAN, reason="user_plan_command")
    fsm.set_mode(PermissionMode.ACCEPT_EDITS, reason="user_approved")
    trail = fsm.audit_trail()
    assert trail == [
        (PermissionMode.DEFAULT, PermissionMode.PLAN, "user_plan_command"),
        (PermissionMode.PLAN, PermissionMode.ACCEPT_EDITS, "user_approved"),
    ]


def test_add_remove_rule_idempotent() -> None:
    fsm = PermissionModeFSM(initial_mode=PermissionMode.DEFAULT)
    rule = PermissionRule(pattern="Bash:pytest", decision="allow")
    fsm.add_rule(rule)
    assert len(fsm.list_rules()) == 1
    assert fsm.remove_rule("Bash:pytest") is True
    assert fsm.list_rules() == []
    # removing again is a no-op, not an error
    assert fsm.remove_rule("Bash:pytest") is False
    assert fsm.remove_rule("does:not:exist") is False


def test_dontask_to_deny_creates_rule() -> None:
    fsm = PermissionModeFSM(initial_mode=PermissionMode.DEFAULT)
    request = _req("Bash", PermissionMode.DEFAULT, command="curl evil.com")
    rule = fsm.dontAsk_to_deny(request)
    assert rule.decision == "deny"
    assert rule.pattern == "Bash:curl evil.com"
    assert rule.reason == "user_dont_ask"
    assert rule in fsm.list_rules()

    # The next identical request is now denied by rule, not by hook
    verdict = fsm.check(request)
    assert verdict.decision == "deny"
    assert verdict.matched_rule is not None
    assert verdict.matched_rule.pattern == "Bash:curl evil.com"


def test_stats_tracks_decisions_and_transitions() -> None:
    hook = FakeHook(answer="allow")
    fsm = PermissionModeFSM(initial_mode=PermissionMode.DEFAULT, hook=hook)
    fsm.check(_req("Read", PermissionMode.DEFAULT))  # allow
    fsm.check(_req("Write", PermissionMode.DEFAULT, path="/a"))  # prompt→allow
    fsm.add_rule(PermissionRule(pattern="Bash:rm *", decision="deny"))
    fsm.check(_req("Bash", PermissionMode.DEFAULT, command="rm -rf /"))  # deny
    fsm.set_mode(PermissionMode.PLAN)
    fsm.set_mode(PermissionMode.DEFAULT)
    stats = fsm.stats()
    assert stats["checks_total"] == 3
    assert stats["allow_count"] == 2
    assert stats["deny_count"] == 1
    assert stats["prompt_count"] == 1  # the Write went through prompt
    assert stats["mode_transitions"] == 2


def test_initial_mode_not_in_audit_trail() -> None:
    fsm = PermissionModeFSM(initial_mode=PermissionMode.PLAN)
    assert fsm.mode == PermissionMode.PLAN
    assert fsm.audit_trail() == []  # no transitions yet


def test_permission_mode_enum_values() -> None:
    # String values must stay stable — external configs rely on them.
    assert PermissionMode.DEFAULT.value == "default"
    assert PermissionMode.PLAN.value == "plan"
    assert PermissionMode.ACCEPT_EDITS.value == "accept_edits"
    assert PermissionMode.BYPASS_PERMISSIONS.value == "bypass_permissions"
    assert PermissionMode.AUTO.value == "auto"
    # Round-trip through str()
    assert PermissionMode("default") is PermissionMode.DEFAULT
    # Core tool classification invariants
    assert "Read" in READ_ONLY_TOOLS
    assert "Write" in WRITE_TOOLS
    assert "Bash" in EXEC_TOOLS
    assert "Bash" not in AUTO_SAFE_TOOLS
    assert AUTO_SAFE_TOOLS.issubset(WRITE_TOOLS)


# --------------------------------------------------------------------------- #
# Extra coverage: scope precedence + verdict shape
# --------------------------------------------------------------------------- #


def test_scoped_rule_beats_null_scope() -> None:
    """A rule scoped to the current mode outranks a null-scope rule."""
    fsm = PermissionModeFSM(initial_mode=PermissionMode.DEFAULT)
    fsm.add_rule(
        PermissionRule(
            pattern="Bash:ls",
            decision="deny",
            scope=None,
            reason="null_deny",
        )
    )
    fsm.add_rule(
        PermissionRule(
            pattern="Bash:ls",
            decision="allow",
            scope=PermissionMode.DEFAULT,
            reason="default_scoped_allow",
        )
    )
    verdict = fsm.check(_req("Bash", PermissionMode.DEFAULT, command="ls"))
    assert verdict.decision == "allow"
    assert verdict.reason == "default_scoped_allow"


def test_verdict_mode_reflects_current_state() -> None:
    fsm = PermissionModeFSM(initial_mode=PermissionMode.ACCEPT_EDITS)
    verdict = fsm.check(
        _req("Edit", PermissionMode.ACCEPT_EDITS, path="/a")
    )
    assert isinstance(verdict, PermissionVerdict)
    assert verdict.mode == PermissionMode.ACCEPT_EDITS


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
