"""Tests for concinno.decision.redblue — architect red/blue decision flow."""

from __future__ import annotations

import pytest

from concinno.decision import RedBlueDecision, adjudicate, build_redblue_prompt

# ── Dataclass validation ────────────────────────────────────


class TestRedBlueDecisionCtor:
    def test_minimal_construction(self) -> None:
        d = RedBlueDecision(proposal="ship v2", radius="low")
        assert d.proposal == "ship v2"
        assert d.radius == "low"
        assert d.commander_verdict == "CONDITIONAL_GO"
        assert d.red_attacks == []
        assert d.blue_defense == []
        assert d.must_run_experiments == []
        assert d.prior_art_urls == []

    def test_full_construction(self) -> None:
        d = RedBlueDecision(
            proposal="monkey-patch apply_rotary_pos_emb",
            radius="high",
            red_attacks=[
                {
                    "attack": "brittle to transformers upgrade",
                    "evidence": "transformers 5.x removed position_ids",
                    "severity": "HIGH",
                }
            ],
            blue_defense=[
                {
                    "claim": "uses *args/**kwargs for version compat",
                    "evidence": "kv_prerope.py:88",
                }
            ],
            commander_verdict="CONDITIONAL_GO",
            must_run_experiments=["4.x compat smoke test", "5.x compat smoke test"],
        )
        assert d.radius == "high"
        assert len(d.red_attacks) == 1
        assert len(d.must_run_experiments) == 2

    def test_invalid_radius_raises(self) -> None:
        with pytest.raises(ValueError, match="radius must be in"):
            RedBlueDecision(proposal="x", radius="huge")  # type: ignore[arg-type]

    def test_invalid_verdict_raises(self) -> None:
        with pytest.raises(ValueError, match="commander_verdict must be in"):
            RedBlueDecision(
                proposal="x", radius="low", commander_verdict="MAYBE"  # type: ignore[arg-type]
            )

    def test_invalid_severity_raises(self) -> None:
        with pytest.raises(ValueError, match="severity must be in"):
            RedBlueDecision(
                proposal="x",
                radius="low",
                red_attacks=[
                    {"attack": "a", "evidence": "b", "severity": "DOOM"}
                ],
            )

    def test_missing_red_attack_field_raises(self) -> None:
        with pytest.raises(ValueError, match="missing keys"):
            RedBlueDecision(
                proposal="x",
                radius="low",
                red_attacks=[{"attack": "a", "severity": "HIGH"}],  # no evidence
            )

    def test_missing_blue_defense_field_raises(self) -> None:
        with pytest.raises(ValueError, match="missing keys"):
            RedBlueDecision(
                proposal="x",
                radius="low",
                blue_defense=[{"claim": "c"}],  # no evidence
            )

    def test_red_attack_not_dict_raises(self) -> None:
        with pytest.raises(TypeError):
            RedBlueDecision(
                proposal="x",
                radius="low",
                red_attacks=["string not dict"],  # type: ignore[list-item]
            )


# ── Prompt builder ──────────────────────────────────────────


class TestBuildRedbluePrompt:
    def test_red_prompt_mentions_attack(self) -> None:
        p = build_redblue_prompt("red", "ship feature X", {"budget": "$10"})
        assert "architecture attacker" in p
        assert "Goodhart" in p
        assert "ship feature X" in p
        assert "budget: $10" in p

    def test_blue_prompt_mentions_defend(self) -> None:
        p = build_redblue_prompt("blue", "ship feature X", {})
        assert "architecture defender" in p
        assert "ship feature X" in p
        assert "(none provided)" in p

    def test_invalid_role_raises(self) -> None:
        with pytest.raises(ValueError, match="role must be"):
            build_redblue_prompt("green", "x")  # type: ignore[arg-type]

    def test_empty_proposal_raises(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            build_redblue_prompt("red", "   ")

    def test_context_none_ok(self) -> None:
        p = build_redblue_prompt("red", "x", None)
        assert "(none provided)" in p

    def test_context_rendered_as_kv(self) -> None:
        p = build_redblue_prompt(
            "red", "x", {"files": "a.py b.py", "prior_art": "none found"}
        )
        assert "files: a.py b.py" in p
        assert "prior_art: none found" in p


# ── Commander adjudicate ────────────────────────────────────


class TestAdjudicate:
    def test_accepts_evidence_based_attack(self) -> None:
        d = RedBlueDecision(
            proposal="x",
            radius="high",
            red_attacks=[
                {
                    "attack": "breaks on upgrade",
                    "evidence": "transformers/models/llama/modeling_llama.py:231",
                    "severity": "HIGH",
                }
            ],
            commander_verdict="CONDITIONAL_GO",
        )
        summary = adjudicate(d)
        assert "accepted (1)" in summary
        assert "[HIGH] breaks on upgrade" in summary
        assert "rejected" not in summary.lower() or "rejected (0" not in summary

    def test_rejects_vibes_attack(self) -> None:
        d = RedBlueDecision(
            proposal="x",
            radius="high",
            red_attacks=[
                {"attack": "feels wrong", "evidence": "idk", "severity": "WEAK"}
            ],
            commander_verdict="GO",
        )
        summary = adjudicate(d)
        # evidence "idk" is < 8 chars → rejected bucket
        assert "rejected (1" in summary

    def test_kill_without_prior_art_warns(self) -> None:
        d = RedBlueDecision(
            proposal="x",
            radius="high",
            red_attacks=[
                {"attack": "bad", "evidence": "vague", "severity": "MED"}
            ],
            commander_verdict="KILL",
        )
        summary = adjudicate(d)
        assert "WARNING" in summary
        assert "prior_art_urls" in summary

    def test_kill_with_prior_art_no_warning(self) -> None:
        d = RedBlueDecision(
            proposal="reinvent OBCache",
            radius="high",
            commander_verdict="KILL",
            prior_art_urls=["https://arxiv.org/abs/2510.07651"],
        )
        summary = adjudicate(d)
        assert "WARNING" not in summary
        assert "arxiv.org/abs/2510.07651" in summary

    def test_kill_with_fatal_attack_no_warning(self) -> None:
        d = RedBlueDecision(
            proposal="x",
            radius="high",
            red_attacks=[
                {
                    "attack": "already patented",
                    "evidence": "USPTO patent 10,xxx,xxx filed 2023",
                    "severity": "FATAL",
                }
            ],
            commander_verdict="KILL",
        )
        summary = adjudicate(d)
        assert "WARNING" not in summary

    def test_must_run_experiments_listed(self) -> None:
        d = RedBlueDecision(
            proposal="x",
            radius="high",
            commander_verdict="CONDITIONAL_GO",
            must_run_experiments=["N=30 seed sweep", "v5.x compat test"],
        )
        summary = adjudicate(d)
        assert "MUST-RUN EXPERIMENTS" in summary
        assert "N=30 seed sweep" in summary

    def test_blue_defense_rendered(self) -> None:
        d = RedBlueDecision(
            proposal="x",
            radius="medium",
            blue_defense=[
                {"claim": "uses *args", "evidence": "kv_prerope.py:88"}
            ],
            commander_verdict="GO",
        )
        summary = adjudicate(d)
        assert "uses *args" in summary
        assert "kv_prerope.py:88" in summary

    def test_verdict_in_summary(self) -> None:
        d = RedBlueDecision(
            proposal="x", radius="low", commander_verdict="GO"
        )
        assert "VERDICT: GO" in adjudicate(d)
