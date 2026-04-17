"""concinno.tools.builtin.python_exec — sandboxed Python expression eval.

@module python_exec
@responsibility A single ``PythonExecTool`` conforming to the Concinno
    :class:`Tool` protocol (sync ``call(**kwargs)``). Evaluates a
    **pure expression** (``mode='eval'``, not ``exec``) under an AST
    whitelist + builtin whitelist, intended for the LLM's arithmetic
    / list-munging needs during benchmark runs when ``date_calc``
    alone is not enough.

    Explicit non-goals:

    * **No statements.** No assignment, no ``import``, no ``def``,
      no ``class``, no ``for``/``while``, no ``try``. If the caller
      wants statements, they are asking for a different tool.
    * **No attribute access.** ``().__class__.__bases__[0].__subclasses__()``
      escape is closed at the AST layer, not the builtins layer.
    * **No networking / filesystem / process.** Builtins list
      deliberately excludes ``open``, ``eval``, ``exec``, ``compile``,
      ``__import__``, ``getattr``, ``setattr``, ``delattr``,
      ``globals``, ``locals``, ``vars``, ``dir``, ``input``,
      ``help``, ``type`` (would let callers synthesize arbitrary
      classes).

@dependencies stdlib only (``ast``). Zero new deps in Concinno.
@exports PythonExecTool, PythonExecError

Threat model
------------
The AST whitelist is the primary defence — even if the builtins dict
were wrong, the absence of ``ast.Attribute`` / ``ast.Import`` /
``ast.ImportFrom`` / ``ast.Lambda`` / ``ast.FunctionDef`` /
``ast.ClassDef`` / ``ast.Assign`` / ``ast.AugAssign`` /
``ast.NamedExpr`` means none of the standard escape payloads can
even parse. The builtins whitelist is a second sieve that only adds
pure functions (no introspection surface).

We also limit the source length (8 KB) and parse-tree size (256
nodes) because the caller controls the string verbatim and we do not
want a pathological expression like ``((...((1))...))`` with a
million nested parens to DoS the parser. These are soft limits —
the purpose is fail-fast audit, not anti-abuse cryptography.
"""

from __future__ import annotations

import ast

_MAX_SOURCE_LEN = 8_192
"""Hard source-length cap. Benchmark expressions rarely exceed 200 chars;
8 KB is ~40× larger than anything legit and still small enough to keep
``ast.parse`` linear."""

_MAX_NODE_COUNT = 256
"""Upper bound on AST node count. A 256-node expression is already
unreadable; real benchmark uses run 10-50."""

_ALLOWED_NODES: frozenset[type[ast.AST]] = frozenset(
    {
        ast.Expression,
        # literals
        ast.Constant,
        # names (gated via _ALLOWED_NAMES when ctx=Load; Store is
        # allowed unconditionally for comprehension targets — the
        # comprehension variable binds only inside its own generator
        # frame so leaking a name here cannot reach the sandbox
        # outside the one expression).
        ast.Name, ast.Load, ast.Store,
        # operators
        ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare,
        ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod,
        ast.Pow, ast.FloorDiv, ast.MatMult,
        ast.USub, ast.UAdd, ast.Not, ast.Invert,
        ast.And, ast.Or,
        ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
        ast.In, ast.NotIn, ast.Is, ast.IsNot,
        # containers
        ast.Tuple, ast.List, ast.Dict, ast.Set,
        ast.Starred,
        # subscript / slice
        ast.Subscript, ast.Slice,
        # conditional expression
        ast.IfExp,
        # function calls (callees gated via _ALLOWED_NAMES)
        ast.Call, ast.keyword,
        # comprehensions
        ast.ListComp, ast.SetComp, ast.DictComp,
        ast.GeneratorExp, ast.comprehension,
        # bitwise — sometimes useful for benchmark math
        ast.BitOr, ast.BitAnd, ast.BitXor, ast.LShift, ast.RShift,
    }
)
"""Every ``ast.AST`` subclass the sandbox will accept. Any node type
outside this set raises :class:`PythonExecError`. Critically absent:

* ``Attribute`` — closes ``obj.__class__`` escapes.
* ``Lambda``, ``FunctionDef``, ``ClassDef`` — closes callable
  synthesis.
* ``Import``, ``ImportFrom`` — closes the module system.
* ``Assign``, ``AugAssign``, ``NamedExpr`` — closes assignment
  (pure expression only).
* ``For``, ``While``, ``If``, ``Try``, ``With``, ``Return``,
  ``Yield``, ``Await``, ``Global``, ``Nonlocal``, ``Delete`` — no
  statements parse in ``mode='eval'`` but listed for clarity.
"""

_ALLOWED_NAMES: frozenset[str] = frozenset(
    {
        # literal constants
        "True", "False", "None",
        # numeric
        "abs", "round", "pow", "divmod", "min", "max", "sum",
        # sequence / iteration
        "len", "range", "enumerate", "reversed", "sorted", "zip",
        "map", "filter", "all", "any",
        # constructors (pure data, no introspection surface)
        "bool", "int", "float", "str", "list", "tuple", "dict",
        "set", "frozenset", "bytes", "complex",
        # char / number conversion
        "ord", "chr", "hex", "oct", "bin",
    }
)
"""Every name the sandbox resolves. Both ``ast.Name`` references and
``ast.Call`` callees must have their ``.id`` (or
``Call.func.id`` after a callee-is-Name check) in this set.

Deliberately absent: ``open``, ``eval``, ``exec``, ``compile``,
``__import__``, ``getattr``, ``setattr``, ``delattr``, ``globals``,
``locals``, ``vars``, ``dir``, ``input``, ``help``, ``type``,
``object``, ``super``, ``isinstance``, ``issubclass``,
``staticmethod``, ``classmethod``, ``property``, ``iter``, ``next``,
``breakpoint``, ``id``, ``memoryview``, ``bytearray``, ``slice``,
``format``, ``repr``, ``ascii``, ``hash``, ``print``. Some (``iter``/
``next``/``format``/``print``) are harmless but unused; adding them
later requires an explicit audit entry in this docstring."""


class PythonExecError(Exception):
    """Raised when the AST whitelist rejects an expression.

    Not caught by :meth:`PythonExecTool.call` — the tool converts the
    message to an ``"error: ..."`` string so the LLM sees a parseable
    observation rather than a stack trace. Direct callers of the
    helpers below (``_validate`` / ``_evaluate``) see the raised
    exception.
    """


def _validate(tree: ast.AST) -> None:
    """Walk ``tree`` and reject any disallowed node / name.

    Comprehension variables (the ``x`` in ``sum(x*x for x in ...)``)
    are resolved by first collecting every ``Store`` target name
    into a local-bindings set, then allowing Load references against
    that set on top of :data:`_ALLOWED_NAMES`. The comprehension
    frame does not leak outward, so the only names that can appear
    in Store context are generator targets — which are safe.
    """
    local_binds: set[str] = {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
    }

    node_count = 0
    for node in ast.walk(tree):
        node_count += 1
        if node_count > _MAX_NODE_COUNT:
            raise PythonExecError(
                f"expression too large: > {_MAX_NODE_COUNT} AST nodes"
            )
        if type(node) not in _ALLOWED_NODES:
            raise PythonExecError(
                f"disallowed AST node: {type(node).__name__}"
            )
        if isinstance(node, ast.Name):
            if isinstance(node.ctx, ast.Load):
                if (
                    node.id not in _ALLOWED_NAMES
                    and node.id not in local_binds
                ):
                    raise PythonExecError(
                        f"disallowed name: {node.id!r}"
                    )
        # Call callees must be ast.Name — closes ``"".join(...)`` and
        # all other attribute-style calls. Comprehension-bound names
        # are allowed here too so e.g. ``[f(x) for f in [abs]]`` works
        # when the bound value is itself a safe callable; the checker
        # only restricts what the AST can reference, not what values
        # flow through it.
        if isinstance(node, ast.Call):
            func = node.func
            if not isinstance(func, ast.Name):
                raise PythonExecError(
                    "call target must be a bare name (no attribute "
                    "calls)"
                )
            if (
                func.id not in _ALLOWED_NAMES
                and func.id not in local_binds
            ):
                raise PythonExecError(
                    f"disallowed call target: {func.id!r}"
                )


def _build_safe_globals() -> dict[str, object]:
    """Construct the ``globals`` dict handed to ``eval``.

    ``__builtins__`` is set to an empty dict so the interpreter
    cannot rescue the normal builtins; every allowed name is then
    added explicitly. This double-gate (AST + globals) means a bug
    in either layer does not open the sandbox.
    """
    import builtins as _b

    safe: dict[str, object] = {"__builtins__": {}}
    for name in _ALLOWED_NAMES:
        if name in ("True", "False", "None"):
            safe[name] = getattr(_b, name)
            continue
        value = getattr(_b, name, None)
        if value is not None:
            safe[name] = value
    return safe


def _evaluate(source: str) -> object:
    """Compile + run a whitelisted expression. Raises on any violation."""
    if len(source) > _MAX_SOURCE_LEN:
        raise PythonExecError(
            f"source too long: {len(source)} > {_MAX_SOURCE_LEN}"
        )
    try:
        tree = ast.parse(source, mode="eval")
    except SyntaxError as exc:
        raise PythonExecError(f"syntax error: {exc}") from exc
    _validate(tree)
    code = compile(tree, "<python_exec>", "eval")
    return eval(code, _build_safe_globals(), {})  # noqa: S307


class PythonExecTool:
    """Sandboxed Python expression evaluator.

    Attributes:
        name: ``"python_exec"`` — LLM-facing tool name.
        description: Reminds the LLM of the expression-only contract
            and lists the tool's usable builtins so wrong-tool calls
            are less likely.
        is_concurrency_safe: ``True`` — pure evaluation, no shared
            state.
    """

    name: str = "python_exec"
    description: str = (
        "Evaluate a single Python expression under a strict AST "
        "whitelist. Pure expression only — no import, no "
        "assignment, no attribute access, no lambda. Usable "
        "builtins: abs, round, pow, divmod, min, max, sum, len, "
        "range, enumerate, reversed, sorted, zip, map, filter, "
        "all, any, bool, int, float, str, list, tuple, dict, set, "
        "frozenset, bytes, complex, ord, chr, hex, oct, bin. "
        "Result is stringified via str(...) before return."
    )
    is_concurrency_safe: bool = True

    def call(self, *, code: str) -> str:
        """Evaluate ``code`` and return ``str(result)``.

        Errors (AST reject / syntax error / runtime exception) are
        returned as ``"error: ..."`` strings so the multi-step
        agent loop observes and retries rather than raising.
        """
        if not isinstance(code, str) or not code.strip():
            return "error: code must be a non-empty string"
        try:
            result = _evaluate(code)
        except PythonExecError as exc:
            return f"error: {exc}"
        except Exception as exc:  # noqa: BLE001 — observation tool
            return f"error: runtime: {type(exc).__name__}: {exc}"
        try:
            return str(result)
        except Exception as exc:  # noqa: BLE001 — defensive
            return f"error: stringify: {type(exc).__name__}: {exc}"


__all__ = ["PythonExecError", "PythonExecTool"]
