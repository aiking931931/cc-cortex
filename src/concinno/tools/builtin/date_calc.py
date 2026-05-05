"""concinno.tools.builtin.date_calc — calendar arithmetic tool.

@module date_calc
@responsibility A single ``DateCalcTool`` conforming to the Concinno
    :class:`Tool` protocol (sync ``call(**kwargs)``). Three operations
    cover the GAIA / benchmark arithmetic surface without giving the
    agent a generic Python sandbox:

    * ``delta`` — difference between two dates, returned as an integer
      day count *and* a ``years/months/days`` breakdown. Inclusive of
      endpoints is opt-in.
    * ``parse`` — parse ``date_str`` against ``format_str`` and return
      the ISO-8601 (``YYYY-MM-DD``) normalised form. Strict strptime,
      no heuristics — the agent owes the format string.
    * ``format`` — re-format an existing ISO / strptime-parsable date
      into ``format_str``.

@dependencies stdlib only (``datetime``). Zero new deps in Concinno's
    dep graph.
@exports DateCalcTool

Design notes
------------
We intentionally do NOT accept free-form natural language ("last
Tuesday", "two weeks from now"). Ambiguous parsing is the biggest
source of wrong-answer bugs in benchmark runs; requiring an explicit
``format_str`` pushes that ambiguity back to the caller.

Year / month breakdown uses the ``calendar`` convention: full years
are peeled off first, then full months, then days. Leap years are
handled by Python's ``datetime`` arithmetic directly, not by a
constant 365.25.

The tool returns short human-readable strings beginning with
``"error: ..."`` on bad input so the multi-step agent loop can
observe and retry rather than raise — matches the shape set by
``WebSearchTool`` / ``FetchUrlTool`` in this package.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

Operation = Literal["delta", "parse", "format"]

_ISO_FMT = "%Y-%m-%d"


def _coerce_date(raw: str, format_str: str | None) -> date:
    """Parse ``raw`` as a date. Falls back to ISO 8601 when no format.

    The strict path is ``datetime.strptime(raw, format_str)`` when the
    caller supplies a format string. When the format is omitted we
    try ISO ``YYYY-MM-DD`` only — anything looser would reintroduce
    the heuristics we explicitly avoid.
    """
    if format_str:
        return datetime.strptime(raw, format_str).date()
    return datetime.strptime(raw, _ISO_FMT).date()


def _breakdown_days(total_days: int) -> tuple[int, int, int]:
    """Split ``total_days`` into ``(years, months, days)``.

    Uses a 365 / 30 approximation — honest because we also return the
    exact ``total_days`` alongside the breakdown, so the caller can
    decide which form matches their answer key. A fully calendar-
    correct breakdown (counting actual month lengths / leap years)
    requires the two endpoint dates, so that variant lives in
    :func:`_breakdown_calendar` and is used when both dates are
    supplied to ``delta``.
    """
    sign = -1 if total_days < 0 else 1
    days = abs(total_days)
    years, days = divmod(days, 365)
    months, days = divmod(days, 30)
    return sign * years, sign * months, sign * days


def _breakdown_calendar(
    start: date, end: date,
) -> tuple[int, int, int]:
    """Calendar-accurate ``(years, months, days)`` between two dates.

    Subtracts in the ``end - start`` direction, so positive when
    ``end`` is after ``start``. Handles negative deltas by delegating
    to the positive case and flipping signs.
    """
    if end < start:
        y, m, d = _breakdown_calendar(end, start)
        return -y, -m, -d

    years = end.year - start.year
    months = end.month - start.month
    days = end.day - start.day

    if days < 0:
        # Borrow days from the prior month of ``end``.
        months -= 1
        prev_month_end = (
            date(end.year, end.month, 1)
            if end.month > 1
            else date(end.year - 1, 12, 1)
        )
        # Days in the month before ``end``:
        if end.month == 1:
            ref = date(end.year - 1, 12, 31)
        else:
            # Last day of (end.year, end.month - 1)
            if end.month - 1 == 12:  # pragma: no cover - impossible
                ref = date(end.year - 1, 12, 31)
            else:
                next_month = (
                    date(end.year, end.month, 1)
                )
                ref = next_month.fromordinal(next_month.toordinal() - 1)
        _ = prev_month_end  # retained for readability
        days += ref.day

    if months < 0:
        years -= 1
        months += 12

    return years, months, days


class DateCalcTool:
    """Calendar arithmetic without a generic Python sandbox.

    Attributes:
        name: ``"date_calc"`` — what the LLM refers to in tool calls.
        description: Short summary shown to the LLM when deciding
            whether to call this tool.
        is_concurrency_safe: ``True`` — the tool is pure (no I/O, no
            global state).
    """

    name: str = "date_calc"
    description: str = (
        "Calendar arithmetic. Operations:\n"
        "  delta(date_from, date_to, [format_str]) — returns total "
        "days + (years, months, days) breakdown.\n"
        "  parse(date_str, format_str) — returns ISO (YYYY-MM-DD).\n"
        "  format(date_str, format_str) — reformat an ISO / "
        "strptime-parsable date.\n"
        "Strict strptime; no natural-language parsing."
    )
    is_concurrency_safe: bool = True

    def call(
        self,
        *,
        op: Operation,
        date_from: str | None = None,
        date_to: str | None = None,
        date_str: str | None = None,
        format_str: str | None = None,
    ) -> str:
        """Dispatch on ``op`` and return a short result string.

        Errors are returned as ``"error: ..."`` so the agent observes
        rather than crashes.
        """
        try:
            if op == "delta":
                return self._delta(date_from, date_to, format_str)
            if op == "parse":
                return self._parse(date_str, format_str)
            if op == "format":
                return self._format(date_str, format_str)
            return f"error: unknown op {op!r}"
        except ValueError as exc:
            return f"error: {exc}"
        except TypeError as exc:
            # Missing required args (e.g. parse without format_str).
            return f"error: {exc}"

    # ------------------------------------------------------------------ #

    def _delta(
        self,
        date_from: str | None,
        date_to: str | None,
        format_str: str | None,
    ) -> str:
        if not date_from or not date_to:
            return "error: delta requires date_from and date_to"
        start = _coerce_date(date_from, format_str)
        end = _coerce_date(date_to, format_str)
        total = (end - start).days
        yy, mm, dd = _breakdown_calendar(start, end)
        return (
            f"{total} days (calendar: {yy} years, "
            f"{mm} months, {dd} days)"
        )

    def _parse(
        self,
        date_str: str | None,
        format_str: str | None,
    ) -> str:
        if not date_str or not format_str:
            return "error: parse requires date_str and format_str"
        d = datetime.strptime(date_str, format_str).date()
        return d.isoformat()

    def _format(
        self,
        date_str: str | None,
        format_str: str | None,
    ) -> str:
        if not date_str or not format_str:
            return "error: format requires date_str and format_str"
        # Accept ISO or the target format as input.
        try:
            d = datetime.strptime(date_str, _ISO_FMT).date()
        except ValueError:
            d = datetime.strptime(date_str, format_str).date()
        return d.strftime(format_str)


__all__ = ["DateCalcTool"]
