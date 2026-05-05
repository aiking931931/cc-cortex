"""Tests for concinno.fidelity_delta — subagent fork fidelity measurement."""

from __future__ import annotations

import pytest

from concinno.fidelity_delta import (
    PRESERVATION_THRESHOLD,
    FidelityDeltaRecord,
    compute_fidelity_delta,
)
from concinno.field_read import ElidedSection

# ── Public API shape ───────────────────────────────────────────────


def test_returns_fidelity_delta_record():
    r = compute_fidelity_delta("analyze the file", "I analyzed the file")
    assert isinstance(r, FidelityDeltaRecord)
    assert hasattr(r, "delta")
    assert hasattr(r, "recall")
    assert hasattr(r, "fields_in")
    assert hasattr(r, "fields_preserved")
    assert hasattr(r, "lost_fields")
    assert hasattr(r, "confidence")
    assert hasattr(r, "summary")


def test_delta_and_recall_always_in_unit_interval():
    for in_msg, out_msg in [
        ("abc", "xyz"),
        ("abc def ghi", "abc"),
        ("", "reply"),
        ("prompt", ""),
        ("- one\n- two\n- three", "one two"),
    ]:
        r = compute_fidelity_delta(in_msg, out_msg)
        assert 0.0 <= r.delta <= 1.0
        assert 0.0 <= r.recall <= 1.0
        assert abs(r.delta + r.recall - 1.0) < 1e-6 or r.fields_in == 0


# ── Perfect preservation ───────────────────────────────────────────


def test_perfect_preservation_zero_delta():
    in_msg = "please analyze the csv file with rows alpha and beta"
    out_msg = "I analyzed the csv file and found rows alpha and beta look fine"
    r = compute_fidelity_delta(in_msg, out_msg)
    assert r.delta == 0.0
    assert r.recall == 1.0
    assert r.lost_fields == []


def test_partial_preservation_reports_loss():
    in_msg = (
        "Please do these things:\n"
        "- Find the capital of France\n"
        "- Compute arithmetic quickly\n"
        "- Summarize the report document"
    )
    out_msg = "The capital of France is Paris."
    r = compute_fidelity_delta(in_msg, out_msg)
    assert 0.5 <= r.delta <= 1.0
    assert len(r.lost_fields) >= 1
    # Each lost field should carry a gist and confidence.
    for lost in r.lost_fields:
        assert isinstance(lost, ElidedSection)
        assert lost.gist
        assert 0.0 <= lost.confidence <= 1.0


# ── Degenerate inputs ──────────────────────────────────────────────


def test_empty_input_returns_zero_delta_low_confidence():
    r = compute_fidelity_delta("", "some reply")
    assert r.delta == 0.0
    assert r.recall == 1.0
    assert r.fields_in == 0
    assert r.confidence == 0.0
    assert "empty" in r.summary


def test_whitespace_only_input_treated_as_empty():
    r = compute_fidelity_delta("   \n\n  ", "reply")
    assert r.fields_in == 0
    assert r.confidence == 0.0


def test_empty_output_on_non_empty_input_is_total_loss():
    r = compute_fidelity_delta(
        "analyze the rows alpha and beta in the attached spreadsheet",
        "",
    )
    assert r.delta == 1.0
    assert r.recall == 0.0
    assert r.fields_in >= 1
    assert r.fields_preserved == 0
    assert all(lost.confidence == 1.0 for lost in r.lost_fields)


def test_whitespace_only_output_is_total_loss():
    r = compute_fidelity_delta("do the thing alpha beta", "   \n  ")
    assert r.delta == 1.0


# ── Structured extraction ──────────────────────────────────────────


def test_bullets_become_separate_fields():
    in_msg = (
        "- alpha\n"
        "- beta\n"
        "- gamma"
    )
    r = compute_fidelity_delta(in_msg, "alpha mentioned in reply")
    # Three distinct bullet fields expected.
    assert r.fields_in == 3
    # Only the alpha bullet is preserved.
    assert r.fields_preserved == 1
    assert len(r.lost_fields) == 2


def test_numbered_list_becomes_separate_fields():
    in_msg = (
        "Tasks:\n"
        "1. Compute alpha statistic\n"
        "2. Compute beta statistic\n"
        "3. Compute gamma statistic"
    )
    r = compute_fidelity_delta(in_msg, "computed alpha statistic successfully")
    assert r.fields_in == 3
    assert r.fields_preserved >= 1


def test_headed_sections_without_bullets_are_single_fields():
    in_msg = (
        "## Goal\nFind the largest prime under one hundred.\n\n"
        "## Constraint\nUse only elementary arithmetic."
    )
    r = compute_fidelity_delta(
        in_msg, "The largest prime under one hundred is ninety-seven.",
    )
    assert r.fields_in == 2
    # Goal section preserved; constraint section not echoed.
    assert r.fields_preserved >= 1


def test_unstructured_prompt_is_single_field():
    in_msg = "Summarize the report about climate science briefly"
    r = compute_fidelity_delta(in_msg, "Climate science report summarized briefly")
    assert r.fields_in == 1
    assert r.fields_preserved == 1


# ── Max field cap ──────────────────────────────────────────────────


def test_max_fields_caps_extraction():
    bullets = "\n".join(f"- item number {i} alpha{i}" for i in range(100))
    r = compute_fidelity_delta(bullets, "item number 0", max_fields=10)
    assert r.fields_in <= 10


def test_max_fields_keeps_heaviest():
    # Three bullets; the heaviest carries more keywords that match the
    # output. With max_fields=1 only the heaviest survives extraction,
    # and the output echoes its keywords fully.
    in_msg = (
        "- tiny\n"
        "- small\n"
        "- extremely elaborate detailed heavyweight bullet"
    )
    r = compute_fidelity_delta(
        in_msg,
        "extremely elaborate detailed heavyweight bullet got handled",
        max_fields=1,
    )
    # Only the heaviest bullet kept.
    assert r.fields_in == 1
    assert r.fields_preserved == 1


# ── Threshold override ─────────────────────────────────────────────


def test_threshold_override_strict_flags_partial_as_lost():
    in_msg = "Find the capital and population of France"
    # Output covers capital but not population.
    out_msg = "The capital of France is Paris"
    strict = compute_fidelity_delta(
        in_msg, out_msg, preservation_threshold=0.99,
    )
    lenient = compute_fidelity_delta(
        in_msg, out_msg, preservation_threshold=0.1,
    )
    assert strict.delta >= lenient.delta


def test_threshold_override_clamped_to_unit_interval():
    # Way-out-of-range threshold should not crash.
    r_low = compute_fidelity_delta("x alpha", "y", preservation_threshold=-5)
    r_high = compute_fidelity_delta("x alpha", "y", preservation_threshold=99)
    assert 0.0 <= r_low.delta <= 1.0
    assert 0.0 <= r_high.delta <= 1.0


# ── Confidence signal ──────────────────────────────────────────────


def test_confidence_scales_with_keyword_density():
    sparse = compute_fidelity_delta("a", "b")  # tiny field, few kws
    dense = compute_fidelity_delta(
        "analyze the climate science report about arctic temperature trends",
        "I analyzed the climate science report and found arctic trends",
    )
    assert dense.confidence > sparse.confidence


def test_confidence_zero_on_empty_input():
    r = compute_fidelity_delta("", "output")
    assert r.confidence == 0.0


# ── Field id & heading survive ──────────────────────────────────────


def test_lost_field_records_carry_stable_ids():
    in_msg = (
        "- alpha aaaa\n"
        "- beta bbbb"
    )
    # Output fully echoes the alpha bullet but never mentions beta.
    r = compute_fidelity_delta(in_msg, "alpha aaaa received and processed")
    # beta is lost; its id must be present and deterministic.
    lost_ids = [lost.id for lost in r.lost_fields]
    assert len(lost_ids) == 1
    # id is stable across calls.
    r2 = compute_fidelity_delta(in_msg, "alpha aaaa received and processed")
    assert [lost.id for lost in r2.lost_fields] == lost_ids


# ── Summary string format ──────────────────────────────────────────


def test_summary_reports_preserved_total():
    r = compute_fidelity_delta(
        "- alpha\n- beta\n- gamma",
        "alpha and beta and gamma all present",
    )
    assert "3/3" in r.summary or "preserved" in r.summary


# ── Constants surface ──────────────────────────────────────────────


def test_preservation_threshold_constant():
    assert 0.0 < PRESERVATION_THRESHOLD < 1.0


# ── Roundtrip: preserve a structured handoff-style prompt ──────────


def test_structured_handoff_style_prompt_survives_echo():
    in_msg = (
        "## next_step\n"
        "- Run ablation alpha on the deduplication pipeline\n\n"
        "## constraints\n"
        "- Do not touch the baseline comparator\n"
        "- Keep batch size exactly at thirty two"
    )
    out_msg = (
        "I will run ablation alpha on the deduplication pipeline without "
        "touching the baseline comparator. Batch size is kept at thirty two."
    )
    r = compute_fidelity_delta(in_msg, out_msg)
    assert r.delta <= 0.25  # mostly preserved
    assert r.fields_preserved >= 2


# ── Dataclass equality (for record-assertion in integration tests) ─


def test_fidelity_record_equality():
    a = FidelityDeltaRecord(
        delta=0.5, recall=0.5,
        fields_in=2, fields_preserved=1,
        lost_fields=[], confidence=0.7,
        summary="1/2 fields preserved (Δ=0.50)",
    )
    b = FidelityDeltaRecord(
        delta=0.5, recall=0.5,
        fields_in=2, fields_preserved=1,
        lost_fields=[], confidence=0.7,
        summary="1/2 fields preserved (Δ=0.50)",
    )
    assert a == b


# ── Sanity: reusing concinno.field_read.ElidedSection ──────────────


def test_lost_field_is_elided_section_shape():
    """lost_fields items must be the FieldRead ElidedSection dataclass."""
    r = compute_fidelity_delta("- alpha thing\n- beta thing", "nothing")
    assert r.lost_fields
    for lost in r.lost_fields:
        # Structural check — reuses the public dataclass.
        assert isinstance(lost, ElidedSection)
        assert isinstance(lost.id, str)
        assert isinstance(lost.heading, str)
        assert isinstance(lost.lines, int)
        assert isinstance(lost.gist, str)
        assert 0.0 <= lost.confidence <= 1.0


# ── Non-ascii / CJK ────────────────────────────────────────────────


def test_cjk_input_handled():
    # FieldRead's keyword extractor treats runs of CJK as a single token,
    # so an exact substring echo will match as a set element. This test
    # just asserts CJK inputs don't crash — exact delta value is a
    # function of the (intentionally coarse) tokenizer.
    r = compute_fidelity_delta(
        "分析檔案, 摘要 報告",
        "我已經 分析檔案 and 摘要 報告 了",
    )
    assert 0.0 <= r.delta <= 1.0
    assert r.fields_in >= 1


# ── Smoke: doesn't raise on adversarial inputs ─────────────────────


@pytest.mark.parametrize("in_msg,out_msg", [
    ("a" * 10_000, "b" * 10_000),
    ("- " * 500, "nothing"),
    ("\n" * 1000, "reply"),
    ("### only heading", "reply"),
])
def test_no_crash_on_edge_inputs(in_msg, out_msg):
    r = compute_fidelity_delta(in_msg, out_msg)
    assert 0.0 <= r.delta <= 1.0
