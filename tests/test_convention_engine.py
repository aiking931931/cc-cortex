"""Tests for cc_cortex.convention_engine."""

from __future__ import annotations

from cc_cortex.convention_engine import (
    ConventionEngine,
    check_naming,
    suggest_path,
)


class TestCheckNaming:
    def test_patent_correct(self):
        engine = ConventionEngine()
        r = engine.check_naming("PAT-001_ZIQ-CGLF_provisional.md", "patent")
        assert r.passed

    def test_patent_wrong(self):
        engine = ConventionEngine()
        r = engine.check_naming("Patent_Draft_v1.md", "patent")
        assert not r.passed
        assert "PAT-" in r.suggestion

    def test_handoff_correct(self):
        engine = ConventionEngine()
        r = engine.check_naming("handoff_CCC.md", "handoff")
        assert r.passed

    def test_feedback_correct(self):
        engine = ConventionEngine()
        r = engine.check_naming("feedback_git_fast_commit.md", "feedback")
        assert r.passed

    def test_unknown_type_passes(self):
        engine = ConventionEngine()
        r = engine.check_naming("random_file.txt")
        assert r.passed

    def test_auto_detect_patent(self):
        engine = ConventionEngine()
        r = engine.check_naming("PAT-003_CBUA_claims.md")
        assert r.passed


class TestSuggestPlacement:
    def test_patent_placement(self):
        engine = ConventionEngine()
        path = engine.suggest_placement("PAT-001_ZIQ.md")
        assert "07_Patents" in path

    def test_handoff_placement(self):
        engine = ConventionEngine()
        path = engine.suggest_placement("交接_CCC.md", project="cc-cortex")
        assert "06_Handoffs" in path
        assert "cc-cortex" in path

    def test_feedback_placement(self):
        engine = ConventionEngine()
        path = engine.suggest_placement("feedback_test.md")
        assert "memory" in path

    def test_no_match(self):
        engine = ConventionEngine()
        path = engine.suggest_placement("random.txt")
        assert path == "random.txt"


class TestCheckPlacement:
    def test_correct_placement(self):
        engine = ConventionEngine()
        r = engine.check_placement("07_Patents/PAT-001_ZIQ.md")
        assert r.passed

    def test_wrong_placement(self):
        engine = ConventionEngine()
        r = engine.check_placement("random_dir/PAT-001_ZIQ.md")
        assert not r.passed
        assert "07_Patents" in r.suggestion


class TestGetTemplate:
    def test_handoff_template(self):
        engine = ConventionEngine()
        t = engine.get_template("handoff")
        assert t is not None
        assert "frontmatter" in t
        assert "sections" in t
        assert "Status" in t["sections"]

    def test_memory_template(self):
        engine = ConventionEngine()
        t = engine.get_template("memory")
        assert t is not None
        assert "name" in t["frontmatter"]

    def test_unknown_template(self):
        engine = ConventionEngine()
        assert engine.get_template("nonexistent") is None


class TestConvenience:
    def test_check_naming_func(self):
        r = check_naming("PAT-001_ZIQ_provisional.md", "patent")
        assert r.passed

    def test_suggest_path_func(self):
        path = suggest_path("feedback_test.md")
        assert "memory" in path


class TestAutoDetect:
    def test_detect_patent(self):
        engine = ConventionEngine()
        assert engine._detect_type("PAT-001_foo.md") == "patent"

    def test_detect_handoff(self):
        engine = ConventionEngine()
        assert engine._detect_type("交接_CCC.md") == "handoff"

    def test_detect_handoff_summary(self):
        engine = ConventionEngine()
        assert engine._detect_type("交接_CCC_summary.md") == "handoff_summary"

    def test_detect_feedback(self):
        engine = ConventionEngine()
        assert engine._detect_type("feedback_test.md") == "feedback"

    def test_detect_session(self):
        engine = ConventionEngine()
        assert engine._detect_type("session_2026-04-12_test.md") == "session"

    def test_detect_unknown(self):
        engine = ConventionEngine()
        assert engine._detect_type("random.py") == ""
