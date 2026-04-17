"""Tests for concinno.prompt_engine — dynamic prompt assembly."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone

from concinno.prompt_engine import (
    DRIFT_INTERVAL,
    DriftTracker,
    DynamicSlots,
    PromptEngine,
    StaticCache,
    _build_ftrl_context,
    _estimate_tokens,
)


class TestTokenEstimation:
    def test_empty_string(self):
        assert _estimate_tokens("") == 0

    def test_ascii_text(self):
        # 100 chars ASCII ≈ 25 tokens
        t = _estimate_tokens("a" * 100)
        assert 20 <= t <= 30

    def test_cjk_text(self):
        # 10 CJK chars ≈ 15 tokens
        t = _estimate_tokens("你好世界測試繁體中文字")
        assert t >= 10

    def test_mixed_text(self):
        t = _estimate_tokens("Hello 你好 World 世界")
        assert t > 0


class TestStaticCache:
    def test_default_empty(self):
        cache = StaticCache()
        assert cache.total_tokens == 0
        assert cache.render() == ""

    def test_render_with_identity(self):
        cache = StaticCache(identity="I am AI King", iron_laws="⛔ Rule 1")
        result = cache.render()
        assert "AI King" in result
        assert "Rule 1" in result

    def test_total_tokens_nonzero(self):
        cache = StaticCache(identity="x" * 100)
        cache._total_tokens = _estimate_tokens("x" * 100)
        assert cache.total_tokens > 0


class TestDynamicSlots:
    def test_empty_slots(self):
        slots = DynamicSlots()
        assert slots.total_tokens() == 0
        assert slots.render(1000) == ""

    def test_priority_order(self):
        slots = DynamicSlots(
            thinking="THINK",
            memory_hits="MEMORY",
            delivery="DELIVER",
        )
        result = slots.render(5000)
        # Thinking should come before memory
        assert result.index("THINK") < result.index("MEMORY")
        assert result.index("MEMORY") < result.index("DELIVER")

    def test_budget_truncation(self):
        slots = DynamicSlots(
            thinking="A" * 100,
            handoff_summary="B" * 50000,  # Way over budget
        )
        result = slots.render(200)  # Very tight budget
        # Should include thinking but truncate handoff
        assert "A" * 50 in result

    def test_budget_zero_skips_all(self):
        slots = DynamicSlots(thinking="THINK")
        result = slots.render(0)
        assert result == ""


class TestDriftTracker:
    def test_initial_state(self):
        tracker = DriftTracker()
        assert tracker.calls_since_reinject == 0
        assert not tracker.should_reinject()

    def test_tick_increments(self):
        tracker = DriftTracker()
        for _ in range(5):
            tracker.tick()
        assert tracker.calls_since_reinject == 5

    def test_should_reinject_at_interval(self):
        tracker = DriftTracker()
        for _ in range(DRIFT_INTERVAL):
            tracker.tick()
        assert tracker.should_reinject()

    def test_reset_clears(self):
        tracker = DriftTracker()
        for _ in range(DRIFT_INTERVAL):
            tracker.tick()
        tracker.reset()
        assert tracker.calls_since_reinject == 0
        assert not tracker.should_reinject()
        assert tracker.last_reinject_time > 0

    def test_custom_interval(self):
        tracker = DriftTracker()
        for _ in range(5):
            tracker.tick()
        assert tracker.should_reinject(interval=5)
        assert not tracker.should_reinject(interval=10)


class TestPromptEngine:
    def test_create_default(self):
        engine = PromptEngine()
        assert not engine._loaded

    def test_load_static(self):
        engine = PromptEngine(workspace="")
        engine.load_static()
        assert engine._loaded

    def test_assemble_empty(self):
        engine = PromptEngine()
        result = engine.assemble()
        assert isinstance(result, str)

    def test_assemble_with_complexity(self):
        engine = PromptEngine()
        engine.load_static()
        minimal = engine.assemble(complexity="minimal")
        full = engine.assemble(complexity="full")
        # Full should be longer than minimal
        assert len(full) >= len(minimal)

    def test_on_tool_call_no_reinject(self):
        engine = PromptEngine()
        engine.load_static()
        # First call should not trigger reinject
        result = engine.on_tool_call()
        assert result is None

    def test_on_tool_call_reinject_at_interval(self):
        engine = PromptEngine()
        engine.static = StaticCache(identity="I am AI King")
        engine._loaded = True
        # Tick to just before interval
        for _ in range(DRIFT_INTERVAL - 1):
            assert engine.on_tool_call() is None
        # This call should trigger reinject
        result = engine.on_tool_call()
        assert result is not None
        assert "AI King" in result

    def test_force_drift_reinject(self):
        engine = PromptEngine()
        engine.static = StaticCache(identity="IDENTITY")
        engine._loaded = True
        result = engine.assemble(include_drift=True)
        assert "IDENTITY" in result


# ── FTRL learning injection (R2) ───────────────────────────


def _write_learnings(tmpdir: str, items: list[dict]) -> str:
    """Write a learnings.json fixture and return its path."""
    path = os.path.join(tmpdir, "learnings.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"learnings": items}, f)
    return path


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TestFTRLDynamicSlot:
    """The ``ftrl_learnings`` slot participates in the budget pipeline."""

    def test_default_empty(self):
        slots = DynamicSlots()
        assert slots.ftrl_learnings == ""

    def test_total_tokens_includes_ftrl(self):
        slots = DynamicSlots(ftrl_learnings="x" * 100)
        assert slots.total_tokens() > 0

    def test_priority_after_thinking_before_memory(self):
        slots = DynamicSlots(
            thinking="THINK",
            ftrl_learnings="FTRL",
            memory_hits="MEMORY",
            delivery="DELIVER",
        )
        result = slots.render(5000)
        assert result.index("THINK") < result.index("FTRL")
        assert result.index("FTRL") < result.index("MEMORY")
        assert result.index("MEMORY") < result.index("DELIVER")

    def test_budget_truncation_drops_ftrl_last_first(self):
        """Under pressure, thinking stays, ftrl gets truncated before it."""
        slots = DynamicSlots(
            thinking="T" * 40,
            ftrl_learnings="F" * 40,
        )
        result = slots.render(50)  # fits thinking, part of ftrl
        assert "T" * 10 in result


class TestBuildFTRLContext:
    """``_build_ftrl_context`` helper resolves learnings + FTRL ranking."""

    def test_missing_file_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "does_not_exist.json")
            assert _build_ftrl_context(learnings_path=path) is None

    def test_empty_learnings_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_learnings(tmp, [])
            assert _build_ftrl_context(learnings_path=path) is None

    def test_malformed_json_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "learnings.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write("{ not valid json")
            assert _build_ftrl_context(learnings_path=path) is None

    def test_below_threshold_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_learnings(tmp, [
                {
                    "count": 1,
                    "last_seen": _iso_now(),
                    "correction_text": "trivial",
                },
            ])
            # ftrl_threshold default 3.0 → count 1 fails
            assert _build_ftrl_context(learnings_path=path) is None

    def test_above_threshold_returns_formatted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_learnings(tmp, [
                {
                    "count": 7,
                    "last_seen": _iso_now(),
                    "correction_text": "always read before edit",
                },
            ])
            result = _build_ftrl_context(learnings_path=path)
            assert result is not None
            assert "FTRL" in result
            assert "7x" in result
            assert "always read before edit" in result

    def test_excludes_promoted_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_learnings(tmp, [
                {
                    "count": 9,
                    "last_seen": _iso_now(),
                    "correction_text": "already sedimented",
                    "promoted": True,
                },
            ])
            assert _build_ftrl_context(learnings_path=path) is None

    def test_respects_max_items(self):
        with tempfile.TemporaryDirectory() as tmp:
            items = [
                {
                    "count": 10 + i,
                    "last_seen": _iso_now(),
                    "correction_text": f"correction #{i}",
                }
                for i in range(5)
            ]
            path = _write_learnings(tmp, items)
            result = _build_ftrl_context(
                learnings_path=path, max_items=2,
            )
            assert result is not None
            # Header + 2 bullets = 3 lines
            assert result.count("\n") == 2

    def test_sorts_by_ftrl_weight_desc(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_learnings(tmp, [
                {
                    "count": 4,
                    "last_seen": _iso_now(),
                    "correction_text": "lower weight",
                },
                {
                    "count": 20,
                    "last_seen": _iso_now(),
                    "correction_text": "top weight",
                },
            ])
            result = _build_ftrl_context(learnings_path=path)
            assert result is not None
            assert result.index("top weight") < result.index("lower weight")

    def test_malformed_item_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_learnings(tmp, [
                {"count": "not-int", "last_seen": _iso_now(),
                 "correction_text": "bad"},
                {"count": 8, "last_seen": _iso_now(),
                 "correction_text": "good"},
            ])
            result = _build_ftrl_context(learnings_path=path)
            assert result is not None
            assert "good" in result
            assert "bad" not in result

    def test_default_path_used_when_empty(self):
        """Empty string routes to ~/.claude/cognitive/learnings.json — may
        or may not exist locally; the call must not raise."""
        # Should not raise regardless of whether the default file exists.
        result = _build_ftrl_context(learnings_path="")
        assert result is None or isinstance(result, str)


class TestPromptEngineFTRLIntegration:
    """``PromptEngine.build_ftrl_context`` + ``assemble()`` wire the slot."""

    def test_build_ftrl_context_method_delegates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_learnings(tmp, [
                {
                    "count": 6,
                    "last_seen": _iso_now(),
                    "correction_text": "wire PromptEngine",
                },
            ])
            engine = PromptEngine()
            result = engine.build_ftrl_context(learnings_path=path)
            assert result is not None
            assert "wire PromptEngine" in result

    def test_assemble_includes_ftrl_slot(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_learnings(tmp, [
                {
                    "count": 6,
                    "last_seen": _iso_now(),
                    "correction_text": "verify before write",
                },
            ])
            engine = PromptEngine()
            engine.load_static()
            result = engine.assemble(learnings_path=path)
            assert "verify before write" in result

    def test_assemble_skips_ftrl_when_file_missing(self):
        engine = PromptEngine()
        engine.load_static()
        result = engine.assemble(
            learnings_path="/definitely/does/not/exist.json",
        )
        assert "FTRL" not in result


class TestFTRLHookDelegation:
    """``_ftrl_learning_injection`` in on_prompt_submit is a thin wrapper."""

    def test_delegates_to_prompt_engine(self):
        # When the default learnings path does not yield hits, the adapter
        # returns None without raising.
        from concinno.hooks.on_prompt_submit import _ftrl_learning_injection

        result = _ftrl_learning_injection(
            cache_dir="",
            max_items=3,
            ftrl_threshold=999.0,  # impossibly high
        )
        assert result is None

