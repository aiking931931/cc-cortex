"""Tests for :mod:`concinno.tools.builtin.date_calc`.

Covers three ops (``delta`` / ``parse`` / ``format``) and the tool
contract surface (``name`` / ``is_concurrency_safe`` / strict
strptime rejections).
"""

from __future__ import annotations

from concinno.tools.builtin.date_calc import DateCalcTool

# ── Contract surface ──────────────────────────────────


def test_tool_contract_attrs() -> None:
    t = DateCalcTool()
    assert t.name == "date_calc"
    assert t.is_concurrency_safe is True
    assert "calendar" in t.description.lower()
    assert "strict" in t.description.lower()


# ── delta ─────────────────────────────────────────────


def test_delta_simple_day_count_iso_input() -> None:
    t = DateCalcTool()
    out = t.call(op="delta", date_from="2020-01-01", date_to="2020-01-11")
    assert out.startswith("10 days")


def test_delta_calendar_breakdown_one_year_exact() -> None:
    t = DateCalcTool()
    out = t.call(
        op="delta", date_from="2020-03-15", date_to="2021-03-15",
    )
    assert "1 years" in out
    assert "0 months" in out
    assert "0 days" in out


def test_delta_calendar_breakdown_mixed() -> None:
    t = DateCalcTool()
    out = t.call(
        op="delta", date_from="2020-01-15", date_to="2022-05-20",
    )
    # 2 years, 4 months, 5 days.
    assert "2 years" in out
    assert "4 months" in out
    assert "5 days" in out


def test_delta_negative_when_end_before_start() -> None:
    t = DateCalcTool()
    out = t.call(
        op="delta", date_from="2020-12-31", date_to="2020-12-25",
    )
    assert out.startswith("-6 days")


def test_delta_custom_format_str() -> None:
    t = DateCalcTool()
    out = t.call(
        op="delta",
        date_from="01/01/2020",
        date_to="01/11/2020",
        format_str="%m/%d/%Y",
    )
    assert out.startswith("10 days")


def test_delta_missing_endpoint_returns_error() -> None:
    t = DateCalcTool()
    assert t.call(op="delta", date_from="2020-01-01").startswith("error:")
    assert t.call(op="delta", date_to="2020-01-01").startswith("error:")


def test_delta_invalid_format_returns_error() -> None:
    t = DateCalcTool()
    out = t.call(
        op="delta",
        date_from="not-a-date",
        date_to="2020-01-11",
    )
    assert out.startswith("error:")


# ── parse ─────────────────────────────────────────────


def test_parse_returns_iso() -> None:
    t = DateCalcTool()
    out = t.call(
        op="parse", date_str="01/15/2024", format_str="%m/%d/%Y",
    )
    assert out == "2024-01-15"


def test_parse_requires_format() -> None:
    t = DateCalcTool()
    out = t.call(op="parse", date_str="2024-01-15")
    assert out.startswith("error:")


def test_parse_strict_rejects_mismatched_format() -> None:
    t = DateCalcTool()
    out = t.call(
        op="parse", date_str="01/15/2024", format_str="%Y-%m-%d",
    )
    assert out.startswith("error:")


# ── format ────────────────────────────────────────────


def test_format_reformats_iso() -> None:
    t = DateCalcTool()
    out = t.call(
        op="format", date_str="2024-01-15", format_str="%m/%d/%Y",
    )
    assert out == "01/15/2024"


def test_format_accepts_custom_input() -> None:
    t = DateCalcTool()
    out = t.call(
        op="format", date_str="15-Jan-2024", format_str="%d-%b-%Y",
    )
    assert out == "15-Jan-2024"


# ── dispatch / misuse ────────────────────────────────


def test_unknown_op_returns_error() -> None:
    t = DateCalcTool()
    out = t.call(op="bogus")  # type: ignore[arg-type]
    assert out.startswith("error:")
