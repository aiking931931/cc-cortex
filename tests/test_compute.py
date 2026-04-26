"""Tests for the structured-plan compute helper.

Covers :func:`execute_statistics_plan`, :func:`execute_arithmetic_plan`,
:func:`format_number`, the LLM-facing :class:`ComputeTool` wrapper, and
feature_config registration.
"""

from __future__ import annotations

import json

import pytest

from concinno.tools.builtin.compute import (
    ARITHMETIC_OP_WHITELIST,
    STATS_FN_WHITELIST,
    ComputePlanError,
    ComputeTool,
    execute_arithmetic_plan,
    execute_statistics_plan,
    format_number,
)

# ─────────────────────────── format_number ──────────────────────────────


class TestFormatNumber:
    def test_integer_no_decimals(self):
        assert format_number(42.0) == "42"

    def test_close_to_integer(self):
        assert format_number(42.04) == "42"

    def test_decimal_default(self):
        assert format_number(17.05601) == "17.056"

    def test_explicit_decimals(self):
        assert format_number(17.0560099, 3) == "17.056"
        assert format_number(17.0560099, 5) == "17.05601"

    def test_zero_decimals(self):
        assert format_number(17.6, 0) == "18"

    def test_decimals_out_of_range_ignored(self):
        # decimals=20 out of range → fall back to default
        assert format_number(42.0, 20) == "42"


# ──────────────────── execute_statistics_plan ───────────────────────────


class TestExecuteStatisticsPlan:
    def test_plan_must_be_dict(self):
        with pytest.raises(ComputePlanError, match="dict"):
            execute_statistics_plan("not a dict", {})  # type: ignore[arg-type]

    def test_simple_mean(self):
        plan = {
            "intermediate": [],
            "final": {"fn": "mean", "input": "red"},
        }
        out = execute_statistics_plan(plan, {"red": [10, 20, 30]})
        assert out["answer"] == "20"
        assert out["raw_result"] == 20.0

    def test_pstdev_stdev_mean_3_decimals(self):
        # Mirror cont'd¹⁰ df6561b2 production scenario
        plan = {
            "intermediate": [
                {"name": "rp", "fn": "pstdev", "input": "red"},
                {"name": "gs", "fn": "stdev", "input": "green"},
            ],
            "final": {"fn": "mean", "input": ["rp", "gs"]},
            "round_decimals": 3,
        }
        red = [
            24, 74, 28, 54, 73, 33, 64, 73, 60, 53, 59, 40, 65, 76, 48,
            34, 62, 70, 31, 24, 51, 55, 78, 76, 41, 77, 51,
        ]
        green = [
            39, 29, 28, 72, 68, 47, 64, 74, 72, 40, 75, 26, 27, 37, 31,
            55, 44, 64, 65, 38, 46, 66, 35, 76, 61, 53, 49,
        ]
        out = execute_statistics_plan(plan, {"red": red, "green": green})
        assert out["answer"] == "17.056"
        assert "rp" in out["intermediates"]
        assert "gs" in out["intermediates"]

    def test_invalid_fn(self):
        plan = {
            "intermediate": [],
            "final": {"fn": "eval", "input": "red"},
        }
        with pytest.raises(ComputePlanError, match="whitelist"):
            execute_statistics_plan(plan, {"red": [1, 2, 3]})

    def test_intermediate_input_not_in_data(self):
        plan = {
            "intermediate": [
                {"name": "x", "fn": "mean", "input": "purple"},
            ],
            "final": {"fn": "mean", "input": ["x"]},
        }
        with pytest.raises(ComputePlanError, match="purple"):
            execute_statistics_plan(plan, {"red": [1, 2, 3]})

    def test_final_input_list_with_raw_data_id_rejected(self):
        plan = {
            "intermediate": [],
            "final": {"fn": "mean", "input": ["red"]},
        }
        with pytest.raises(ComputePlanError, match="raw data"):
            execute_statistics_plan(plan, {"red": [1, 2, 3]})

    def test_statistics_module_error_propagates(self):
        # stdev requires at least 2 data points
        plan = {
            "intermediate": [],
            "final": {"fn": "stdev", "input": "red"},
        }
        with pytest.raises(ComputePlanError):
            execute_statistics_plan(plan, {"red": [42]})

    def test_non_numeric_data_raises(self):
        plan = {
            "intermediate": [],
            "final": {"fn": "mean", "input": "red"},
        }
        with pytest.raises(ComputePlanError, match="non-numeric"):
            execute_statistics_plan(plan, {"red": [1, "two", 3]})


# ──────────────────── execute_arithmetic_plan ───────────────────────────


class TestExecuteArithmeticPlan:
    def test_simple_add(self):
        plan = {
            "steps": [
                {"name": "s1", "op": "add", "args": ["a", "b"]},
            ],
            "final": "s1",
        }
        out = execute_arithmetic_plan(plan, {"a": 5, "b": 3})
        assert out["answer"] == "8"

    def test_chained_ops(self):
        # (a + b) * c - d → (5+3)*2 - 1 = 15
        plan = {
            "steps": [
                {"name": "s1", "op": "add", "args": ["a", "b"]},
                {"name": "s2", "op": "mul", "args": ["s1", "c"]},
                {"name": "s3", "op": "sub", "args": ["s2", "d"]},
            ],
            "final": "s3",
        }
        out = execute_arithmetic_plan(
            plan, {"a": 5, "b": 3, "c": 2, "d": 1},
        )
        assert out["answer"] == "15"

    def test_literal_args(self):
        plan = {
            "steps": [
                {"name": "s1", "op": "add", "args": ["x", 100]},
            ],
            "final": "s1",
        }
        out = execute_arithmetic_plan(plan, {"x": 5})
        assert out["answer"] == "105"

    def test_div_by_zero(self):
        plan = {
            "steps": [
                {"name": "s1", "op": "div", "args": ["a", "b"]},
            ],
            "final": "s1",
        }
        with pytest.raises(ComputePlanError, match="zero"):
            execute_arithmetic_plan(plan, {"a": 5, "b": 0})

    def test_pow_with_decimals(self):
        plan = {
            "steps": [
                {"name": "s1", "op": "pow", "args": ["b", 2]},
            ],
            "final": "s1",
            "round_decimals": 1,
        }
        out = execute_arithmetic_plan(plan, {"b": 2.5})
        assert out["answer"] == "6.2"

    def test_sum_list_op(self):
        plan = {
            "steps": [
                {"name": "s1", "op": "sum_list", "args": ["lst"]},
            ],
            "final": "s1",
        }
        out = execute_arithmetic_plan(plan, {"lst": [1, 2, 3, 4]})
        assert out["answer"] == "10"

    def test_mean_list_op(self):
        plan = {
            "steps": [
                {"name": "s1", "op": "mean_list", "args": ["lst"]},
            ],
            "final": "s1",
            "round_decimals": 2,
        }
        out = execute_arithmetic_plan(plan, {"lst": [10, 20, 30]})
        assert out["answer"] == "20.00"

    def test_butterfat_percentage_scenario(self):
        # GAIA b2c257e0-style: pint butterfat % vs federal standard
        # Sample: pint reports 14.6%, federal standard 10% → diff +4.6
        plan = {
            "steps": [
                {"name": "diff", "op": "sub",
                 "args": ["pint_pct", "federal_pct"]},
            ],
            "final": "diff",
            "round_decimals": 1,
        }
        out = execute_arithmetic_plan(
            plan, {"pint_pct": 14.6, "federal_pct": 10},
        )
        assert out["answer"] == "4.6"

    def test_unknown_op_rejected(self):
        plan = {
            "steps": [
                {"name": "s1", "op": "exec", "args": ["a"]},
            ],
            "final": "s1",
        }
        with pytest.raises(ComputePlanError, match="whitelist"):
            execute_arithmetic_plan(plan, {"a": 1})

    def test_unresolved_arg(self):
        plan = {
            "steps": [
                {"name": "s1", "op": "add", "args": ["a", "ghost"]},
            ],
            "final": "s1",
        }
        with pytest.raises(ComputePlanError, match="unresolved"):
            execute_arithmetic_plan(plan, {"a": 1})

    def test_list_op_on_scalar_var_rejected(self):
        plan = {
            "steps": [
                {"name": "s1", "op": "sum_list", "args": ["a"]},
            ],
            "final": "s1",
        }
        with pytest.raises(ComputePlanError):
            execute_arithmetic_plan(plan, {"a": 5})

    def test_scalar_op_on_list_var_rejected(self):
        plan = {
            "steps": [
                {"name": "s1", "op": "add", "args": ["lst", 1]},
            ],
            "final": "s1",
        }
        with pytest.raises(ComputePlanError, match="list"):
            execute_arithmetic_plan(plan, {"lst": [1, 2, 3]})

    def test_final_step_missing(self):
        plan = {
            "steps": [
                {"name": "s1", "op": "add", "args": ["a", "b"]},
            ],
            "final": "ghost",
        }
        with pytest.raises(ComputePlanError, match="ghost"):
            execute_arithmetic_plan(plan, {"a": 1, "b": 2})


# ────────────────────────── ComputeTool ─────────────────────────────────


class TestComputeTool:
    def test_statistics_kind(self):
        tool = ComputeTool()
        out = tool(
            kind="statistics",
            plan={
                "intermediate": [],
                "final": {"fn": "mean", "input": "lst"},
            },
            data={"lst": [1, 2, 3]},
        )
        assert out["answer"] == "2"

    def test_arithmetic_kind(self):
        tool = ComputeTool()
        out = tool(
            kind="arithmetic",
            plan={
                "steps": [
                    {"name": "s1", "op": "add", "args": ["a", "b"]},
                ],
                "final": "s1",
            },
            variables={"a": 10, "b": 5},
        )
        assert out["answer"] == "15"

    def test_unknown_kind(self):
        tool = ComputeTool()
        out = tool(kind="exec", plan={}, data={})
        assert "error" in out
        assert "exec" in out["error"]

    def test_validation_error_returned_as_error_dict(self):
        tool = ComputeTool()
        out = tool(
            kind="statistics",
            plan={"intermediate": [],
                  "final": {"fn": "eval", "input": "lst"}},
            data={"lst": [1]},
        )
        assert "error" in out

    def test_call_json(self):
        tool = ComputeTool()
        raw = json.dumps({
            "kind": "statistics",
            "plan": {
                "intermediate": [],
                "final": {"fn": "mean", "input": "x"},
            },
            "data": {"x": [10, 20]},
        })
        out = json.loads(tool.call_json(raw))
        assert out["answer"] == "15"

    def test_call_json_invalid(self):
        tool = ComputeTool()
        out = json.loads(tool.call_json("not json"))
        assert "error" in out


# ──────────────────────── feature_config ────────────────────────────────


class TestFeatureRegistration:
    def test_compute_feature_registered(self):
        from concinno.feature_config import FEATURE_META
        assert "compute_structured_plan" in FEATURE_META

    def test_feature_meta_fields(self):
        from concinno.feature_config import FEATURE_META
        meta = FEATURE_META["compute_structured_plan"]
        assert meta["category"] == "context"
        assert meta["ziq_autotunable"] is False
        assert meta["cosmetic"] is False


# ────────────────────────── whitelists ──────────────────────────────────


class TestWhitelists:
    def test_stats_whitelist_no_dangerous(self):
        # Whitelist must not include eval/exec/etc
        for forbidden in ("eval", "exec", "open", "import", "compile"):
            assert forbidden not in STATS_FN_WHITELIST

    def test_arithmetic_whitelist_no_dangerous(self):
        for forbidden in ("eval", "exec", "open", "import", "compile"):
            assert forbidden not in ARITHMETIC_OP_WHITELIST

    def test_stats_whitelist_has_essentials(self):
        for essential in ("mean", "stdev", "pstdev", "median"):
            assert essential in STATS_FN_WHITELIST

    def test_arithmetic_whitelist_has_essentials(self):
        for essential in ("add", "sub", "mul", "div"):
            assert essential in ARITHMETIC_OP_WHITELIST
