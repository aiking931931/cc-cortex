"""Structured-plan compute helper — calculator + statistics for agents.

Where ``python_exec`` lets an LLM evaluate arbitrary whitelisted Python
expressions, this module exposes a *structured plan* DSL — the agent
emits a JSON object describing the computation, and Python executes it
deterministically. The plan format is the one validated in
``cont'd¹⁰`` while solving GAIA df6561b2 (red/green numbers statistics);
the validation step caught Sonnet arithmetic-mid-precision drift that
free-form compute exhibited (3 trials gave 17.045 / 17.396 / 17.642 vs
expected 17.056). Plan execution sat at 3/3 stable.

Why a separate module from ``python_exec``:
  * **Narrower attack surface**: plan DSL only allows statistics /
    arithmetic operations on *named* data lists, not arbitrary code.
  * **Auditable**: each step (intermediate / final) returns its
    inputs and result, so the chain is fully traceable.
  * **LLM-friendly**: parsing natural-language operation specs into
    a constrained JSON plan is a sub-spec where Sonnet is reliable;
    by contrast, mid-precision arithmetic across N inputs is where
    LLMs drift.

Public API:
  * :func:`execute_statistics_plan`
  * :func:`execute_arithmetic_plan`
  * :func:`format_number`
  * :data:`STATS_FN_WHITELIST`
  * :data:`ARITHMETIC_OP_WHITELIST`
  * :class:`ComputeTool` (LLM-facing tool wrapper)

Feature gating: the LLM-facing tool registers under feature key
``compute_structured_plan`` (default on). Disable via
``cfg.feature("compute_structured_plan", "enabled") = False`` to hide
the tool from the agent loop. The Python API stays usable regardless.
"""

from __future__ import annotations

import json
import statistics
from typing import Any

# ─────────────────────────── whitelists ─────────────────────────────────


#: Functions in :mod:`statistics` accepted as ``fn`` values in a plan.
#: Any reduction operating on a list of numbers and returning a single
#: number. ``mode`` returns the most common value (multi-mode raises
#: ``StatisticsError`` — caller surfaces the error).
STATS_FN_WHITELIST: frozenset[str] = frozenset({
    "pstdev", "stdev", "pvariance", "variance",
    "mean", "median", "mode",
    "geometric_mean", "harmonic_mean", "fmean",
})


#: Binary / unary arithmetic operations accepted in
#: :func:`execute_arithmetic_plan`. ``add`` / ``sub`` / ``mul`` / ``div``
#: take exactly two operand IDs; ``neg`` / ``abs`` take one; ``pow``
#: takes two; ``sum_list`` / ``mean_list`` operate on a colour list ID.
ARITHMETIC_OP_WHITELIST: frozenset[str] = frozenset({
    "add", "sub", "mul", "div", "neg", "abs", "pow",
    "sum_list", "mean_list", "max_list", "min_list",
})


class ComputePlanError(ValueError):
    """Raised when a plan fails validation or an op fails to execute."""


# ─────────────────────────── helpers ────────────────────────────────────


def format_number(value: float, decimals: int | None = None) -> str:
    """Render a numeric value as a string.

    * If ``decimals`` is a non-negative integer, format with that many
      decimal places (``f"{value:.{decimals}f}"``).
    * If ``decimals`` is None and ``value`` is a whole number within
      0.05, render as an integer.
    * Otherwise keep three decimal places (the GAIA-grade default).

    Args:
        value: The number to format.
        decimals: Optional rounding instruction (0..12). Out-of-range
            or non-integer values are ignored.

    Returns:
        Formatted decimal string.
    """
    if isinstance(decimals, int) and 0 <= decimals <= 12:
        rounded = round(value, decimals)
        return f"{rounded:.{decimals}f}"
    nearest = round(value)
    if abs(value - nearest) < 0.05:
        return str(int(nearest))
    return f"{value:.3f}"


def _coerce_list(values: Any) -> list[float]:
    """Coerce a list-like to ``list[float]``. Empty / wrong-shape inputs
    raise :class:`ComputePlanError`."""
    if not isinstance(values, (list, tuple)):
        raise ComputePlanError(f"expected list, got {type(values).__name__}")
    out: list[float] = []
    for v in values:
        try:
            out.append(float(v))
        except (TypeError, ValueError) as err:
            raise ComputePlanError(
                f"non-numeric element in list: {v!r}",
            ) from err
    return out


# ──────────────────── execute_statistics_plan ───────────────────────────


def execute_statistics_plan(
    plan: dict, data: dict[str, list[float]],
) -> dict:
    """Run a statistics plan against named data lists.

    Plan schema::

        {
          "intermediate": [
            {"name": "<id>", "fn": "<stats fn>", "input": "<list id>"},
            ...
          ],
          "final": {"fn": "<stats fn>",
                    "input": "<list id>" | ["<intermediate id>", ...]},
          "round_decimals": <int N or null>
        }

    The ``fn`` field must be in :data:`STATS_FN_WHITELIST`.
    Intermediate ``input`` is always a colour-list name from ``data``.
    Final ``input`` may be a single list name (``str``) or a list of
    intermediate IDs (``list[str]``).

    Args:
        plan: The structured plan dict.
        data: Mapping of list name → numeric list.

    Returns:
        Dict with keys ``answer`` (formatted string), ``raw_result``
        (float), ``intermediates`` (id → float), ``plan`` (echoed).

    Raises:
        :class:`ComputePlanError` on any validation / execution failure.
    """
    if not isinstance(plan, dict):
        raise ComputePlanError("plan must be a dict")

    intermediate = plan.get("intermediate") or []
    if not isinstance(intermediate, list):
        raise ComputePlanError("plan.intermediate must be a list")

    intermediates: dict[str, float] = {}
    for entry in intermediate:
        if not isinstance(entry, dict):
            raise ComputePlanError(
                f"intermediate entry must be a dict: {entry!r}",
            )
        name = entry.get("name")
        fn = entry.get("fn")
        inp = entry.get("input")
        if not name or not isinstance(name, str):
            raise ComputePlanError(f"intermediate name invalid: {entry!r}")
        if fn not in STATS_FN_WHITELIST:
            raise ComputePlanError(
                f"fn not in whitelist: {fn!r} for {name}",
            )
        if not isinstance(inp, str) or inp not in data:
            raise ComputePlanError(
                f"intermediate input {inp!r} not in data for {name}",
            )
        fn_obj = getattr(statistics, fn)
        try:
            intermediates[name] = float(fn_obj(_coerce_list(data[inp])))
        except statistics.StatisticsError as err:
            raise ComputePlanError(
                f"statistics.{fn} on {inp}: {err}",
            ) from err

    final = plan.get("final")
    if not isinstance(final, dict):
        raise ComputePlanError("plan.final must be a dict")
    final_fn = final.get("fn")
    final_inp = final.get("input")
    if final_fn not in STATS_FN_WHITELIST:
        raise ComputePlanError(f"final fn not in whitelist: {final_fn!r}")
    fn_obj = getattr(statistics, final_fn)

    if isinstance(final_inp, str):
        if final_inp in data:
            final_input = _coerce_list(data[final_inp])
        elif final_inp in intermediates:
            final_input = [intermediates[final_inp]]
        else:
            raise ComputePlanError(f"final input not found: {final_inp!r}")
    elif isinstance(final_inp, list):
        final_input = []
        for ref in final_inp:
            if ref in intermediates:
                final_input.append(intermediates[ref])
            elif ref in data:
                raise ComputePlanError(
                    "final input list contains raw data id "
                    f"{ref!r} — use a single string for raw lists",
                )
            else:
                raise ComputePlanError(
                    f"final input id not found: {ref!r}",
                )
    else:
        raise ComputePlanError("final input must be str or list")

    try:
        result = float(fn_obj(final_input))
    except statistics.StatisticsError as err:
        raise ComputePlanError(
            f"statistics.{final_fn}: {err}",
        ) from err

    decimals = plan.get("round_decimals")
    answer = format_number(result, decimals if isinstance(decimals, int) else None)
    return {
        "answer": answer,
        "raw_result": result,
        "intermediates": intermediates,
        "plan": plan,
    }


# ──────────────────── execute_arithmetic_plan ───────────────────────────


def _arith_op(op: str, args: list[float]) -> float:
    """Execute a single arithmetic op on already-resolved float operands."""
    if op == "add":
        if len(args) != 2:
            raise ComputePlanError(f"add takes 2 args, got {len(args)}")
        return args[0] + args[1]
    if op == "sub":
        if len(args) != 2:
            raise ComputePlanError(f"sub takes 2 args, got {len(args)}")
        return args[0] - args[1]
    if op == "mul":
        if len(args) != 2:
            raise ComputePlanError(f"mul takes 2 args, got {len(args)}")
        return args[0] * args[1]
    if op == "div":
        if len(args) != 2:
            raise ComputePlanError(f"div takes 2 args, got {len(args)}")
        if args[1] == 0:
            raise ComputePlanError("division by zero")
        return args[0] / args[1]
    if op == "pow":
        if len(args) != 2:
            raise ComputePlanError(f"pow takes 2 args, got {len(args)}")
        return args[0] ** args[1]
    if op == "neg":
        if len(args) != 1:
            raise ComputePlanError(f"neg takes 1 arg, got {len(args)}")
        return -args[0]
    if op == "abs":
        if len(args) != 1:
            raise ComputePlanError(f"abs takes 1 arg, got {len(args)}")
        return abs(args[0])
    if op == "sum_list":
        return float(sum(args))
    if op == "mean_list":
        if not args:
            raise ComputePlanError("mean_list of empty list")
        return float(sum(args) / len(args))
    if op == "max_list":
        if not args:
            raise ComputePlanError("max_list of empty list")
        return float(max(args))
    if op == "min_list":
        if not args:
            raise ComputePlanError("min_list of empty list")
        return float(min(args))
    raise ComputePlanError(f"unknown op: {op!r}")


def execute_arithmetic_plan(
    plan: dict, variables: dict[str, float | list[float]],
) -> dict:
    """Run an arithmetic operation graph against named variables.

    Plan schema::

        {
          "steps": [
            {"name": "<id>", "op": "<op name>",
             "args": ["<var/step id>" | <number>, ...]},
            ...
          ],
          "final": "<id>",
          "round_decimals": <int N or null>
        }

    Each step's ``args`` may reference earlier step IDs, variable names
    in ``variables``, or be literal numbers. ``op`` must be in
    :data:`ARITHMETIC_OP_WHITELIST`. List-type ops (``sum_list`` /
    ``mean_list`` / ``max_list`` / ``min_list``) take a SINGLE arg
    referencing a variable that is itself a list.

    Args:
        plan: Operation graph dict.
        variables: Mapping of variable name → scalar or list.

    Returns:
        Dict with keys ``answer`` (formatted string), ``raw_result``
        (float), ``steps`` (id → float), ``plan`` (echoed).

    Raises:
        :class:`ComputePlanError` on any validation / execution failure.
    """
    if not isinstance(plan, dict):
        raise ComputePlanError("plan must be a dict")

    raw_steps = plan.get("steps") or []
    if not isinstance(raw_steps, list):
        raise ComputePlanError("plan.steps must be a list")

    resolved: dict[str, float] = {}

    def _resolve_arg(arg: Any, list_op: bool) -> Any:
        # For list ops, return the raw list; for scalar ops, return the
        # resolved float (from variables, prior steps, or literal).
        if isinstance(arg, (int, float)):
            return float(arg)
        if not isinstance(arg, str):
            raise ComputePlanError(f"invalid arg: {arg!r}")
        if arg in resolved:
            return resolved[arg]
        if arg in variables:
            v = variables[arg]
            if isinstance(v, list):
                if list_op:
                    return _coerce_list(v)
                raise ComputePlanError(
                    f"variable {arg!r} is a list — use a list op",
                )
            return float(v)
        raise ComputePlanError(f"unresolved arg: {arg!r}")

    for entry in raw_steps:
        if not isinstance(entry, dict):
            raise ComputePlanError(
                f"step must be a dict: {entry!r}",
            )
        name = entry.get("name")
        op = entry.get("op")
        args = entry.get("args")
        if not name or not isinstance(name, str):
            raise ComputePlanError(f"step name invalid: {entry!r}")
        if op not in ARITHMETIC_OP_WHITELIST:
            raise ComputePlanError(
                f"op not in whitelist: {op!r} for {name}",
            )
        if not isinstance(args, list):
            raise ComputePlanError(
                f"step {name!r} args must be a list",
            )
        list_op = op in {"sum_list", "mean_list", "max_list", "min_list"}
        if list_op:
            if len(args) != 1:
                raise ComputePlanError(
                    f"{op} takes exactly 1 list arg, got {len(args)}",
                )
            resolved_list = _resolve_arg(args[0], list_op=True)
            if not isinstance(resolved_list, list):
                raise ComputePlanError(
                    f"{op} input must be a list",
                )
            value = _arith_op(op, resolved_list)
        else:
            scalar_args = [_resolve_arg(a, list_op=False) for a in args]
            value = _arith_op(op, scalar_args)
        resolved[name] = value

    final_id = plan.get("final")
    if not isinstance(final_id, str):
        raise ComputePlanError("plan.final must be a step id string")
    if final_id not in resolved:
        raise ComputePlanError(f"final step not found: {final_id!r}")
    result = resolved[final_id]

    decimals = plan.get("round_decimals")
    answer = format_number(result, decimals if isinstance(decimals, int) else None)
    return {
        "answer": answer,
        "raw_result": result,
        "steps": resolved,
        "plan": plan,
    }


# ──────────────────────── LLM-facing tool ──────────────────────────────


_TOOL_DESCRIPTION = (
    "Execute a structured compute plan against named data and return a "
    "formatted numeric answer. Use this instead of doing arithmetic in "
    "your reasoning — Python computes deterministically.\n\n"
    "Plan kinds:\n"
    "  * statistics: {kind:'statistics', plan:<plan>, data:<lists>} — "
    "runs a chain of statistics-module reductions over named lists. "
    "Allowed fn: pstdev, stdev, pvariance, variance, mean, median, "
    "mode, geometric_mean, harmonic_mean, fmean.\n"
    "  * arithmetic: {kind:'arithmetic', plan:<plan>, variables:<vars>} "
    "— runs an arithmetic op graph. Allowed ops: add, sub, mul, div, "
    "neg, abs, pow, sum_list, mean_list, max_list, min_list.\n"
    "Both plan kinds support a top-level round_decimals integer (0-12) "
    "for the final answer.\n"
)


class ComputeTool:
    """LLM-facing tool wrapping :func:`execute_statistics_plan` and
    :func:`execute_arithmetic_plan`. Single entry-point with a
    ``kind`` discriminator so the LLM only has to learn one tool.

    Returns a JSON-serialisable dict with keys ``answer`` and
    ``raw_result`` on success, or ``{"error": "<message>"}`` on
    validation failure.
    """

    name = "compute"
    description = _TOOL_DESCRIPTION

    def __call__(self, **kwargs: Any) -> dict:
        kind = kwargs.get("kind")
        if kind == "statistics":
            try:
                return execute_statistics_plan(
                    plan=kwargs.get("plan", {}),
                    data=kwargs.get("data", {}),
                )
            except ComputePlanError as err:
                return {"error": str(err)}
        if kind == "arithmetic":
            try:
                return execute_arithmetic_plan(
                    plan=kwargs.get("plan", {}),
                    variables=kwargs.get("variables", {}),
                )
            except ComputePlanError as err:
                return {"error": str(err)}
        return {"error": f"unknown kind: {kind!r}"}

    def call_json(self, raw_arg: str) -> str:
        """Parse a JSON-encoded argument string and return JSON output.
        Convenience for tool-loop integrations that pass strings."""
        try:
            args = json.loads(raw_arg)
        except json.JSONDecodeError as err:
            return json.dumps({"error": f"json parse: {err}"})
        if not isinstance(args, dict):
            return json.dumps({"error": "args must be a JSON object"})
        return json.dumps(self(**args))


__all__ = [
    "ARITHMETIC_OP_WHITELIST",
    "ComputePlanError",
    "ComputeTool",
    "STATS_FN_WHITELIST",
    "execute_arithmetic_plan",
    "execute_statistics_plan",
    "format_number",
]
