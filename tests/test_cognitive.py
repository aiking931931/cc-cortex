"""Tests for cc_cortex.cognitive module."""

from __future__ import annotations

from cc_cortex.cognitive import (  # noqa: I001
    DEFAULT_THRESHOLDS,
    THRESHOLD_BOUNDS,
    AdaptiveThresholds,
    CognitiveEngine,
    DecisionJournal,
    SessionProfile,
    _classify_file_domain,
)

# ── _classify_file_domain ─────────────────────────────────


class TestClassifyFileDomain:
    def test_src_directory(self):
        assert _classify_file_domain("src/main.py") == "source"

    def test_lib_directory(self):
        assert _classify_file_domain("lib/utils.js") == "source"

    def test_tests_directory(self):
        assert _classify_file_domain("tests/test_foo.py") == "test"

    def test_test_directory(self):
        assert _classify_file_domain("test/spec_bar.js") == "test"

    def test_docs_directory(self):
        assert _classify_file_domain("docs/guide.md") == "docs"

    def test_md_extension_fallback(self):
        assert _classify_file_domain("README.md") == "docs"

    def test_json_extension_fallback(self):
        assert _classify_file_domain("package.json") == "config"

    def test_yaml_extension_fallback(self):
        assert _classify_file_domain("docker-compose.yaml") == "config"

    def test_unknown_path_defaults_to_source(self):
        assert _classify_file_domain("main.py") == "source"

    def test_empty_path(self):
        assert _classify_file_domain("") == "unknown"

    def test_none_path(self):
        assert _classify_file_domain(None) == "unknown"

    def test_backslash_normalized(self):
        assert _classify_file_domain("src\\core\\engine.py") == "source"

    def test_claude_directory(self):
        assert _classify_file_domain(".claude/rules/10-core.md") == "ai-config"

    def test_scripts_directory(self):
        assert _classify_file_domain("scripts/deploy.sh") == "tooling"

    def test_github_directory(self):
        assert _classify_file_domain(".github/workflows/ci.yml") == "ci"


# ── SessionProfile ────────────────────────────────────────


class TestSessionProfile:
    def test_record_tool_counts(self, tmp_path):
        p = SessionProfile("sess-001", str(tmp_path))
        p.record_tool("Read", {"file_path": "src/main.py"})
        p.record_tool("Read", {"file_path": "src/utils.py"})
        p.record_tool("Edit", {"file_path": "src/main.py"})
        assert p.tool_counts == {"Read": 2, "Edit": 1}

    def test_record_tool_tracks_files(self, tmp_path):
        p = SessionProfile("sess-002", str(tmp_path))
        p.record_tool("Read", {"file_path": "src/a.py"})
        p.record_tool("Read", {"file_path": "src/b.py"})
        p.record_tool("Edit", {"file_path": "src/a.py"})
        assert len(p.files_touched) == 2

    def test_record_tool_classifies_domains(self, tmp_path):
        p = SessionProfile("sess-003", str(tmp_path))
        p.record_tool("Read", {"file_path": "src/main.py"})
        p.record_tool("Read", {"file_path": "tests/test_main.py"})
        assert p.file_domains.get("source") == 1
        assert p.file_domains.get("test") == 1

    def test_classify_bugfix(self, tmp_path):
        p = SessionProfile("sess-004", str(tmp_path))
        p.record_user_message("fix the broken error handling")
        result = p.classify()
        assert result == "bugfix"

    def test_classify_feature(self, tmp_path):
        p = SessionProfile("sess-005", str(tmp_path))
        p.record_user_message("add a new feature to create users")
        result = p.classify()
        assert result == "feature"

    def test_classify_unknown_no_messages(self, tmp_path):
        p = SessionProfile("sess-006", str(tmp_path))
        result = p.classify()
        assert result == "unknown"

    def test_classify_general_no_signals(self, tmp_path):
        p = SessionProfile("sess-007", str(tmp_path))
        p.record_user_message("hello world")
        result = p.classify()
        assert result == "general"

    def test_read_write_ratio_reads_only(self, tmp_path):
        p = SessionProfile("sess-008", str(tmp_path))
        p.record_tool("Read", {"file_path": "a.py"})
        p.record_tool("Grep", {"pattern": "foo"})
        assert p.read_write_ratio == 2.0

    def test_read_write_ratio_writes_only(self, tmp_path):
        p = SessionProfile("sess-009", str(tmp_path))
        p.record_tool("Edit", {"file_path": "a.py"})
        p.record_tool("Write", {"file_path": "b.py"})
        assert p.read_write_ratio == 0.0

    def test_read_write_ratio_mixed(self, tmp_path):
        p = SessionProfile("sess-010", str(tmp_path))
        p.record_tool("Read", {"file_path": "a.py"})
        p.record_tool("Read", {"file_path": "b.py"})
        p.record_tool("Edit", {"file_path": "a.py"})
        assert p.read_write_ratio == 2.0

    def test_read_write_ratio_no_tools(self, tmp_path):
        p = SessionProfile("sess-011", str(tmp_path))
        assert p.read_write_ratio == 0.0

    def test_save_and_load(self, tmp_path):
        p = SessionProfile("sess-012", str(tmp_path))
        p.record_user_message("fix a bug")
        p.record_tool("Edit", {"file_path": "src/main.py"})
        p.classify()
        assert p.save() is True

        history = SessionProfile.load_history(str(tmp_path))
        assert len(history) == 1
        assert history[0]["session_id"] == "sess-012"
        assert history[0]["session_type"] == "bugfix"

    def test_save_updates_existing(self, tmp_path):
        p = SessionProfile("sess-013", str(tmp_path))
        p.record_user_message("create feature")
        p.classify()
        p.save()

        p.record_tool("Edit", {"file_path": "src/new.py"})
        p.save()

        history = SessionProfile.load_history(str(tmp_path))
        assert len(history) == 1
        assert history[0]["files_touched_count"] == 1

    def test_type_distribution(self, tmp_path):
        for i, msg in enumerate(["fix bug", "fix error", "add feature"]):
            p = SessionProfile(f"sess-dist-{i}", str(tmp_path))
            p.record_user_message(msg)
            p.classify()
            p.save()

        dist = SessionProfile.get_type_distribution(str(tmp_path))
        assert dist.get("bugfix") == 2
        assert dist.get("feature") == 1

    def test_to_dict_fields(self, tmp_path):
        p = SessionProfile("sess-dict", str(tmp_path))
        p.record_user_message("test something")
        p.classify()
        d = p.to_dict()
        assert "session_id" in d
        assert "short_id" in d
        assert "session_type" in d
        assert "tool_counts" in d
        assert "read_write_ratio" in d
        assert "duration_seconds" in d


# ── DecisionJournal ───────────────────────────────────────


class TestDecisionJournal:
    def test_record_returns_id(self, tmp_path):
        j = DecisionJournal(str(tmp_path))
        did = j.record("s1", "tool_choice", "ctx", "used Read")
        assert isinstance(did, str)
        assert len(did) == 8

    def test_record_outcome_accepted(self, tmp_path):
        j = DecisionJournal(str(tmp_path))
        did = j.record("s1", "approach", "ctx", "chose strategy A")
        assert j.record_outcome(did, "accepted") is True

    def test_record_outcome_not_found(self, tmp_path):
        j = DecisionJournal(str(tmp_path))
        assert j.record_outcome("nonexist", "accepted") is False

    def test_quality_score_all_accepted(self, tmp_path):
        j = DecisionJournal(str(tmp_path))
        for i in range(5):
            did = j.record("s1", "edit", f"ctx{i}", f"action{i}")
            j.record_outcome(did, "accepted")
        assert j.get_quality_score("edit") == 1.0

    def test_quality_score_all_corrected(self, tmp_path):
        j = DecisionJournal(str(tmp_path))
        for i in range(5):
            did = j.record("s1", "edit", f"ctx{i}", f"action{i}")
            j.record_outcome(did, "corrected")
        assert j.get_quality_score("edit") == 0.0

    def test_quality_score_no_data(self, tmp_path):
        j = DecisionJournal(str(tmp_path))
        assert j.get_quality_score() == 0.5

    def test_quality_score_mixed(self, tmp_path):
        j = DecisionJournal(str(tmp_path))
        d1 = j.record("s1", "edit", "c1", "a1")
        j.record_outcome(d1, "accepted")
        d2 = j.record("s1", "edit", "c2", "a2")
        j.record_outcome(d2, "corrected")
        score = j.get_quality_score("edit")
        assert score == 0.5

    def test_weak_spots(self, tmp_path):
        j = DecisionJournal(str(tmp_path))
        for i in range(4):
            did = j.record("s1", "naming", f"ctx{i}", f"bad_name{i}")
            j.record_outcome(did, "corrected")
        for i in range(4):
            did = j.record("s1", "approach", f"ctx{i}", f"good{i}")
            j.record_outcome(did, "accepted")

        weak = j.get_weak_spots(threshold=0.4, min_entries=3)
        assert len(weak) == 1
        assert weak[0]["decision_type"] == "naming"
        assert weak[0]["quality"] == 0.0

    def test_weak_spots_min_entries(self, tmp_path):
        j = DecisionJournal(str(tmp_path))
        did = j.record("s1", "rare", "c", "a")
        j.record_outcome(did, "corrected")
        weak = j.get_weak_spots(threshold=0.4, min_entries=3)
        assert len(weak) == 0

    def test_stats(self, tmp_path):
        j = DecisionJournal(str(tmp_path))
        d1 = j.record("s1", "edit", "c", "a")
        j.record_outcome(d1, "accepted")
        j.record("s1", "edit", "c", "a")  # unscored entry

        stats = j.stats()
        assert stats["total_decisions"] == 2
        assert stats["scored_decisions"] == 1
        assert stats["outcomes"]["accepted"] == 1

    def test_get_recent(self, tmp_path):
        j = DecisionJournal(str(tmp_path))
        for i in range(5):
            j.record("s1", "edit", f"c{i}", f"a{i}")
        recent = j.get_recent(limit=3)
        assert len(recent) == 3


# ── AdaptiveThresholds ────────────────────────────────────


class TestAdaptiveThresholds:
    def test_get_defaults(self, tmp_path):
        t = AdaptiveThresholds(str(tmp_path))
        assert t.get("sentinel_repeat") == DEFAULT_THRESHOLDS["sentinel_repeat"]

    def test_get_unknown_key(self, tmp_path):
        t = AdaptiveThresholds(str(tmp_path))
        assert t.get("nonexistent") == 0

    def test_adjust_increases(self, tmp_path):
        t = AdaptiveThresholds(str(tmp_path))
        new_val = t.adjust("sentinel_repeat", 2, "test increase")
        assert new_val == DEFAULT_THRESHOLDS["sentinel_repeat"] + 2
        assert t.get("sentinel_repeat") == new_val

    def test_adjust_respects_upper_bound(self, tmp_path):
        t = AdaptiveThresholds(str(tmp_path))
        upper = THRESHOLD_BOUNDS["sentinel_repeat"][1]
        new_val = t.adjust("sentinel_repeat", 100, "over upper")
        assert new_val == upper

    def test_adjust_respects_lower_bound(self, tmp_path):
        t = AdaptiveThresholds(str(tmp_path))
        lower = THRESHOLD_BOUNDS["sentinel_repeat"][0]
        new_val = t.adjust("sentinel_repeat", -100, "under lower")
        assert new_val == lower

    def test_reset_single_key(self, tmp_path):
        t = AdaptiveThresholds(str(tmp_path))
        t.adjust("sentinel_repeat", 3)
        t.reset("sentinel_repeat")
        assert t.get("sentinel_repeat") == DEFAULT_THRESHOLDS["sentinel_repeat"]

    def test_reset_all(self, tmp_path):
        t = AdaptiveThresholds(str(tmp_path))
        t.adjust("sentinel_repeat", 2)
        t.adjust("sentinel_scope", 5)
        t.reset()
        assert t.get("sentinel_repeat") == DEFAULT_THRESHOLDS["sentinel_repeat"]
        assert t.get("sentinel_scope") == DEFAULT_THRESHOLDS["sentinel_scope"]

    def test_get_all_merges(self, tmp_path):
        t = AdaptiveThresholds(str(tmp_path))
        t.adjust("sentinel_repeat", 1)
        all_t = t.get_all()
        assert all_t["sentinel_repeat"] == DEFAULT_THRESHOLDS["sentinel_repeat"] + 1
        assert all_t["sentinel_scope"] == DEFAULT_THRESHOLDS["sentinel_scope"]

    def test_status_shows_deviation(self, tmp_path):
        t = AdaptiveThresholds(str(tmp_path))
        t.adjust("sentinel_repeat", 2)
        status = t.status()
        assert status["sentinel_repeat"]["deviation"] == 2
        assert status["sentinel_scope"]["deviation"] == 0


# ── CognitiveEngine ──────────────────────────────────────


class TestCognitiveEngine:
    def test_on_session_start_no_insights(self, tmp_path):
        e = CognitiveEngine("sess-e1", str(tmp_path))
        result = e.on_session_start("hello")
        assert result is None

    def test_on_session_start_with_weak_spots(self, tmp_path):
        j = DecisionJournal(str(tmp_path))
        for i in range(5):
            did = j.record("s0", "naming", f"c{i}", f"a{i}")
            j.record_outcome(did, "corrected")

        e = CognitiveEngine("sess-e2", str(tmp_path))
        result = e.on_session_start("do stuff")
        assert result is not None
        assert "naming" in result

    def test_on_session_start_with_adapted_thresholds(self, tmp_path):
        t = AdaptiveThresholds(str(tmp_path))
        t.adjust("sentinel_repeat", 2, "test")

        e = CognitiveEngine("sess-e3", str(tmp_path))
        result = e.on_session_start()
        assert result is not None
        assert "adapted" in result.lower() or "threshold" in result.lower()

    def test_on_tool_use_records_in_profile(self, tmp_path):
        e = CognitiveEngine("sess-e4", str(tmp_path))
        e.on_tool_use("Read", {"file_path": "src/main.py"})
        assert e.profile.tool_counts["Read"] == 1
        assert "src/main.py" in e.profile.files_touched

    def test_on_correction_records_journal(self, tmp_path):
        e = CognitiveEngine("sess-e5", str(tmp_path))
        e.on_correction("wrong variable name", "editing code")
        entries = e.journal.get_recent(1)
        assert len(entries) == 1
        assert entries[0]["decision_type"] == "user_correction"
        assert entries[0]["outcome"] is None  # recorded as entry, not outcome
        assert entries[0]["confidence"] == 0.0

    def test_ingest_corrections(self, tmp_path):
        e = CognitiveEngine("sess-e6", str(tmp_path))
        corrections = [
            {
                "session_id": "s1",
                "assistant_before": "I used Read",
                "user_correction": "Should use Grep",
                "confidence": 0.9,
            },
            {
                "session_id": "s2",
                "assistant_before": "deleted file",
                "user_correction": "should backup first",
                "confidence": 0.8,
            },
        ]
        count = e.ingest_corrections(corrections, source="test")
        assert count == 2
        entries = e.journal.get_recent(5)
        assert len(entries) == 2
        assert all(e["decision_type"] == "user_correction" for e in entries)

    def test_on_session_end_saves_profile(self, tmp_path):
        e = CognitiveEngine("sess-e7", str(tmp_path))
        e.profile.record_user_message("fix bug")
        e.profile.record_tool("Edit", {"file_path": "src/a.py"})
        e.on_session_end()

        history = SessionProfile.load_history(str(tmp_path))
        assert len(history) == 1
        assert history[0]["session_id"] == "sess-e7"

    def test_get_dashboard(self, tmp_path):
        e = CognitiveEngine("sess-e8", str(tmp_path))
        e.profile.record_user_message("add feature")
        dash = e.get_dashboard()
        assert "session_profile" in dash
        assert "decision_quality" in dash
        assert "adaptive_thresholds" in dash
        assert "session_type_distribution" in dash

    def test_get_summary_returns_string(self, tmp_path):
        e = CognitiveEngine("sess-e9", str(tmp_path))
        summary = e.get_summary()
        assert isinstance(summary, str)
        assert "Cognitive" in summary

    def test_no_session_id_no_profile(self, tmp_path):
        e = CognitiveEngine("", str(tmp_path))
        assert e.profile is None
        e.on_tool_use("Read", {"file_path": "a.py"})  # should not crash
