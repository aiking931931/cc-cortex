"""Tests for concinno.structured_handoff — fixed-field handoff template."""

from __future__ import annotations

from concinno.guards.base import GuardContext
from concinno.structured_handoff import (
    CONTEXT_SUMMARY_MAX,
    NEXT_STEP_MAX,
    HandoffRecord,
    HandoffTemplate,
    StructuredHandoffGuard,
)


def _record(**kw) -> HandoffRecord:
    defaults = {
        "session_id": "sess-1234",
        "task_name": "Fix login bug",
        "status": "partial",
        "completed": ["Identified root cause"],
        "pending": ["Write fix", "Add tests"],
        "blockers": [],
        "files_touched": ["src/auth.py"],
        "context_summary": "Login fails on expired tokens",
        "next_step": "Apply the fix in auth.py line 42",
    }
    defaults.update(kw)
    return HandoffRecord(**defaults)


def _ctx(**kw) -> GuardContext:
    defaults = {
        "tool_name": "Write",
        "tool_input": {},
        "session_id": "test",
        "hook_event": "PostToolUse",
        "cache_dir": "",
    }
    defaults.update(kw)
    return GuardContext(**defaults)


# ═══════════════════════════════════════════════════════════
# HandoffRecord
# ═══════════════════════════════════════════════════════════


class TestHandoffRecord:
    def test_defaults(self):
        r = _record()
        assert r.session_id == "sess-1234"
        assert r.status == "partial"
        assert r.token_used == 0
        assert r.created_at  # auto-filled

    def test_created_at_auto(self):
        r = _record()
        assert r.created_at  # non-empty
        assert "T" in r.created_at  # ISO format


# ═══════════════════════════════════════════════════════════
# HandoffTemplate.build
# ═══════════════════════════════════════════════════════════


class TestBuild:
    def test_contains_frontmatter(self):
        md = HandoffTemplate.build(_record())
        assert md.startswith("---\n")
        assert "session_id: sess-1234" in md
        assert "status: partial" in md

    def test_contains_sections(self):
        md = HandoffTemplate.build(_record())
        assert "## Completed" in md
        assert "## Pending" in md
        assert "## Blockers" in md
        assert "## Files" in md

    def test_completed_items(self):
        md = HandoffTemplate.build(_record())
        assert "- ✅ Identified root cause" in md

    def test_pending_items(self):
        md = HandoffTemplate.build(_record())
        assert "- ⬜ Write fix" in md
        assert "- ⬜ Add tests" in md

    def test_empty_blockers(self):
        md = HandoffTemplate.build(_record(blockers=[]))
        assert "- (none)" in md

    def test_files_backtick(self):
        md = HandoffTemplate.build(_record())
        assert "- `src/auth.py`" in md

    def test_context_summary(self):
        md = HandoffTemplate.build(_record())
        assert "**Context**: Login fails on expired tokens" in md

    def test_next_step(self):
        md = HandoffTemplate.build(_record())
        assert "**Next step**: Apply the fix" in md

    def test_title(self):
        md = HandoffTemplate.build(_record())
        assert "# Handoff: Fix login bug" in md


# ═══════════════════════════════════════════════════════════
# HandoffTemplate.parse
# ═══════════════════════════════════════════════════════════


class TestParse:
    def test_roundtrip(self):
        original = _record()
        md = HandoffTemplate.build(original)
        parsed = HandoffTemplate.parse(md)
        assert parsed is not None
        assert parsed.session_id == original.session_id
        assert parsed.task_name == original.task_name
        assert parsed.status == original.status
        assert parsed.completed == original.completed
        assert parsed.pending == original.pending
        assert parsed.blockers == original.blockers
        assert parsed.files_touched == original.files_touched
        assert parsed.context_summary == original.context_summary
        assert parsed.next_step == original.next_step
        assert parsed.token_used == original.token_used

    def test_no_frontmatter(self):
        assert HandoffTemplate.parse("no frontmatter here") is None

    def test_invalid_status_defaults(self):
        md = HandoffTemplate.build(_record())
        md = md.replace("status: partial", "status: bogus")
        parsed = HandoffTemplate.parse(md)
        assert parsed is not None
        assert parsed.status == "partial"  # default fallback

    def test_empty_lists(self):
        r = _record(
            completed=[],
            pending=["Do something"],
            blockers=[],
            files_touched=[],
        )
        md = HandoffTemplate.build(r)
        parsed = HandoffTemplate.parse(md)
        assert parsed is not None
        assert parsed.completed == []
        assert parsed.pending == ["Do something"]
        assert parsed.files_touched == []

    def test_multiple_files(self):
        r = _record(files_touched=["a.py", "b.ts", "c.md"])
        md = HandoffTemplate.build(r)
        parsed = HandoffTemplate.parse(md)
        assert parsed is not None
        assert parsed.files_touched == ["a.py", "b.ts", "c.md"]

    def test_blockers_parsed(self):
        r = _record(blockers=["API key missing", "Server down"])
        md = HandoffTemplate.build(r)
        parsed = HandoffTemplate.parse(md)
        assert parsed is not None
        assert parsed.blockers == ["API key missing", "Server down"]


# ═══════════════════════════════════════════════════════════
# HandoffTemplate.validate
# ═══════════════════════════════════════════════════════════


class TestValidate:
    def test_valid_record(self):
        errors = HandoffTemplate.validate(_record())
        assert errors == []

    def test_missing_session_id(self):
        errors = HandoffTemplate.validate(_record(session_id=""))
        assert any("session_id" in e for e in errors)

    def test_missing_task_name(self):
        errors = HandoffTemplate.validate(_record(task_name=""))
        assert any("task_name" in e for e in errors)

    def test_empty_completed_and_pending(self):
        errors = HandoffTemplate.validate(
            _record(completed=[], pending=[]),
        )
        assert any("completed/pending" in e for e in errors)

    def test_blocked_without_blockers(self):
        errors = HandoffTemplate.validate(
            _record(status="blocked", blockers=[]),
        )
        assert any("blockers" in e for e in errors)

    def test_completed_with_pending(self):
        errors = HandoffTemplate.validate(
            _record(status="completed", pending=["leftover"]),
        )
        assert any("completed" in e and "pending" in e for e in errors)

    def test_context_summary_too_long(self):
        errors = HandoffTemplate.validate(
            _record(context_summary="x" * (CONTEXT_SUMMARY_MAX + 1)),
        )
        assert any("context_summary" in e for e in errors)

    def test_next_step_too_long(self):
        errors = HandoffTemplate.validate(
            _record(next_step="x" * (NEXT_STEP_MAX + 1)),
        )
        assert any("next_step" in e for e in errors)

    def test_next_step_required_for_partial(self):
        errors = HandoffTemplate.validate(
            _record(status="partial", next_step=""),
        )
        assert any("next_step" in e for e in errors)

    def test_next_step_optional_for_completed(self):
        errors = HandoffTemplate.validate(
            _record(
                status="completed",
                pending=[],
                next_step="",
            ),
        )
        assert not any("next_step" in e for e in errors)

    def test_valid_completed_status(self):
        errors = HandoffTemplate.validate(
            _record(
                status="completed",
                completed=["All done"],
                pending=[],
                next_step="",
            ),
        )
        assert errors == []

    def test_valid_blocked_status(self):
        errors = HandoffTemplate.validate(
            _record(
                status="blocked",
                blockers=["Missing API key"],
                next_step="Get API key from admin",
            ),
        )
        assert errors == []


# ═══════════════════════════════════════════════════════════
# HandoffTemplate.to_json / from_json
# ═══════════════════════════════════════════════════════════


class TestJson:
    def test_roundtrip(self):
        original = _record()
        json_str = HandoffTemplate.to_json(original)
        restored = HandoffTemplate.from_json(json_str)
        assert restored is not None
        assert restored.session_id == original.session_id
        assert restored.completed == original.completed

    def test_invalid_json(self):
        assert HandoffTemplate.from_json("not json") is None

    def test_json_contains_fields(self):
        json_str = HandoffTemplate.to_json(_record())
        assert "sess-1234" in json_str
        assert "Fix login bug" in json_str


# ═══════════════════════════════════════════════════════════
# StructuredHandoffGuard
# ═══════════════════════════════════════════════════════════


class TestStructuredHandoffGuard:
    def test_metadata(self):
        g = StructuredHandoffGuard()
        assert g.name == "structured_handoff_guard"
        assert g.category.name == "QUALITY"

    def test_check_returns_none(self):
        g = StructuredHandoffGuard()
        ctx = _ctx()
        assert g.check(ctx) is None

    def test_ignores_non_write(self):
        g = StructuredHandoffGuard()
        ctx = _ctx(tool_name="Read")
        assert g.on_post_tool(ctx) is None

    def test_ignores_non_md(self):
        g = StructuredHandoffGuard()
        ctx = _ctx(tool_input={"file_path": "test.py", "content": "x"})
        assert g.on_post_tool(ctx) is None

    def test_ignores_non_handoff_content(self):
        g = StructuredHandoffGuard()
        ctx = _ctx(tool_input={
            "file_path": "notes.md",
            "content": "just some notes",
        })
        assert g.on_post_tool(ctx) is None

    def test_valid_handoff_no_result(self):
        g = StructuredHandoffGuard()
        md = HandoffTemplate.build(_record())
        ctx = _ctx(tool_input={
            "file_path": "handoff_test.md",
            "content": md,
        })
        assert g.on_post_tool(ctx) is None

    def test_invalid_handoff_returns_errors(self):
        g = StructuredHandoffGuard()
        r = _record(session_id="", task_name="")
        md = HandoffTemplate.build(r)
        ctx = _ctx(tool_input={
            "file_path": "handoff_test.md",
            "content": md,
        })
        result = g.on_post_tool(ctx)
        assert result is not None
        assert "session_id" in result.context
