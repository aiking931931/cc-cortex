"""concinno.prompt_hooks_routes — PromptJudge ``route`` decision handler.

@module prompt_hooks_routes
@responsibility Handle ``{"decision": "route", "route_to": ..., "route_context": ...}``
    outputs from prompt-hook judges. 2.11.0 scope: *advisory-only*
    (stderr log). Active cross-process dispatch (spawning Opus
    subagent, running deploy recipes, injecting citation prompts) is
    deferred until either (a) the CC hook protocol exposes an output
    channel between hook phases, or (b) Sancio ships a process-level
    supervisor (MEMORY #53 / #56 L3).
@dependencies stdlib only (``dataclasses``, ``sys``, ``re``, ``typing``)
@exports RouteContext, RouteResult, BUILTIN_ROUTES, echo_advisory,
    validate_route_payload, dispatch

Rationale (2.11.0, red-blue CBUA S5):
  The red team proved — and CC official docs confirmed — that
  Concinno cannot receive a ``type: "prompt"`` hook's stdout in a
  subsequent Concinno hook process. Hooks run in parallel, share no
  state, and cannot chain outputs. Any automated dispatcher inside
  the hook chain is architecturally unreachable.

  The blue team proved — via grep on all ``decision`` consumers in
  concinno/src — that Concinno ships zero Python branches that parse
  judge-emitted ``decision`` strings (4 independent ``decision``
  namespaces are fully isolated). Adding a third enum value is a
  safe schema extension that CC runtime fall-through handles
  transparently (unknown ``decision`` → allow).

  Commander's 5-态 verdict: keep schema extension, drop auto
  dispatcher, ship advisory-only. This module is the minimal
  dispatch surface: user code that *wants* to act on a ``route``
  decision can manually call ``dispatch()`` — Concinno itself does
  not invoke this from any hook.

Design constraints:
  - Zero runtime dependencies. stdlib only.
  - No LLM SDK imports anywhere in this module's call graph.
  - Validator is conservative: rejects known injection patterns
    (shell meta, path traversal, unicode homoglyph, non-string
    keys, nested depth > 4). It is not a sandbox; it is a
    sanitizer for log output.
  - ``BUILTIN_ROUTES`` is a frozen mapping. No ``register_route``
    API exists in 2.11.0. Adding user-registered handlers is a
    2.12.0+ decision that requires its own red-blue review (the
    2026-04-21 red team flagged arbitrary-exec surface as FATAL;
    resolving that requires a capability manifest design not yet
    scoped).
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

# ── Shell / path / unicode injection patterns ─────────────────

_SHELL_META_PATTERN = re.compile(r"[;&|`$><\\]|\$\(")
"""Rejected characters when found in str values within route_context.

Conservative: anything that could be interpreted as shell
metacharacters or command substitution when echoed into a shell.
We are *not* running a shell — but downstream tooling that reads
log lines might, and we prefer not to emit payloads a log reader
could mis-interpret.
"""

_PATH_TRAVERSAL_PATTERN = re.compile(r"(^|[/\\])\.\.([/\\]|$)")
"""Reject ``..`` path components (``../etc``, ``foo/../bar``)."""


_UNICODE_HOMOGLYPH_CATEGORIES = frozenset({
    # Confusable with ASCII but would let an attacker smuggle
    # lookalike text into a handler name. We do not exhaustively
    # enumerate Unicode; we simply require handler names to be
    # pure ASCII (checked separately in validate_route_payload).
})


_MAX_CONTEXT_DEPTH = 4
"""Reject deeply nested route_context payloads.

Advisory channel, not RPC — there is no reason for the judge to
emit a 10-level nested dict. Deep nesting is both a log-flooding
signal and a potential parser-confusion vector.
"""


_MAX_CONTEXT_STR_LEN = 2048
"""Reject individual str values longer than this in route_context.

Prevents a judge from smuggling large payloads through the
advisory channel. 2 KiB is enough for any legitimate hint.
"""


# ── Dataclasses ───────────────────────────────────────────────


@dataclass(frozen=True)
class RouteContext:
    """Validated payload parsed from a judge's ``route_context`` dict.

    Attributes:
        route_to: handler name (pure ASCII, matches
            ``BUILTIN_ROUTES`` key).
        payload: sanitized mapping of the original ``route_context``
            dict. Values are all either str (length-capped,
            shell/path sanitized), int, float, or bool.
        reason: one-sentence rationale from the judge.
    """

    route_to: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    reason: str = ""


@dataclass(frozen=True)
class RouteResult:
    """Outcome of dispatching a route decision.

    Attributes:
        handled: whether a handler actually processed this route.
            False means fall-through to allow (e.g. unknown
            route_to, validation failed).
        action: one of ``"advisory"``, ``"noop"``, ``"reject"``.
            2.11.0 only emits ``advisory`` (stderr log) and
            ``reject`` (validation failed).
        message: human-readable summary, safe for stderr.
    """

    handled: bool
    action: str
    message: str


# ── Validator ─────────────────────────────────────────────────


def _is_safe_str(value: str) -> bool:
    """Return True if value is safe to echo to stderr / log."""
    if len(value) > _MAX_CONTEXT_STR_LEN:
        return False
    if _SHELL_META_PATTERN.search(value):
        return False
    if _PATH_TRAVERSAL_PATTERN.search(value):
        return False
    # Control characters except newline / tab (explicit allow-list
    # for log readability; reject everything else <0x20).
    for ch in value:
        if ord(ch) < 0x20 and ch not in ("\n", "\t"):
            return False
    return True


def _is_ascii_identifier(value: str) -> bool:
    """Return True if value is a pure-ASCII identifier (handler names).

    Prevents unicode homoglyph attacks — a handler name that *looks
    like* ``opus_reviewer`` but contains Cyrillic ``о`` would bypass
    a naive string compare.
    """
    if not value:
        return False
    if not value.isascii():
        return False
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value))


def _validate_payload_recursive(
    payload: Any,
    depth: int = 0,
) -> tuple[bool, str]:
    """Walk the route_context payload rejecting unsafe content."""
    if depth > _MAX_CONTEXT_DEPTH:
        return False, f"route_context nested deeper than {_MAX_CONTEXT_DEPTH}"
    if payload is None or isinstance(payload, (bool, int, float)):
        return True, ""
    if isinstance(payload, str):
        return (True, "") if _is_safe_str(payload) else (False, "unsafe str value")
    if isinstance(payload, Mapping):
        for key, val in payload.items():
            if not isinstance(key, str) or not _is_safe_str(key):
                return False, f"non-string or unsafe key: {key!r}"
            ok, err = _validate_payload_recursive(val, depth + 1)
            if not ok:
                return False, err
        return True, ""
    if isinstance(payload, (list, tuple)):
        for item in payload:
            ok, err = _validate_payload_recursive(item, depth + 1)
            if not ok:
                return False, err
        return True, ""
    return False, f"unsupported type: {type(payload).__name__}"


def validate_route_payload(
    decision: Mapping[str, Any],
) -> tuple[bool, str]:
    """Validate a parsed judge-output dict that claims decision=route.

    Returns (valid, error_message). On valid=True the caller can
    safely construct a RouteContext from ``decision``.

    Rejects:
      - decision != "route"
      - missing / empty / non-ASCII / non-identifier route_to
      - unknown route_to (not in BUILTIN_ROUTES)
      - route_context: non-dict, nested > 4, str values with shell
        meta / path traversal / length > 2 KiB / control chars
      - non-string reason field

    The caller is expected to have already parsed JSON into a dict;
    this validator is the second line of defense after JSON parsing.
    """
    if decision.get("decision") != "route":
        return False, "decision is not 'route'"
    route_to = decision.get("route_to")
    if not isinstance(route_to, str) or not _is_ascii_identifier(route_to):
        return False, "route_to must be a pure-ASCII identifier"
    if route_to not in BUILTIN_ROUTES:
        return False, f"unknown route_to: {route_to!r} (expected one of {sorted(BUILTIN_ROUTES)})"
    ctx = decision.get("route_context", {})
    if not isinstance(ctx, Mapping):
        return False, "route_context must be a mapping"
    ok, err = _validate_payload_recursive(ctx)
    if not ok:
        return False, f"route_context validation failed: {err}"
    reason = decision.get("reason", "")
    if not isinstance(reason, str):
        return False, "reason must be a string"
    if reason and not _is_safe_str(reason):
        return False, "reason contains unsafe content"
    return True, ""


# ── Handlers (2.11.0: all map to echo_advisory) ───────────────


def echo_advisory(ctx: RouteContext) -> RouteResult:
    """2.11.0 built-in handler: log the advisory to stderr.

    Pure function. No subprocess, no LLM call, no filesystem
    write. Returns a RouteResult describing what was logged.

    Output format is intentionally terse — a single line like::

        [concinno:route] citation :: claim='foo' suggested_source=bar :: reason

    Downstream log aggregators can grep ``[concinno:route]`` to
    find all advisory events.
    """
    payload_parts = []
    for key, val in sorted(ctx.payload.items()):
        # Values are already validated; truncate defensive.
        val_repr = str(val)
        if len(val_repr) > 200:
            val_repr = val_repr[:197] + "..."
        payload_parts.append(f"{key}={val_repr!r}")
    payload_str = " ".join(payload_parts) if payload_parts else "-"
    reason_str = ctx.reason or "-"
    line = f"[concinno:route] {ctx.route_to} :: {payload_str} :: {reason_str}"
    try:
        print(line, file=sys.stderr, flush=True)
    except (OSError, ValueError):
        # stderr closed — callers in short-lived subprocess contexts
        # may have redirected it. Advisory is best-effort.
        return RouteResult(
            handled=True,
            action="noop",
            message=f"stderr unavailable; would have logged: {line[:200]}",
        )
    return RouteResult(
        handled=True,
        action="advisory",
        message=line,
    )


# ── Registry ─────────────────────────────────────────────────


BUILTIN_ROUTES: Mapping[str, Callable[[RouteContext], RouteResult]] = {
    # 2.11.0: all five declared route_to names map to echo_advisory.
    # Advisory-only; no exec / no subprocess / no LLM call.
    # Replacing any entry is a 2.12.0+ decision subject to red-blue
    # CBUA review.
    "echo_advisory": echo_advisory,
    "citation": echo_advisory,
    "opus_reviewer": echo_advisory,
    "expert_review": echo_advisory,
    "deploy_recipe": echo_advisory,
}


# ── Public dispatcher (manual call entry) ─────────────────────


def dispatch(decision: Mapping[str, Any]) -> RouteResult:
    """Dispatch a validated route decision to its registered handler.

    This function is **not** invoked automatically by any Concinno
    hook in 2.11.0 — CC hook protocol cannot deliver judge output
    to a subsequent hook (red team FATAL-1, confirmed by
    `code.claude.com/docs/en/hooks` 2026-04: hooks run in parallel
    with no shared state or output chaining). Users who want to
    act on a ``route`` decision must call this function explicitly
    from code that has parsed the decision JSON themselves (e.g. a
    wrapper script reading hook transcripts).

    Returns a RouteResult. On validation failure or unknown
    route_to, returns ``RouteResult(handled=False, action="reject",
    message=...)`` — callers should treat unhandled results as
    equivalent to ``decision=allow`` (fail-open, never block on
    route validation errors).
    """
    ok, err = validate_route_payload(decision)
    if not ok:
        return RouteResult(
            handled=False,
            action="reject",
            message=f"route validation failed: {err}",
        )
    route_to = decision["route_to"]
    handler = BUILTIN_ROUTES.get(route_to)
    if handler is None:
        # validate_route_payload already checked this, but guard
        # against mutation of BUILTIN_ROUTES between check and call.
        return RouteResult(
            handled=False,
            action="reject",
            message=f"handler vanished for route_to={route_to!r}",
        )
    ctx = RouteContext(
        route_to=route_to,
        payload=dict(decision.get("route_context", {})),
        reason=decision.get("reason", ""),
    )
    try:
        return handler(ctx)
    except Exception as exc:  # noqa: BLE001 — fail-open for hook safety
        # A crashed handler must never propagate up into the hook
        # chain; CC would then see a hook exit code != 0 and may
        # mis-interpret. Log to stderr and fall through.
        try:
            print(
                f"[concinno:route] handler error for {route_to}: {exc!r}",
                file=sys.stderr,
                flush=True,
            )
        except (OSError, ValueError):
            pass
        return RouteResult(
            handled=False,
            action="reject",
            message=f"handler raised: {type(exc).__name__}",
        )


__all__ = [
    "RouteContext",
    "RouteResult",
    "BUILTIN_ROUTES",
    "echo_advisory",
    "validate_route_payload",
    "dispatch",
]
