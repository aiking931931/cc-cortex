"""concinno.security.sql_injection_guard — SQL injection pattern scanner.

@module security.sql_injection_guard
@responsibility Inspect tool inputs for SQL injection patterns. SQLi
    is distinct from RCE / SSRF / unsafe deserialize because it
    targets *query construction* — the agent writes Python that
    interpolates user input into a SQL string instead of using a
    parametrized API. This guard scans the source side of that
    pipeline (Edit / Write / NotebookEdit payloads, plus inline
    Python passed through ``Bash python -c`` and ``run_python``)
    and flags the five canonical unsafe construction styles while
    whitelisting the four common safe alternatives.

    OWASP A03:2021 (Injection) is the top-line reference. Detection
    is regex-based and intentionally narrow: we'd rather miss a
    bespoke ORM helper than warn on every cursor.execute() the agent
    writes. Operators wanting deeper coverage can layer the L9
    PolicyEngine ContentPatternMatcher on top.

@dependencies stdlib only — :mod:`re`. Inherits :class:`PolicyGate`
    so the 4-tier fail-mode chain, escape hatch, audit log, and ZIQ
    outcome bus emit are reused verbatim.

@exports
    SqlInjectionGuard, SqlInjectionFinding (re-export of
    :class:`Finding` for convenience),
    extract_sql_payload, _SqlPayload (private dataclass).

Detection styles
----------------

Five **unsafe** styles produce findings:

  1. **String concatenation** — ``"SELECT ..." + user_input`` /
     ``"SELECT ..." + str(x) + "..."``. Severity ``critical`` when the
     concatenated token resolves to an obvious user-input name
     (``user_input`` / ``request.*`` / ``params[...]``), otherwise
     ``high``.
  2. **f-string interpolation** — ``f"SELECT ... WHERE id={value}"``.
     Severity ``high`` because the formatting happens at call site
     and there is no parametrization layer.
  3. **% formatting** — ``"... WHERE col=%s" % user_input`` /
     ``"... %s" % (a, b)``. Severity ``medium`` because the syntax
     occasionally turns up in legitimate cursor.execute("...", params)
     calls — we tighten the regex to require an explicit ``%`` outside
     a placeholder context.
  4. **.format() on SQL** — ``"SELECT ...".format(x)`` /
     ``query.format(x)``. Severity ``medium``.
  5. **Dynamic identifier injection** — ``f"SELECT * FROM {table}"``
     with no quoting helper. Severity ``low`` because identifier
     interpolation is sometimes unavoidable (sharded tables, dynamic
     view names) — but ``psycopg.sql.Identifier`` /
     ``sqlalchemy.sql.text`` users get a free pass via the whitelist.

Four **safe** styles short-circuit the scan (no findings):

  * Parametrized DB-API: ``cursor.execute("...?...", (...))`` /
    ``cursor.execute("...:name...", {...})`` /
    ``cursor.execute("...%(name)s...", {...})``.
  * SQLAlchemy ``text()`` with ``bindparams`` /
    ``.bindparam(...)``.
  * ORM filter syntax: ``Model.objects.filter(...)`` (Django),
    ``session.query(Model).filter_by(...)`` (SQLAlchemy ORM),
    ``Model.query.filter(...)``.
  * ``psycopg.sql.SQL(...).format(Identifier(...))`` /
    ``psycopg2.sql.*`` typed composition.

False-positive controls
-----------------------

* **File-extension gating**: only ``.py`` / ``.sql`` / ``.ipynb``
  bodies enter the scanner. ``.md`` / ``.txt`` / ``.json`` / unknown
  extensions are skipped silently — SQL fragments in Markdown
  examples are documentation, not execution.
* **Test-fixture skip**: lines whose left-of-string context contains
  ``test_`` / ``pytest`` / ``assert`` heuristics are skipped. Negative
  test data like ``"' OR 1=1 --"`` is by design in fixtures and
  flagging it teaches the agent to delete the fixture, which is the
  opposite of what we want.
* **Docstring / comment skip**: lines inside triple-quoted blocks
  or starting with ``#`` are skipped. The scanner uses a tiny
  line-by-line state machine — full Python parsing is out of scope
  (the caller owns lexing).

Wiring
------

The natural caller is the PreToolUse hook chain. A thin
:class:`SqlInjectionBaseGuard` adapter in
:mod:`concinno.guards.sql_injection_adapter` registers the guard in
the SECURITY layer of :func:`concinno.guards.registry.create_default_pipeline`
so every Edit / Write / NotebookEdit (and inline-python Bash) tool
call passes through ``SqlInjectionGuard().evaluate(...)`` before
anything reaches disk. The default ``warn+log`` fail-mode under the
``mainstream`` profile keeps the guard non-blocking; ``strict`` /
``paranoid`` upgrade to ``hard_deny``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, ClassVar

from concinno.guards.base import (
    BaseGuard,
    GuardCategory,
    GuardContext,
    GuardResult,
)
from concinno.security.policy_gate import (
    FailMode,
    Finding,
    PolicyGate,
    Severity,
)
from concinno.security.policy_gate import (
    Finding as SqlInjectionFinding,
)

__all__ = [
    "SqlInjectionFinding",
    "SqlInjectionGuard",
    "extract_sql_payload",
]


# ── File extension gate ────────────────────────────────────────

_SCAN_EXTENSIONS: frozenset[str] = frozenset({".py", ".sql", ".ipynb"})

# Tools whose ``tool_input`` may carry source code we want to scan.
# NotebookEdit ships ``new_source`` instead of ``content``; Bash
# carries inline Python through ``-c`` / heredocs which we reach via
# :func:`extract_sql_payload`.
_SCANNABLE_TOOLS: frozenset[str] = frozenset(
    {"Edit", "Write", "NotebookEdit", "Bash", "MultiEdit"}
)


# ── Patterns ───────────────────────────────────────────────────
#
# Heuristic rather than parser-grade. The aim is high precision on
# the five canonical agent-written shapes, not exhaustive grammar
# coverage. Patterns operate on a single physical line — multi-line
# query literals get scanned line-by-line, so a 3-line ``f"""..."""``
# block with interpolation flags on the line that contains the
# braces.

# A SQL-ish keyword set used to gate every detector. Without this
# gate we'd flag every ``f"hello {name}"`` in the codebase. The set
# is deliberately small and case-insensitive (we lowercase before
# scan) — adding obscure DDL keywords (``ALTER``, ``GRANT``) would
# trip on legitimate metaprogramming.
_SQL_KEYWORDS = (
    "select",
    "insert",
    "update",
    "delete from",
    "drop table",
    "create table",
    "where ",
    "values (",
    "values(",
    "from ",
)

# 1. Concatenation: ``"SELECT ..." + name`` / ``"SELECT ..." + str(x)``.
#    Anchors on a quoted string ending followed by ``+`` followed by
#    a non-string token. Quoted-string + quoted-string concatenation
#    is treated as a separate "static glue" case (severity ``low``,
#    flagged below if user input clearly threads through).
_PAT_CONCAT = re.compile(
    r"""
    (?P<sql>["']\s*\+\s*           # closing quote → + → optional ws
        (?!["']\s*\)?\s*$)         # not pure string-string glue at EOL
        [A-Za-z_][\w\.\[\]\(\)]*)  # variable / call / subscript
    """,
    re.VERBOSE,
)

# 2. f-string interpolation directly into SQL.
#    ``f"...{x}..."`` / ``rf"..."`` / ``fr"..."`` / triple-quoted variants.
#    The keyword gate below decides whether the f-string is SQL.
_PAT_FSTRING = re.compile(
    r"""
    (?P<prefix>\b(?:rf|fr|f)['"])      # f / rf / fr quote prefix
    (?P<body>[^'"]*?\{[^{}]+\}[^'"]*?) # body that contains {...}
    """,
    re.VERBOSE | re.IGNORECASE,
)

# 3. % formatting on a SQL string literal:
#    ``"... %s ..." % var`` / ``"...%(name)s..." % d``.
#    Heuristic: a quoted string followed by ``%`` and a non-quote
#    token. cursor.execute("...", (...)) does NOT use ``%`` so this
#    is reasonably specific.
_PAT_PERCENT = re.compile(
    r"""
    ["']\s*%\s*                # closing quote → % → optional ws
    (?:\(|[A-Za-z_])           # tuple or identifier (not another quote)
    """,
    re.VERBOSE,
)

# 4. ``.format()`` on a SQL string.
#    ``"SELECT ...".format(x)`` / ``query.format(x)``.
_PAT_FORMAT = re.compile(
    r"""
    (?:["']|\b[A-Za-z_]\w*)    # closing quote OR identifier
    \.format\s*\(              # literal .format(
    """,
    re.VERBOSE,
)

# 5. Dynamic identifier interpolation — same as f-string but the
#    interpolated token sits in an identifier position
#    (``FROM {table}`` / ``UPDATE {schema}.{tbl}``). We detect by
#    finding ``FROM`` / ``UPDATE`` / ``INTO`` immediately followed by
#    a brace-interpolated token in an f-string body. Severity is
#    deliberately ``low`` — sharded systems sometimes need this.
_PAT_DYNAMIC_IDENT = re.compile(
    r"""
    \b(?:FROM|UPDATE|INTO|JOIN)\s+ # SQL identifier-position keyword
    \{[^{}]+\}                     # brace-interpolated token
    """,
    re.VERBOSE | re.IGNORECASE,
)

# Whitelist patterns — line-level. If any of these match, we
# short-circuit and emit zero findings for the line.
_PAT_WHITELIST = re.compile(
    r"""
    (?:
        # parametrized DB-API: cursor.execute("...?...", (...))
        \bexecute\s*\(\s*["'][^"']*["']\s*,\s*(?:\(|\{|\[)
        |
        # SQLAlchemy text() with bindparams
        \btext\s*\([^)]*\)\s*\.\s*bindparam(?:s)?\b
        |
        # Django / SQLAlchemy ORM filter syntax
        \b(?:filter|filter_by|exclude|get|first|all)\s*\(
        |
        # psycopg.sql.SQL composition
        \b(?:psycopg|psycopg2)\.sql\.(?:SQL|Identifier|Literal|Composed)\b
        |
        # sqlalchemy.sql.text without trailing format()
        \bsqlalchemy\.sql\.text\s*\(
    )
    """,
    re.VERBOSE,
)

# Pytest / test-fixture heuristic — line is intentional bad-string
# bait, do not flag.
_PAT_TEST_NEGATIVE = re.compile(
    r"\b(?:test_|pytest\.|assert\s|@pytest|@parametrize|"
    r"\bExpected\s*\w*\s*=)",
    re.IGNORECASE,
)

# Markers for "the variable on the right side of `+` looks like
# user input" — promotes the concat finding from ``high`` to
# ``critical``.
_USER_INPUT_HINTS = re.compile(
    r"\b(?:user_input|request\.|req\.|params\[|args\[|"
    r"input\(|cgi\.|flask\.request|django\.request|"
    r"event\[|payload\[|body\[)",
    re.IGNORECASE,
)


# ── Payload extraction ─────────────────────────────────────────


@dataclass(frozen=True)
class _SqlPayload:
    """Code text + tool-call context for a single scan invocation.

    Attributes:
        code: The source text to scan. May be multi-line; the scanner
            walks it line by line.
        file_path: The target file path on disk, when known. Used for
            extension gating and for the audit-log entry.
        tool_name: ``Edit`` / ``Write`` / ``NotebookEdit`` / ``Bash`` /
            ``MultiEdit`` — included so the audit consumer can group
            findings by tool.
        language_hint: ``python`` / ``sql`` / ``ipynb`` / ``""``. The
            scanner currently treats all three identically but we
            keep the hint so future per-language tightening doesn't
            require a payload schema bump.
    """

    code: str
    file_path: str
    tool_name: str
    language_hint: str = ""


def _ext_of(path: str) -> str:
    """Return the lowercase extension of *path*, including the dot."""
    if not path or "." not in path:
        return ""
    return "." + path.rsplit(".", 1)[-1].lower()


def extract_sql_payload(
    tool_name: str, tool_input: dict[str, Any]
) -> _SqlPayload | None:
    """Extract a scannable payload from a CC hook ``tool_input`` dict.

    Returns ``None`` when the tool is out of scope (not in
    :data:`_SCANNABLE_TOOLS`) or when the target file extension is
    not in :data:`_SCAN_EXTENSIONS`. The extension gate keeps the
    scanner off ``.md`` / ``.json`` / ``.txt`` / unknown-extension
    payloads where SQL fragments are documentation, not code.

    The Bash branch only scans the command *string* — the scanner
    relies on the keyword gate to filter out non-Python commands. We
    cannot statically inspect the file extension of a Bash command,
    so we apply a softer rule: scan only when the command contains
    ``python`` / ``python3`` / ``ipython``. Other Bash commands are
    skipped.
    """
    if tool_name not in _SCANNABLE_TOOLS:
        return None
    if not isinstance(tool_input, dict):
        return None

    if tool_name in ("Edit", "Write", "MultiEdit"):
        path = str(tool_input.get("file_path", ""))
        ext = _ext_of(path)
        if ext not in _SCAN_EXTENSIONS:
            return None
        # Edit ships new_string; Write ships content; MultiEdit ships
        # an ``edits`` list of ``{old_string, new_string}`` records.
        if tool_name == "Edit":
            code = str(tool_input.get("new_string", ""))
        elif tool_name == "Write":
            code = str(tool_input.get("content", ""))
        else:  # MultiEdit
            edits = tool_input.get("edits") or []
            if not isinstance(edits, list):
                return None
            code = "\n".join(
                str(e.get("new_string", ""))
                for e in edits
                if isinstance(e, dict)
            )
        if not code:
            return None
        return _SqlPayload(
            code=code,
            file_path=path,
            tool_name=tool_name,
            language_hint=ext.lstrip("."),
        )

    if tool_name == "NotebookEdit":
        path = str(tool_input.get("notebook_path", ""))
        ext = _ext_of(path)
        if ext not in _SCAN_EXTENSIONS:
            return None
        code = str(tool_input.get("new_source", ""))
        if not code:
            return None
        return _SqlPayload(
            code=code,
            file_path=path,
            tool_name=tool_name,
            language_hint="ipynb",
        )

    # Bash branch — only scan when the command obviously runs Python.
    cmd = str(tool_input.get("command", ""))
    if not cmd:
        return None
    if not re.search(r"\b(?:python3?|ipython)\b", cmd):
        return None
    return _SqlPayload(
        code=cmd,
        file_path="",
        tool_name="Bash",
        language_hint="python",
    )


# ── Helpers ────────────────────────────────────────────────────


def _line_contains_sql_keyword(lower_line: str) -> bool:
    """Return True if *lower_line* contains a SQL keyword.

    Caller already lowercased — this is the cheap gate that lets us
    skip every line of a non-SQL file in O(N).
    """
    return any(kw in lower_line for kw in _SQL_KEYWORDS)


def _strip_docstrings_and_comments(code: str) -> list[tuple[int, str]]:
    """Return ``[(line_index, content)]`` with docstrings + comments dropped.

    Tiny line-by-line state machine — we only care about the four
    triple-quote variants and the ``#`` comment marker. Multi-line
    f-strings inside docstrings are deliberately not flagged because
    they are documentation. The state machine is conservative: when
    a triple-quote toggle could plausibly be inside a normal string
    we err on the side of *skipping* the line (lower false-positive
    rate is the goal).
    """
    out: list[tuple[int, str]] = []
    in_triple_double = False
    in_triple_single = False
    for i, line in enumerate(code.splitlines()):
        stripped = line.lstrip()
        # Pure comment line — drop.
        if stripped.startswith("#"):
            continue

        # Track triple-quote toggles. We count occurrences of
        # ``"""`` and ``'''`` per line; an odd count flips the state.
        td = line.count('"""')
        ts = line.count("'''")

        # If we start the line already inside a docstring, skip it
        # entirely — even if it contains an interpolation we don't
        # treat documentation as executed code.
        was_inside = in_triple_double or in_triple_single
        if td % 2 == 1:
            in_triple_double = not in_triple_double
        if ts % 2 == 1:
            in_triple_single = not in_triple_single
        if was_inside:
            continue

        # Drop inline comments — split on the first ``#`` that isn't
        # inside a string. The cheap heuristic: split on `` # `` (with
        # surrounding whitespace) which catches the typical case
        # without confusing `#` inside an f-string brace.
        idx = line.find(" # ")
        if idx >= 0:
            line = line[:idx]
        out.append((i, line))
    return out


# ── SqlInjectionGuard ──────────────────────────────────────────


class SqlInjectionGuard(PolicyGate):
    """Regex-based SQL injection detection guard.

    Subclass of :class:`PolicyGate` — see the base class for the
    fail-mode resolution chain, escape hatch, audit log, and ZIQ
    emit. This class only owns the pattern catalogue, the per-line
    scanner, and the safe-pattern whitelist.

    Args:
        profile: Active feature-toggle profile. Forwarded to the
            base class for fail-mode resolution.
        fail_mode_override: Pin the fail-mode for this instance,
            ignoring profile defaults. Tests use this to exercise
            each branch of the decision matrix.
        min_severity: Findings below this rank are dropped before the
            base class decides. ``"low"`` keeps every match incl.
            dynamic-identifier shape; ``"medium"`` (default) drops
            the dynamic-identifier shape only; ``"high"`` keeps the
            three high-severity styles only.
        skip_test_files: When True (default), scan skips files whose
            path contains ``/tests/`` or ``\\tests\\`` AND whose
            content lines look like pytest fixtures. Operators on
            production codebases that house tests under odd paths
            can flip this off.
    """

    name: str = "sql_injection_guard"

    _SEVERITY_RANK: ClassVar[dict[str, int]] = {
        "low": 0,
        "medium": 1,
        "high": 2,
        "critical": 3,
    }

    def __init__(
        self,
        profile: str = "lite",
        fail_mode_override: FailMode | None = None,
        *,
        min_severity: Severity = "medium",
        skip_test_files: bool = True,
    ) -> None:
        super().__init__(
            profile=profile, fail_mode_override=fail_mode_override
        )
        if min_severity not in self._SEVERITY_RANK:
            raise ValueError(
                f"min_severity {min_severity!r} not in "
                f"{sorted(self._SEVERITY_RANK)}"
            )
        self._min_severity: str = min_severity
        self._skip_test_files: bool = bool(skip_test_files)

    # ── Public scan ────────────────────────────────────────────

    def scan(
        self, payload: str | bytes | dict[str, Any] | _SqlPayload
    ) -> list[Finding]:
        """Return all SQL-injection findings in ``payload``.

        Accepts either a raw text payload (str / bytes / dict) for
        ad-hoc callers and tests, or a pre-extracted
        :class:`_SqlPayload` from :func:`extract_sql_payload`. The
        latter shape carries file-path + tool-name context so we can
        apply extension and test-fixture gates; the raw shape skips
        those gates and scans whatever it gets.

        Empty list = clean. The base class :meth:`evaluate` will turn
        a clean scan into ``Decision.accept`` and skip both the audit
        write and the stderr warn.
        """
        sql_payload = self._coerce_payload(payload)
        if sql_payload is None:
            return []

        code = sql_payload.code
        path = sql_payload.file_path
        if not code:
            return []

        is_test_path = self._skip_test_files and (
            "/tests/" in path.replace("\\", "/")
            or path.endswith("_test.py")
            or "/test_" in path.replace("\\", "/")
        )

        findings: list[Finding] = []
        offset = 0
        for _idx, raw_line in _strip_docstrings_and_comments(code):
            line_len = len(raw_line) + 1  # +1 for the newline
            line_offset = offset
            offset += line_len

            if not raw_line.strip():
                continue
            lower = raw_line.lower()
            if not _line_contains_sql_keyword(lower):
                continue
            if _PAT_WHITELIST.search(raw_line):
                continue
            if is_test_path and _PAT_TEST_NEGATIVE.search(raw_line):
                continue

            findings.extend(
                self._scan_line(raw_line, line_offset)
            )

        return [f for f in findings if self._at_or_above(f.severity)]

    # ── Internals ──────────────────────────────────────────────

    def _coerce_payload(
        self, payload: str | bytes | dict[str, Any] | _SqlPayload
    ) -> _SqlPayload | None:
        """Normalise *payload* into an :class:`_SqlPayload`.

        ``_SqlPayload`` → returned as-is. ``str`` / ``bytes`` → wrapped
        with empty file_path so extension gating is bypassed (caller
        opted in by passing raw text). ``dict`` → treated as a
        tool_input shape and routed through :func:`extract_sql_payload`
        with ``tool_name="Edit"`` as the default; callers wanting the
        precise extension gate should pass ``_SqlPayload`` directly.
        """
        if isinstance(payload, _SqlPayload):
            return payload
        if isinstance(payload, bytes):
            return _SqlPayload(
                code=payload.decode("utf-8", errors="replace"),
                file_path="",
                tool_name="",
                language_hint="python",
            )
        if isinstance(payload, str):
            return _SqlPayload(
                code=payload,
                file_path="",
                tool_name="",
                language_hint="python",
            )
        if isinstance(payload, dict):
            # Heuristic: dict with a ``code`` / ``content`` /
            # ``new_string`` key → treat as raw payload. dict with
            # ``tool_name`` + ``tool_input`` → route through
            # extract_sql_payload.
            if "tool_name" in payload and "tool_input" in payload:
                return extract_sql_payload(
                    str(payload.get("tool_name", "")),
                    payload.get("tool_input", {}),
                )
            for key in ("code", "content", "new_string", "new_source"):
                if key in payload and isinstance(payload[key], str):
                    return _SqlPayload(
                        code=payload[key],
                        file_path=str(payload.get("file_path", "")),
                        tool_name="",
                        language_hint="python",
                    )
        return None

    def _scan_line(self, line: str, line_offset: int) -> list[Finding]:
        """Run the five detectors on a single source line."""
        out: list[Finding] = []

        # 1. Concatenation
        for m in _PAT_CONCAT.finditer(line):
            tail = line[m.start():m.start() + 80]
            sev: Severity = (
                "critical" if _USER_INPUT_HINTS.search(tail) else "high"
            )
            out.append(
                Finding(
                    type="sqli.concat",
                    span=(line_offset + m.start(), line_offset + m.end()),
                    snippet=_redact(line.strip()),
                    severity=sev,
                    message="String concatenation into SQL — "
                    "use parametrized queries instead.",
                )
            )

        # 2. f-string
        for m in _PAT_FSTRING.finditer(line):
            out.append(
                Finding(
                    type="sqli.fstring",
                    span=(line_offset + m.start(), line_offset + m.end()),
                    snippet=_redact(line.strip()),
                    severity="high",
                    message="f-string interpolation into SQL — "
                    "use cursor.execute(query, params) parametrization.",
                )
            )

        # 3. % formatting
        for m in _PAT_PERCENT.finditer(line):
            out.append(
                Finding(
                    type="sqli.percent",
                    span=(line_offset + m.start(), line_offset + m.end()),
                    snippet=_redact(line.strip()),
                    severity="medium",
                    message="% formatting into SQL — use parametrized "
                    "API parameters instead of string formatting.",
                )
            )

        # 4. .format()
        for m in _PAT_FORMAT.finditer(line):
            out.append(
                Finding(
                    type="sqli.format",
                    span=(line_offset + m.start(), line_offset + m.end()),
                    snippet=_redact(line.strip()),
                    severity="medium",
                    message=".format() on SQL string — use parametrized "
                    "API parameters instead of string formatting.",
                )
            )

        # 5. Dynamic identifier
        for m in _PAT_DYNAMIC_IDENT.finditer(line):
            out.append(
                Finding(
                    type="sqli.dynamic_identifier",
                    span=(line_offset + m.start(), line_offset + m.end()),
                    snippet=_redact(line.strip()),
                    severity="low",
                    message="Dynamic identifier interpolation — use "
                    "psycopg.sql.Identifier or sqlalchemy.text() "
                    "to prevent table/column injection.",
                )
            )
        return out

    def _at_or_above(self, severity: Severity) -> bool:
        """Return True when *severity* meets the configured floor."""
        return self._SEVERITY_RANK.get(severity, 0) >= self._SEVERITY_RANK.get(
            self._min_severity, 0
        )


def _redact(line: str, max_len: int = 80) -> str:
    """Truncate *line* so audit lines stay readable."""
    if len(line) <= max_len:
        return line
    return line[: max_len - 3] + "..."


# ── BaseGuard adapter — wires into the PreToolUse pipeline ──────


class SqlInjectionBaseGuard(BaseGuard):
    """Pipeline adapter that wires :class:`SqlInjectionGuard` into
    :func:`concinno.guards.registry.create_default_pipeline`.

    PolicyGate is the right shape for *standalone* scanning — it
    returns a rich :class:`PolicyGateResult` and integrates with the
    fail-mode chain / audit log / ZIQ outcome bus. The unified
    PreToolUse pipeline, however, speaks the
    :class:`concinno.guards.base.BaseGuard` contract and emits
    :class:`concinno.guards.base.GuardResult`. This class is the
    minimal bridge:

      * ``check`` extracts the source payload via
        :func:`extract_sql_payload`,
      * runs ``SqlInjectionGuard().evaluate(...)``,
      * maps ``deny`` → ``GuardResult.deny`` (hard block),
        ``warn`` → ``GuardResult.allow_advisory(context=...)``
        (visible to the LLM, suppressed in ``competition`` profile),
        ``accept`` → ``None`` (no opinion).

    Default category is ``SECURITY`` — SQL injection is on par with
    deserialize / RCE / SSRF as a hard-deny threat class. No
    step-back; the user can paste ``# CONCINNO_DISABLE:<reason>``
    into the source line to override per the PolicyGate base
    contract.
    """

    name: str = "sql_injection_guard"
    category: GuardCategory = GuardCategory.SECURITY
    feature_name: str = "sql_injection_guard"
    step_back_reason: str = ""

    def __init__(self, guard: SqlInjectionGuard | None = None) -> None:
        # Lazy default so tests can inject a custom-tuned guard.
        self._guard = guard or SqlInjectionGuard(
            profile="lite",
            fail_mode_override="warn",  # default warn — opt-in to deny
        )

    def check(self, ctx: GuardContext) -> GuardResult | None:
        payload = extract_sql_payload(ctx.tool_name, ctx.tool_input)
        if payload is None:
            return None  # no opinion — out of scope tool/extension

        # ``_SqlPayload`` is a richer type than the PolicyGate base
        # contract (which only declares ``str | bytes | dict``). The
        # subclass ``scan`` accepts it natively; the cast keeps mypy
        # quiet without weakening the runtime contract — the base
        # class only uses ``payload`` for escape-pattern scanning,
        # which we proxy via ``_payload_to_text``.
        result = self._guard.evaluate(payload)  # type: ignore[arg-type]
        if result.decision == "deny":
            summary = ", ".join(f.type for f in result.findings[:3])
            return GuardResult.deny(
                reason=f"SQL injection pattern: {summary}",
                context=(
                    f"⚠ SqlInjectionGuard: blocked "
                    f"{len(result.findings)} finding(s).\n"
                    "Escape with `# CONCINNO_DISABLE:<reason>` on the "
                    "offending source line if this is a false positive."
                ),
                sql_findings=[f.type for f in result.findings],
            )
        if result.decision == "warn":
            preview = ", ".join(f.type for f in result.findings[:3])
            return GuardResult.allow_advisory(
                context=(
                    f"ℹ SqlInjectionGuard: {len(result.findings)} "
                    f"finding(s) (warn-only). Types: {preview}"
                ),
            )
        return None


__all__.append("SqlInjectionBaseGuard")
