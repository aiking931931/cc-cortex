"""Tests for cc_cortex.cognitive_anchor — Red-team anchoring guard."""

from __future__ import annotations

import tempfile

import pytest

from cc_cortex.cognitive_anchor import (
    CognitiveAnchorGuard,
    classify_risk,
    get_anchor_prompt,
    get_base_identity,
)
from cc_cortex.guards.base import GuardAction, GuardContext


@pytest.fixture()
def cache_dir():
    """Provide a temporary cache directory."""
    with tempfile.TemporaryDirectory() as d:
        yield d


def _ctx(
    tool_name: str = "Edit",
    tool_input: dict | None = None,
    cache_dir: str = "",
) -> GuardContext:
    """Build a GuardContext for testing."""
    return GuardContext(
        tool_name=tool_name,
        tool_input=tool_input or {},
        session_id="test-session",
        cache_dir=cache_dir,
        hook_event="PreToolUse",
    )


# ── classify_risk tests ──────────────────────────────────────


class TestClassifyRisk:
    """Test risk classification logic."""

    def test_architecture_file_guards(self):
        result = classify_risk("Edit", {"file_path": "src/cc_cortex/guards/base.py"})
        assert result == "architecture"

    def test_architecture_file_core(self):
        result = classify_risk("Write", {"file_path": "src/cc_cortex/core/config.py"})
        assert result == "architecture"

    def test_architecture_file_init(self):
        assert classify_risk("Edit", {"file_path": "src/cc_cortex/__init__.py"}) == "architecture"

    def test_architecture_file_pipeline(self):
        assert classify_risk("Edit", {"file_path": "guards/pipeline.py"}) == "architecture"

    def test_architecture_file_registry(self):
        assert classify_risk("Edit", {"file_path": "guards/registry.py"}) == "architecture"

    def test_normal_file_no_risk(self):
        assert classify_risk("Edit", {"file_path": "src/cc_cortex/rag.py"}) is None

    def test_large_deletion(self):
        old = "line\n" * 60
        new = "line\n" * 5
        assert classify_risk("Edit", {
            "file_path": "foo.py",
            "old_string": old,
            "new_string": new,
        }) == "deletion"

    def test_small_deletion_no_risk(self):
        old = "line\n" * 10
        new = "line\n" * 5
        assert classify_risk("Edit", {
            "file_path": "foo.py",
            "old_string": old,
            "new_string": new,
        }) is None

    def test_new_module(self, tmp_path):
        nonexistent = str(tmp_path / "brand_new.py")
        assert classify_risk("Write", {"file_path": nonexistent}) == "new_module"

    def test_existing_file_not_new_module(self, tmp_path):
        existing = tmp_path / "existing.py"
        existing.write_text("x = 1")
        assert classify_risk("Write", {"file_path": str(existing)}) is None

    def test_deploy_bash(self):
        assert classify_risk("Bash", {"command": "git push origin main"}) == "deploy"

    def test_force_push(self):
        assert classify_risk("Bash", {"command": "git push --force"}) == "deploy"

    def test_deploy_script(self):
        assert classify_risk("Bash", {"command": "python deploy.py"}) == "deploy"

    def test_rm_bash(self):
        assert classify_risk("Bash", {"command": "rm -rf /tmp/old"}) == "deploy"

    def test_safe_bash(self):
        assert classify_risk("Bash", {"command": "ls -la"}) is None

    def test_read_tool_no_risk(self):
        assert classify_risk("Read", {"file_path": "guards/base.py"}) is None

    def test_grep_no_risk(self):
        assert classify_risk("Grep", {"pattern": "foo"}) is None


# ── get_anchor_prompt tests ──────────────────────────────────


class TestGetAnchorPrompt:
    """Test gas-state language prompt generation."""

    def test_architecture_contains_identity_narrative(self):
        prompt = get_anchor_prompt("architecture", path="guards/base.py")
        assert "foundation" in prompt.lower() or "stones" in prompt.lower()
        assert "guards/base.py" in prompt
        assert "breaks" in prompt.lower()

    def test_deletion_contains_identity_narrative(self):
        prompt = get_anchor_prompt("deletion", path="foo.py", lines="55")
        assert "trace" in prompt.lower() or "connected" in prompt.lower()
        assert "55" in prompt
        assert "?" in prompt

    def test_new_module_contains_identity_narrative(self):
        prompt = get_anchor_prompt("new_module", path="new_guard.py")
        assert "new_guard.py" in prompt
        assert "room" in prompt.lower() or "serve" in prompt.lower()

    def test_deploy_contains_identity_narrative(self):
        prompt = get_anchor_prompt("deploy")
        assert "workshop" in prompt.lower() or "world" in prompt.lower()
        assert "breaks" in prompt.lower()

    def test_unknown_risk_type_empty(self):
        assert get_anchor_prompt("unknown") == ""

    def test_architecture_has_reflective_questions(self):
        prompt = get_anchor_prompt("architecture", path="x.py")
        assert "?" in prompt  # gas-state uses questions, not commands


class TestGetBaseIdentity:
    """Test base identity for session start injection."""

    def test_returns_default(self):
        identity = get_base_identity()
        assert "I built this" in identity
        assert "mine" in identity.lower()
        assert "trusting" in identity.lower()

    def test_config_override(self):
        custom = "I am the shield. I guard this perimeter."
        assert get_base_identity(custom) == custom

    def test_empty_override_returns_default(self):
        assert "I built this" in get_base_identity("")

    def test_first_person(self):
        identity = get_base_identity()
        assert identity.startswith("I built")
        # Should contain first-person throughout
        assert "I know" in identity
        assert "I check" in identity


# ── CognitiveAnchorGuard tests ───────────────────────────────


class TestCognitiveAnchorGuard:
    """Test the guard integration."""

    def test_no_cache_dir_returns_none(self):
        guard = CognitiveAnchorGuard()
        ctx = _ctx(cache_dir="")
        assert guard.check(ctx) is None

    def test_read_tool_returns_none(self, cache_dir):
        guard = CognitiveAnchorGuard()
        ctx = _ctx(
            tool_name="Read", tool_input={"file_path": "guards/base.py"}, cache_dir=cache_dir
        )
        assert guard.check(ctx) is None

    def test_architecture_edit_triggers(self, cache_dir):
        guard = CognitiveAnchorGuard()
        ctx = _ctx(
            tool_name="Edit",
            tool_input={
                "file_path": "src/cc_cortex/guards/base.py",
                "old_string": "x",
                "new_string": "y",
            },
            cache_dir=cache_dir,
        )
        result = guard.check(ctx)
        assert result is not None
        assert result.action == GuardAction.ALLOW
        assert "foundation" in result.context.lower() or "stones" in result.context.lower()
        assert "?" in result.context

    def test_session_dedup_same_file(self, cache_dir):
        guard = CognitiveAnchorGuard()
        ctx = _ctx(
            tool_name="Edit",
            tool_input={"file_path": "guards/base.py", "old_string": "x", "new_string": "y"},
            cache_dir=cache_dir,
        )
        # First call triggers
        r1 = guard.check(ctx)
        assert r1 is not None
        # Second call to same file — deduped
        r2 = guard.check(ctx)
        assert r2 is None

    def test_different_files_both_trigger(self, cache_dir):
        guard = CognitiveAnchorGuard()
        ctx1 = _ctx(
            tool_name="Edit",
            tool_input={"file_path": "guards/base.py", "old_string": "x", "new_string": "y"},
            cache_dir=cache_dir,
        )
        ctx2 = _ctx(
            tool_name="Edit",
            tool_input={"file_path": "core/config.py", "old_string": "a", "new_string": "b"},
            cache_dir=cache_dir,
        )
        assert guard.check(ctx1) is not None
        assert guard.check(ctx2) is not None

    def test_normal_file_no_trigger(self, cache_dir):
        guard = CognitiveAnchorGuard()
        ctx = _ctx(
            tool_name="Edit",
            tool_input={"file_path": "src/cc_cortex/rag.py", "old_string": "x", "new_string": "y"},
            cache_dir=cache_dir,
        )
        assert guard.check(ctx) is None

    def test_large_deletion_triggers(self, cache_dir):
        guard = CognitiveAnchorGuard()
        old = "line\n" * 60
        new = "line\n" * 5
        ctx = _ctx(
            tool_name="Edit",
            tool_input={"file_path": "foo.py", "old_string": old, "new_string": new},
            cache_dir=cache_dir,
        )
        result = guard.check(ctx)
        assert result is not None
        assert "trace" in result.context.lower() or "connected" in result.context.lower()

    def test_deploy_bash_triggers(self, cache_dir):
        guard = CognitiveAnchorGuard()
        ctx = _ctx(
            tool_name="Bash",
            tool_input={"command": "git push origin main"},
            cache_dir=cache_dir,
        )
        result = guard.check(ctx)
        assert result is not None
        assert "workshop" in result.context.lower() or "world" in result.context.lower()

    def test_on_post_tool_is_noop(self, cache_dir):
        guard = CognitiveAnchorGuard()
        ctx = _ctx(cache_dir=cache_dir)
        assert guard.on_post_tool(ctx) is None

    def test_never_denies(self, cache_dir):
        """Guard MUST never deny — only allow with context."""
        guard = CognitiveAnchorGuard()
        ctx = _ctx(
            tool_name="Edit",
            tool_input={"file_path": "guards/pipeline.py", "old_string": "x", "new_string": "y"},
            cache_dir=cache_dir,
        )
        result = guard.check(ctx)
        assert result is not None
        assert result.action == GuardAction.ALLOW
