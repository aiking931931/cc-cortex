"""Tests for the 11-variant PermissionDecisionReason discriminated union.

P0.7 port of CC's ``types/permissions.ts:271-324`` typed decision reason
onto concinno's existing 5-mode FSM. Covers:

  - All 11 enum variants exist with exact string values
  - PermissionDecisionReason immutability & invariants
  - Variant-specific extras (rule_id, classifier_score, metadata)
  - PermissionResult action literal enforcement
  - make_decision ergonomic helper for each variant
  - legacy_reason() snapshot format stability
  - FSM.check_result() bridging for MODE and RULE variants
  - Equality and frozen-dataclass semantics
  - Regression: existing PermissionMode FSM behavior unchanged
"""

from __future__ import annotations

import dataclasses

import pytest

from concinno.security.permission_mode import (
    PermissionAction,
    PermissionDecisionReason,
    PermissionDecisionReasonType,
    PermissionMode,
    PermissionModeFSM,
    PermissionRequest,
    PermissionResult,
    PermissionRule,
    make_decision,
)

# --------------------------------------------------------------------------- #
# Enum variants — exact string values match CC source
# --------------------------------------------------------------------------- #


def test_eleven_variants_exist_with_exact_values() -> None:
    """CC types/permissions.ts:271-324 defines exactly 11 variants."""
    expected = {
        "RULE": "rule",
        "MODE": "mode",
        "SUBCOMMAND_RESULTS": "subcommand_results",
        "PERMISSION_PROMPT_TOOL": "permission_prompt_tool",
        "HOOK": "hook",
        "ASYNC_AGENT": "async_agent",
        "SANDBOX_OVERRIDE": "sandbox_override",
        "CLASSIFIER": "classifier",
        "WORKING_DIR": "working_dir",
        "SAFETY_CHECK": "safety_check",
        "OTHER": "other",
    }
    assert len(expected) == 11
    actual = {m.name: m.value for m in PermissionDecisionReasonType}
    assert actual == expected


def test_enum_is_str_subclass_for_json_serialization() -> None:
    assert isinstance(PermissionDecisionReasonType.RULE, str)
    assert PermissionDecisionReasonType.RULE == "rule"


# --------------------------------------------------------------------------- #
# PermissionDecisionReason — dataclass invariants
# --------------------------------------------------------------------------- #


def test_reason_is_frozen() -> None:
    pdr = PermissionDecisionReason(
        type=PermissionDecisionReasonType.MODE,
        detail="mode=plan",
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        pdr.detail = "mutated"  # type: ignore[misc]


def test_reason_default_fields() -> None:
    pdr = PermissionDecisionReason(type=PermissionDecisionReasonType.HOOK)
    assert pdr.detail == ""
    assert pdr.rule_id is None
    assert pdr.classifier_score is None
    assert pdr.metadata == {}


def test_reason_rule_variant_carries_rule_id() -> None:
    pdr = PermissionDecisionReason(
        type=PermissionDecisionReasonType.RULE,
        detail="blocked",
        rule_id="bash:rm-rf",
    )
    assert pdr.rule_id == "bash:rm-rf"


def test_reason_classifier_variant_carries_score() -> None:
    pdr = PermissionDecisionReason(
        type=PermissionDecisionReasonType.CLASSIFIER,
        detail="high-risk bash",
        classifier_score=0.87,
    )
    assert pdr.classifier_score == 0.87


def test_reason_metadata_accepts_arbitrary_dict() -> None:
    meta = {"line": 42, "file": "guards.py", "tag": "k"}
    pdr = PermissionDecisionReason(
        type=PermissionDecisionReasonType.WORKING_DIR,
        detail="cwd scoped",
        metadata=meta,
    )
    assert pdr.metadata == meta


def test_reason_other_requires_detail() -> None:
    """OTHER variant MUST carry a detail per CC contract."""
    with pytest.raises(ValueError, match="OTHER requires non-empty detail"):
        PermissionDecisionReason(
            type=PermissionDecisionReasonType.OTHER,
            detail="",
        )


def test_reason_equality() -> None:
    a = PermissionDecisionReason(
        type=PermissionDecisionReasonType.MODE,
        detail="mode=default",
    )
    b = PermissionDecisionReason(
        type=PermissionDecisionReasonType.MODE,
        detail="mode=default",
    )
    assert a == b


# --------------------------------------------------------------------------- #
# PermissionResult — action literal enforcement
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "action", ["allow", "deny", "ask", "passthrough"]
)
def test_result_accepts_four_literal_actions(action: PermissionAction) -> None:
    pdr = PermissionDecisionReason(
        type=PermissionDecisionReasonType.MODE,
        detail="x",
    )
    result = PermissionResult(action=action, reason=pdr)
    assert result.action == action


def test_result_rejects_invalid_action() -> None:
    pdr = PermissionDecisionReason(
        type=PermissionDecisionReasonType.MODE,
        detail="x",
    )
    with pytest.raises(ValueError, match="must be one of"):
        PermissionResult(action="maybe", reason=pdr)  # type: ignore[arg-type]


def test_result_is_frozen() -> None:
    pdr = PermissionDecisionReason(
        type=PermissionDecisionReasonType.MODE,
        detail="x",
    )
    result = PermissionResult(action="allow", reason=pdr)
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.action = "deny"  # type: ignore[misc]


def test_result_equality() -> None:
    pdr1 = PermissionDecisionReason(
        type=PermissionDecisionReasonType.RULE,
        detail="x",
        rule_id="r1",
    )
    pdr2 = PermissionDecisionReason(
        type=PermissionDecisionReasonType.RULE,
        detail="x",
        rule_id="r1",
    )
    r1 = PermissionResult(action="deny", reason=pdr1)
    r2 = PermissionResult(action="deny", reason=pdr2)
    assert r1 == r2


# --------------------------------------------------------------------------- #
# make_decision ergonomic helper — one test per variant
# --------------------------------------------------------------------------- #


def test_make_decision_rule_variant() -> None:
    r = make_decision(
        "deny",
        PermissionDecisionReasonType.RULE,
        detail="blocked",
        rule_id="bash:rm",
    )
    assert r.action == "deny"
    assert r.reason.type == PermissionDecisionReasonType.RULE
    assert r.reason.rule_id == "bash:rm"


def test_make_decision_mode_variant() -> None:
    r = make_decision(
        "deny",
        PermissionDecisionReasonType.MODE,
        detail="mode=plan, write blocked",
    )
    assert r.reason.type == PermissionDecisionReasonType.MODE
    assert "plan" in r.reason.detail


def test_make_decision_subcommand_results_variant() -> None:
    r = make_decision(
        "ask",
        PermissionDecisionReasonType.SUBCOMMAND_RESULTS,
        detail="2 of 3 subcommands ask",
        metadata={"parts": 3},
    )
    assert r.reason.type == PermissionDecisionReasonType.SUBCOMMAND_RESULTS
    assert r.reason.metadata["parts"] == 3


def test_make_decision_permission_prompt_tool_variant() -> None:
    r = make_decision(
        "allow",
        PermissionDecisionReasonType.PERMISSION_PROMPT_TOOL,
        detail="user said allow",
        metadata={"tool": "prompt_user"},
    )
    assert r.reason.type == PermissionDecisionReasonType.PERMISSION_PROMPT_TOOL


def test_make_decision_hook_variant() -> None:
    r = make_decision(
        "allow",
        PermissionDecisionReasonType.HOOK,
        detail="PreToolUse hook allowed",
        metadata={"hook": "git_safety"},
    )
    assert r.reason.type == PermissionDecisionReasonType.HOOK


def test_make_decision_async_agent_variant() -> None:
    r = make_decision(
        "allow",
        PermissionDecisionReasonType.ASYNC_AGENT,
        detail="background classifier approved",
    )
    assert r.reason.type == PermissionDecisionReasonType.ASYNC_AGENT


def test_make_decision_sandbox_override_variant() -> None:
    r = make_decision(
        "allow",
        PermissionDecisionReasonType.SANDBOX_OVERRIDE,
        detail="excludedCommand",
    )
    assert r.reason.type == PermissionDecisionReasonType.SANDBOX_OVERRIDE


def test_make_decision_classifier_variant() -> None:
    r = make_decision(
        "deny",
        PermissionDecisionReasonType.CLASSIFIER,
        detail="high-risk bash",
        classifier_score=0.91,
    )
    assert r.reason.type == PermissionDecisionReasonType.CLASSIFIER
    assert r.reason.classifier_score == 0.91


def test_make_decision_working_dir_variant() -> None:
    r = make_decision(
        "deny",
        PermissionDecisionReasonType.WORKING_DIR,
        detail="outside cwd",
        rule_id="cwd:/tmp",
    )
    assert r.reason.type == PermissionDecisionReasonType.WORKING_DIR
    assert r.reason.rule_id == "cwd:/tmp"


def test_make_decision_safety_check_variant() -> None:
    r = make_decision(
        "deny",
        PermissionDecisionReasonType.SAFETY_CHECK,
        detail="destruction_guard",
    )
    assert r.reason.type == PermissionDecisionReasonType.SAFETY_CHECK


def test_make_decision_other_variant() -> None:
    r = make_decision(
        "ask",
        PermissionDecisionReasonType.OTHER,
        detail="unknown reason",
    )
    assert r.reason.type == PermissionDecisionReasonType.OTHER


def test_make_decision_with_suggested_message() -> None:
    r = make_decision(
        "ask",
        PermissionDecisionReasonType.OTHER,
        detail="d",
        suggested_message="Ask user: are you sure?",
    )
    assert r.suggested_message == "Ask user: are you sure?"


# --------------------------------------------------------------------------- #
# legacy_reason() snapshot — stable strings per variant
# --------------------------------------------------------------------------- #


def test_legacy_reason_mode_snapshot() -> None:
    r = make_decision(
        "deny",
        PermissionDecisionReasonType.MODE,
        detail="mode=plan, write blocked",
    )
    assert r.legacy_reason() == "[MODE] mode=plan, write blocked"


def test_legacy_reason_rule_snapshot() -> None:
    r = make_decision(
        "deny",
        PermissionDecisionReasonType.RULE,
        detail="blocked",
        rule_id="bash:rm-rf",
    )
    assert r.legacy_reason() == "[RULE rule_id=bash:rm-rf] blocked"


def test_legacy_reason_classifier_snapshot() -> None:
    r = make_decision(
        "deny",
        PermissionDecisionReasonType.CLASSIFIER,
        detail="high-risk",
        classifier_score=0.9,
    )
    assert r.legacy_reason() == "[CLASSIFIER score=0.9] high-risk"


def test_legacy_reason_hook_snapshot() -> None:
    r = make_decision(
        "allow",
        PermissionDecisionReasonType.HOOK,
        detail="hook_allow",
    )
    assert r.legacy_reason() == "[HOOK] hook_allow"


def test_legacy_reason_no_detail() -> None:
    r = make_decision("allow", PermissionDecisionReasonType.HOOK)
    assert r.legacy_reason() == "[HOOK]"


# --------------------------------------------------------------------------- #
# FSM bridge — check_result() produces typed reasons
# --------------------------------------------------------------------------- #


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


def test_fsm_default_mode_read_only_yields_mode_variant() -> None:
    fsm = PermissionModeFSM(initial_mode=PermissionMode.DEFAULT)
    result = fsm.check_result(
        _req("Read", PermissionMode.DEFAULT, path="/a.py")
    )
    assert result.action == "allow"
    assert result.reason.type == PermissionDecisionReasonType.MODE
    assert "mode=default" in result.reason.detail


def test_fsm_plan_mode_rejects_edit_yields_mode_variant() -> None:
    fsm = PermissionModeFSM(initial_mode=PermissionMode.PLAN)
    result = fsm.check_result(
        _req("Edit", PermissionMode.PLAN, path="/tmp/x.py")
    )
    assert result.action == "deny"
    assert result.reason.type == PermissionDecisionReasonType.MODE
    assert "mode=plan" in result.reason.detail
    assert "plan_mode_read_only" in result.reason.detail


def test_fsm_rule_match_yields_rule_variant() -> None:
    fsm = PermissionModeFSM(initial_mode=PermissionMode.DEFAULT)
    fsm.add_rule(
        PermissionRule(
            pattern="Bash:rm -rf *",
            decision="deny",
            reason="never_rm_rf",
        )
    )
    result = fsm.check_result(
        _req("Bash", PermissionMode.DEFAULT, command="rm -rf /etc")
    )
    assert result.action == "deny"
    assert result.reason.type == PermissionDecisionReasonType.RULE
    assert result.reason.rule_id == "Bash:rm -rf *"
    assert result.reason.metadata["rule_decision"] == "deny"


def test_fsm_bypass_mode_yields_mode_variant() -> None:
    fsm = PermissionModeFSM(initial_mode=PermissionMode.BYPASS_PERMISSIONS)
    result = fsm.check_result(
        _req("Bash", PermissionMode.BYPASS_PERMISSIONS, command="ls")
    )
    assert result.action == "allow"
    assert result.reason.type == PermissionDecisionReasonType.MODE
    assert "bypass_permissions" in result.reason.detail


def test_fsm_ambiguous_deny_yields_safety_check_variant() -> None:
    """No hook + ambiguous Write → safety_check fallback."""
    fsm = PermissionModeFSM(initial_mode=PermissionMode.DEFAULT)
    result = fsm.check_result(
        _req("Write", PermissionMode.DEFAULT, path="/tmp/x.txt")
    )
    assert result.action == "deny"
    assert result.reason.type == PermissionDecisionReasonType.SAFETY_CHECK
    assert "no_hook_ambiguous_deny" in result.reason.detail


# --------------------------------------------------------------------------- #
# Regression: existing FSM behavior unchanged
# --------------------------------------------------------------------------- #


def test_regression_existing_check_still_returns_verdict() -> None:
    """The new bridge must not break the legacy ``check()`` API."""
    fsm = PermissionModeFSM(initial_mode=PermissionMode.DEFAULT)
    verdict = fsm.check(
        _req("Read", PermissionMode.DEFAULT, path="/a.py")
    )
    # Returned object is still the legacy PermissionVerdict, not the
    # new PermissionResult — verified structurally.
    assert verdict.decision == "allow"
    assert verdict.reason == "read_only_tool_auto_allow"
    assert hasattr(verdict, "matched_rule")
