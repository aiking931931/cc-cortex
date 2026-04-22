"""Tests for :mod:`concinno.agent.commander` — tier router (Tier 1).

S5 verdict anchors:
* F1 — α_t uses only c0_router + file_count (no peakedness / Platt).
* F3 — thresholds are warm-start; auto-tune activates at N≥60 (sunset flag).
* M3 — adapter-level env flag defaults off (tested in persona-api).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from concinno.agent.commander import (
    COMPLEXITY_PRIOR,
    QUESTION_FILE_BOOST_MAX,
    QUESTION_FILE_BOOST_PER_FILE,
    TIER_0_MAX_ALPHA,
    TIER_1_MAX_ALPHA,  # noqa: F401
    TIER_2_MAX_ALPHA,
    TIER_BUDGETS,
    Commander,
    TierBudget,
    TierDecision,
)

# ─────────────────────────── Stub C0Router ───────────────────────────


@dataclass
class _C0Stub:
    """Stand-in ``C0Result`` shape the commander consumes."""

    complexity: str
    prompt_budget: int = 1_500
    guard_level: str = "normal"
    signals: dict[str, Any] = field(default_factory=dict)
    escalation_reason: str = ""
    redteam_required: bool = False
    a2a_suggested: bool = False
    hysteresis_locked: bool = False


class _FakeRouter:
    """Deterministic router stub.

    Exposes the same ``classify`` / ``classify_with_hysteresis`` surface
    the commander depends on, without touching the real cognitive.router
    heuristics. Tests can pin whichever complexity class they want by
    constructing ``_FakeRouter(complexity="complex")``.
    """

    def __init__(self, complexity: str = "complicated") -> None:
        self.complexity = complexity
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def classify(self, task_prompt: str, **kwargs: Any) -> _C0Stub:
        self.calls.append(("classify", {"prompt": task_prompt, **kwargs}))
        return _C0Stub(complexity=self.complexity)

    def classify_with_hysteresis(
        self,
        task_prompt: str,
        cache_dir: str,
        session_id: str,
        **kwargs: Any,
    ) -> _C0Stub:
        self.calls.append(
            ("hysteresis", {
                "prompt": task_prompt,
                "cache_dir": cache_dir,
                "session_id": session_id,
                **kwargs,
            }),
        )
        return _C0Stub(complexity=self.complexity, hysteresis_locked=True)


# ─────────────────────────── Module constants ───────────────────────────


class TestConstants:
    """Module-level constants freeze the warm-start heuristic anchors."""

    def test_complexity_prior_covers_four_classes(self) -> None:
        assert set(COMPLEXITY_PRIOR) == {
            "simple", "complicated", "complex", "chaotic",
        }

    def test_complexity_prior_monotonic(self) -> None:
        assert (
            COMPLEXITY_PRIOR["simple"]
            < COMPLEXITY_PRIOR["complicated"]
            < COMPLEXITY_PRIOR["complex"]
            < COMPLEXITY_PRIOR["chaotic"]
        )

    def test_tier_thresholds_monotonic(self) -> None:
        assert TIER_0_MAX_ALPHA < TIER_1_MAX_ALPHA < TIER_2_MAX_ALPHA < 1.0

    def test_tier_budgets_cover_all_tiers(self) -> None:
        assert set(TIER_BUDGETS) == {0, 1, 2, 3}

    def test_tier_budgets_monotonic_max_tokens(self) -> None:
        tokens = [TIER_BUDGETS[t].max_tokens for t in range(4)]
        assert tokens == sorted(tokens)

    def test_tier_budgets_monotonic_iterations(self) -> None:
        iters = [TIER_BUDGETS[t].max_iterations for t in range(4)]
        assert iters == sorted(iters)

    def test_file_boost_cap_sensible(self) -> None:
        assert 0 < QUESTION_FILE_BOOST_PER_FILE < QUESTION_FILE_BOOST_MAX
        assert QUESTION_FILE_BOOST_MAX < min(COMPLEXITY_PRIOR.values()) + 0.5


# ─────────────────────────── Dataclasses ───────────────────────────


class TestTierBudget:
    def test_frozen(self) -> None:
        b = TierBudget(max_tokens=4_096, max_iterations=8, per_role_timeout_s=300)
        with pytest.raises((AttributeError, Exception)):
            b.max_tokens = 9_999  # type: ignore[misc]


class TestTierDecision:
    def test_default_sunset_flag_is_60(self) -> None:
        d = TierDecision(
            tier=0,
            alpha_t=0.1,
            budget=TIER_BUDGETS[0],
            reason="",
        )
        assert d.thresholds_frozen_until_n_outcomes == 60

    def test_signals_default_empty(self) -> None:
        d = TierDecision(tier=0, alpha_t=0.1, budget=TIER_BUDGETS[0], reason="")
        assert d.signals == {}

    def test_frozen(self) -> None:
        d = TierDecision(tier=0, alpha_t=0.1, budget=TIER_BUDGETS[0], reason="")
        with pytest.raises((AttributeError, Exception)):
            d.tier = 3  # type: ignore[misc]


# ─────────────────────────── Commander.route happy paths ───────────────────────────


class TestRouteByClass:
    """Every c0 class routes to a sensible tier."""

    def test_simple_routes_tier_0(self) -> None:
        c = Commander(router=_FakeRouter(complexity="simple"))
        d = c.route("q", {})
        assert d.tier == 0
        assert d.alpha_t == pytest.approx(COMPLEXITY_PRIOR["simple"])

    def test_complicated_routes_tier_1(self) -> None:
        c = Commander(router=_FakeRouter(complexity="complicated"))
        d = c.route("q", {})
        assert d.tier == 1
        assert d.alpha_t == pytest.approx(COMPLEXITY_PRIOR["complicated"])

    def test_complex_routes_tier_2(self) -> None:
        c = Commander(router=_FakeRouter(complexity="complex"))
        d = c.route("q", {})
        assert d.tier == 2
        assert d.alpha_t == pytest.approx(COMPLEXITY_PRIOR["complex"])

    def test_chaotic_routes_tier_3(self) -> None:
        c = Commander(router=_FakeRouter(complexity="chaotic"))
        d = c.route("q", {})
        assert d.tier == 3
        assert d.alpha_t == pytest.approx(COMPLEXITY_PRIOR["chaotic"])

    def test_unknown_class_falls_through_to_complicated(self) -> None:
        """Defensive default — unknown class shouldn't dump to SAS."""
        c = Commander(router=_FakeRouter(complexity="mystery"))
        d = c.route("q", {})
        assert d.alpha_t == pytest.approx(COMPLEXITY_PRIOR["complicated"])


# ─────────────────────────── File count boost ───────────────────────────


class TestFileBoost:
    def test_zero_files_no_boost(self) -> None:
        c = Commander(router=_FakeRouter(complexity="complicated"))
        d = c.route("q", {"attached_file_count": 0})
        assert d.alpha_t == pytest.approx(COMPLEXITY_PRIOR["complicated"])

    def test_one_file_boost(self) -> None:
        c = Commander(router=_FakeRouter(complexity="complicated"))
        d = c.route("q", {"attached_file_count": 1})
        assert d.alpha_t == pytest.approx(0.50 - 0.10)

    def test_two_files_boost(self) -> None:
        c = Commander(router=_FakeRouter(complexity="complicated"))
        d = c.route("q", {"attached_file_count": 2})
        assert d.alpha_t == pytest.approx(0.50 - 0.20)

    def test_many_files_clamps_at_max(self) -> None:
        """10 files should not pull α_t below prior - MAX."""
        c = Commander(router=_FakeRouter(complexity="complicated"))
        d = c.route("q", {"attached_file_count": 10})
        expected = COMPLEXITY_PRIOR["complicated"] - QUESTION_FILE_BOOST_MAX
        assert d.alpha_t == pytest.approx(expected)

    def test_negative_file_count_treated_as_zero(self) -> None:
        c = Commander(router=_FakeRouter(complexity="complicated"))
        d = c.route("q", {"attached_file_count": -5})
        assert d.alpha_t == pytest.approx(COMPLEXITY_PRIOR["complicated"])

    def test_file_boost_can_demote_tier(self) -> None:
        """Complicated (α=0.50) with 2 files (boost -0.2) → α=0.30 → tier 0."""
        c = Commander(router=_FakeRouter(complexity="complicated"))
        d = c.route("q", {"attached_file_count": 2})
        assert d.tier == 0

    def test_file_boost_cannot_flip_chaotic_to_sas(self) -> None:
        """Chaotic (0.90) - MAX (0.20) = 0.70 → still tier 2, not tier 0."""
        c = Commander(router=_FakeRouter(complexity="chaotic"))
        d = c.route("q", {"attached_file_count": 10})
        assert d.tier == 2


# ─────────────────────────── Boundary cases ───────────────────────────


class TestTierBoundaries:
    """α_t right at the tier boundary should fall to the higher tier."""

    def test_alpha_at_tier_0_boundary_goes_tier_1(self) -> None:
        # α_t == TIER_0_MAX_ALPHA means "escalate to tier 1"
        tier = Commander._alpha_to_tier(TIER_0_MAX_ALPHA)
        assert tier == 1

    def test_alpha_just_below_tier_0_boundary_is_tier_0(self) -> None:
        tier = Commander._alpha_to_tier(TIER_0_MAX_ALPHA - 0.0001)
        assert tier == 0

    def test_alpha_at_tier_2_boundary_goes_tier_3(self) -> None:
        assert Commander._alpha_to_tier(TIER_2_MAX_ALPHA) == 3

    def test_alpha_zero_is_tier_0(self) -> None:
        assert Commander._alpha_to_tier(0.0) == 0

    def test_alpha_one_is_tier_3(self) -> None:
        assert Commander._alpha_to_tier(1.0) == 3


# ─────────────────────────── Fallback chain ───────────────────────────


class TestFallbackChain:
    def test_tier_0_fallback_is_just_sas(self) -> None:
        c = Commander(router=_FakeRouter(complexity="simple"))
        d = c.route("q", {})
        assert d.fallback_chain == [0]

    def test_tier_1_fallback_includes_sas(self) -> None:
        c = Commander(router=_FakeRouter(complexity="complicated"))
        d = c.route("q", {})
        assert d.fallback_chain[0] == 1
        assert d.fallback_chain[-1] == 0

    def test_tier_3_fallback_steps_down(self) -> None:
        c = Commander(router=_FakeRouter(complexity="chaotic"))
        d = c.route("q", {})
        # Primary is tier 3, next is tier 2, final catch-all is 0.
        assert d.fallback_chain == [3, 2, 0]

    def test_fallback_always_ends_in_zero(self) -> None:
        for complexity in COMPLEXITY_PRIOR:
            c = Commander(router=_FakeRouter(complexity=complexity))
            d = c.route("q", {})
            assert d.fallback_chain[-1] == 0

    def test_fallback_has_no_duplicates(self) -> None:
        for complexity in COMPLEXITY_PRIOR:
            c = Commander(router=_FakeRouter(complexity=complexity))
            d = c.route("q", {})
            assert len(d.fallback_chain) == len(set(d.fallback_chain))


# ─────────────────────────── Hysteresis routing ───────────────────────────


class TestHysteresisUse:
    def test_hysteresis_used_when_cache_dir_and_session_id(self) -> None:
        router = _FakeRouter(complexity="complicated")
        c = Commander(router=router)
        c.route("q", {"cache_dir": "/tmp/x", "session_id": "s1"})
        assert router.calls[0][0] == "hysteresis"

    def test_plain_classify_when_missing_cache_dir(self) -> None:
        router = _FakeRouter(complexity="complicated")
        c = Commander(router=router)
        c.route("q", {"session_id": "s1"})  # cache_dir missing
        assert router.calls[0][0] == "classify"

    def test_plain_classify_when_missing_session_id(self) -> None:
        router = _FakeRouter(complexity="complicated")
        c = Commander(router=router)
        c.route("q", {"cache_dir": "/tmp/x"})  # session_id missing
        assert router.calls[0][0] == "classify"

    def test_plain_classify_when_ctx_empty(self) -> None:
        router = _FakeRouter(complexity="complicated")
        c = Commander(router=router)
        c.route("q", {})
        assert router.calls[0][0] == "classify"

    def test_none_ctx_treated_as_empty(self) -> None:
        router = _FakeRouter(complexity="complicated")
        c = Commander(router=router)
        d = c.route("q", None)
        assert d.tier == 1
        assert router.calls[0][0] == "classify"


# ─────────────────────────── Signal dump ───────────────────────────


class TestSignals:
    def test_signals_dump_c0_class(self) -> None:
        c = Commander(router=_FakeRouter(complexity="complex"))
        d = c.route("q", {})
        assert d.signals["c0_class"] == "complex"

    def test_signals_dump_attached_file_count(self) -> None:
        c = Commander(router=_FakeRouter(complexity="simple"))
        d = c.route("q", {"attached_file_count": 3})
        assert d.signals["attached_file_count"] == 3

    def test_signals_include_tier_thresholds(self) -> None:
        c = Commander(router=_FakeRouter(complexity="simple"))
        d = c.route("q", {})
        assert "tier_thresholds" in d.signals
        assert d.signals["tier_thresholds"]["tier_0_max"] == TIER_0_MAX_ALPHA


# ─────────────────────────── Budget allocation ───────────────────────────


class TestBudgetAllocation:
    def test_tier_0_gets_tier_0_budget(self) -> None:
        c = Commander(router=_FakeRouter(complexity="simple"))
        d = c.route("q", {})
        assert d.budget is TIER_BUDGETS[0]

    def test_tier_3_gets_tier_3_budget(self) -> None:
        c = Commander(router=_FakeRouter(complexity="chaotic"))
        d = c.route("q", {})
        assert d.budget is TIER_BUDGETS[3]


# ─────────────────────────── FTRL sunset flag ───────────────────────────


class TestFTRLSunsetFlag:
    """S5 藍 C2: flag exists but Tier 1 doesn't activate auto-tune."""

    def test_default_value_is_60(self) -> None:
        c = Commander(router=_FakeRouter(complexity="simple"))
        d = c.route("q", {})
        assert d.thresholds_frozen_until_n_outcomes == 60

    def test_sunset_flag_is_int(self) -> None:
        c = Commander(router=_FakeRouter(complexity="simple"))
        d = c.route("q", {})
        assert isinstance(d.thresholds_frozen_until_n_outcomes, int)


# ─────────────────────────── Reason rendering ───────────────────────────


class TestReasonString:
    def test_reason_includes_class(self) -> None:
        c = Commander(router=_FakeRouter(complexity="complicated"))
        d = c.route("q", {})
        assert "c0=complicated" in d.reason

    def test_reason_includes_alpha(self) -> None:
        c = Commander(router=_FakeRouter(complexity="complex"))
        d = c.route("q", {})
        assert "alpha_t" in d.reason

    def test_reason_includes_tier(self) -> None:
        c = Commander(router=_FakeRouter(complexity="chaotic"))
        d = c.route("q", {})
        assert "tier 3" in d.reason

    def test_reason_mentions_files_when_nonzero(self) -> None:
        c = Commander(router=_FakeRouter(complexity="complicated"))
        d = c.route("q", {"attached_file_count": 2})
        assert "files=2" in d.reason

    def test_reason_omits_files_when_zero(self) -> None:
        c = Commander(router=_FakeRouter(complexity="complicated"))
        d = c.route("q", {"attached_file_count": 0})
        assert "files=" not in d.reason


# ─────────────────────────── Forbidden path regression ───────────────────────────


class TestS5ForbiddenPaths:
    """Regression: commander must not introduce peakedness / Platt imports.

    Enforced at the *import* layer (``inspect.getsource`` is line-scanned
    for ``import`` / ``from ... import`` statements). Docstring mentions
    are allowed — callers are allowed to reference the forbidden paths
    in explanation. What we block is adding a real dependency.
    """

    @staticmethod
    def _import_lines() -> list[str]:
        import inspect

        import concinno.agent.commander as m
        src = inspect.getsource(m)
        return [
            line.strip().lower()
            for line in src.splitlines()
            if line.lstrip().startswith(("import ", "from "))
        ]

    def test_module_does_not_import_peakedness(self) -> None:
        for line in self._import_lines():
            assert "peakedness" not in line
            assert "ziq_retrieval" not in line

    def test_module_does_not_import_sklearn(self) -> None:
        for line in self._import_lines():
            assert "sklearn" not in line

    def test_module_does_not_import_platt(self) -> None:
        for line in self._import_lines():
            assert "platt" not in line

    def test_module_does_not_import_scipy(self) -> None:
        for line in self._import_lines():
            assert "scipy" not in line

    def test_module_does_not_import_logprob_path(self) -> None:
        """No raw logprob probe dependency in Tier 1."""
        for line in self._import_lines():
            assert "logprob" not in line
