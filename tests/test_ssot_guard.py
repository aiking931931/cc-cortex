"""Tests for cc_cortex.ssot_guard — SSOT enforcement guard."""

from __future__ import annotations

import json
import os

from cc_cortex.guards.base import GuardAction, GuardCategory, GuardContext
from cc_cortex.ssot_guard import (
    SSOTGuard,
    SSOTRule,
    SSOTViolation,
    _file_matches_scope,
    _load_rules,
    _rules_cache,
    check_ssot,
    format_deny,
)


def _make_ctx(
    tool_name: str = "Edit",
    tool_input: dict | None = None,
    workspace: str = "",
    hook_event: str = "PostToolUse",
) -> GuardContext:
    return GuardContext(
        tool_name=tool_name,
        tool_input=tool_input or {},
        session_id="test-sess",
        cache_dir="",
        hook_event=hook_event,
        workspace=workspace,
    )


def _write_rules(ws_path, rules_list: list[dict]) -> None:
    """Write .ssot-rules.json to workspace."""
    path = os.path.join(str(ws_path), ".ssot-rules.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"rules": rules_list}, f)


# ── SSOTRule dataclass ──────────────────────────────────────


def test_ssot_rule_defaults():
    r = SSOTRule(id="x", pattern="foo", message="bar")
    assert r.scope == "*.tsx"
    assert r.exclude == ()
    assert r.severity == "deny"


# ── _load_rules ─────────────────────────────────────────────


def test_load_rules_no_file(tmp_path):
    _rules_cache.clear()
    rules = _load_rules(str(tmp_path))
    assert rules == []


def test_load_rules_empty_workspace():
    _rules_cache.clear()
    assert _load_rules("") == []


def test_load_rules_valid(tmp_path):
    _rules_cache.clear()
    _write_rules(tmp_path, [
        {"id": "r1", "pattern": "color:", "message": "Use theme", "scope": "*.tsx"},
    ])
    rules = _load_rules(str(tmp_path))
    assert len(rules) == 1
    assert rules[0].id == "r1"
    assert rules[0].pattern == "color:"


def test_load_rules_skips_invalid_entries(tmp_path):
    _rules_cache.clear()
    _write_rules(tmp_path, [
        {"id": "ok", "pattern": "x", "message": "m"},
        {"id": "", "pattern": "x", "message": "m"},      # empty id
        {"id": "no-pat", "pattern": "", "message": "m"},  # empty pattern
        "not a dict",
    ])
    rules = _load_rules(str(tmp_path))
    assert len(rules) == 1
    assert rules[0].id == "ok"


def test_load_rules_bad_json(tmp_path):
    _rules_cache.clear()
    (tmp_path / ".ssot-rules.json").write_text("NOT JSON", encoding="utf-8")
    assert _load_rules(str(tmp_path)) == []


def test_load_rules_caches_by_mtime(tmp_path):
    _rules_cache.clear()
    _write_rules(tmp_path, [{"id": "r", "pattern": "p", "message": "m"}])
    r1 = _load_rules(str(tmp_path))
    r2 = _load_rules(str(tmp_path))
    assert r1 is r2  # same object = cache hit


# ── _file_matches_scope ─────────────────────────────────────


def test_scope_match_basename():
    assert _file_matches_scope("src/app.tsx", "*.tsx", ()) is True


def test_scope_no_match():
    assert _file_matches_scope("app.py", "*.tsx", ()) is False


def test_scope_exclude_basename():
    assert _file_matches_scope("avatars.ts", "*.ts", ("avatars.ts",)) is False


def test_scope_exclude_glob():
    assert _file_matches_scope("src/avatars.ts", "*.ts", ("**/avatars.ts",)) is False


# ── check_ssot ──────────────────────────────────────────────


def test_check_ssot_finds_violation():
    rule = SSOTRule(id="no-hex", pattern=r"#[0-9a-fA-F]{6}", message="Use theme var", scope="*.tsx")
    violations = check_ssot("comp.tsx", "color: #FF0000;", [rule])
    assert len(violations) == 1
    assert violations[0].rule_id == "no-hex"
    assert violations[0].line == 1


def test_check_ssot_no_violation():
    rule = SSOTRule(id="no-hex", pattern=r"#[0-9a-fA-F]{6}", message="Use theme", scope="*.tsx")
    violations = check_ssot("comp.tsx", "color: var(--primary);", [rule])
    assert violations == []


def test_check_ssot_respects_scope():
    rule = SSOTRule(id="r", pattern="bad", message="m", scope="*.tsx")
    assert check_ssot("app.py", "bad", [rule]) == []


def test_check_ssot_invalid_regex():
    rule = SSOTRule(id="r", pattern="[invalid", message="m", scope="*.tsx")
    # Should not raise — logs warning and skips
    violations = check_ssot("comp.tsx", "anything", [rule])
    assert violations == []


def test_check_ssot_multiline_line_numbers():
    rule = SSOTRule(id="r", pattern="BAD", message="m", scope="*.tsx")
    source = "line1\nline2\nBAD here\nline4"
    violations = check_ssot("x.tsx", source, [rule])
    assert len(violations) == 1
    assert violations[0].line == 3


# ── format_deny ─────────────────────────────────────────────


def test_format_deny_basic():
    vs = [SSOTViolation(rule_id="r1", message="Use theme", line=10, severity="deny")]
    text = format_deny("src/comp.tsx", vs)
    assert "comp.tsx" in text
    assert "r1" in text
    assert "L10" in text


def test_format_deny_truncates_at_5():
    vs = [SSOTViolation(rule_id=f"r{i}", message="m", line=i, severity="deny") for i in range(8)]
    text = format_deny("x.tsx", vs)
    assert "and 3 more" in text


# ── SSOTGuard.check() — PreToolUse no-op ────────────────────


def test_guard_check_returns_none():
    g = SSOTGuard()
    ctx = _make_ctx(tool_name="Edit", hook_event="PreToolUse")
    assert g.check(ctx) is None


# ── SSOTGuard metadata ──────────────────────────────────────


def test_guard_name_and_category():
    g = SSOTGuard()
    assert g.name == "ssot_guard"
    assert g.category == GuardCategory.QUALITY
    assert g.step_back_reason != ""


# ── SSOTGuard.on_post_tool() ────────────────────────────────


def test_on_post_tool_ignores_non_write_tools():
    g = SSOTGuard()
    for tool in ("Read", "Bash", "Grep", "Glob"):
        ctx = _make_ctx(tool_name=tool)
        assert g.on_post_tool(ctx) is None


def test_on_post_tool_no_rules_file(tmp_path):
    _rules_cache.clear()
    # Create a .ts file that would normally violate
    target = tmp_path / "comp.tsx"
    target.write_text("color: #FF0000;", encoding="utf-8")

    g = SSOTGuard()
    ctx = _make_ctx(
        tool_name="Edit",
        tool_input={"file_path": str(target)},
        workspace=str(tmp_path),
    )
    assert g.on_post_tool(ctx) is None


def test_on_post_tool_deny_on_violation(tmp_path):
    _rules_cache.clear()
    _write_rules(tmp_path, [
        {"id": "no-hex", "pattern": r"#[0-9a-fA-F]{6}", "message": "Use theme", "scope": "*.tsx"},
    ])
    target = tmp_path / "comp.tsx"
    target.write_text("color: #FF0000;", encoding="utf-8")

    g = SSOTGuard()
    ctx = _make_ctx(
        tool_name="Write",
        tool_input={"file_path": str(target)},
        workspace=str(tmp_path),
    )
    result = g.on_post_tool(ctx)
    assert result is not None
    assert result.action == GuardAction.DENY
    assert "no-hex" in result.reason


def test_on_post_tool_clean_file(tmp_path):
    _rules_cache.clear()
    _write_rules(tmp_path, [
        {"id": "no-hex", "pattern": r"#[0-9a-fA-F]{6}", "message": "Use theme", "scope": "*.tsx"},
    ])
    target = tmp_path / "comp.tsx"
    target.write_text("color: var(--primary);", encoding="utf-8")

    g = SSOTGuard()
    ctx = _make_ctx(
        tool_name="Edit",
        tool_input={"file_path": str(target)},
        workspace=str(tmp_path),
    )
    assert g.on_post_tool(ctx) is None


def test_on_post_tool_skips_non_code_extensions(tmp_path):
    _rules_cache.clear()
    _write_rules(tmp_path, [
        {"id": "r", "pattern": "bad", "message": "m", "scope": "*"},
    ])
    target = tmp_path / "readme.md"
    target.write_text("bad content", encoding="utf-8")

    g = SSOTGuard()
    ctx = _make_ctx(
        tool_name="Write",
        tool_input={"file_path": str(target)},
        workspace=str(tmp_path),
    )
    assert g.on_post_tool(ctx) is None


def test_on_post_tool_context_severity(tmp_path):
    """severity=context should return allow with context, not deny."""
    _rules_cache.clear()
    _write_rules(tmp_path, [
        {
            "id": "info", "pattern": "TODO", "message": "Resolve TODOs",
            "scope": "*.py", "severity": "context",
        },
    ])
    target = tmp_path / "app.py"
    target.write_text("# TODO: fix this", encoding="utf-8")

    g = SSOTGuard()
    ctx = _make_ctx(
        tool_name="Edit",
        tool_input={"file_path": str(target)},
        workspace=str(tmp_path),
    )
    result = g.on_post_tool(ctx)
    assert result is not None
    assert result.action == GuardAction.ALLOW
    assert "info" in result.context
