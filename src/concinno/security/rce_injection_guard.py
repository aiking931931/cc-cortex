"""concinno.security.rce_injection_guard — RCE injection static scanner.

@module security.rce_injection_guard
@responsibility Static-analysis guard (4.6.0 W4) that inspects tool
    invocations for **Remote Code Execution injection** patterns —
    specifically the *dynamic-code-construction* vector that is
    distinct from straight unsafe-deserialize (owned by
    :mod:`concinno.security.deserialize_guard`):

      * **f-string into shell** —
        ``os.system(f"echo {x}")`` /
        ``subprocess.run(f"...{x}...", shell=True)`` /
        ``subprocess.Popen(f"...", shell=True)`` /
        ``os.popen(f"...{x}...")``.
        These build a shell command from interpolated values; if the
        interpolated source includes a tool argument or external
        data, an attacker who controls that source controls the
        executed command.
      * **% / .format on shell strings** —
        ``os.system("..." % user)`` / ``"...".format(x)`` passed to
        ``subprocess.run(..., shell=True)``. Same vector, different
        surface.
      * **eval / exec on dynamic input** —
        ``eval(user_input)`` / ``exec(payload)`` /
        ``compile(s, "<x>", "exec")`` where the argument is **not** a
        literal constant. Literal-only forms (``eval("1+2")``) are
        downgraded to ``low``.
      * **Bash command-injection shapes** — Bash tool invocations whose
        ``command`` contains unquoted shell-variable expansion
        (``$VAR`` outside double quotes), backtick command
        substitution (``$(cmd)`` or back-quoted form), or a chained
        ``;`` / ``&&`` whose right-hand side reads from another
        variable. We **delegate** the heavy lifting to
        :mod:`concinno.security.bash_validators` — this guard only
        adds the RCE-specific layered checks that the 24-validator
        chain doesn't already perform.

    Out of scope (owned elsewhere — duplication would drift):

      * ``pickle.loads`` / ``yaml.load`` / ``dill`` / ``marshal`` /
        ``__reduce__`` — :class:`DeserializeGuard`.
      * SQL injection — ``sql_injection_guard`` (parallel sibling).
      * SSRF / outbound HTTP — :class:`SSRFGuard`.

    OWASP cross-reference: **LLM01 (Prompt Injection)** triggers
    indirect RCE when an LLM is induced to write attacker-controlled
    code that later executes; **LLM08 (Excessive Agency)** captures
    the *eval-on-tool-output* sub-class. The guard wears the LLM08
    label in audit logs because the failure mode is the agent
    granting itself code-execution authority via dynamic compilation.

@dependencies stdlib only — :mod:`ast`, :mod:`re`, :class:`PolicyGate`,
    :mod:`bash_validators` helpers (in-package; no new runtime dep).
@exports
    RceInjectionGuard,
    RceInjectionBaseGuard,
    RcePayload,
    RceFinding (re-export of :class:`Finding`),
    RCE_PATTERN_SEVERITY.

The guard inherits :class:`PolicyGate` for the standard 4-tier
fail-mode chain (``silent`` / ``warn`` / ``warn+log`` / ``hard_deny``),
profile-aware fail-mode resolution, the
``# CONCINNO_DISABLE:rce_injection_guard:<reason>`` per-line escape,
and the ZIQ outcome emit hook. Wiring into the PreToolUse pipeline
is via :class:`RceInjectionBaseGuard`, the thin adapter that converts
``GuardContext`` → :class:`RcePayload` → ``PolicyGateResult`` →
``GuardResult``.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from typing import Any, Optional

from concinno.guards.base import (
    BaseGuard,
    GuardCategory,
    GuardContext,
    GuardResult,
)
from concinno.security.bash_validators import (
    BashValidator,
    BashValidatorConfig,
)
from concinno.security.policy_gate import (
    FailMode,
    PolicyGate,
    Severity,
)
from concinno.security.policy_gate import (
    Finding as RceFinding,
)

__all__ = [
    "RCE_PATTERN_SEVERITY",
    "RceFinding",
    "RceInjectionBaseGuard",
    "RceInjectionGuard",
    "RcePayload",
    "extract_code_payload",
]


# ── Severity catalogue ───────────────────────────────────────────

#: Default severity per detector key. The most dangerous combination
#: (eval/exec on a dynamic argument **and** an f-string passed to a
#: shell sink) is ``critical`` because successful injection grants
#: arbitrary code execution; pure-literal forms are ``low`` because
#: the detector still wants to record them but they cannot RCE on
#: their own.
RCE_PATTERN_SEVERITY: dict[str, Severity] = {
    # Dynamic-code execution sinks
    "eval.dynamic": "critical",
    "exec.dynamic": "critical",
    "compile.exec": "critical",
    "eval.literal": "low",
    "exec.literal": "low",
    # f-string built shell command (subprocess shell=True / os.system /
    # os.popen / commands.getoutput-style sinks)
    "fstring_shell.system": "critical",
    "fstring_shell.subprocess_shell": "critical",
    "fstring_shell.popen": "high",
    # Old-style % or .format() pumped into shell
    "format_shell": "high",
    # Bash injection shapes (delegated to bash_validators for confirm)
    "bash_unquoted_var": "medium",
    "bash_backtick_subst": "high",
}


# Severity rank — used by min_severity filter.
_SEVERITY_RANK: dict[str, int] = {
    "low": 0, "medium": 1, "high": 2, "critical": 3,
}


# ── Per-line escape comment regex (mirrors deserialize_guard) ────

_PER_LINE_ESCAPE = re.compile(
    r"#\s*CONCINNO_DISABLE\s*:\s*rce_injection_guard\b",
    re.IGNORECASE,
)


# ── Shell-sink names ─────────────────────────────────────────────

#: Functions whose first positional arg is a shell command string.
#: ``subprocess.run`` / ``subprocess.Popen`` are special-cased — they
#: only execute through a shell when ``shell=True`` is set.
_SHELL_SINKS_DIRECT: frozenset[str] = frozenset({
    "os.system",
    "os.popen",
    "commands.getoutput",
    "commands.getstatusoutput",
})

#: subprocess functions that take ``shell=`` keyword.
_SUBPROCESS_SHELL_FUNCS: frozenset[str] = frozenset({
    "subprocess.run",
    "subprocess.call",
    "subprocess.check_call",
    "subprocess.check_output",
    "subprocess.Popen",
    "subprocess.getoutput",
    "subprocess.getstatusoutput",
})


# ── Payload data class ───────────────────────────────────────────


@dataclass(frozen=True)
class RcePayload:
    """Extracted code text + originating tool context.

    Attributes:
        code: Source string to scan. For Edit tools we use
            ``new_string``; for Write we use ``content``; for Bash
            we use ``command``; for NotebookEdit we use the cell's
            ``new_source``.
        tool_name: ``"Edit"`` / ``"Write"`` / ``"Bash"`` /
            ``"NotebookEdit"``. Drives which detector subset runs —
            Bash invocations skip the AST-based Python detectors and
            the AST detectors skip the bash-shell shape checks.
        file_path: Optional file path being edited / written. Used
            only to emit a clearer audit message; does not change
            detector behaviour.
    """

    code: str
    tool_name: str
    file_path: str = ""


def extract_code_payload(
    tool_name: str, tool_input: dict[str, Any],
) -> Optional[RcePayload]:
    """Pull the executable text out of a tool invocation.

    Returns ``None`` when the tool is irrelevant or the relevant
    field is empty/non-string. The wrapper guard treats ``None`` as
    "no opinion" so the pipeline passes the call through.
    """
    if not isinstance(tool_input, dict):
        return None

    if tool_name == "Bash":
        cmd = tool_input.get("command")
        if isinstance(cmd, str) and cmd.strip():
            return RcePayload(code=cmd, tool_name=tool_name)
        return None

    if tool_name == "Write":
        content = tool_input.get("content")
        if isinstance(content, str) and content.strip():
            return RcePayload(
                code=content,
                tool_name=tool_name,
                file_path=str(tool_input.get("file_path", "")),
            )
        return None

    if tool_name == "Edit":
        new_str = tool_input.get("new_string")
        if isinstance(new_str, str) and new_str.strip():
            return RcePayload(
                code=new_str,
                tool_name=tool_name,
                file_path=str(tool_input.get("file_path", "")),
            )
        return None

    if tool_name == "NotebookEdit":
        new_src = tool_input.get("new_source")
        if isinstance(new_src, str) and new_src.strip():
            return RcePayload(
                code=new_src,
                tool_name=tool_name,
                file_path=str(tool_input.get("notebook_path", "")),
            )
        return None

    return None


# ── AST helpers ──────────────────────────────────────────────────


def _qualified_name(node: ast.AST) -> str:
    """Return ``"module.attr.attr"`` for an Attribute chain.

    Mirrors :func:`deserialize_guard._qualified_name` — kept in this
    module so the two guards stay independent (one can be subclassed
    or replaced without breaking the other).
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


def _is_literal_constant(node: ast.AST) -> bool:
    """``True`` iff ``node`` evaluates to a compile-time constant.

    Tuple/list of constants count as constant. f-strings with **no**
    ``FormattedValue`` children are constant (rare but legal).
    """
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, (ast.Tuple, ast.List)):
        return all(_is_literal_constant(e) for e in node.elts)
    if isinstance(node, ast.JoinedStr):
        return all(isinstance(v, ast.Constant) for v in node.values)
    return False


def _is_dynamic_fstring(node: ast.AST) -> bool:
    """``True`` iff ``node`` is an f-string with at least one
    ``FormattedValue`` child (i.e. a real interpolation).

    Pure-constant ``JoinedStr`` returns False so the caller can treat
    them as ordinary literals.
    """
    if not isinstance(node, ast.JoinedStr):
        return False
    return any(isinstance(v, ast.FormattedValue) for v in node.values)


def _has_dynamic_format(node: ast.AST) -> bool:
    """``True`` iff ``node`` is an old-style ``%`` or ``.format()``
    construction whose left-hand side is a non-constant string."""
    # str % args
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        if isinstance(node.left, ast.Constant) and isinstance(
            node.left.value, str
        ):
            # Either side non-constant means runtime interpolation.
            return not _is_literal_constant(node.right)
    # str.format(...)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        if node.func.attr == "format":
            base = node.func.value
            if isinstance(base, ast.Constant) and isinstance(base.value, str):
                return any(not _is_literal_constant(a) for a in node.args)
    return False


def _shell_kw_is_true(call: ast.Call) -> bool:
    """``True`` when ``shell=True`` (a constant) is among the kwargs.

    Anything not a literal ``True`` (variable, missing) returns False
    — false positives here are expensive (every legitimate
    ``subprocess.run`` would trip).
    """
    for kw in call.keywords:
        if kw.arg == "shell":
            v = kw.value
            if isinstance(v, ast.Constant) and v.value is True:
                return True
    return False


def _line_text(lines: list[str], lineno: int) -> str:
    if 1 <= lineno <= len(lines):
        return lines[lineno - 1]
    return ""


# ── Bash-side helpers (non-AST) ──────────────────────────────────


# Backtick command substitution is the most lethal RCE primitive in
# Bash because the substitution always evaluates regardless of
# quoting context. We re-detect it here even though
# bash_validators.validate_dangerous_patterns already does — the RCE
# guard wants its own audit-log entry tagged with
# ``bash_backtick_subst`` so triage can route by attack class.
_BACKTICK_SUBST = re.compile(r"`[^`]+`")

# Unquoted ``$VAR`` followed by a non-quote char (heuristic — the
# 24-validator chain owns precise quoting analysis; we just catch
# the obvious shape).
_UNQUOTED_VAR = re.compile(
    r"(?<![\"'])\$\{?[A-Za-z_]\w*\}?(?=[^\"']|$)"
)


# ── Visitor — collects findings on an AST tree ───────────────────


class _RceVisitor(ast.NodeVisitor):
    """Walks an AST tree collecting RCE-injection findings.

    State is per-visit; the parent guard creates a fresh visitor per
    :meth:`scan` call.
    """

    def __init__(
        self,
        *,
        source_lines: list[str],
        severity: dict[str, Severity],
    ) -> None:
        self.findings: list[RceFinding] = []
        self._lines = source_lines
        self._sev = severity

    # ── ast.NodeVisitor entry points ────────────────────────────

    def visit_Call(self, node: ast.Call) -> None:
        if self._line_escaped(getattr(node, "lineno", 0)):
            self.generic_visit(node)
            return
        self._dispatch_call(node)
        self.generic_visit(node)

    def _dispatch_call(self, node: ast.Call) -> None:
        """Route a Call node to its sink-family inspector.

        Kept flat (depth ≤ 4) so the structural-nesting guard stays
        green. Each branch delegates to a helper that owns its own
        inspection logic.
        """
        qname = _qualified_name(node.func)

        if qname in _SHELL_SINKS_DIRECT:
            self._inspect_shell_call(node, qname)
            return
        if qname in _SUBPROCESS_SHELL_FUNCS and _shell_kw_is_true(node):
            self._inspect_shell_call(
                node, qname, sink_kind="subprocess_shell",
            )
            return
        if isinstance(node.func, ast.Name) and node.func.id in {
            "eval", "exec",
        }:
            self._inspect_eval_exec(node, node.func.id)
            return
        if qname == "compile":
            self._inspect_compile(node)

    @staticmethod
    def _is_compile_exec_dynamic(node: ast.Call) -> bool:
        """``True`` iff ``compile(src, fname, 'exec')`` with non-literal
        ``src``. Helper kept static so :meth:`_inspect_compile` stays a
        single-branch flat function."""
        if len(node.args) < 3:
            return False
        mode = node.args[2]
        if not (isinstance(mode, ast.Constant) and mode.value == "exec"):
            return False
        return not _is_literal_constant(node.args[0])

    def _inspect_compile(self, node: ast.Call) -> None:
        if self._is_compile_exec_dynamic(node):
            self._record(
                node, "compile.exec",
                snippet="compile(<dynamic>, ..., 'exec')",
            )

    # ── Inspectors per sink family ──────────────────────────────

    def _inspect_shell_call(
        self,
        node: ast.Call,
        qname: str,
        sink_kind: str = "",
    ) -> None:
        """Examine first positional arg of a shell sink for f-string /
        format / % interpolation. ``sink_kind`` overrides the
        detector key when present (used by ``subprocess.run`` to
        distinguish itself from ``os.system``)."""
        if not node.args:
            return
        first = node.args[0]

        # f-string interpolation — most common LLM-generated RCE shape.
        if _is_dynamic_fstring(first):
            if sink_kind == "subprocess_shell":
                self._record(
                    node, "fstring_shell.subprocess_shell",
                    snippet=f"{qname}(f'...', shell=True)",
                )
            elif qname in {"os.popen"}:
                self._record(
                    node, "fstring_shell.popen",
                    snippet=f"{qname}(f'...')",
                )
            else:
                self._record(
                    node, "fstring_shell.system",
                    snippet=f"{qname}(f'...')",
                )
            return

        # Old-style % or .format() composing the shell string.
        if _has_dynamic_format(first):
            self._record(
                node, "format_shell",
                snippet=f"{qname}(<%-or-format>)",
            )
            return

        # Direct concatenation of a Name into a string. We don't fire
        # on a single Name (could be a vetted constant); only on a
        # BinOp that mixes a literal prefix with a non-literal — this
        # is the same risk class as f-string but rarer.
        if (
            isinstance(first, ast.BinOp)
            and isinstance(first.op, ast.Add)
            and (
                _is_literal_str_const(first.left)
                or _is_literal_str_const(first.right)
            )
            and not _is_literal_constant(first)
        ):
            self._record(
                node, "format_shell",
                snippet=f"{qname}(<concat>)",
            )

    def _inspect_eval_exec(self, node: ast.Call, fname: str) -> None:
        if not node.args:
            self._record(node, f"{fname}.dynamic", snippet=f"{fname}()")
            return
        first = node.args[0]
        if _is_literal_constant(first):
            self._record(
                node, f"{fname}.literal",
                snippet=f"{fname}(<literal>)",
            )
        else:
            self._record(
                node, f"{fname}.dynamic",
                snippet=f"{fname}(<dynamic>)",
            )

    # ── Bookkeeping ─────────────────────────────────────────────

    def _record(
        self, node: ast.AST, detector: str, snippet: str,
    ) -> None:
        sev = self._sev.get(detector, "medium")
        span = self._span_for(node)
        lineno = getattr(node, "lineno", "?")
        self.findings.append(
            RceFinding(
                type=detector,
                span=span,
                snippet=snippet,
                severity=sev,
                message=f"rce-injection: {detector} at line {lineno}",
            )
        )

    def _span_for(self, node: ast.AST) -> tuple[int, int]:
        if not self._lines:
            return (-1, -1)
        lineno = getattr(node, "lineno", None)
        col = getattr(node, "col_offset", None)
        end_lineno = getattr(node, "end_lineno", None)
        end_col = getattr(node, "end_col_offset", None)
        if lineno is None or col is None:
            return (-1, -1)
        start = sum(len(ln) + 1 for ln in self._lines[: lineno - 1]) + col
        if end_lineno is None or end_col is None:
            return (start, start)
        end = sum(len(ln) + 1 for ln in self._lines[: end_lineno - 1]) + end_col
        return (start, end)

    def _line_escaped(self, lineno: int) -> bool:
        return bool(_PER_LINE_ESCAPE.search(_line_text(self._lines, lineno)))


def _is_literal_str_const(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Constant) and isinstance(node.value, str)
    )


# ── PolicyGate subclass ──────────────────────────────────────────


class RceInjectionGuard(PolicyGate):
    """Static scan for RCE-injection patterns in tool inputs.

    Accepts a :class:`RcePayload`, a raw Python source string, or a
    raw Bash command string. All other payload shapes resolve to a
    single ``low`` ``"malformed_payload"`` finding rather than
    raising — security guards must never crash the hook chain.

    Parameters:
        profile: feature-toggle profile name. Forwarded to base.
        fail_mode_override: explicit fail-mode that beats the
            profile chain. Forwarded to base.
        min_severity: drop findings below this rank. Default
            ``"medium"`` (drops literal eval/exec which are usually
            benign in test files).
        flag_eval_literal: when ``False``, suppress
            ``eval.literal`` / ``exec.literal`` entirely. Default
            ``True`` so the audit trail still records them.
        bash_chain: optional pre-built :class:`BashValidator` to
            reuse the 24-validator chain for Bash payloads. Default
            constructs one with permissive config.
    """

    name: str = "rce_injection_guard"

    def __init__(
        self,
        profile: str = "lite",
        fail_mode_override: FailMode | None = None,
        *,
        min_severity: Severity = "medium",
        flag_eval_literal: bool = True,
        bash_chain: BashValidator | None = None,
    ) -> None:
        super().__init__(
            profile=profile, fail_mode_override=fail_mode_override
        )
        if min_severity not in _SEVERITY_RANK:
            raise ValueError(
                f"min_severity {min_severity!r} not in "
                f"{sorted(_SEVERITY_RANK)}"
            )
        self._min_severity: str = min_severity
        self._flag_eval_literal: bool = bool(flag_eval_literal)
        self._bash_chain = bash_chain or BashValidator(
            config=BashValidatorConfig(
                allow_git_commit_messages=True,
                split_compound=True,
                strip_safe_wrappers=True,
            )
        )
        # Per-instance copy so subclasses can mutate without leak.
        self._severity: dict[str, Severity] = dict(RCE_PATTERN_SEVERITY)

    # ── Public scan entry point ─────────────────────────────────

    def scan(
        self, payload: str | bytes | dict[str, Any] | RcePayload,
    ) -> list[RceFinding]:
        """Return RCE findings; empty list = clean."""
        rce_payload = self._coerce_payload(payload)
        if rce_payload is None:
            return [
                RceFinding(
                    type="malformed_payload",
                    span=(-1, -1),
                    snippet="",
                    severity="low",
                    message="RceInjectionGuard: unsupported payload shape",
                )
            ]

        if rce_payload.tool_name == "Bash":
            findings = self._scan_bash(rce_payload.code)
        else:
            findings = self._scan_python_source(rce_payload.code)

        # min_severity + literal-eval filter
        out: list[RceFinding] = []
        floor = _SEVERITY_RANK[self._min_severity]
        for f in findings:
            if (
                not self._flag_eval_literal
                and f.type in {"eval.literal", "exec.literal"}
            ):
                continue
            if _SEVERITY_RANK.get(f.severity, 0) < floor:
                continue
            out.append(f)
        return out

    # ── Coercion helpers ────────────────────────────────────────

    def _coerce_payload(self, payload: Any) -> RcePayload | None:
        """Return the canonical :class:`RcePayload` or ``None`` on
        unsupported input. Strings/bytes default to ``Write``-style
        Python source — the safe assumption when the caller hasn't
        told us the tool name."""
        if isinstance(payload, RcePayload):
            return payload
        if isinstance(payload, str):
            return RcePayload(code=payload, tool_name="Write")
        if isinstance(payload, bytes):
            return RcePayload(
                code=payload.decode("utf-8", errors="replace"),
                tool_name="Write",
            )
        if isinstance(payload, dict):
            tool_name = payload.get("tool_name") or "Write"
            tool_input = payload.get("tool_input") or payload
            extracted = extract_code_payload(str(tool_name), tool_input)
            if extracted is not None:
                return extracted
            # Best-effort: try the dict as raw fields.
            cmd = payload.get("command")
            if isinstance(cmd, str):
                return RcePayload(code=cmd, tool_name="Bash")
            content = payload.get("content") or payload.get("new_string")
            if isinstance(content, str):
                return RcePayload(code=content, tool_name="Write")
        return None

    # ── Python-source scanner ───────────────────────────────────

    def _scan_python_source(self, source: str) -> list[RceFinding]:
        """AST-walk + visitor. Returns findings or a single
        ``parse_error`` low-severity entry on syntax failure."""
        if not source.strip():
            return []
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            return [
                RceFinding(
                    type="parse_error",
                    span=(-1, -1),
                    snippet="",
                    severity="low",
                    message=f"AST parse failed: {exc.msg}",
                )
            ]
        visitor = _RceVisitor(
            source_lines=source.splitlines(),
            severity=self._severity,
        )
        visitor.visit(tree)
        return list(visitor.findings)

    # ── Bash-command scanner ────────────────────────────────────

    def _scan_bash(self, command: str) -> list[RceFinding]:
        """Lightweight RCE-specific Bash checks layered on top of the
        24-validator chain. Returns findings without consulting the
        chain's verdict — both layers run in parallel and both
        contribute to the audit log if they fire."""
        findings: list[RceFinding] = []

        # Backtick command substitution — always-evaluating RCE primitive.
        m = _BACKTICK_SUBST.search(command)
        if m:
            findings.append(
                RceFinding(
                    type="bash_backtick_subst",
                    span=m.span(),
                    snippet=command[m.start():m.end()][:64],
                    severity=self._severity["bash_backtick_subst"],
                    message="rce-injection: backtick command substitution",
                )
            )

        # Unquoted variable in dangerous position. We look for
        # ``$VAR`` outside any single/double quote span.
        for m in _UNQUOTED_VAR.finditer(command):
            # Trim — find rough quote context. _UNQUOTED_VAR's
            # lookaround is heuristic; double-check by walking quote
            # state up to the match index.
            if not _is_in_unquoted_region(command, m.start()):
                continue
            findings.append(
                RceFinding(
                    type="bash_unquoted_var",
                    span=m.span(),
                    snippet=m.group(0),
                    severity=self._severity["bash_unquoted_var"],
                    message="rce-injection: unquoted shell variable",
                )
            )
            # One unquoted-var finding is enough — the chain catches
            # the rest. Avoid flooding the audit log.
            break

        return findings


def _is_in_unquoted_region(s: str, idx: int) -> bool:
    """``True`` when position ``idx`` in ``s`` is outside any
    single- or double-quoted span. Walks the string with the same
    state machine bash uses (escape via backslash, single quotes
    suppress escape, double quotes honour it)."""
    in_sq = False
    in_dq = False
    esc = False
    for i, ch in enumerate(s):
        if i == idx:
            return not (in_sq or in_dq)
        if esc:
            esc = False
            continue
        if ch == "\\" and not in_sq:
            esc = True
            continue
        if ch == "'" and not in_dq:
            in_sq = not in_sq
            continue
        if ch == '"' and not in_sq:
            in_dq = not in_dq
            continue
    return not (in_sq or in_dq)


# ── BaseGuard wrapper — wires into the PreToolUse pipeline ───────


class RceInjectionBaseGuard(BaseGuard):
    """Pipeline adapter that wires :class:`RceInjectionGuard` into
    :func:`concinno.guards.registry.create_default_pipeline`.

    PolicyGate is the right shape for *standalone* scanning (returns
    a rich :class:`PolicyGateResult`), but the unified pipeline
    speaks ``BaseGuard`` and ``GuardResult``. This class is the
    minimal bridge:

      * ``check`` extracts the code payload via
        :func:`extract_code_payload`,
      * runs ``RceInjectionGuard().evaluate(...)``,
      * maps ``deny`` → ``GuardResult.deny``, ``warn`` →
        ``GuardResult.allow(context=...)`` (advisory), ``accept`` →
        ``None``.

    Default category is ``SECURITY`` — RCE injection is a hard-deny
    threat class on par with destruction / secret-scan. No
    step-back; the user can paste ``# CONCINNO_DISABLE:
    rce_injection_guard:<reason>`` into the source line to override.
    """

    name: str = "rce_injection_guard"
    category: GuardCategory = GuardCategory.SECURITY
    feature_name: str = "rce_injection_guard"
    # No step-back — security category. Hard deny only (or warn via
    # fail-mode chain).
    step_back_reason: str = ""

    def __init__(
        self,
        guard: RceInjectionGuard | None = None,
    ) -> None:
        # Lazy default so tests can inject a custom-tuned guard.
        self._guard = guard or RceInjectionGuard(
            profile="lite",
            fail_mode_override="warn",  # default warn — opt-in to deny
        )

    def check(self, ctx: GuardContext) -> Optional[GuardResult]:
        payload = extract_code_payload(ctx.tool_name, ctx.tool_input)
        if payload is None:
            return None  # no opinion

        # ``RcePayload`` is a richer type than the PolicyGate base
        # contract (which only declares ``str | bytes | dict``). The
        # subclass ``scan`` accepts it natively; the cast keeps mypy
        # quiet without weakening the runtime contract — the base
        # class only uses ``payload`` for escape-pattern scanning,
        # which we proxy via ``_payload_to_text``.
        result = self._guard.evaluate(payload)  # type: ignore[arg-type]
        if result.decision == "deny":
            findings_summary = ", ".join(
                f.type for f in result.findings[:3]
            )
            return GuardResult.deny(
                reason=f"RCE injection pattern: {findings_summary}",
                context=(
                    f"⚠ RceInjectionGuard: blocked {len(result.findings)} "
                    f"finding(s).\n"
                    "Escape with `# CONCINNO_DISABLE:rce_injection_guard:"
                    "<reason>` on the offending source line if this is a "
                    "false positive."
                ),
                rce_findings=[f.type for f in result.findings],
            )
        if result.decision == "warn":
            return GuardResult.allow(
                context=(
                    f"ℹ RceInjectionGuard: {len(result.findings)} "
                    f"finding(s) (warn-only). "
                    f"Types: {', '.join(f.type for f in result.findings[:3])}"
                ),
            )
        return None
