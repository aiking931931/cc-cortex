"""Tests for L1 domain-typed procedure anchors (music / polygon-area /
web-only) and the ``_get_domain_procedure`` dispatcher.

Origin: 2026-04-26 — replaces the L2 totally-generic
``_VISUAL_REASONING_SCAFFOLD`` for question types where Gemma 4 31B
Q4_K_M still fails because generic four-step structure cannot rescue
domain-knowledge errors (bass-clef line vs space confusion, polygon
edge length closure miss, no-attachment web tool dispatch).

Anti-leakage assertions guard against L0 regression: each procedure
body MUST NOT contain GAIA validation answer-path strings (DECADE / 90
/ Dastardly Mash / Ben & Jerry / 39). Anchor design rationale lives
in ``~/.claude/skills/kb_benchmark/generic-anchor-design.md``.
"""
from __future__ import annotations

import sys

import pytest


@pytest.fixture
def gaia_agent_mod(monkeypatch):
    monkeypatch.setenv("HF_TOKEN", "test-dummy")
    sys.modules.pop("concinno.skills.public.agent.gaia_agent", None)
    from concinno.skills.public.agent import gaia_agent
    return gaia_agent


# ── Music notation detection ───────────────────────────────────

class TestMusicDetectPositiveAndNegative:
    def test_positive_bass_clef(self, gaia_agent_mod):
        assert gaia_agent_mod._is_music_notation_question(
            "Read the bass clef and translate the notes to letters."
        )

    def test_negative_no_music_terms(self, gaia_agent_mod):
        assert not gaia_agent_mod._is_music_notation_question(
            "How many apples are on the table?"
        )

    def test_chinese_translated_positive(self, gaia_agent_mod):
        # Multilingual triggers (rules/L1/multilingual_triggers.md):
        # Chinese phrasing with English protocol token "bass clef" still
        # matches. The detector matches on the canonical English domain
        # term, not on the natural-language wrapper.
        assert gaia_agent_mod._is_music_notation_question(
            "請讀取 bass clef 上的音符並翻譯為字母"
        )


# ── Orthogonal polygon-area detection ──────────────────────────

class TestPolygonAreaDetect:
    def test_positive_area_with_polygon(self, gaia_agent_mod):
        assert gaia_agent_mod._is_orthogonal_polygon_area_question(
            "What is the area of this polygon in square cm?"
        )

    def test_positive_area_with_label_cue(self, gaia_agent_mod):
        assert gaia_agent_mod._is_orthogonal_polygon_area_question(
            "Compute the total area of the shape using the side "
            "length labels shown."
        )

    def test_negative_polygon_count_not_area(self, gaia_agent_mod):
        # Edge-counting question — should NOT trigger the area
        # procedure (handled by the existing polygon_counting flow).
        assert not gaia_agent_mod._is_orthogonal_polygon_area_question(
            "How many edges does this polygon have?"
        )

    def test_negative_unrelated(self, gaia_agent_mod):
        assert not gaia_agent_mod._is_orthogonal_polygon_area_question(
            "Who painted the Mona Lisa?"
        )

    def test_chinese_translated_positive(self, gaia_agent_mod):
        # Mixed Chinese question with English domain protocol terms
        # ("area" + "polygon"). Multilingual triggers rule: match by
        # canonical English term, not surface natural-language string.
        assert gaia_agent_mod._is_orthogonal_polygon_area_question(
            "請計算這個 polygon 的 area，單位為 cm。"
        )


# ── No-attachment web-only detection ───────────────────────────

class TestWebOnlyDetect:
    def test_positive_as_of_temporal(self, gaia_agent_mod):
        assert gaia_agent_mod._is_web_only_question(
            "As of 2022, what was the oldest flavor in the Ben "
            "& Jerry's flavor graveyard?",
            file_path="",
        )

    def test_positive_visible_on_url(self, gaia_agent_mod):
        assert gaia_agent_mod._is_web_only_question(
            "Find the headline visible on https://example.com today.",
            file_path=None,
        )

    def test_positive_two_proper_nouns(self, gaia_agent_mod):
        assert gaia_agent_mod._is_web_only_question(
            "Who founded Microsoft Research in Cambridge?",
            file_path="",
        )

    def test_negative_has_attachment_blocks(self, gaia_agent_mod):
        # Even with strong web cues, an attachment short-circuits
        # the web-only routing — file content is the primary source.
        assert not gaia_agent_mod._is_web_only_question(
            "As of 2022, what does the attached document say?",
            file_path="/tmp/somefile.pdf",
        )

    def test_negative_generic_no_cues(self, gaia_agent_mod):
        assert not gaia_agent_mod._is_web_only_question(
            "How many apples are on the table?",
            file_path="",
        )

    def test_chinese_translated_positive(self, gaia_agent_mod):
        # Chinese natural-language wrapper around an English temporal
        # qualifier + named entity — still triggers via "as of".
        assert gaia_agent_mod._is_web_only_question(
            "請查詢：as of 2023 年底，OpenAI 的 CEO 是誰？",
            file_path="",
        )


# ── Dispatcher precedence ──────────────────────────────────────

class TestDomainProcedureDispatcher:
    def test_music_takes_precedence(self, gaia_agent_mod):
        out = gaia_agent_mod._get_domain_procedure(
            "Bass clef notes — translate to letters.",
            file_path="/tmp/some.png",
        )
        assert out == gaia_agent_mod._MUSIC_NOTATION_PROCEDURE

    def test_polygon_area_routes_to_polygon_anchor(self, gaia_agent_mod):
        out = gaia_agent_mod._get_domain_procedure(
            "Compute the total area of this orthogonal polygon "
            "using the labels (cm).",
            file_path="/tmp/poly.png",
        )
        assert out == gaia_agent_mod._ORTHOGONAL_POLYGON_PROCEDURE

    def test_web_only_routes_when_no_file(self, gaia_agent_mod):
        out = gaia_agent_mod._get_domain_procedure(
            "As of 2022, what is the population of Tokyo?",
            file_path="",
        )
        assert out == gaia_agent_mod._WEB_ONLY_PROCEDURE

    def test_generic_image_falls_back_to_scaffold(self, gaia_agent_mod):
        # An image attached but no domain-specific cue — fall through
        # to the L2 generic visual-reasoning scaffold.
        out = gaia_agent_mod._get_domain_procedure(
            "What color is the cat in this image?",
            file_path="/tmp/cat.png",
        )
        assert out == gaia_agent_mod._VISUAL_REASONING_SCAFFOLD

    def test_no_image_no_web_cue_returns_empty(self, gaia_agent_mod):
        # Pure factual question, no attachment, no temporal /
        # named-entity cues — no anchor, ReAct reasoning unmodified.
        out = gaia_agent_mod._get_domain_procedure(
            "How many apples are on the table?",
            file_path="",
        )
        assert out == ""


# ── Anti-leakage: anchors must NOT contain GAIA answer paths ───

# These strings appear in GAIA validation answers / annotator-step
# specifics — if any leaks into anchor text, that's L0 (cheating).
# Detection guards against accidental regression to the pre-2.24.0
# bass-clef-hardcoded prompt.
#
# Note on case sensitivity: ``DECADE`` (all-caps) is the spelled-out
# puzzle word from 8f80e01c — that exact form is leakage. The
# lowercase dictionary noun "decade" (and its definition "10 years")
# is generic textbook time-unit knowledge per
# ``generic-anchor-design.md`` §case 1, and IS allowed in the L1
# music anchor. Hence case-sensitive substring check below.
_FORBIDDEN_LEAKS = (
    "DECADE",          # 8f80e01c puzzle word (all-caps) — leakage
    "Dastardly Mash",  # 624cbf11 oldest flavor entity
    "Ben & Jerry",     # 624cbf11 brand mention as solution route
    "Sweet Potato",    # 624cbf11 headstone-decipher entity
    "let it die",      # 624cbf11 final answer phrase
)

# Numeric leaks need word-boundary care: "39" appears legitimately
# in unrelated text. The check below applies only to standalone
# numeric tokens in the anchor body.
_FORBIDDEN_NUMERIC = ("39", "90")


def _has_forbidden_string_leak(body: str) -> str | None:
    # Case-sensitive: see _FORBIDDEN_LEAKS comment above.
    for needle in _FORBIDDEN_LEAKS:
        if needle in body:
            return needle
    return None


def _has_forbidden_numeric_leak(body: str) -> str | None:
    import re

    for needle in _FORBIDDEN_NUMERIC:
        # Match as standalone numeric token not part of another number.
        if re.search(rf"(?<!\d){re.escape(needle)}(?!\d)", body):
            return needle
    return None


class TestAntiLeakage:
    def test_music_procedure_no_string_leak(self, gaia_agent_mod):
        body = gaia_agent_mod._MUSIC_NOTATION_PROCEDURE
        assert _has_forbidden_string_leak(body) is None, (
            f"music procedure leaked GAIA answer path: "
            f"{_has_forbidden_string_leak(body)!r}"
        )

    def test_music_procedure_no_numeric_leak(self, gaia_agent_mod):
        body = gaia_agent_mod._MUSIC_NOTATION_PROCEDURE
        leak = _has_forbidden_numeric_leak(body)
        assert leak is None, (
            f"music procedure leaked numeric answer-path: {leak!r}"
        )

    def test_polygon_procedure_no_string_leak(self, gaia_agent_mod):
        body = gaia_agent_mod._ORTHOGONAL_POLYGON_PROCEDURE
        assert _has_forbidden_string_leak(body) is None

    def test_polygon_procedure_no_numeric_leak(self, gaia_agent_mod):
        body = gaia_agent_mod._ORTHOGONAL_POLYGON_PROCEDURE
        leak = _has_forbidden_numeric_leak(body)
        assert leak is None, (
            f"polygon procedure leaked numeric answer-path: {leak!r}"
        )

    def test_web_only_procedure_no_string_leak(self, gaia_agent_mod):
        body = gaia_agent_mod._WEB_ONLY_PROCEDURE
        assert _has_forbidden_string_leak(body) is None

    def test_web_only_procedure_no_numeric_leak(self, gaia_agent_mod):
        body = gaia_agent_mod._WEB_ONLY_PROCEDURE
        leak = _has_forbidden_numeric_leak(body)
        assert leak is None


# ── Feature toggle behavior ────────────────────────────────────

class TestFeatureToggleFallthrough:
    def test_music_toggle_off_still_routes_when_polygon_matches(
        self, gaia_agent_mod, monkeypatch,
    ):
        # Music toggle OFF + a music-only question + image → fall
        # through past music to scaffold (polygon-area / web-only do
        # not match a music question), NOT to empty string.
        def _fake(name, default=True):
            if name == "gaia_music_procedure_anchor":
                return False
            return True

        monkeypatch.setattr(gaia_agent_mod, "_feature_enabled", _fake)
        out = gaia_agent_mod._get_domain_procedure(
            "Read the bass clef and identify the noteheads.",
            file_path="/tmp/img.png",
        )
        # Fall-through: not the music anchor; eventual scaffold.
        assert out != gaia_agent_mod._MUSIC_NOTATION_PROCEDURE
        assert out == gaia_agent_mod._VISUAL_REASONING_SCAFFOLD

    def test_polygon_toggle_off_falls_through_to_scaffold(
        self, gaia_agent_mod, monkeypatch,
    ):
        def _fake(name, default=True):
            if name == "gaia_polygon_area_procedure_anchor":
                return False
            return True

        monkeypatch.setattr(gaia_agent_mod, "_feature_enabled", _fake)
        out = gaia_agent_mod._get_domain_procedure(
            "Compute the area of this polygon (cm).",
            file_path="/tmp/img.png",
        )
        assert out != gaia_agent_mod._ORTHOGONAL_POLYGON_PROCEDURE
        assert out == gaia_agent_mod._VISUAL_REASONING_SCAFFOLD

    def test_web_toggle_off_returns_empty_when_no_image(
        self, gaia_agent_mod, monkeypatch,
    ):
        def _fake(name, default=True):
            if name == "gaia_web_only_procedure_anchor":
                return False
            return True

        monkeypatch.setattr(gaia_agent_mod, "_feature_enabled", _fake)
        out = gaia_agent_mod._get_domain_procedure(
            "As of 2022, what is the population of Tokyo?",
            file_path="",
        )
        # No file, no other anchor matches — empty string (no scaffold
        # fallback when there is no image to reason about).
        assert out == ""

    def test_all_toggles_off_returns_empty_or_scaffold_only(
        self, gaia_agent_mod, monkeypatch,
    ):
        # All three L1 toggles off — only the scaffold remains, gated
        # solely by file_path presence.
        monkeypatch.setattr(
            gaia_agent_mod, "_feature_enabled",
            lambda name, default=True: False,
        )
        with_image = gaia_agent_mod._get_domain_procedure(
            "Read the bass clef notes.",
            file_path="/tmp/img.png",
        )
        no_image = gaia_agent_mod._get_domain_procedure(
            "As of 2022, what is the population of Tokyo?",
            file_path="",
        )
        assert with_image == gaia_agent_mod._VISUAL_REASONING_SCAFFOLD
        assert no_image == ""
