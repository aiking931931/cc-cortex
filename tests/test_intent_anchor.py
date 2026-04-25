"""Tests for ``concinno.intent_anchor`` (v2.10 minimal Stage -1 dataclass)."""

from __future__ import annotations

from concinno.intent_anchor import (
    IntentAnchor,
    _heuristic_constraints,
    _heuristic_done_spec,
    extract_anchor,
    render_anchor_block,
)

# ── IntentAnchor dataclass ──────────────────────────────────────────────


class TestIntentAnchor:
    def test_default_construction_is_empty(self):
        anchor = IntentAnchor()
        assert anchor.is_empty() is True
        assert anchor.summary == ""
        assert anchor.original == ""
        assert anchor.done_spec == ""
        assert anchor.constraints == ""

    def test_summary_alone_is_not_empty(self):
        assert IntentAnchor(summary="Build it").is_empty() is False

    def test_original_alone_is_not_empty(self):
        assert IntentAnchor(original="raw prompt").is_empty() is False

    def test_to_dict_round_trip(self):
        anchor = IntentAnchor(
            summary="Count albums",
            original="how many studio albums…",
            done_spec="answer with an integer",
            constraints="as of 2009",
        )
        round_tripped = IntentAnchor.from_dict(anchor.to_dict())
        assert round_tripped == anchor

    def test_from_dict_honours_legacy_intent_key(self):
        # v2.9 stored only ``intent`` — make sure 2.10 readers fold it in
        # without losing the value to a missing ``summary`` key.
        anchor = IntentAnchor.from_dict({"intent": "Old goal"})
        assert anchor.summary == "Old goal"

    def test_from_dict_prefers_summary_over_intent(self):
        # When both are present, the v2.10 ``summary`` wins; ``intent``
        # is a back-compat alias only.
        anchor = IntentAnchor.from_dict(
            {"summary": "new", "intent": "old"},
        )
        assert anchor.summary == "new"

    def test_from_dict_tolerates_none_values(self):
        anchor = IntentAnchor.from_dict(
            {"summary": None, "done_spec": None, "constraints": None},
        )
        # is_empty when both summary and original are blank
        assert anchor.is_empty() is True


# ── _heuristic_done_spec ────────────────────────────────────────────────


class TestHeuristicDoneSpec:
    def test_empty_input_returns_empty(self):
        assert _heuristic_done_spec("") == ""

    def test_extracts_answer_clause(self):
        result = _heuristic_done_spec(
            "How many studio albums did the artist release?"
        )
        # No "answer X" keyword, but the question itself doesn't trip a
        # done-spec pattern — heuristic gracefully returns ""
        assert result == ""

    def test_extracts_explicit_answer_directive(self):
        result = _heuristic_done_spec(
            "Please answer with the integer count of items."
        )
        assert "answer" in result.lower()

    def test_extracts_count_of_phrase(self):
        result = _heuristic_done_spec("Return the count of items in the list.")
        assert "count" in result.lower()

    def test_extracts_chinese_quantity_phrase(self):
        result = _heuristic_done_spec("請回答專輯的數量")
        # Either Chinese "回答" pattern or "的數量" pattern should fire
        assert result != ""

    def test_caps_output_length(self):
        long = "Please answer with " + "x" * 500
        result = _heuristic_done_spec(long)
        assert len(result) <= 120


# ── _heuristic_constraints ──────────────────────────────────────────────


class TestHeuristicConstraints:
    def test_empty_input_returns_empty(self):
        assert _heuristic_constraints("") == ""

    def test_extracts_must_not_clause(self):
        result = _heuristic_constraints(
            "You must not include duplicates in the list."
        )
        assert "must not" in result.lower()

    def test_extracts_as_of_year(self):
        result = _heuristic_constraints("Studio albums released as of 2009.")
        assert "2009" in result

    def test_extracts_chinese_negation(self):
        result = _heuristic_constraints("不能列入合輯，僅算錄音室專輯。")
        assert result != ""

    def test_caps_at_three_constraints(self):
        text = (
            "must not include A. cannot include B. do not include C. "
            "exclude D. don't include E."
        )
        result = _heuristic_constraints(text)
        # Pipe separator — should never have more than 3 segments
        segments = [s.strip() for s in result.split("|") if s.strip()]
        assert len(segments) <= 3


# ── extract_anchor ──────────────────────────────────────────────────────


class TestExtractAnchor:
    def test_empty_prompt_returns_empty_anchor(self):
        anchor = extract_anchor("")
        assert anchor.is_empty() is True

    def test_short_prompt_returns_summary_at_least(self):
        anchor = extract_anchor("Build a REST API")
        assert anchor.summary != ""
        assert anchor.original == "Build a REST API"

    def test_summary_truncates_at_max_len(self):
        long = "A" * 500
        anchor = extract_anchor(long, summary_max_len=100)
        assert len(anchor.summary) <= 100

    def test_full_prompt_populates_all_fields(self):
        prompt = (
            "Please answer with the integer count of studio albums "
            "released as of 2009. You must not include compilations."
        )
        anchor = extract_anchor(prompt)
        assert anchor.summary != ""
        assert anchor.original == prompt
        assert anchor.done_spec != ""
        assert "2009" in anchor.constraints or "must not" in anchor.constraints.lower()


# ── render_anchor_block ─────────────────────────────────────────────────


class TestRenderAnchorBlock:
    def test_renders_summary_only_when_extras_blank(self):
        anchor = IntentAnchor(summary="Build a thing")
        block = render_anchor_block(anchor)
        assert "原始意圖" in block
        assert "Build a thing" in block
        assert "尚未建立" in block  # placeholder appears when both blank

    def test_includes_done_spec_when_present(self):
        anchor = IntentAnchor(
            summary="Build", done_spec="answer as integer",
        )
        block = render_anchor_block(anchor)
        assert "做完長什麼樣" in block
        assert "answer as integer" in block

    def test_includes_constraints_when_present(self):
        anchor = IntentAnchor(
            summary="Build", constraints="as of 2009",
        )
        block = render_anchor_block(anchor)
        assert "限制" in block
        assert "2009" in block

    def test_no_placeholder_when_either_extra_present(self):
        anchor = IntentAnchor(
            summary="Build", done_spec="answer as integer",
        )
        block = render_anchor_block(anchor)
        assert "尚未建立" not in block

    def test_custom_title(self):
        anchor = IntentAnchor(summary="x")
        block = render_anchor_block(anchor, title="🎯 Stage -1")
        assert block.startswith("🎯 Stage -1")
