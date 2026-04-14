"""Tests for premise_gate — Verify external premises before execution."""

from cc_cortex.core.state_store import StateStore
from cc_cortex.guards.base import GuardAction, GuardContext
from cc_cortex.premise_gate import (
    PremiseGate,
    _has_external_constraints,
)


def _ctx(
    tmp_path,
    tool_name="Edit",
    tool_input=None,
    *,
    hook_event="PreToolUse",
    tool_result="",
    session_id="test-session",
):
    return GuardContext(
        tool_name=tool_name,
        tool_input=tool_input or {"file_path": "test.md", "new_string": "content"},
        session_id=session_id,
        cache_dir=str(tmp_path),
        hook_event=hook_event,
        tool_result=tool_result,
        workspace="",
    )


# ── Regex patterns ─────────────────────────────────────────────────────────────


class TestHasExternalConstraints:
    """Test co-occurrence constraint detection (domain + context words)."""

    def test_domain_word_alone_matches(self):
        assert _has_external_constraints("this is a competition")

    def test_chinese_domain_word(self):
        assert _has_external_constraints("這是一個比賽")

    def test_hackathon(self):
        assert _has_external_constraints("join the hackathon today")

    def test_two_context_words_match(self):
        # "requirements" + "deadline" = two context words → co-occurrence match
        assert _has_external_constraints("the requirements have a deadline")

    def test_single_context_word_no_match(self):
        # "rules" alone is too common — should NOT match without domain word
        assert not _has_external_constraints("follow the CSS rules")

    def test_chinese_two_context_words(self):
        assert _has_external_constraints("根據規則第三條，截止日期是明天")

    def test_no_match_plain_text(self):
        assert not _has_external_constraints("write some code today")


# ── PremiseGate.check() ────────────────────────────────────────────────────────


class TestPremiseGateCheck:
    def test_no_cache_dir_returns_none(self):
        guard = PremiseGate()
        ctx = GuardContext(
            tool_name="Edit",
            tool_input={"file_path": "test.md", "new_string": "x"},
            session_id="s",
            cache_dir="",
            hook_event="PreToolUse",
        )
        assert guard.check(ctx) is None

    def test_premise_verified_returns_none(self, tmp_path):
        guard = PremiseGate()
        store = StateStore(str(tmp_path))
        store.write("premise_gate", "test-session", {
            "premise_verified": True,
            "has_external_constraints": True,
        })
        ctx = _ctx(tmp_path)
        assert guard.check(ctx) is None

    def test_no_constraints_returns_none(self, tmp_path):
        guard = PremiseGate()
        # State is empty — no constraints detected
        ctx = _ctx(tmp_path)
        assert guard.check(ctx) is None

    def test_simple_complexity_returns_none(self, tmp_path):
        guard = PremiseGate()
        store = StateStore(str(tmp_path))
        store.write("premise_gate", "test-session", {
            "has_external_constraints": True,
        })
        store.write("c0_route", "test-session", {"complexity": "simple"})
        ctx = _ctx(tmp_path)
        assert guard.check(ctx) is None

    def test_constraints_unverified_complicated_returns_deny(self, tmp_path):
        guard = PremiseGate()
        store = StateStore(str(tmp_path))
        store.write("premise_gate", "test-session", {
            "has_external_constraints": True,
            "constraint_sample": "competition",
        })
        store.write("c0_route", "test-session", {"complexity": "complicated"})
        ctx = _ctx(tmp_path)
        result = guard.check(ctx)
        assert result is not None
        assert result.action == GuardAction.DENY

    def test_deny_result_mentions_constraint(self, tmp_path):
        guard = PremiseGate()
        store = StateStore(str(tmp_path))
        store.write("premise_gate", "test-session", {
            "has_external_constraints": True,
            "constraint_sample": "hackathon",
        })
        ctx = _ctx(tmp_path)
        result = guard.check(ctx)
        assert result is not None
        assert "hackathon" in result.context

    def test_non_write_tool_returns_none(self, tmp_path):
        guard = PremiseGate()
        store = StateStore(str(tmp_path))
        store.write("premise_gate", "test-session", {
            "has_external_constraints": True,
        })
        ctx = _ctx(tmp_path, tool_name="Read", tool_input={"file_path": "rules.md"})
        assert guard.check(ctx) is None


# ── PremiseGate.on_post_tool() ─────────────────────────────────────────────────


class TestPremiseGateOnPostTool:
    def test_detects_constraints_in_tool_result(self, tmp_path):
        guard = PremiseGate()
        ctx = _ctx(
            tmp_path,
            tool_name="Bash",
            tool_input={"command": "echo hi"},
            hook_event="PostToolUse",
            tool_result="This hackathon requires special submission rules",
        )
        guard.on_post_tool(ctx)
        store = StateStore(str(tmp_path))
        state = store.read("premise_gate", "test-session", default={})
        assert state.get("has_external_constraints") is True

    def test_marks_verified_after_read_with_constraint_content(self, tmp_path):
        guard = PremiseGate()
        store = StateStore(str(tmp_path))
        store.write("premise_gate", "test-session", {
            "has_external_constraints": True,
        })
        # Content must contain BOTH verification evidence AND constraint keywords
        content = "According to the competition rules, submissions are due by Friday."
        ctx = _ctx(
            tmp_path,
            tool_name="Read",
            tool_input={"file_path": "rules.md"},
            hook_event="PostToolUse",
            tool_result=content,
        )
        guard.on_post_tool(ctx)
        state = store.read("premise_gate", "test-session", default={})
        assert state.get("premise_verified") is True

    def test_no_verify_with_unrelated_content(self, tmp_path):
        """Reading a long file without constraint keywords should NOT verify."""
        guard = PremiseGate()
        store = StateStore(str(tmp_path))
        store.write("premise_gate", "test-session", {
            "has_external_constraints": True,
        })
        ctx = _ctx(
            tmp_path,
            tool_name="Read",
            tool_input={"file_path": "readme.md"},
            hook_event="PostToolUse",
            tool_result="A" * 250,  # long but no constraint keywords
        )
        guard.on_post_tool(ctx)
        state = store.read("premise_gate", "test-session", default={})
        assert state.get("premise_verified") is not True

    def test_does_not_verify_with_short_content(self, tmp_path):
        guard = PremiseGate()
        store = StateStore(str(tmp_path))
        store.write("premise_gate", "test-session", {
            "has_external_constraints": True,
        })
        ctx = _ctx(
            tmp_path,
            tool_name="Read",
            tool_input={"file_path": "rules.md"},
            hook_event="PostToolUse",
            tool_result="short",
        )
        guard.on_post_tool(ctx)
        state = store.read("premise_gate", "test-session", default={})
        assert not state.get("premise_verified")

    def test_no_cache_dir_returns_none(self):
        guard = PremiseGate()
        ctx = GuardContext(
            tool_name="Read",
            tool_input={"file_path": "x.md"},
            session_id="s",
            cache_dir="",
            hook_event="PostToolUse",
            tool_result="some result",
        )
        assert guard.on_post_tool(ctx) is None


# ── Ceiling detection (Mode 2) ─────────────────────────────────────────────────


class TestCeilingDetection:
    """Mode 2: block write-tool when assistant references a platform limit
    without having WebFetched official CC docs first. Hardens
    feedback_ceiling_misalignment.md — CCC 1.3.0 误 KILL H1 because of
    limitations that had already been removed several versions earlier.
    """

    def _mark_complicated(self, tmp_path):
        StateStore(str(tmp_path)).write(
            "c0_route", "test-session", {"complexity": "complicated"},
        )

    # ── Phase 1: ceiling claim detection ──────────────────────

    def test_l3_reference_sets_has_ceiling_claim(self, tmp_path):
        guard = PremiseGate()
        ctx = _ctx(
            tmp_path,
            tool_name="Write",
            tool_input={
                "file_path": "note.md",
                "content": "L3 limitation means hook cannot call LLM",
            },
            hook_event="PostToolUse",
        )
        guard.on_post_tool(ctx)
        state = StateStore(str(tmp_path)).read(
            "premise_gate", "test-session", default={},
        )
        assert state.get("has_ceiling_claim") is True
        assert "L3" in state.get("ceiling_sample", "")

    def test_updated_input_reference_sets_claim(self, tmp_path):
        guard = PremiseGate()
        ctx = _ctx(
            tmp_path,
            tool_name="Bash",
            tool_input={"command": "echo 'updatedInput is not supported'"},
            hook_event="PostToolUse",
        )
        guard.on_post_tool(ctx)
        state = StateStore(str(tmp_path)).read(
            "premise_gate", "test-session", default={},
        )
        assert state.get("has_ceiling_claim") is True

    def test_cc_unsupported_chinese(self, tmp_path):
        guard = PremiseGate()
        ctx = _ctx(
            tmp_path,
            tool_name="Edit",
            tool_input={"file_path": "h.md", "new_string": "CC 不支援這個功能"},
            hook_event="PostToolUse",
        )
        guard.on_post_tool(ctx)
        state = StateStore(str(tmp_path)).read(
            "premise_gate", "test-session", default={},
        )
        assert state.get("has_ceiling_claim") is True

    def test_harmless_text_does_not_trigger(self, tmp_path):
        guard = PremiseGate()
        ctx = _ctx(
            tmp_path,
            tool_name="Write",
            tool_input={"file_path": "a.md", "content": "write some normal code"},
            hook_event="PostToolUse",
        )
        guard.on_post_tool(ctx)
        state = StateStore(str(tmp_path)).read(
            "premise_gate", "test-session", default={},
        )
        assert not state.get("has_ceiling_claim")

    # ── Phase 2: official-docs verification ───────────────────

    def test_webfetch_official_docs_verifies(self, tmp_path):
        guard = PremiseGate()
        store = StateStore(str(tmp_path))
        store.write("premise_gate", "test-session", {
            "has_ceiling_claim": True,
            "ceiling_sample": "L3",
        })
        ctx = _ctx(
            tmp_path,
            tool_name="WebFetch",
            tool_input={"url": "https://code.claude.com/docs/en/hooks", "prompt": "x"},
            hook_event="PostToolUse",
            tool_result="Hook documentation content...",
        )
        guard.on_post_tool(ctx)
        state = store.read("premise_gate", "test-session", default={})
        assert state.get("ceiling_verified") is True
        assert state.get("ceiling_verified_via") == "WebFetch"

    def test_webfetch_other_site_does_not_verify(self, tmp_path):
        guard = PremiseGate()
        store = StateStore(str(tmp_path))
        store.write("premise_gate", "test-session", {
            "has_ceiling_claim": True,
            "ceiling_sample": "L3",
        })
        ctx = _ctx(
            tmp_path,
            tool_name="WebFetch",
            tool_input={"url": "https://example.com/blog", "prompt": "x"},
            hook_event="PostToolUse",
            tool_result="A random blog post...",
        )
        guard.on_post_tool(ctx)
        state = store.read("premise_gate", "test-session", default={})
        assert not state.get("ceiling_verified")

    def test_websearch_with_official_host_in_result_verifies(self, tmp_path):
        guard = PremiseGate()
        store = StateStore(str(tmp_path))
        store.write("premise_gate", "test-session", {
            "has_ceiling_claim": True,
        })
        ctx = _ctx(
            tmp_path,
            tool_name="WebSearch",
            tool_input={"query": "claude code hooks"},
            hook_event="PostToolUse",
            tool_result="See https://code.claude.com/docs/en/hooks for details",
        )
        guard.on_post_tool(ctx)
        state = store.read("premise_gate", "test-session", default={})
        assert state.get("ceiling_verified") is True

    def test_read_does_not_verify_ceiling(self, tmp_path):
        """Read is not a platform-docs verification — must be WebFetch/Search."""
        guard = PremiseGate()
        store = StateStore(str(tmp_path))
        store.write("premise_gate", "test-session", {
            "has_ceiling_claim": True,
        })
        ctx = _ctx(
            tmp_path,
            tool_name="Read",
            tool_input={"file_path": "notes.md"},
            hook_event="PostToolUse",
            tool_result="the L3 limit is... local note",
        )
        guard.on_post_tool(ctx)
        state = store.read("premise_gate", "test-session", default={})
        assert not state.get("ceiling_verified")

    # ── check(): deny + bypass logic ──────────────────────────

    def test_check_denies_when_ceiling_unverified_and_complicated(self, tmp_path):
        guard = PremiseGate()
        store = StateStore(str(tmp_path))
        store.write("premise_gate", "test-session", {
            "has_ceiling_claim": True,
            "ceiling_sample": "updatedInput",
        })
        self._mark_complicated(tmp_path)
        ctx = _ctx(tmp_path, tool_name="Write",
                   tool_input={"file_path": "x.py", "content": "..."})
        result = guard.check(ctx)
        assert result is not None
        assert result.action == GuardAction.DENY
        assert "天花板" in result.context
        assert "updatedInput" in result.context

    def test_check_bypassed_when_verified(self, tmp_path):
        guard = PremiseGate()
        store = StateStore(str(tmp_path))
        store.write("premise_gate", "test-session", {
            "has_ceiling_claim": True,
            "ceiling_verified": True,
        })
        self._mark_complicated(tmp_path)
        ctx = _ctx(tmp_path, tool_name="Write",
                   tool_input={"file_path": "x.py", "content": "..."})
        assert guard.check(ctx) is None

    def test_check_bypassed_when_simple_complexity(self, tmp_path):
        guard = PremiseGate()
        store = StateStore(str(tmp_path))
        store.write("premise_gate", "test-session", {
            "has_ceiling_claim": True,
        })
        store.write("c0_route", "test-session", {"complexity": "simple"})
        ctx = _ctx(tmp_path, tool_name="Write",
                   tool_input={"file_path": "x.py", "content": "..."})
        assert guard.check(ctx) is None
        # Should mark as skipped so we don't re-trigger next tool call
        state = store.read("premise_gate", "test-session", default={})
        assert state.get("ceiling_verified") is True
        assert state.get("ceiling_skip_reason") == "simple_task"

    def test_check_non_write_tool_returns_none(self, tmp_path):
        guard = PremiseGate()
        store = StateStore(str(tmp_path))
        store.write("premise_gate", "test-session", {
            "has_ceiling_claim": True,
        })
        self._mark_complicated(tmp_path)
        ctx = _ctx(tmp_path, tool_name="Read",
                   tool_input={"file_path": "x.md"})
        assert guard.check(ctx) is None

    def test_both_modes_coexist(self, tmp_path):
        """When both external constraints AND ceiling claim are pending,
        the earlier-declared mode (external) wins the deny; the other
        stays pending until that one is resolved."""
        guard = PremiseGate()
        store = StateStore(str(tmp_path))
        store.write("premise_gate", "test-session", {
            "has_external_constraints": True,
            "constraint_sample": "hackathon",
            "has_ceiling_claim": True,
            "ceiling_sample": "L3",
        })
        self._mark_complicated(tmp_path)
        ctx = _ctx(tmp_path, tool_name="Write",
                   tool_input={"file_path": "x.py", "content": "..."})
        result = guard.check(ctx)
        assert result is not None
        assert result.action == GuardAction.DENY
        # External constraints branch wins first
        assert "hackathon" in result.context
