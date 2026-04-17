"""Tests for c0_router — CBUA C0 complexity classifier + operational config."""

from __future__ import annotations

import json
import os
import tempfile

from concinno.c0_router import NAMESPACE, C0Result, C0Router

# ── Helpers ──────────────────────────────────────────────

def _router() -> C0Router:
    return C0Router()


# ── Basic classification ─────────────────────────────────


class TestClassifyBasic:
    def test_simple_short_prompt(self):
        r = _router().classify("fix typo")
        assert r.complexity == "simple"
        assert r.prompt_budget == 800
        assert r.guard_level == "relaxed"

    def test_simple_chinese(self):
        r = _router().classify("改名")
        assert r.complexity == "simple"

    def test_complicated_multi_step(self):
        r = _router().classify("先讀交接檔，然後修改 API，接著更新測試，最後部署")
        assert r.complexity == "complicated"
        assert r.prompt_budget == 1500
        assert r.guard_level == "normal"

    def test_complex_exploration(self):
        r = _router().classify("探索一下這個新的架構，不確定該怎麼做")
        assert r.complexity == "complex"
        assert r.prompt_budget == 3000
        assert r.guard_level == "strict"

    def test_chaotic_emergency(self):
        r = _router().classify("緊急！伺服器崩潰了，全掛")
        assert r.complexity == "chaotic"
        assert r.prompt_budget == 3000
        assert r.guard_level == "strict"


# ── Heavy keyword escalation ────────────────────────────


class TestHeavyKeywords:
    def test_refactor_escalates_simple(self):
        r = _router().classify("refactor this function")
        assert r.complexity in ("complicated", "complex")
        assert r.signals.get("heavy_keywords") is True

    def test_architecture_escalates(self):
        r = _router().classify("architecture redesign")
        assert r.complexity in ("complicated", "complex")

    def test_migration_escalates(self):
        r = _router().classify("migration to new DB")
        assert r.complexity in ("complicated", "complex")


# ── File count escalation ────────────────────────────────


class TestFileCountEscalation:
    def test_many_files_escalates_to_complicated(self):
        files = [f"file_{i}.py" for i in range(7)]
        r = _router().classify("fix typo", file_paths=files)
        # >5 files → at least complicated
        assert r.complexity in ("complicated", "complex", "chaotic")
        assert r.signals["file_count"] == 7

    def test_very_many_files_escalates_to_complex(self):
        files = [f"file_{i}.py" for i in range(20)]
        r = _router().classify("fix typo", file_paths=files)
        assert r.complexity in ("complex", "chaotic")
        assert "file_count" in r.escalation_reason

    def test_no_files_no_escalation(self):
        r = _router().classify("fix typo")
        assert r.signals["file_count"] == 0


# ── Tool history escalation ──────────────────────────────


class TestToolHistoryEscalation:
    def test_many_tools_escalates_to_complicated(self):
        tools = ["Read"] * 35
        r = _router().classify("fix typo", tool_history=tools)
        assert r.complexity in ("complicated", "complex", "chaotic")
        assert r.signals["tool_count"] == 35

    def test_very_many_tools_escalates_to_complex(self):
        tools = ["Edit"] * 65
        r = _router().classify("fix typo", tool_history=tools)
        assert r.complexity in ("complex", "chaotic")
        assert "tool_count" in r.escalation_reason

    def test_few_tools_no_escalation(self):
        tools = ["Read"] * 5
        r = _router().classify("fix typo", tool_history=tools)
        assert r.complexity == "simple"


# ── Static methods ───────────────────────────────────────


class TestStaticMethods:
    def test_get_prompt_budget_all_levels(self):
        assert C0Router.get_prompt_budget("simple") == 800
        assert C0Router.get_prompt_budget("complicated") == 1500
        assert C0Router.get_prompt_budget("complex") == 3000
        assert C0Router.get_prompt_budget("chaotic") == 3000

    def test_get_prompt_budget_unknown_fallback(self):
        assert C0Router.get_prompt_budget("alien") == 1500

    def test_get_guard_level_all_levels(self):
        assert C0Router.get_guard_level("simple") == "relaxed"
        assert C0Router.get_guard_level("complicated") == "normal"
        assert C0Router.get_guard_level("complex") == "strict"
        assert C0Router.get_guard_level("chaotic") == "strict"

    def test_get_guard_level_unknown_fallback(self):
        assert C0Router.get_guard_level("alien") == "normal"


# ── C0Result serialization ───────────────────────────────


class TestC0Result:
    def test_round_trip(self):
        original = C0Result(
            complexity="complicated",
            prompt_budget=1500,
            guard_level="normal",
            signals={"markers": "neutral", "tool_count": 10},
            escalation_reason="",
        )
        d = original.to_dict()
        restored = C0Result.from_dict(d)
        assert restored.complexity == original.complexity
        assert restored.prompt_budget == original.prompt_budget
        assert restored.guard_level == original.guard_level
        assert restored.signals == original.signals

    def test_from_dict_defaults(self):
        r = C0Result.from_dict({})
        assert r.complexity == "simple"
        assert r.prompt_budget == 800


# ── StateStore persistence ───────────────────────────────


class TestPersistence:
    def test_persist_and_load(self):
        router = _router()
        result = router.classify("refactor the whole module")
        with tempfile.TemporaryDirectory() as tmpdir:
            router.persist(result, tmpdir, "test-session-123")
            loaded = router.load(tmpdir, "test-session-123")
            assert loaded is not None
            assert loaded.complexity == result.complexity
            assert loaded.prompt_budget == result.prompt_budget
            assert loaded.guard_level == result.guard_level

    def test_load_missing_returns_none(self):
        router = _router()
        with tempfile.TemporaryDirectory() as tmpdir:
            loaded = router.load(tmpdir, "nonexistent")
            assert loaded is None

    def test_persist_creates_namespace_dir(self):
        router = _router()
        result = router.classify("deploy")
        with tempfile.TemporaryDirectory() as tmpdir:
            router.persist(result, tmpdir, "abc-session")
            ns_dir = os.path.join(tmpdir, NAMESPACE)
            assert os.path.isdir(ns_dir)
            files = os.listdir(ns_dir)
            assert len(files) == 1
            with open(os.path.join(ns_dir, files[0]), encoding="utf-8") as f:
                data = json.load(f)
            assert data["complexity"] == result.complexity


# ── Escalation never downgrades ──────────────────────────


class TestNoDowngrade:
    def test_chaotic_not_downgraded_by_few_tools(self):
        r = _router().classify("緊急！伺服器崩潰了", tool_history=["Read"])
        assert r.complexity == "chaotic"

    def test_complex_not_downgraded_by_few_files(self):
        r = _router().classify("探索新架構，不確定", file_paths=["a.py"])
        assert r.complexity == "complex"
