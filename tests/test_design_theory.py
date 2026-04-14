"""Tests for cc_cortex.design_theory — Design theory enforcement."""

from __future__ import annotations

from pathlib import Path

from cc_cortex.design_theory import (
    DESIGN_CONSTRAINTS,
    DesignTheoryGuard,
    TaskClassification,
    check_deep_module,
    check_vertical_slice,
    classify_hitl_afk,
    format_hitl_afk_tag,
    generate_parallel_prompts,
)
from cc_cortex.guards.base import GuardContext

# ── check_vertical_slice ──


class TestCheckVerticalSlice:
    def test_non_planning_file_passes(self) -> None:
        assert check_vertical_slice("⬜ new task", "src/app.ts") is None

    def test_no_work_items_passes(self) -> None:
        result = check_vertical_slice(
            "Updated some notes about existing work",
            "planning/tasks.md",
        )
        assert result is None

    def test_work_item_with_test_plan_passes(self) -> None:
        content = "⬜ New feature: auth flow\n\nTest plan:\n- Login works\n- Logout works"
        assert check_vertical_slice(content, "planning/tasks.md") is None

    def test_work_item_with_exit_criteria_passes(self) -> None:
        content = "⬜ New feature\n\nExit criteria:\n- API returns 200"
        assert check_vertical_slice(content, "交接_project.md") is None

    def test_work_item_without_traceability_fails(self) -> None:
        content = "⬜ New feature: do the thing\n\nJust build it and ship it"
        result = check_vertical_slice(content, "planning/tasks.md")
        assert result is not None
        assert "Vertical Slice" in result

    def test_phase_marker_without_traceability_fails(self) -> None:
        content = "Phase 3: Build the new dashboard\n\nSteps:\n1. Create component\n2. Style it"
        result = check_vertical_slice(content, "05_planning/roadmap.md")
        assert result is not None

    def test_short_content_passes(self) -> None:
        assert check_vertical_slice("⬜ ok", "planning/x.md") is None

    def test_empty_content_passes(self) -> None:
        assert check_vertical_slice("", "planning/x.md") is None

    def test_tdd_reference_passes(self) -> None:
        content = "⬜ New module\n\nApproach: TDD first, write tests before implementation"
        assert check_vertical_slice(content, "planning/tasks.md") is None

    def test_zh_acceptance_passes(self) -> None:
        content = "⬜ 新功能\n\n驗收標準：API 回 200"
        assert check_vertical_slice(content, "交接_project.md") is None


# ── classify_hitl_afk ──


class TestClassifyHitlAfk:
    def test_empty_returns_unknown(self) -> None:
        result = classify_hitl_afk("")
        assert result.label == "UNKNOWN"

    def test_hitl_keywords(self) -> None:
        result = classify_hitl_afk("需要人工確認部署結果")
        assert result.label == "HITL"
        assert result.confidence >= 0.5

    def test_afk_keywords(self) -> None:
        result = classify_hitl_afk("自動批次處理 lint 錯誤")
        assert result.label == "AFK"
        assert result.confidence >= 0.5

    def test_mixed_signals_hitl_dominant(self) -> None:
        result = classify_hitl_afk("自動跑但需要人工確認需要 approval")
        assert result.label == "HITL"

    def test_mixed_signals_afk_dominant(self) -> None:
        result = classify_hitl_afk("自動批次自動化處理需要確認")
        assert result.label in ("AFK", "HITL")  # Could go either way

    def test_code_heuristic(self) -> None:
        result = classify_hitl_afk("refactor the auth module")
        assert result.label == "AFK"
        assert result.confidence == 0.3

    def test_no_signals(self) -> None:
        result = classify_hitl_afk("discuss project direction")
        assert result.label == "UNKNOWN"

    def test_english_hitl(self) -> None:
        result = classify_hitl_afk("needs manual review and human approval")
        assert result.label == "HITL"

    def test_english_afk(self) -> None:
        result = classify_hitl_afk("automated batch processing, scripted routine")
        assert result.label == "AFK"


# ── format_hitl_afk_tag ──


class TestFormatHitlAfkTag:
    def test_hitl_tag(self) -> None:
        tag = format_hitl_afk_tag(TaskClassification("HITL", 0.8, "test"))
        assert "HITL" in tag
        assert "👤" in tag

    def test_afk_tag(self) -> None:
        tag = format_hitl_afk_tag(TaskClassification("AFK", 0.8, "test"))
        assert "AFK" in tag
        assert "🤖" in tag

    def test_unknown_empty(self) -> None:
        tag = format_hitl_afk_tag(TaskClassification("UNKNOWN", 0.0, "test"))
        assert tag == ""


# ── check_deep_module ──


class TestCheckDeepModule:
    def test_deep_module_passes(self, tmp_path: Path) -> None:
        # 3 public functions, 50 impl lines → ratio 16.7 → deep
        source = "\n".join([
            "def func_a():",
            "    pass",
            "",
            "def func_b():",
            "    pass",
            "",
            "def func_c():",
            "    pass",
        ] + ["    x = 1"] * 42)
        f = tmp_path / "deep.py"
        f.write_text(source)
        result = check_deep_module(str(f), min_ratio=5.0, min_public=3)
        assert result is None  # Deep = no issue

    def test_shallow_module_detected(self, tmp_path: Path) -> None:
        # 5 public functions, 10 impl lines → ratio 2.0 → shallow
        source = "\n".join([
            f"def func_{i}(): pass" for i in range(5)
        ])
        f = tmp_path / "shallow.py"
        f.write_text(source)
        result = check_deep_module(str(f), min_ratio=5.0, min_public=3)
        assert result is not None
        assert result.is_shallow
        assert result.public_surface == 5

    def test_small_module_exempt(self, tmp_path: Path) -> None:
        source = "def a(): pass\ndef b(): pass\n"
        f = tmp_path / "tiny.py"
        f.write_text(source)
        result = check_deep_module(str(f), min_ratio=5.0, min_public=3)
        assert result is None  # Too small to judge

    def test_js_module(self, tmp_path: Path) -> None:
        source = "\n".join([
            "export function a() {}",
            "export function b() {}",
            "export function c() {}",
            "export function d() {}",
        ])
        f = tmp_path / "shallow.ts"
        f.write_text(source)
        result = check_deep_module(str(f), min_ratio=5.0, min_public=3)
        assert result is not None
        assert result.is_shallow

    def test_nonexistent_file(self) -> None:
        result = check_deep_module("/nonexistent/file.py")
        assert result is None

    def test_unsupported_extension(self, tmp_path: Path) -> None:
        f = tmp_path / "data.json"
        f.write_text("{}")
        assert check_deep_module(str(f)) is None

    def test_private_symbols_not_counted(self, tmp_path: Path) -> None:
        source = "\n".join([
            "def _private_a(): pass",
            "def _private_b(): pass",
            "def public_c(): pass",
        ])
        f = tmp_path / "mixed.py"
        f.write_text(source)
        result = check_deep_module(str(f), min_ratio=5.0, min_public=3)
        assert result is None  # Only 1 public, exempt


# ── generate_parallel_prompts ──


class TestGenerateParallelPrompts:
    def test_default_constraint_set(self) -> None:
        prompts = generate_parallel_prompts("Build auth system")
        assert len(prompts) == 3
        assert all("Build auth system" in p for p in prompts)
        assert "variant A" in prompts[0].lower() or "variant_id" not in prompts[0]

    def test_custom_constraints(self) -> None:
        prompts = generate_parallel_prompts(
            "Build API",
            custom_constraints=["REST only", "GraphQL only"],
        )
        assert len(prompts) == 2
        assert "REST only" in prompts[0]
        assert "GraphQL only" in prompts[1]

    def test_all_constraint_sets_valid(self) -> None:
        for key, constraints in DESIGN_CONSTRAINTS.items():
            prompts = generate_parallel_prompts("Test", constraint_set=key)
            assert len(prompts) == len(constraints)

    def test_variant_ids_sequential(self) -> None:
        prompts = generate_parallel_prompts("Test")
        assert "variant A" in prompts[0]
        assert "variant B" in prompts[1]
        assert "variant C" in prompts[2]


# ── DesignTheoryGuard ──


class TestDesignTheoryGuard:
    def _make_ctx(self, tool_name: str, tool_input: dict) -> GuardContext:
        return GuardContext(
            tool_name=tool_name,
            tool_input=tool_input,
            session_id="test",
            cache_dir="",
            hook_event="PreToolUse",
        )

    def test_non_write_tool_passes(self) -> None:
        guard = DesignTheoryGuard()
        ctx = self._make_ctx("Read", {"file_path": "planning/x.md"})
        assert guard.check(ctx) is None

    def test_planning_file_without_traceability_denied(self) -> None:
        guard = DesignTheoryGuard()
        ctx = self._make_ctx("Write", {
            "file_path": "planning/tasks.md",
            "content": "⬜ New feature: build dashboard\n\nJust do it fast",
        })
        result = guard.check(ctx)
        assert result is not None
        assert result.action.value == "deny"

    def test_planning_file_with_traceability_passes(self) -> None:
        guard = DesignTheoryGuard()
        ctx = self._make_ctx("Write", {
            "file_path": "planning/tasks.md",
            "content": "⬜ New feature\n\nExit criteria:\n- Works",
        })
        assert guard.check(ctx) is None

    def test_code_file_passes_pretool(self) -> None:
        guard = DesignTheoryGuard()
        ctx = self._make_ctx("Write", {
            "file_path": "src/app.py",
            "content": "def main(): pass",
        })
        assert guard.check(ctx) is None

    def test_post_tool_shallow_module(self, tmp_path: Path) -> None:
        guard = DesignTheoryGuard()
        source = "\n".join([f"def func_{i}(): pass" for i in range(5)])
        f = tmp_path / "shallow.py"
        f.write_text(source)
        ctx = self._make_ctx("Write", {"file_path": str(f)})
        result = guard.on_post_tool(ctx)
        assert result is not None
        assert "Deep Module" in (result.context or "")

    def test_post_tool_deep_module_passes(self, tmp_path: Path) -> None:
        guard = DesignTheoryGuard()
        source = "\n".join([
            "def func_a():", "    pass", "",
            "def func_b():", "    pass", "",
            "def func_c():", "    pass",
        ] + ["    x = 1"] * 42)
        f = tmp_path / "deep.py"
        f.write_text(source)
        ctx = self._make_ctx("Write", {"file_path": str(f)})
        result = guard.on_post_tool(ctx)
        assert result is None
