"""Tests for cognitive.router compression-breakeven integration (4.4.0).

Covers:
* ``compress_breakeven_for_route`` per-complexity routing
* ``_BUDGET_TABLE`` invariants (still 4-class, percentages sum to 100,
  unchanged from 4.3.0)
* ``FEATURE_META['field_read']`` schema validity (entry shape, vmin/vmax
  bounds, ZIQ tunable flag, ships ON)
* End-to-end aggregation: router complexity → field_read breakeven →
  same value as direct lookup.
"""

from __future__ import annotations

from concinno.cognitive.router import (
    _BUDGET_TABLE,
    ComplexityDomain,
    compress_breakeven_for_route,
    route,
)
from concinno.feature_config import FEATURE_META
from concinno.field_read import (
    COMPRESS_BREAKEVEN_BY_COMPLEXITY,
    compress_breakeven_for,
)

# ── compress_breakeven_for_route ───────────────────────────


class TestCompressBreakevenForRoute:
    def test_simple_message_returns_aggressive_breakeven(self):
        r = route("rename foo to bar")
        assert r.complexity == ComplexityDomain.SIMPLE
        assert compress_breakeven_for_route(r) == 1500

    def test_chaotic_message_returns_conservative_breakeven(self):
        r = route("emergency, system crashed, urgent rescue now")
        assert r.complexity == ComplexityDomain.CHAOTIC
        assert compress_breakeven_for_route(r) == 4000

    def test_complex_message_returns_high_breakeven(self):
        r = route("explore novel architecture, uncertain approach")
        assert r.complexity == ComplexityDomain.COMPLEX
        assert compress_breakeven_for_route(r) == 3500

    def test_complicated_message_returns_default_breakeven(self):
        # Generic non-simple, non-emergency message → COMPLICATED
        r = route(
            "implement caching layer with TTL eviction and "
            "instrumentation for hit ratio tracking",
        )
        # Could be COMPLICATED or COMPLEX depending on heuristics;
        # whichever it is, the breakeven must match the table.
        expected = COMPRESS_BREAKEVEN_BY_COMPLEXITY[r.complexity.value]
        assert compress_breakeven_for_route(r) == expected

    def test_consistency_with_direct_lookup(self):
        for msg in [
            "rename x to y",
            "explore novel uncertain area",
            "emergency system crashed",
            "fix typo",
            "implement complex caching with eviction",
        ]:
            r = route(msg)
            via_route = compress_breakeven_for_route(r)
            via_direct = compress_breakeven_for(r.complexity.value)
            assert via_route == via_direct


# ── _BUDGET_TABLE invariants (4.3.0 carryover, MUST not regress) ──


class TestBudgetTableInvariants:
    def test_four_complexity_classes(self):
        assert set(_BUDGET_TABLE) == {
            ComplexityDomain.SIMPLE,
            ComplexityDomain.COMPLICATED,
            ComplexityDomain.COMPLEX,
            ComplexityDomain.CHAOTIC,
        }

    def test_percentages_sum_to_100(self):
        for domain, (r, a, m) in _BUDGET_TABLE.items():
            assert r + a + m == 100, (
                f"{domain} budget must sum to 100, got {r + a + m}"
            )

    def test_simple_action_heavy(self):
        # Simple should put majority of tokens into action, not reasoning.
        r, a, _ = _BUDGET_TABLE[ComplexityDomain.SIMPLE]
        assert a > r

    def test_chaotic_meta_high(self):
        # Chaotic earns more meta budget than Simple does.
        _, _, m_chaotic = _BUDGET_TABLE[ComplexityDomain.CHAOTIC]
        _, _, m_simple = _BUDGET_TABLE[ComplexityDomain.SIMPLE]
        assert m_chaotic > m_simple

    def test_unchanged_from_4_3_0(self):
        # Plan promise: budget split is NOT what 4.4.0 changes — only
        # the per-complexity compression breakeven is added. If this
        # test starts failing the 4.4.0 scope crept.
        assert _BUDGET_TABLE[ComplexityDomain.SIMPLE] == (15, 75, 10)
        assert _BUDGET_TABLE[ComplexityDomain.COMPLICATED] == (
            30, 50, 20
        )
        assert _BUDGET_TABLE[ComplexityDomain.COMPLEX] == (35, 40, 25)
        assert _BUDGET_TABLE[ComplexityDomain.CHAOTIC] == (40, 25, 35)


# ── FEATURE_META.field_read schema ─────────────────────────


class TestFieldReadFeatureMeta:
    def test_entry_present(self):
        assert "field_read" in FEATURE_META

    def test_required_top_level_keys(self):
        e = FEATURE_META["field_read"]
        for key in (
            "category", "description", "description_zh",
            "ziq_autotunable", "cosmetic", "params",
        ):
            assert key in e, f"missing required key {key!r}"

    def test_ziq_autotunable_true(self):
        # Required by Plan: outcome-tunable via expand() trigger rate.
        assert FEATURE_META["field_read"]["ziq_autotunable"] is True

    def test_cosmetic_false(self):
        # Affects routing & retrieval semantics — NOT cosmetic.
        assert FEATURE_META["field_read"]["cosmetic"] is False

    def test_compress_breakeven_param_bounds(self):
        p = FEATURE_META["field_read"]["params"]["compress_breakeven_tokens"]
        assert p["min"] == 1500
        assert p["max"] == 4000
        assert p["default"] == 2500
        assert p["recommended"] == 2500

    def test_compress_breakeven_envelope_covers_per_complexity_table(self):
        p = FEATURE_META["field_read"]["params"]["compress_breakeven_tokens"]
        for v in COMPRESS_BREAKEVEN_BY_COMPLEXITY.values():
            assert p["min"] <= v <= p["max"], (
                f"per-complexity breakeven {v} outside FEATURE_META envelope "
                f"[{p['min']}, {p['max']}]"
            )

    def test_enabled_param_default_true(self):
        # Ships ON — silent v1 fallback only when explicitly disabled.
        assert (
            FEATURE_META["field_read"]["params"]["enabled"]["default"] is True
        )

    def test_include_breadcrumbs_default_true(self):
        p = FEATURE_META["field_read"]["params"]["include_breadcrumbs"]
        assert p["default"] is True

    def test_default_off_4_0_0_membership(self):
        # field_read is a context feature, NOT a hard gate — must NOT
        # be in DEFAULT_OFF_4_0_0 (it ships on).
        from concinno.feature_config import DEFAULT_OFF_4_0_0

        assert "field_read" not in DEFAULT_OFF_4_0_0


# ── Aggregation correctness ────────────────────────────────


class TestRouterFieldReadAggregation:
    def test_route_then_breakeven_aggregates_to_table(self):
        # End-to-end: route a message, derive complexity, look up
        # breakeven — must match the COMPRESS_BREAKEVEN_BY_COMPLEXITY
        # entry exactly.
        msgs = {
            "rename foo": ComplexityDomain.SIMPLE,
            "explore novel uncertain": ComplexityDomain.COMPLEX,
            "emergency crash urgent": ComplexityDomain.CHAOTIC,
        }
        for msg, expected_class in msgs.items():
            r = route(msg)
            assert r.complexity == expected_class
            assert compress_breakeven_for_route(r) == (
                COMPRESS_BREAKEVEN_BY_COMPLEXITY[r.complexity.value]
            )

    def test_breakeven_table_keys_match_complexity_enum(self):
        # Each enum value MUST have a corresponding breakeven entry,
        # otherwise router-fan-out will silently fall back to the
        # global default and ZIQ FTRL learning can never converge.
        enum_values = {d.value for d in ComplexityDomain}
        table_keys = set(COMPRESS_BREAKEVEN_BY_COMPLEXITY)
        assert enum_values == table_keys
