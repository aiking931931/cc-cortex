"""Tests for :mod:`concinno.tools.builtin.python_exec`.

Covers:

* Happy-path arithmetic / sequence / comprehension expressions.
* AST reject paths (attribute, lambda, assign, import, statements).
* Builtin gating (``open`` / ``eval`` / ``getattr`` unreachable).
* Size caps (source length, node count).
* Tool contract (``name`` / ``description`` / ``is_concurrency_safe``).
"""

from __future__ import annotations

import pytest

from concinno.tools.builtin.python_exec import (
    PythonExecError,
    PythonExecTool,
    _evaluate,
)

# ── Contract ─────────────────────────────────────────


def test_contract_attrs() -> None:
    t = PythonExecTool()
    assert t.name == "python_exec"
    assert t.is_concurrency_safe is True
    # Description must name the key restrictions so the LLM knows
    # what it cannot do.
    desc = t.description
    assert "whitelist" in desc.lower()
    assert "no import" in desc.lower()
    assert "no lambda" in desc.lower()


# ── Happy path ───────────────────────────────────────


def test_basic_arithmetic() -> None:
    t = PythonExecTool()
    assert t.call(code="2 + 3 * 4") == "14"


def test_power_and_divmod() -> None:
    t = PythonExecTool()
    assert t.call(code="pow(2, 10)") == "1024"
    assert t.call(code="divmod(17, 5)") == "(3, 2)"


def test_list_comprehension() -> None:
    t = PythonExecTool()
    out = t.call(code="sum(x * x for x in range(10))")
    assert out == "285"


def test_sorted_and_zip() -> None:
    t = PythonExecTool()
    out = t.call(
        code="sorted(zip([3, 1, 2], ['c', 'a', 'b']))",
    )
    assert out == "[(1, 'a'), (2, 'b'), (3, 'c')]"


def test_dict_comprehension() -> None:
    t = PythonExecTool()
    out = t.call(code="{x: x*x for x in range(4)}")
    # Python dict str is deterministic insertion order.
    assert out == "{0: 0, 1: 1, 2: 4, 3: 9}"


def test_compare_and_boolean() -> None:
    t = PythonExecTool()
    assert t.call(code="3 < 5 < 10 and 1 == 1") == "True"


# ── Security: AST rejections ─────────────────────────


def test_rejects_attribute_access() -> None:
    t = PythonExecTool()
    # The classic escape vector.
    out = t.call(code="().__class__.__bases__[0]")
    assert out.startswith("error:")
    assert "disallowed" in out.lower()


def test_rejects_lambda() -> None:
    t = PythonExecTool()
    out = t.call(code="(lambda x: x + 1)(5)")
    assert out.startswith("error:")


def test_rejects_walrus() -> None:
    t = PythonExecTool()
    out = t.call(code="(x := 5) + x")
    assert out.startswith("error:")


def test_rejects_import_expression_level() -> None:
    # Can't import in eval-mode anyway, but the attribute escape
    # ``__import__`` as a Name is caught by the name whitelist.
    t = PythonExecTool()
    out = t.call(code="__import__('os').system('echo x')")
    assert out.startswith("error:")


# ── Security: builtin gating ─────────────────────────


def test_open_is_not_a_name() -> None:
    t = PythonExecTool()
    out = t.call(code="open('/etc/passwd')")
    assert out.startswith("error:")


def test_eval_exec_compile_unreachable() -> None:
    t = PythonExecTool()
    assert t.call(code="eval('1+1')").startswith("error:")
    assert t.call(code="exec('x=1')").startswith("error:")
    assert t.call(code="compile('1', '<x>', 'eval')").startswith("error:")


def test_getattr_setattr_unreachable() -> None:
    t = PythonExecTool()
    assert t.call(code="getattr([], 'append')").startswith("error:")
    assert t.call(code="setattr([], 'x', 1)").startswith("error:")


def test_string_method_call_blocked() -> None:
    # ``"x".upper()`` is an Attribute access on a constant — blocked
    # at the AST layer.
    t = PythonExecTool()
    out = t.call(code="'abc'.upper()")
    assert out.startswith("error:")


# ── Statements rejected pre-parse ────────────────────


def test_rejects_assignment() -> None:
    t = PythonExecTool()
    # ``mode='eval'`` already rejects assignment statements as a
    # SyntaxError; we just confirm the tool surfaces it cleanly.
    out = t.call(code="x = 1")
    assert out.startswith("error:")


def test_rejects_statements() -> None:
    t = PythonExecTool()
    out = t.call(code="for i in range(3): pass")
    assert out.startswith("error:")


# ── Size caps ────────────────────────────────────────


def test_rejects_oversized_source() -> None:
    t = PythonExecTool()
    # 10 KB of harmless arithmetic -> over the 8 KB cap.
    code = "1 + " * 3000 + "1"
    out = t.call(code=code)
    assert out.startswith("error:")
    assert "too long" in out.lower()


def test_rejects_excessive_node_count() -> None:
    # 300 nested parens = > _MAX_NODE_COUNT of 256.
    code = "(" * 300 + "1" + ")" * 300
    with pytest.raises(PythonExecError):
        _evaluate(code)


# ── Runtime errors surfaced as string ────────────────


def test_zero_division_returns_error_string() -> None:
    t = PythonExecTool()
    out = t.call(code="1 / 0")
    assert out.startswith("error:")
    assert "ZeroDivisionError" in out or "division" in out.lower()


def test_empty_code_returns_error() -> None:
    t = PythonExecTool()
    assert t.call(code="").startswith("error:")
    assert t.call(code="   ").startswith("error:")


def test_non_string_code_returns_error() -> None:
    t = PythonExecTool()
    assert t.call(code=None).startswith("error:")  # type: ignore[arg-type]
    assert t.call(code=123).startswith("error:")  # type: ignore[arg-type]
