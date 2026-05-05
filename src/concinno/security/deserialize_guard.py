"""concinno.security.deserialize_guard — AST-based unsafe-deserialize scanner.

@module security.deserialize_guard
@responsibility Static-analysis guard (4.3.0 Plan B Week 1) that scans
    Python source — provided as a string, a :class:`pathlib.Path`, or a
    pre-parsed :class:`ast.Module` — and flags calls that deserialize
    untrusted bytes through inherently dangerous APIs:

      * ``pickle.load`` / ``pickle.loads``        (critical — RCE on load)
      * ``yaml.load`` / ``yaml.unsafe_load``      (critical when Loader is
                                                   not SafeLoader / CSafeLoader)
      * ``dill.load`` / ``dill.loads``            (high — superset of pickle)
      * ``marshal.load`` / ``marshal.loads``      (high — version-locked but
                                                   still arbitrary code)
      * ``eval(...)`` / ``exec(...)``             (critical when arg is not a
                                                   literal constant)
      * ``__reduce__`` method overrides           (low — often legitimate)
      * ``subprocess.Popen(..., shell=True)``     (medium — RCE adjacent)

    The scan is **purely static** — we never import or execute the code
    being analysed. We use the stdlib :mod:`ast` module and never
    ``compile(... "exec")``-execute the tree. Findings localise to the
    exact ``(start, end)`` byte offsets of the call expression so an
    audit tool can render context.

@dependencies stdlib only — :mod:`ast`, :mod:`pathlib`, :mod:`re`.
@exports
    DeserializeGuard,
    DeserializeFinding (re-export of :class:`Finding` for convenience),
    PATTERN_SEVERITY (mapping of detector key → default severity),
    SAFE_YAML_LOADERS (frozenset of loader names treated as safe).

The guard inherits :class:`PolicyGate` so it gains the standard 4-tier
fail-mode chain (``silent`` / ``warn`` / ``warn+log`` / ``hard_deny``),
profile-aware fail-mode resolution via
:func:`feature_config.get_fail_mode`, the
``# CONCINNO_DISABLE:deserialize_guard:<reason>`` escape hatch, and the
ZIQ outcome bus emit hook for online learning. See
:mod:`concinno.security.policy_gate` for the base contract.

False-positive controls
-----------------------
1. Per-call escape comment: ``# CONCINNO_DISABLE:deserialize_guard:<reason>``
   on the same source line as the offending call short-circuits the
   detector for *just that call*. The base class also recognises the
   broader ``# CONCINNO_DISABLE:`` token (no guard suffix) which
   suppresses the entire scan; that broader form is delegated to the
   base.
2. Test-fixture path detection: when the input is a :class:`Path` whose
   string form contains ``/tests/`` or ``\\tests\\``, every finding has
   its severity downgraded by one tier (``critical`` → ``high`` → ``medium``
   → ``low``; ``low`` stays ``low``). Test fixtures often deserialize
   pickles deliberately as part of the test setup.
3. Trusted-source allow-list: the ``trusted_modules`` parameter (default
   empty) suppresses findings whose call expression resolves to a name
   on the list. ``"json"`` is conceptually safe but never deserialised
   here because we don't flag it; the list exists for legitimate uses
   like ``yaml.safe_load``-pinned consumers.

The detector is conservative on ``eval`` / ``exec``: a literal-constant
argument (string / number / bytes) is treated as safe (severity
``low``), while a *Name* / *Call* / *Attribute* argument escalates to
``critical`` because the LLM agent that generated the code likely
threaded user input through it.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any, Literal

from concinno.security.policy_gate import (
    Finding as DeserializeFinding,
)
from concinno.security.policy_gate import (
    PolicyGate,
    Severity,
)

__all__ = [
    "DeserializeFinding",
    "DeserializeGuard",
    "PATTERN_SEVERITY",
    "SAFE_YAML_LOADERS",
]


# ── Severity taxonomy ────────────────────────────────────────────

#: Default severity per detector key. Subclasses can override by
#: passing ``pattern_severity_overrides=`` to ``__init__``.
PATTERN_SEVERITY: dict[str, Severity] = {
    "pickle.load": "critical",
    "pickle.loads": "critical",
    "yaml.load": "critical",
    "yaml.unsafe_load": "critical",
    "dill.load": "high",
    "dill.loads": "high",
    "marshal.load": "high",
    "marshal.loads": "high",
    "eval": "critical",
    "exec": "critical",
    "eval.literal": "low",
    "exec.literal": "low",
    "__reduce__": "low",
    "subprocess.Popen.shell": "medium",
}

#: Loader names treated as safe for ``yaml.load``. Anything outside this
#: set (default Loader, FullLoader, UnsafeLoader, custom subclass) is a
#: critical finding.
SAFE_YAML_LOADERS: frozenset[str] = frozenset({
    "SafeLoader",
    "CSafeLoader",
    "BaseLoader",  # contentless — safe by definition.
})

#: ``yaml`` module-level functions that are inherently safe (no Loader
#: argument can make them unsafe).
SAFE_YAML_FUNCTIONS: frozenset[str] = frozenset({
    "safe_load",
    "safe_load_all",
})

_SEVERITY_LADDER: tuple[Severity, ...] = ("critical", "high", "medium", "low")


def _downgrade_severity(sev: Severity) -> Severity:
    """Drop one tier in :data:`_SEVERITY_LADDER`. ``low`` stays ``low``."""
    try:
        idx = _SEVERITY_LADDER.index(sev)
    except ValueError:
        return sev
    if idx == len(_SEVERITY_LADDER) - 1:
        return sev
    return _SEVERITY_LADDER[idx + 1]


_MIN_SEVERITY_RANK: dict[str, int] = {
    "low": 0,
    "medium": 1,
    "high": 2,
    "critical": 3,
}


# ── Per-line escape comment regex ────────────────────────────────

# Matches ``# CONCINNO_DISABLE:deserialize_guard:<reason>``. The trailing
# group is optional but conventional. We only require the guard-name
# segment so a per-line escape doesn't accidentally trigger on the
# broad token (which the base class handles).
_PER_LINE_ESCAPE = re.compile(
    r"#\s*CONCINNO_DISABLE\s*:\s*deserialize_guard\b",
    re.IGNORECASE,
)


# ── Helpers for AST traversal ────────────────────────────────────


def _qualified_name(node: ast.AST) -> str:
    """Return ``"module.attr.attr"`` for an ``Attribute`` chain.

    Examples::

        pickle.loads        -> "pickle.loads"
        a.b.c               -> "a.b.c"
        foo()               -> ""        (not an Attribute / Name)
    """
    parts: list[str] = []
    cur: ast.AST | None = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
        return ".".join(reversed(parts))
    return ""


def _attr_tail(node: ast.AST) -> str:
    """Return just the trailing attribute name for an ``Attribute`` node.

    For ``yaml.load`` returns ``"load"``. For a plain ``Name`` returns the
    name itself. Returns ``""`` for anything else.
    """
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _is_literal_argument(node: ast.AST) -> bool:
    """``True`` when ``node`` is a constant string / bytes / number.

    Used to decide whether ``eval(arg)`` / ``exec(arg)`` is the
    benign literal form (``eval("1 + 2")``) or the dangerous variable
    form (``eval(user_input)``). Tuples / lists of constants count as
    literal — they cannot reach a code path that executes user input.
    """
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, (ast.Tuple, ast.List)):
        return all(_is_literal_argument(elt) for elt in node.elts)
    if isinstance(node, ast.JoinedStr):
        # f-string with no FormattedValue children → constant.
        return all(isinstance(v, ast.Constant) for v in node.values)
    return False


def _yaml_loader_keyword(call: ast.Call) -> ast.AST | None:
    """Return the ``Loader=`` value of a ``yaml.load`` call, or None.

    We accept both ``Loader=`` and (rarer) the second positional
    argument. ``yaml.load(stream)`` with no Loader is a critical
    finding because PyYAML defaults to ``FullLoader`` which is *not*
    safe for untrusted input despite the name.
    """
    for kw in call.keywords:
        if kw.arg == "Loader":
            return kw.value
    if len(call.args) >= 2:
        return call.args[1]
    return None


def _is_safe_yaml_loader(loader_node: ast.AST | None) -> bool:
    """``True`` when the Loader expression resolves to a known-safe class."""
    if loader_node is None:
        return False
    tail = _attr_tail(loader_node)
    return tail in SAFE_YAML_LOADERS


def _line_text(source_lines: list[str], lineno: int) -> str:
    """Return the source line ``lineno`` (1-indexed) or ``""``."""
    if 1 <= lineno <= len(source_lines):
        return source_lines[lineno - 1]
    return ""


# ── Default trusted-modules allow-list ───────────────────────────

#: Empty by default. Operators can pass ``trusted_modules={"my.safe.api"}``
#: to suppress findings whose qualified name starts with one of those
#: prefixes. We never seed this list — the default is "trust nothing"
#: so the guard doesn't quietly green-light a popular-but-unsafe lib.
DEFAULT_TRUSTED_MODULES: frozenset[str] = frozenset()


# ── Guard ────────────────────────────────────────────────────────


class DeserializeGuard(PolicyGate):
    """Static AST scanner for unsafe deserialize patterns.

    Accepts ``str`` (Python source), :class:`pathlib.Path` (a Python
    file to read), or :class:`ast.Module` (a pre-parsed tree). All other
    payload types resolve to a single ``low`` finding of type
    ``"malformed_payload"`` rather than raising.

    Parameters:
        profile: feature-toggle profile name (``lite`` / ``mainstream``
            / ``strict`` / ``paranoid``). Forwarded to :class:`PolicyGate`.
        fail_mode_override: explicit fail-mode that beats the profile
            chain. Forwarded to :class:`PolicyGate`.
        allow_pickle_with_protocol: when ``True``, suppress
            ``pickle.load`` / ``pickle.loads`` findings. Mirrors the
            FEATURE_META ``params`` schema. Default ``False``.
        yaml_safe_loader_only: when ``True``, every ``yaml.load`` call
            that does not pass an explicit safe Loader is flagged.
            Default ``True``.
        flag_reduce_override: when ``True``, classes that override
            ``__reduce__`` get a low-severity finding. Default ``True``.
        min_severity: drop findings whose severity is below this rung
            of the ladder. Default ``"medium"`` (drops ``"low"`` only).
        trusted_modules: prefixes of dotted call-targets that should
            be silently allowed (e.g. ``{"my.safe.wrapper"}``).
        pattern_severity_overrides: per-detector severity override map.
    """

    name: str = "deserialize_guard"

    def __init__(
        self,
        profile: str = "lite",
        fail_mode_override: Any = None,
        *,
        allow_pickle_with_protocol: bool = False,
        yaml_safe_loader_only: bool = True,
        flag_reduce_override: bool = True,
        min_severity: Literal["low", "medium", "high", "critical"] = "medium",
        trusted_modules: frozenset[str] | set[str] | None = None,
        pattern_severity_overrides: dict[str, Severity] | None = None,
    ) -> None:
        super().__init__(profile=profile, fail_mode_override=fail_mode_override)
        if min_severity not in _MIN_SEVERITY_RANK:
            raise ValueError(
                f"min_severity {min_severity!r} not in "
                f"{sorted(_MIN_SEVERITY_RANK)}"
            )
        self._allow_pickle_with_protocol = allow_pickle_with_protocol
        self._yaml_safe_loader_only = yaml_safe_loader_only
        self._flag_reduce_override = flag_reduce_override
        self._min_severity = min_severity
        self._trusted_modules: frozenset[str] = frozenset(
            trusted_modules or DEFAULT_TRUSTED_MODULES
        )
        # Per-instance copy so overrides don't leak across guards.
        self._severity: dict[str, Severity] = dict(PATTERN_SEVERITY)
        if pattern_severity_overrides:
            self._severity.update(pattern_severity_overrides)
        # Set when ``scan`` is called with a Path matching ``/tests/``.
        self._test_fixture_downgrade: bool = False

    # ── PolicyGate hook ─────────────────────────────────────────

    def scan(
        self, payload: str | bytes | dict[str, Any] | Path | ast.Module,
    ) -> list[DeserializeFinding]:
        """Parse ``payload`` and return findings.

        Returns one finding per detected unsafe call (or ``__reduce__``
        override). On AST parse failure returns a single ``low``
        finding of type ``"parse_error"`` so downstream chain still has
        something to evaluate — never raises.
        """
        try:
            tree, source_lines = self._coerce_to_tree(payload)
        except _MalformedPayload as exc:
            return [
                DeserializeFinding(
                    type="malformed_payload",
                    span=(-1, -1),
                    snippet="",
                    severity="low",
                    message=str(exc),
                )
            ]
        except SyntaxError as exc:
            return [
                DeserializeFinding(
                    type="parse_error",
                    span=(-1, -1),
                    snippet="",
                    severity="low",
                    message=f"AST parse failed: {exc.msg}",
                )
            ]

        visitor = _DeserializeVisitor(
            source_lines=source_lines,
            severity=self._severity,
            allow_pickle_with_protocol=self._allow_pickle_with_protocol,
            yaml_safe_loader_only=self._yaml_safe_loader_only,
            flag_reduce_override=self._flag_reduce_override,
            trusted_modules=self._trusted_modules,
        )
        visitor.visit(tree)

        # Apply test-fixture downgrade + min-severity filter.
        out: list[DeserializeFinding] = []
        downgrade = self._test_fixture_downgrade
        min_rank = _MIN_SEVERITY_RANK[self._min_severity]
        for f in visitor.findings:
            sev = _downgrade_severity(f.severity) if downgrade else f.severity
            if _MIN_SEVERITY_RANK[sev] < min_rank:
                continue
            if sev is f.severity:
                out.append(f)
            else:
                out.append(
                    DeserializeFinding(
                        type=f.type,
                        span=f.span,
                        snippet=f.snippet,
                        severity=sev,
                        message=f.message,
                    )
                )
        return out

    # ── Override for path-aware test-fixture detection ──────────

    def evaluate(self, payload: Any, **kwargs: Any) -> Any:
        """Detect ``/tests/`` paths so :meth:`scan` can downgrade.

        We don't change the signature beyond accepting :class:`Path`
        since the base typing already permits ``str | bytes | dict``;
        the runtime payload type is what matters here.
        """
        # Reset per-call so back-to-back evaluate() calls are independent.
        self._test_fixture_downgrade = False
        if isinstance(payload, Path):
            s = str(payload).replace("\\", "/").lower()
            if "/tests/" in s or s.endswith("/tests"):
                self._test_fixture_downgrade = True
        return super().evaluate(payload, **kwargs)

    # ── Internals ───────────────────────────────────────────────

    def _coerce_to_tree(
        self, payload: Any,
    ) -> tuple[ast.Module, list[str]]:
        """Convert ``payload`` to ``(ast.Module, source_lines)``.

        Raises :class:`_MalformedPayload` for unsupported types and
        :class:`SyntaxError` for unparseable source. The returned
        ``source_lines`` list is 0-indexed; callers add 1 to convert to
        AST ``lineno``.
        """
        if isinstance(payload, ast.Module):
            # No source available — per-line escape detection degrades
            # to "never matches". This is acceptable; pre-parsed trees
            # are the most niche input and operators using them have
            # already accepted that comments aren't preserved.
            return payload, []
        if isinstance(payload, Path):
            try:
                source = payload.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                raise _MalformedPayload(
                    f"Cannot read {payload}: {exc}"
                ) from exc
            return ast.parse(source, filename=str(payload)), source.splitlines()
        if isinstance(payload, bytes):
            source = payload.decode("utf-8", errors="replace")
            return ast.parse(source), source.splitlines()
        if isinstance(payload, str):
            return ast.parse(payload), payload.splitlines()
        if isinstance(payload, dict):
            # Dict payloads aren't source. Treat as malformed rather
            # than coercing through json.dumps — we'd produce a JSON
            # string that isn't valid Python.
            raise _MalformedPayload(
                "DeserializeGuard expects str / bytes / Path / ast.Module, "
                f"got dict (len={len(payload)})"
            )
        raise _MalformedPayload(
            f"DeserializeGuard expects str / bytes / Path / ast.Module, "
            f"got {type(payload).__name__}"
        )


class _MalformedPayload(Exception):
    """Internal — payload type cannot be parsed as Python source."""


# ── Visitor ──────────────────────────────────────────────────────


class _DeserializeVisitor(ast.NodeVisitor):
    """Walks an AST collecting unsafe-deserialize findings.

    State is per-visit; create a fresh visitor per :meth:`scan` call.
    """

    def __init__(
        self,
        *,
        source_lines: list[str],
        severity: dict[str, Severity],
        allow_pickle_with_protocol: bool,
        yaml_safe_loader_only: bool,
        flag_reduce_override: bool,
        trusted_modules: frozenset[str],
    ) -> None:
        self.findings: list[DeserializeFinding] = []
        self._lines = source_lines
        self._sev = severity
        self._allow_pickle = allow_pickle_with_protocol
        self._yaml_strict = yaml_safe_loader_only
        self._flag_reduce = flag_reduce_override
        self._trusted = trusted_modules

    # ── ast.NodeVisitor overrides ───────────────────────────────

    def visit_Call(self, node: ast.Call) -> None:
        qname = _qualified_name(node.func)
        tail = _attr_tail(node.func)

        if not self._should_inspect_call(node, qname):
            self.generic_visit(node)
            return

        self._dispatch_call(node, qname, tail)
        self.generic_visit(node)

    # ── Dispatch helpers (kept flat to satisfy nesting guard) ───

    def _should_inspect_call(self, node: ast.Call, qname: str) -> bool:
        """Return ``False`` if the call should be skipped entirely.

        Skip cases: per-line escape comment, trusted-module allow-list.
        """
        if self._line_escaped(node.lineno):
            return False
        if qname and self._is_trusted(qname):
            return False
        return True

    def _is_trusted(self, qname: str) -> bool:
        """Match ``qname`` against the trusted-module prefix list."""
        for prefix in self._trusted:
            if qname == prefix or qname.startswith(prefix + "."):
                return True
        return False

    def _dispatch_call(self, node: ast.Call, qname: str, tail: str) -> None:
        """Route a Call node to the right detector based on ``qname``."""
        if qname in {"pickle.load", "pickle.loads"}:
            if not self._allow_pickle:
                self._record(node, qname, qname)
            return
        if qname in {"dill.load", "dill.loads", "marshal.load", "marshal.loads"}:
            self._record(node, qname, qname)
            return
        if qname == "yaml.unsafe_load":
            self._record(node, qname, "yaml.unsafe_load")
            return
        if qname == "yaml.load":
            self._handle_yaml_load(node)
            return
        if self._is_popen(qname, tail):
            self._handle_popen(node, qname or tail)
            return
        if isinstance(node.func, ast.Name) and node.func.id in {"eval", "exec"}:
            self._handle_eval_exec(node, node.func.id)

    @staticmethod
    def _is_popen(qname: str, tail: str) -> bool:
        """``True`` for ``subprocess.Popen`` / bare ``Popen`` call sites."""
        if qname == "Popen":
            return True
        if tail == "Popen" and qname.endswith(".Popen"):
            return True
        return False

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        if (
            self._flag_reduce
            and node.name == "__reduce__"
            and not self._line_escaped(node.lineno)
        ):
            self._record_node(
                node,
                detector="__reduce__",
                snippet=f"def {node.name}(...)",
            )
        self.generic_visit(node)

    def visit_AsyncFunctionDef(  # pragma: no cover — equivalent path
        self, node: ast.AsyncFunctionDef,
    ) -> None:
        if (
            self._flag_reduce
            and node.name == "__reduce__"
            and not self._line_escaped(node.lineno)
        ):
            self._record_node(
                node,
                detector="__reduce__",
                snippet=f"async def {node.name}(...)",
            )
        self.generic_visit(node)

    # ── Specialised handlers ────────────────────────────────────

    def _handle_yaml_load(self, node: ast.Call) -> None:
        loader = _yaml_loader_keyword(node)
        if self._yaml_strict and not _is_safe_yaml_loader(loader):
            self._record(node, "yaml.load", "yaml.load")

    def _handle_popen(self, node: ast.Call, qname: str) -> None:
        for kw in node.keywords:
            if kw.arg == "shell":
                # Only flag when shell=True (a constant True).
                v = kw.value
                if isinstance(v, ast.Constant) and v.value is True:
                    self._record(
                        node,
                        "subprocess.Popen.shell",
                        snippet=f"{qname}(..., shell=True)",
                    )
                    return

    def _handle_eval_exec(self, node: ast.Call, fname: str) -> None:
        if not node.args:
            # ``eval()`` / ``exec()`` with zero args is a syntax error
            # at runtime; flag conservatively.
            self._record(node, fname, snippet=f"{fname}()")
            return
        first = node.args[0]
        if _is_literal_argument(first):
            self._record(
                node,
                f"{fname}.literal",
                snippet=f"{fname}(<literal>)",
            )
        else:
            self._record(node, fname, snippet=f"{fname}(<dynamic>)")

    # ── Recording ───────────────────────────────────────────────

    def _record(
        self, node: ast.AST, detector: str, snippet: str | None = None,
    ) -> None:
        sev = self._sev.get(detector, "medium")
        snip = snippet or detector
        self._record_node(node, detector=detector, snippet=snip, severity=sev)

    def _record_node(
        self,
        node: ast.AST,
        *,
        detector: str,
        snippet: str,
        severity: Severity | None = None,
    ) -> None:
        sev = severity if severity is not None else self._sev.get(detector, "medium")
        span = self._span_for(node)
        msg = f"unsafe-deserialize: {detector} at line {getattr(node, 'lineno', '?')}"
        self.findings.append(
            DeserializeFinding(
                type=detector,
                span=span,
                snippet=snippet,
                severity=sev,
                message=msg,
            )
        )

    def _span_for(self, node: ast.AST) -> tuple[int, int]:
        """Return ``(start_offset, end_offset)`` in the source string.

        Falls back to ``(-1, -1)`` when the AST node lacks position
        info (pre-parsed ``ast.Module`` inputs without source).
        """
        if not self._lines:
            return (-1, -1)
        lineno = getattr(node, "lineno", None)
        col = getattr(node, "col_offset", None)
        end_lineno = getattr(node, "end_lineno", None)
        end_col = getattr(node, "end_col_offset", None)
        if lineno is None or col is None:
            return (-1, -1)
        # Convert (line, col) -> char offset by summing prior line lengths
        # plus newline characters.
        start = sum(len(ln) + 1 for ln in self._lines[: lineno - 1]) + col
        if end_lineno is None or end_col is None:
            return (start, start)
        end = sum(len(ln) + 1 for ln in self._lines[: end_lineno - 1]) + end_col
        return (start, end)

    def _line_escaped(self, lineno: int) -> bool:
        text = _line_text(self._lines, lineno)
        return bool(_PER_LINE_ESCAPE.search(text))
