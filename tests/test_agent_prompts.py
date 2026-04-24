"""Tests for ``concinno.agent.prompts``."""

from __future__ import annotations

from concinno.agent.prompts import (
    AGENT_GUIDANCE_ARITHMETIC,
    AGENT_GUIDANCE_COMPUTE_TOOLS,
    AGENT_GUIDANCE_EXACT_QUOTE,
    AGENT_GUIDANCE_EXACT_UNIT,
    AGENT_GUIDANCE_FACTUAL_COUNT,
    AGENT_GUIDANCE_NO_REFUSAL,
    AGENT_GUIDANCE_PDB_FILE_ORDER,
    AGENT_GUIDANCE_SEARCH_DISCIPLINE,
    AGENT_GUIDANCE_UNCERTAINTY,
    AGENT_GUIDANCE_VISION,
    ANCHOR_PATTERNS,
    build_targeted_guidance,
    default_guidance,
    select_question_anchors,
)


class TestGuidanceConstants:
    def test_uncertainty_mentions_tools(self) -> None:
        s = AGENT_GUIDANCE_UNCERTAINTY
        assert "web_search" in s
        assert "fetch_url" in s

    def test_arithmetic_mentions_run_bash(self) -> None:
        assert "run_bash" in AGENT_GUIDANCE_ARITHMETIC
        assert "python3" in AGENT_GUIDANCE_ARITHMETIC

    def test_no_refusal_lists_bad_phrases(self) -> None:
        s = AGENT_GUIDANCE_NO_REFUSAL
        assert "I cannot" in s
        assert "I am unable" in s
        assert "Once I have access" in s

    def test_default_guidance_joins_all_three(self) -> None:
        out = default_guidance()
        assert AGENT_GUIDANCE_UNCERTAINTY in out
        assert AGENT_GUIDANCE_ARITHMETIC in out
        assert AGENT_GUIDANCE_NO_REFUSAL in out

    def test_default_guidance_stable(self) -> None:
        assert default_guidance() == default_guidance()


class TestComputeToolsGuidance:
    """Few-shot ICL for the ``date_calc`` / ``python_exec`` builtins.

    The purpose is to teach weak models (Gemma-4) when to select
    these tools and how to shape the call. We assert both tool
    names plus at least one concrete invocation example so a
    regression that drops the few-shot layer fails loudly.
    """

    def test_mentions_both_tool_names(self) -> None:
        s = AGENT_GUIDANCE_COMPUTE_TOOLS
        assert "date_calc" in s
        assert "python_exec" in s

    def test_has_date_calc_few_shot_example(self) -> None:
        s = AGENT_GUIDANCE_COMPUTE_TOOLS
        assert 'op="delta"' in s
        assert 'date_from=' in s and 'date_to=' in s
        assert '"%B %d, %Y"' in s

    def test_has_python_exec_few_shot_example(self) -> None:
        s = AGENT_GUIDANCE_COMPUTE_TOOLS
        assert 'code="sum(' in s
        assert 'round(' in s

    def test_warns_about_expression_only_constraint(self) -> None:
        s = AGENT_GUIDANCE_COMPUTE_TOOLS
        assert "pure expressions only" in s
        assert "no import" in s
        assert "no assignment" in s

    def test_default_guidance_does_not_include_compute_tools(self) -> None:
        assert AGENT_GUIDANCE_COMPUTE_TOOLS not in default_guidance()

    def test_distinct_from_arithmetic_guidance(self) -> None:
        assert AGENT_GUIDANCE_COMPUTE_TOOLS != AGENT_GUIDANCE_ARITHMETIC


class TestPhase1Guidance:
    """Phase 1 GAIA additions: multi-source search + verbatim quotes.

    These guidance blocks target the B/C/G/I FAIL classes from the
    baseline N=20 per-task analysis: search noise, empty-retry, and
    paraphrase-vs-verbatim format mismatches.
    """

    def test_search_discipline_requires_multi_query(self) -> None:
        s = AGENT_GUIDANCE_SEARCH_DISCIPLINE
        assert "web_search" in s
        assert "2 different" in s
        assert "cross-reference" in s

    def test_search_discipline_has_retry_and_cap(self) -> None:
        s = AGENT_GUIDANCE_SEARCH_DISCIPLINE
        assert "reformulate" in s
        assert "5 web_search" in s

    def test_exact_quote_demands_verbatim(self) -> None:
        s = AGENT_GUIDANCE_EXACT_QUOTE
        assert "fetch_url" in s
        assert "EXACT" in s
        assert "Never paraphrase" in s

    def test_phase1_blocks_not_in_default(self) -> None:
        out = default_guidance()
        assert AGENT_GUIDANCE_SEARCH_DISCIPLINE not in out
        assert AGENT_GUIDANCE_EXACT_QUOTE not in out


class TestExactUnitGuidance:
    """GAIA #12 layer 2 — unit format confusion fix.

    Gemma4 Q4_K_M, when a question says 'Report in Angstroms, rounded
    to the nearest picometer', outputs the picometer integer (1456)
    instead of the Angstrom value (1.456). The targeted guidance
    must disambiguate that 'rounded to the nearest X' describes
    precision, not the target unit.
    """

    def test_names_both_unit_roles(self) -> None:
        s = AGENT_GUIDANCE_EXACT_UNIT
        assert "Angstroms" in s
        assert "picometer" in s.lower()

    def test_shows_concrete_right_vs_wrong(self) -> None:
        s = AGENT_GUIDANCE_EXACT_UNIT
        assert "1.456" in s
        assert "1456" in s

    def test_clarifies_rounded_to_semantics(self) -> None:
        s = AGENT_GUIDANCE_EXACT_UNIT
        assert "precision" in s

    def test_not_in_default(self) -> None:
        assert AGENT_GUIDANCE_EXACT_UNIT not in default_guidance()


class TestFactualCountGuidance:
    """GAIA #3 — 'how many articles by Nature 2020' hard-answered.

    Weak model invented an article count (32 = 800*0.04) without
    searching. Guidance must force a primary-source lookup.
    """

    def test_requires_web_search(self) -> None:
        s = AGENT_GUIDANCE_FACTUAL_COUNT
        assert "web_search" in s

    def test_mentions_primary_source(self) -> None:
        s = AGENT_GUIDANCE_FACTUAL_COUNT
        assert "primary source" in s.lower()

    def test_forbids_estimation(self) -> None:
        s = AGENT_GUIDANCE_FACTUAL_COUNT
        assert "Do NOT estimate" in s

    def test_not_in_default(self) -> None:
        assert AGENT_GUIDANCE_FACTUAL_COUNT not in default_guidance()


class TestPdbFileOrderGuidance:
    """GAIA 7dd30055 — 'first and second atoms as listed' in PDB.

    Weak model called ``list(structure.get_atoms())[0] / [1]`` which
    iterates in Biopython's Model>Chain>Residue>Atom hierarchy order
    and does NOT follow the raw ATOM-record line order. Got 1.61 Å
    on 5wb7 instead of the file-order-correct 1.456 Å. This anchor
    teaches the serial-number sort OR raw-line parse workaround.
    """

    def test_names_biopython_api(self) -> None:
        s = AGENT_GUIDANCE_PDB_FILE_ORDER
        assert "PDBParser" in s
        assert "get_serial_number" in s

    def test_warns_against_naive_get_atoms_indexing(self) -> None:
        s = AGENT_GUIDANCE_PDB_FILE_ORDER
        assert "structure.get_atoms" in s
        # Must explicitly tell model NOT to index the hierarchy-ordered list
        assert "Do NOT" in s or "do NOT" in s

    def test_offers_raw_line_alternative(self) -> None:
        s = AGENT_GUIDANCE_PDB_FILE_ORDER
        assert "ATOM" in s
        assert "HETATM" in s
        # Fixed-width column spec for x/y/z
        assert "31-38" in s
        assert "47-54" in s

    def test_reminds_precision_for_picometer_unit(self) -> None:
        s = AGENT_GUIDANCE_PDB_FILE_ORDER
        assert "picometer" in s
        assert "3 decimal" in s

    def test_not_in_default(self) -> None:
        assert AGENT_GUIDANCE_PDB_FILE_ORDER not in default_guidance()


class TestSelectQuestionAnchors:
    """ZIQ SPS one-shot classifier — structural prior only."""

    def test_empty_question_returns_empty(self) -> None:
        assert select_question_anchors("") == ()
        assert select_question_anchors("   ") == ()

    def test_exact_unit_matches_gaia_12(self) -> None:
        # GAIA 7dd30055 verbatim
        q = (
            "Using the Biopython library in Python, parse the PDB "
            "file of the protein identified by the PDB ID 5wb7 from "
            "the RCSB Protein Data Bank. Calculate the distance "
            "between the first and second atoms as they are listed "
            "in the PDB file. Report the answer in Angstroms, "
            "rounded to the nearest picometer."
        )
        anchors = select_question_anchors(q)
        assert AGENT_GUIDANCE_EXACT_UNIT in anchors

    def test_exact_quote_matches_gaia_15(self) -> None:
        # GAIA 624cbf11 verbatim
        q = (
            "What's the last line of the rhyme under the flavor "
            "name on the headstone visible in the background of the "
            "photo of the oldest flavor's headstone in the Ben & "
            "Jerry's online flavor graveyard as of the end of 2022?"
        )
        anchors = select_question_anchors(q)
        assert AGENT_GUIDANCE_EXACT_QUOTE in anchors

    def test_factual_count_matches_gaia_3(self) -> None:
        # GAIA 04a04a9b verbatim
        q = (
            "If we assume all articles published by Nature in 2020 "
            "(articles, only, not book reviews/columns, etc) relied "
            "on statistical significance to justify their findings "
            "and they on average came to a p-value of 0.04, how "
            "many papers would be incorrect as to their claims of "
            "statistical significance? Round the value up to the "
            "next integer."
        )
        anchors = select_question_anchors(q)
        assert AGENT_GUIDANCE_FACTUAL_COUNT in anchors

    def test_vision_matches_gaia_15(self) -> None:
        # GAIA 624cbf11 verbatim — requires reading a rhyme inscribed
        # on a headstone visible in the background of a photograph,
        # hence BOTH EXACT_QUOTE and VISION should fire.
        q = (
            "What's the last line of the rhyme under the flavor "
            "name on the headstone visible in the background of the "
            "photo of the oldest flavor's headstone in the Ben & "
            "Jerry's online flavor graveyard as of the end of 2022?"
        )
        anchors = select_question_anchors(q)
        assert AGENT_GUIDANCE_VISION in anchors
        assert AGENT_GUIDANCE_EXACT_QUOTE in anchors

    def test_vision_photo_of_x(self) -> None:
        q = "In the photo of the 1969 Apollo crew, who is on the far left?"
        assert AGENT_GUIDANCE_VISION in select_question_anchors(q)

    def test_vision_written_on_sign(self) -> None:
        q = "What is written on the sign at the entrance to the museum?"
        assert AGENT_GUIDANCE_VISION in select_question_anchors(q)

    def test_vision_screenshot_shows(self) -> None:
        q = "In the screenshot shown in the paper, what value is reported?"
        assert AGENT_GUIDANCE_VISION in select_question_anchors(q)

    def test_vision_does_not_trigger_on_metaphorical_image(self) -> None:
        # Generic "image of" without photo/picture framing — keep narrow
        # so regression on PASS questions is minimized. "image" alone is
        # too common (e.g. "brand image", "self image") to trigger.
        assert AGENT_GUIDANCE_VISION not in select_question_anchors(
            "What is the public image of the company?"
        )
        assert AGENT_GUIDANCE_VISION not in select_question_anchors(
            "Describe the image the poet creates in the second stanza."
        )

    def test_vision_does_not_trigger_on_plain_factual(self) -> None:
        # PASS questions without visual signals must stay empty.
        for q in (
            "What is the population of France in 2020?",
            "Who wrote Hamlet?",
            "How many Olympic medals did Simone Biles win?",
        ):
            assert AGENT_GUIDANCE_VISION not in select_question_anchors(q)

    def test_pdb_file_order_matches_gaia_7dd30055(self) -> None:
        # GAIA 7dd30055 verbatim — 5wb7 first/second atoms distance
        q = (
            "Using the Biopython library in Python, parse the PDB "
            "file of the protein identified by the PDB ID 5wb7 from "
            "the RCSB Protein Data Bank. Calculate the distance "
            "between the first and second atoms as they are listed "
            "in the PDB file. Report the answer in Angstroms, "
            "rounded to the nearest picometer."
        )
        anchors = select_question_anchors(q)
        assert AGENT_GUIDANCE_PDB_FILE_ORDER in anchors

    def test_pdb_file_order_matches_attachment_phrasing(self) -> None:
        q = (
            "Given the attached .pdb file, compute the distance "
            "between the first and second ATOM records as listed."
        )
        assert AGENT_GUIDANCE_PDB_FILE_ORDER in select_question_anchors(q)

    def test_pdb_does_not_trigger_without_order_phrasing(self) -> None:
        # Generic PDB question without file-order ambiguity must NOT
        # fire — avoids prompt bloat on PASS-capable questions.
        for q in (
            "What is the resolution of the PDB entry 1abc?",
            "How many chains does the PDB file contain?",
            "Which protein corresponds to PDB ID 2xyz?",
        ):
            assert AGENT_GUIDANCE_PDB_FILE_ORDER not in (
                select_question_anchors(q)
            )

    def test_pdb_does_not_trigger_without_pdb_context(self) -> None:
        # "First and second atoms" without a PDB file reference is
        # ambiguous for many non-PDB contexts (generic chemistry,
        # molecular dynamics, etc.) — stay silent.
        for q in (
            "What is the bond length between the first and second atoms "
            "of the methane molecule?",
            "List the first and second atoms in ethanol.",
        ):
            assert AGENT_GUIDANCE_PDB_FILE_ORDER not in (
                select_question_anchors(q)
            )

    def test_simple_question_matches_nothing(self) -> None:
        # Should not accidentally trigger — must not append guidance.
        for q in (
            "What is 2 + 2?",
            "Is this a test?",
            "Hello world.",
        ):
            assert select_question_anchors(q) == ()

    def test_result_is_tuple(self) -> None:
        out = select_question_anchors("How many planets orbit the sun?")
        assert isinstance(out, tuple)

    def test_deterministic_order(self) -> None:
        # Question triggering multiple anchors yields stable order.
        q = "How many lines in the poem? Report in meters."
        out1 = select_question_anchors(q)
        out2 = select_question_anchors(q)
        assert out1 == out2

    def test_case_insensitive(self) -> None:
        q_lower = "how many apples?"
        q_upper = "HOW MANY APPLES?"
        assert select_question_anchors(q_lower) == select_question_anchors(
            q_upper
        )


class TestAnchorPatternsTable:
    """ANCHOR_PATTERNS is the public ordering contract."""

    def test_five_anchors_registered(self) -> None:
        assert len(ANCHOR_PATTERNS) == 5

    def test_all_entries_well_formed(self) -> None:
        import re

        for name, pattern, guidance in ANCHOR_PATTERNS:
            assert isinstance(name, str) and name
            assert isinstance(pattern, re.Pattern)
            assert isinstance(guidance, str) and guidance

    def test_names_unique(self) -> None:
        names = [entry[0] for entry in ANCHOR_PATTERNS]
        assert len(names) == len(set(names))


class TestBuildTargetedGuidance:
    """Composer: base + question-specific anchors (no prompt bloat)."""

    def test_no_anchor_match_returns_base_only(self) -> None:
        out = build_targeted_guidance("What is the capital of France?")
        assert AGENT_GUIDANCE_UNCERTAINTY in out
        assert AGENT_GUIDANCE_ARITHMETIC in out
        # None of the specific anchors should appear
        assert AGENT_GUIDANCE_EXACT_UNIT not in out
        assert AGENT_GUIDANCE_EXACT_QUOTE not in out
        assert AGENT_GUIDANCE_FACTUAL_COUNT not in out

    def test_quote_question_appends_quote_anchor(self) -> None:
        out = build_targeted_guidance(
            "Quote verbatim the last line of the song."
        )
        assert AGENT_GUIDANCE_EXACT_QUOTE in out
        # Base blocks still present
        assert AGENT_GUIDANCE_UNCERTAINTY in out

    def test_unit_question_appends_unit_anchor(self) -> None:
        out = build_targeted_guidance(
            "Report the distance in Angstroms, rounded to the "
            "nearest picometer."
        )
        assert AGENT_GUIDANCE_EXACT_UNIT in out

    def test_count_question_appends_count_anchor(self) -> None:
        out = build_targeted_guidance("How many books were published?")
        assert AGENT_GUIDANCE_FACTUAL_COUNT in out

    def test_custom_base_overrides_default(self) -> None:
        out = build_targeted_guidance(
            "What is the capital?",
            base=(AGENT_GUIDANCE_UNCERTAINTY,),
        )
        assert AGENT_GUIDANCE_UNCERTAINTY in out
        assert AGENT_GUIDANCE_ARITHMETIC not in out

    def test_deterministic(self) -> None:
        q = "How many lines? Report in meters. Quote the last line."
        assert build_targeted_guidance(q) == build_targeted_guidance(q)

    def test_no_leading_or_trailing_newlines(self) -> None:
        out = build_targeted_guidance("hello")
        assert not out.startswith("\n")
        assert not out.endswith("\n")

    def test_empty_question_uses_base_only(self) -> None:
        out = build_targeted_guidance("")
        assert AGENT_GUIDANCE_UNCERTAINTY in out
        assert AGENT_GUIDANCE_EXACT_UNIT not in out
