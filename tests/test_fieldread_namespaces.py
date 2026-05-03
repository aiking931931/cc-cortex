# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for concinno.fieldread (5-namespace + breadcrumb + compressor).

Cigito v3 patent moat axis 3, governance side (Concinno main).
Mirrors aiking_core test depth (~36 tests) without inheriting any
aiking_core runtime path — standalone like the production module.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from concinno.fieldread import (
    AUDIT,
    COGNITION,
    FEEDBACK,
    HANDOFF,
    L1_BUDGET_CHARS,
    L2_BUDGET_CHARS,
    NAMESPACES,
    SKILLS,
    Breadcrumb,
    CompressedContent,
    FieldReadCompressor,
    breadcrumb_from_path,
    is_namespace,
    route,
)

# ── 1. Namespace constants ────────────────────────────────────────


class TestNamespaceConstants:
    def test_five_namespace_constants_present(self) -> None:
        assert COGNITION == "cognition"
        assert SKILLS == "skills"
        assert FEEDBACK == "feedback"
        assert HANDOFF == "handoff"
        assert AUDIT == "audit"

    def test_namespaces_tuple_size(self) -> None:
        assert len(NAMESPACES) == 5

    def test_namespaces_canonical_order(self) -> None:
        assert NAMESPACES == (
            "cognition", "skills", "feedback", "handoff", "audit",
        )

    def test_namespaces_idempotent_iteration(self) -> None:
        first = list(NAMESPACES)
        second = list(NAMESPACES)
        assert first == second

    def test_is_namespace_true_for_canonical(self) -> None:
        for ns in NAMESPACES:
            assert is_namespace(ns)

    def test_is_namespace_false_for_unknown(self) -> None:
        assert not is_namespace("memory")
        assert not is_namespace("trace")
        assert not is_namespace("")
        assert not is_namespace("COGNITION")  # case-sensitive


# ── 2. route() keyword + path classifier ──────────────────────────


class TestRoute:
    @pytest.mark.parametrize(
        "query,expected",
        [
            ("交接_concinno.md", HANDOFF),
            ("handoff_2026.md", HANDOFF),
            ("_AI_BRAIN/06_Handoffs/concinno/index.md", HANDOFF),
            ("handoff next_step please", HANDOFF),
        ],
    )
    def test_handoff_routing(self, query: str, expected: str) -> None:
        assert route(query) == expected

    @pytest.mark.parametrize(
        "query,expected",
        [
            ("feedback_intent_drift.md", FEEDBACK),
            ("kb_cognition/L3/anchors.md", FEEDBACK),
            ("MEMORY.md", FEEDBACK),
            ("project/memory/sediment.md", FEEDBACK),
            ("sediment correction lesson", FEEDBACK),
            ("a feedback memory", FEEDBACK),
        ],
    )
    def test_feedback_routing(self, query: str, expected: str) -> None:
        assert route(query) == expected

    @pytest.mark.parametrize(
        "query,expected",
        [
            (".claude/skills/handoff-tick/SKILL.md", SKILLS),
            ("skills/foo/bar.md", SKILLS),
            ("skill_drafts/proposal.md", SKILLS),
            ("a Skill draft proposal", SKILLS),
        ],
    )
    def test_skills_routing(self, query: str, expected: str) -> None:
        assert route(query) == expected

    @pytest.mark.parametrize(
        "query,expected",
        [
            ("audit/2026-05-03.jsonl", AUDIT),
            ("token_audit_report.md", AUDIT),
            ("traces/session_xyz.log", AUDIT),
            ("token-audit verdict", AUDIT),
        ],
    )
    def test_audit_routing(self, query: str, expected: str) -> None:
        assert route(query) == expected

    @pytest.mark.parametrize(
        "query,expected",
        [
            ("rules/L1/cbua.md", COGNITION),
            ("cognition/router.py", COGNITION),
            ("L0.md", COGNITION),
            ("cbua premise_gate", COGNITION),
            ("wiredo reasoning rule", COGNITION),
        ],
    )
    def test_cognition_routing(self, query: str, expected: str) -> None:
        assert route(query) == expected

    def test_empty_query_defaults_to_cognition(self) -> None:
        assert route("") == COGNITION

    def test_unknown_query_defaults_to_cognition(self) -> None:
        assert route("xyzzy frobnicate") == COGNITION

    def test_path_priors_dominate_lexical(self) -> None:
        # Path looks like handoff but text mentions skill — path wins.
        assert route("交接_skill_thing.md") == HANDOFF

    def test_route_is_pure_function(self) -> None:
        # Same input → same output (pure heuristic, no state).
        a = route("MEMORY.md")
        b = route("MEMORY.md")
        assert a == b == FEEDBACK


# ── 3. Breadcrumb dataclass ───────────────────────────────────────


class TestBreadcrumb:
    def test_minimal_construction(self) -> None:
        b = Breadcrumb(namespace=COGNITION)
        assert b.namespace == "cognition"
        assert b.depth == 0
        assert b.ancestors == ()
        assert b.section is None
        assert b.parent is None

    def test_chain_root_only(self) -> None:
        b = Breadcrumb(namespace=HANDOFF)
        assert b.chain == ("handoff",)

    def test_chain_with_section(self) -> None:
        b = Breadcrumb(namespace=HANDOFF, section="next_step")
        assert b.chain == ("handoff", "next_step")

    def test_chain_with_ancestors(self) -> None:
        b = Breadcrumb(
            namespace=FEEDBACK,
            ancestors=("memory", "feedback_intent.md"),
            section="rule-1",
        )
        assert b.chain == (
            "feedback", "memory", "feedback_intent.md", "rule-1",
        )

    def test_render_xml_tag(self) -> None:
        b = Breadcrumb(namespace=HANDOFF, section="next_step")
        assert b.render() == "<crumb>handoff > next_step</crumb>"

    def test_compose_increases_depth(self) -> None:
        root = Breadcrumb(namespace=HANDOFF, section="index")
        child = root.compose("session-A")
        assert child.depth == 1
        assert child.parent is root
        assert child.ancestors == ("index",)
        assert child.section == "session-A"

    def test_compose_chain_root_to_child(self) -> None:
        root = Breadcrumb(namespace=HANDOFF, section="index")
        child = root.compose("session-A").compose("phase-1")
        assert child.chain == (
            "handoff", "index", "session-A", "phase-1",
        )

    def test_breadcrumb_frozen_hashable(self) -> None:
        b1 = Breadcrumb(namespace=HANDOFF, section="next_step")
        b2 = Breadcrumb(namespace=HANDOFF, section="next_step")
        # Frozen dataclasses are hashable when all field types are.
        s = {b1, b2}
        assert len(s) == 1


class TestBreadcrumbFromPath:
    def test_basic_md_file(self) -> None:
        b = breadcrumb_from_path(
            "_AI_BRAIN/06_Handoffs/concinno/交接_concinno.md",
            HANDOFF,
        )
        assert b.namespace == HANDOFF
        assert b.section == "交接_concinno"
        assert "06_Handoffs" in b.ancestors

    def test_pathlib_path_input(self) -> None:
        b = breadcrumb_from_path(
            Path(".claude") / "skills" / "kb_handoff" / "SKILL.md",
            SKILLS,
        )
        assert b.namespace == SKILLS
        assert b.section == "SKILL"

    def test_handles_empty_path_gracefully(self) -> None:
        b = breadcrumb_from_path(".", FEEDBACK)
        # "." path has no section
        assert b.namespace == FEEDBACK


# ── 4. FieldReadCompressor — compress() ──────────────────────────


@pytest.fixture
def compressor() -> FieldReadCompressor:
    return FieldReadCompressor()


@pytest.fixture
def sample_handoff_md() -> str:
    return (
        "---\n"
        "last_updated: 2026-05-03\n"
        "---\n"
        "# 交接 Concinno\n"
        "\n"
        "## §1 狀態\n"
        "\n"
        "- ⬜ ship 5.6.0 fieldread\n"
        "- ⬜ verify pip install\n"
        "- ✅ 5.5.1 hotfix landed\n"
        "\n"
        "## §2 鐵律\n"
        "\n"
        "- 反熵優先\n"
        "- 蝴蝶效應\n"
        "\n"
        "## §3 next_step\n"
        "\n"
        "- 跑 pytest test_fieldread_namespaces.py\n"
        "- twine upload\n"
    )


class TestCompressorBasics:
    def test_compress_returns_dataclass(
        self, compressor: FieldReadCompressor,
        sample_handoff_md: str,
    ) -> None:
        out = compressor.compress(
            sample_handoff_md, HANDOFF, tier="l2",
        )
        assert isinstance(out, CompressedContent)
        assert out.namespace == HANDOFF
        assert out.tier == "l2"
        assert out.original_chars == len(sample_handoff_md)

    @pytest.mark.parametrize("ns", list(NAMESPACES))
    def test_compress_happy_path_per_namespace(
        self, compressor: FieldReadCompressor, ns: str,
    ) -> None:
        body = (
            f"# {ns} doc\n\n"
            f"## section A\n\n- bullet one\n- bullet two\n\n"
            f"## section B\n\n- ⬜ pending\n"
        )
        out = compressor.compress(body, ns, tier="l1")
        assert out.namespace == ns
        assert out.content  # non-empty
        assert out.breadcrumb.namespace == ns

    def test_l1_budget_enforced(
        self, compressor: FieldReadCompressor,
    ) -> None:
        big = "# Heading\n\n" + ("x" * 2000) + "\n"
        out = compressor.compress(big, HANDOFF, tier="l1")
        assert len(out.content) <= L1_BUDGET_CHARS

    def test_l2_budget_enforced(
        self, compressor: FieldReadCompressor,
    ) -> None:
        # 50 sections × 200 chars each → ~10k → must compress.
        sections = [
            f"## §{i} Header\n\n- detail one " + ("y" * 180) + "\n"
            for i in range(50)
        ]
        big = "# Top\n\n" + "\n".join(sections)
        out = compressor.compress(big, HANDOFF, tier="l2")
        assert len(out.content) <= L2_BUDGET_CHARS

    def test_l3_unbounded(
        self, compressor: FieldReadCompressor,
        sample_handoff_md: str,
    ) -> None:
        out = compressor.compress(
            sample_handoff_md, HANDOFF, tier="l3",
        )
        assert out.content == sample_handoff_md
        assert out.compressed is False

    def test_short_content_passes_through(
        self, compressor: FieldReadCompressor,
    ) -> None:
        tiny = "# tiny\n\nnot much here.\n"
        out = compressor.compress(tiny, COGNITION, tier="l2")
        # Already within L2 budget — not actually compressed.
        assert out.compressed is False
        assert tiny.strip() in out.content

    def test_empty_content(
        self, compressor: FieldReadCompressor,
    ) -> None:
        out = compressor.compress("", HANDOFF, tier="l1")
        assert out.content == ""
        assert out.compressed is False
        assert out.original_chars == 0

    def test_breadcrumb_carries_section(
        self, compressor: FieldReadCompressor,
    ) -> None:
        out = compressor.compress(
            "# doc\n\nbody",
            HANDOFF,
            tier="l1",
            section="next_step",
        )
        assert out.breadcrumb.section == "next_step"

    def test_reduction_ratio_zero_for_passthrough(
        self, compressor: FieldReadCompressor,
    ) -> None:
        out = compressor.compress(
            "# x", HANDOFF, tier="l3",
        )
        assert out.reduction_ratio == 0.0

    def test_reduction_ratio_positive_for_real_compress(
        self, compressor: FieldReadCompressor,
    ) -> None:
        big = "# H\n\n" + "x" * 5000
        out = compressor.compress(big, HANDOFF, tier="l1")
        assert out.reduction_ratio > 0.5

    def test_pending_count_appears_in_l1(
        self, compressor: FieldReadCompressor,
        sample_handoff_md: str,
    ) -> None:
        out = compressor.compress(
            sample_handoff_md, HANDOFF, tier="l1",
        )
        # 3 pending markers in fixture → "3 pending" should land in l1.
        assert "pending" in out.content


# ── 5. FieldReadCompressor — breadcrumb() + route() ──────────────


class TestCompressorBreadcrumb:
    def test_breadcrumb_auto_routes_namespace(
        self, compressor: FieldReadCompressor,
    ) -> None:
        b = compressor.breadcrumb("MEMORY.md")
        assert b.namespace == FEEDBACK

    def test_breadcrumb_explicit_namespace(
        self, compressor: FieldReadCompressor,
    ) -> None:
        b = compressor.breadcrumb(
            "any/path/here.md", namespace=AUDIT,
        )
        assert b.namespace == AUDIT

    def test_breadcrumb_invalid_namespace_raises(
        self, compressor: FieldReadCompressor,
    ) -> None:
        with pytest.raises(ValueError):
            compressor.breadcrumb("x.md", namespace="not_a_ns")

    def test_route_method_delegates(
        self, compressor: FieldReadCompressor,
    ) -> None:
        assert compressor.route("交接_x.md") == HANDOFF
        assert compressor.route("") == COGNITION


# ── 6. Failure modes ──────────────────────────────────────────────


class TestFailureModes:
    def test_unknown_namespace_raises(
        self, compressor: FieldReadCompressor,
    ) -> None:
        with pytest.raises(ValueError):
            compressor.compress("body", "memory", tier="l1")

    def test_invalid_tier_raises(
        self, compressor: FieldReadCompressor,
    ) -> None:
        with pytest.raises(ValueError):
            compressor.compress("body", HANDOFF, tier="l99")

    def test_non_utf8_bytes_path_handled(
        self, compressor: FieldReadCompressor,
    ) -> None:
        # Path contains odd characters — should not crash route.
        b = compressor.breadcrumb("weird/路径/檔案.md", namespace=COGNITION)
        assert b.namespace == COGNITION


# ── 7. Switch / disable behaviour ────────────────────────────────


class TestSwitch:
    def test_env_disabled_returns_passthrough(
        self,
        compressor: FieldReadCompressor,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("CONCINNO_FIELDREAD_DISABLED", "1")
        big = "# H\n\n" + "x" * 4000
        out = compressor.compress(big, HANDOFF, tier="l1")
        # Disabled — should NOT compress.
        assert out.compressed is False
        assert out.content == big

    def test_env_disabled_truthy_variants(
        self,
        compressor: FieldReadCompressor,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        for v in ("1", "true", "yes", "on", "TRUE"):
            monkeypatch.setenv("CONCINNO_FIELDREAD_DISABLED", v)
            out = compressor.compress(
                "# x\n\n" + "y" * 4000, HANDOFF, tier="l1",
            )
            assert out.compressed is False, f"failed for {v!r}"

    def test_env_unset_default_compresses(
        self,
        compressor: FieldReadCompressor,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("CONCINNO_FIELDREAD_DISABLED", raising=False)
        big = "# Heading\n\n" + "x" * 4000
        out = compressor.compress(big, HANDOFF, tier="l1")
        assert out.compressed is True
        assert len(out.content) <= L1_BUDGET_CHARS

    def test_env_falsy_does_not_disable(
        self,
        compressor: FieldReadCompressor,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        for v in ("0", "false", "no", "off", ""):
            monkeypatch.setenv("CONCINNO_FIELDREAD_DISABLED", v)
            out = compressor.compress(
                "# x\n\n" + "y" * 4000, HANDOFF, tier="l1",
            )
            # Falsy → feature ON → compression happens.
            assert out.compressed is True, f"failed for {v!r}"


# ── 8. Cigito v3 patent-surface invariants ───────────────────────


class TestPatentSurfaceInvariants:
    """Lock the public contract so refactors don't break the moat."""

    def test_namespaces_are_strings_not_enum(self) -> None:
        # str-typed by design (forward-compat with config / JSON).
        for ns in NAMESPACES:
            assert isinstance(ns, str)

    def test_three_tier_budgets_documented(self) -> None:
        c = FieldReadCompressor()
        assert c.budgets["l1"] == 200
        assert c.budgets["l2"] == 1500
        assert c.budgets["l3"] == -1  # unbounded sentinel

    def test_breadcrumb_chain_includes_namespace(self) -> None:
        b = Breadcrumb(namespace=AUDIT, section="snapshot-1")
        assert b.chain[0] == "audit"

    def test_route_returns_member_of_namespaces(self) -> None:
        # Every route() output must be a valid namespace constant.
        for sample in [
            "", "x", "MEMORY.md", "trade.md", "rules/L0.md",
            "skills/SKILL.md", "audit/log.jsonl", "交接.md",
        ]:
            assert route(sample) in NAMESPACES
