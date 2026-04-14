"""cc_cortex.security.permission_mode — Session-scoped permission mode FSM.

@module security.permission_mode
@responsibility Provide a session-scoped 5-mode state machine that gates
    tool calls through declarative allow/deny rules. Unlike the existing
    cc-cortex per-event regex guards (stateless, fire-and-forget), this
    machine tracks *session mode* across calls — e.g. a `/plan` command
    can switch the whole session into read-only investigation until the
    user explicitly promotes it back out. This is the "session-scoped
    mode latch" primitive the 55 regex guards lack.

    The module is deliberately minimal: an enum, three `frozenset`
    buckets of well-known tool names, a rule dataclass, a request
    dataclass, a verdict dataclass, a caller-supplied hook Protocol,
    and one FSM class that glues them together. No network, no
    persistence, no I/O — callers serialize `mode + list_rules()`
    themselves if they want durability.

@dependencies stdlib only (dataclasses, enum, fnmatch, typing)
@exports PermissionMode, Decision, PermissionRule, PermissionRequest,
    PermissionVerdict, PermissionHook, PermissionModeFSM,
    READ_ONLY_TOOLS, WRITE_TOOLS, EXEC_TOOLS, AUTO_SAFE_TOOLS

Ported from Claude Code's TypeScript source:
  - types/permissions.ts            (mode enum, PermissionBehavior)
  - utils/permissions/permissions.ts (rule engine, first-match-wins)
  - hooks/toolPermission/permissionLogging.ts (audit-trail conventions)

Design notes:
  - `PermissionMode` is a `str`-backed Enum so serialized mode names
    survive a round trip through JSON/TOML without custom codecs. CC's
    TypeScript side uses plain string literals; we match that shape so
    a caller can ingest a `settings.json` mode field directly.
  - Tool names are plain strings (not an enum) because callers are
    expected to register their own tools (MCP servers, custom shells,
    etc.). The three `frozenset` constants below only classify the
    *canonical CC tools* for mode semantics; everything else falls
    through to rule-based matching.
  - Rules use `fnmatch.fnmatchcase` rather than full regex. This
    mirrors CC's `Bash(git status:*)` glob idiom and keeps the surface
    small — 55 regex guards already exist in cc-cortex for cases that
    need full PCRE power. This primitive is about shape-matching, not
    semantic analysis.
  - `deny` beats `allow` by default, but an `allow` rule with
    `override=True` beats a `deny` — this is CC's "emergency escape"
    pattern (e.g. force-allow `/ls` even while deny rules are active).
  - The FSM is in-memory only. Two sibling FSMs in the same process
    are fully isolated; share a single instance if you want a global
    session state.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal, Optional, Protocol, Sequence

__all__ = [
    "PermissionMode",
    "Decision",
    "PermissionRule",
    "PermissionRequest",
    "PermissionVerdict",
    "PermissionHook",
    "PermissionModeFSM",
    "READ_ONLY_TOOLS",
    "WRITE_TOOLS",
    "EXEC_TOOLS",
    "AUTO_SAFE_TOOLS",
    # 11-variant decision-reason discriminated union (CC parity)
    "PermissionDecisionReasonType",
    "PermissionDecisionReason",
    "PermissionAction",
    "PermissionResult",
    "make_decision",
]


# --------------------------------------------------------------------------- #
# Enums & tool classification
# --------------------------------------------------------------------------- #


class PermissionMode(str, Enum):
    """Session-scoped permission mode. Mirrors CC's ``PermissionMode``.

    - ``DEFAULT``  — normal operation; ambiguous tool calls prompt.
    - ``PLAN``     — read-only investigation; all writes/exec blocked.
    - ``ACCEPT_EDITS`` — user opted to auto-accept Edit/Write until
      mode change. Bash/exec still go through rules/prompt.
    - ``BYPASS_PERMISSIONS`` — dangerous; auto-accept everything, no
      hook calls. Emits a one-time audit warning.
    - ``AUTO`` — heuristic: safe shapes allowed silently, everything
      else falls back to the prompt path.
    """

    DEFAULT = "default"
    PLAN = "plan"
    ACCEPT_EDITS = "accept_edits"
    BYPASS_PERMISSIONS = "bypass_permissions"
    AUTO = "auto"


Decision = Literal["allow", "deny", "prompt"]


# Canonical read-only CC tools. These always allow in DEFAULT/PLAN/AUTO
# without prompting because they have no side effects.
READ_ONLY_TOOLS: frozenset[str] = frozenset(
    {
        "Read",
        "Glob",
        "Grep",
        "WebSearch",
        "WebFetch",
        "NotebookRead",
    }
)

# Canonical filesystem-mutating tools. These are the ones that
# ACCEPT_EDITS auto-accepts and PLAN hard-blocks.
WRITE_TOOLS: frozenset[str] = frozenset(
    {
        "Edit",
        "Write",
        "NotebookEdit",
        "MultiEdit",
    }
)

# Canonical side-effect execution tools. PLAN hard-blocks these;
# ACCEPT_EDITS still runs them through rule/prompt (not auto-allowed).
EXEC_TOOLS: frozenset[str] = frozenset(
    {
        "Bash",
    }
)

# AUTO mode's "heuristic whitelist": tools that look risky on paper but
# are cheap enough to auto-allow when no rule matches. Deliberately a
# strict subset of WRITE_TOOLS — Bash is NOT here, because a blind
# Bash auto-allow is how autonomous agents nuke home directories.
AUTO_SAFE_TOOLS: frozenset[str] = frozenset(
    {
        "Edit",
        "Write",
        "NotebookEdit",
    }
)


# --------------------------------------------------------------------------- #
# Dataclasses
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PermissionRule:
    """A single declarative allow/deny rule.

    Pattern format (ported from CC's ``Bash(git status)`` idiom):
        - ``"Tool"``              — bare tool name, matches the tool only
        - ``"Tool:<glob>"``       — tool + command/path glob
        - ``"*:<glob>"``          — any tool, path glob only

    For Bash requests the glob is matched against ``request.command``;
    for Edit/Write/Notebook* requests it is matched against
    ``request.path``.

    ``scope`` — if non-None, the rule only applies when the FSM is in
    that mode. A rule with ``scope=None`` applies in every mode. Scoped
    rules outrank null-scope rules (see ``_rank_rules``).

    ``override`` — if True, an ``allow`` rule beats a matching ``deny``
    in the same bucket. This is the "always allow /ls even in lockdown"
    escape hatch. Ignored on deny rules.

    ``reason`` — free-form string surfaced in the verdict and audit log.
    """

    pattern: str
    decision: Decision
    scope: PermissionMode | None = None
    override: bool = False
    reason: str = ""


@dataclass
class PermissionRequest:
    """A single tool-call permission check."""

    tool_name: str
    mode: PermissionMode
    command: str = ""  # populated for Bash
    path: str = ""  # populated for Edit/Write/Notebook*
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass
class PermissionVerdict:
    """The FSM's answer to a ``PermissionRequest``."""

    decision: Decision
    matched_rule: PermissionRule | None
    reason: str
    mode: PermissionMode


class PermissionHook(Protocol):
    """Caller-supplied escalation hook for ambiguous cases.

    When the FSM would otherwise return ``"prompt"`` and a hook is
    registered, the hook is invoked to resolve the request. The hook
    MUST return ``"allow"`` or ``"deny"`` — it is not allowed to recurse
    back into ``prompt``. If no hook is registered, ambiguous requests
    fall back to ``deny`` with reason ``"no_hook_ambiguous_deny"``.

    Hooks MUST NOT perform network I/O from inside this module's tests;
    this Protocol intentionally has no ``async`` variant.
    """

    def prompt(
        self, request: PermissionRequest
    ) -> Literal["allow", "deny"]:  # pragma: no cover - protocol
        ...


# --------------------------------------------------------------------------- #
# FSM
# --------------------------------------------------------------------------- #


class PermissionModeFSM:
    """Session-scoped permission state machine.

    Usage::

        fsm = PermissionModeFSM(initial_mode=PermissionMode.DEFAULT)
        fsm.add_rule(PermissionRule("Bash:git *", "allow"))
        verdict = fsm.check(PermissionRequest(
            tool_name="Bash",
            mode=fsm.mode,
            command="git status",
        ))
        if verdict.decision == "allow":
            run_tool()

    The caller is responsible for passing ``fsm.mode`` into the
    ``PermissionRequest`` — this keeps the request serializable for
    audit logs without forcing a back-pointer to the FSM.

    Thread-safety: NOT thread-safe. Wrap in a lock if you share across
    threads; typical CC usage is single-agent single-thread.
    """

    def __init__(
        self,
        *,
        initial_mode: PermissionMode = PermissionMode.DEFAULT,
        rules: Sequence[PermissionRule] = (),
        hook: PermissionHook | None = None,
    ) -> None:
        self._mode: PermissionMode = initial_mode
        self._rules: list[PermissionRule] = list(rules)
        self._hook: PermissionHook | None = hook
        # Audit trail of (from_mode, to_mode, reason). Initial mode is
        # intentionally NOT present — callers can read `.mode` directly
        # for the current state, and the trail only tracks transitions.
        self._trail: list[tuple[PermissionMode, PermissionMode, str]] = []
        self._stats: dict[str, int] = {
            "checks_total": 0,
            "allow_count": 0,
            "deny_count": 0,
            "prompt_count": 0,
            "mode_transitions": 0,
        }
        self._bypass_warned: bool = False

    # ----- mode management -------------------------------------------------

    @property
    def mode(self) -> PermissionMode:
        return self._mode

    def set_mode(
        self, mode: PermissionMode, *, reason: str = "explicit_transition"
    ) -> None:
        """Transition to a new mode. Always logged, even self-transitions.

        Self-transitions are preserved in the audit trail because CC's
        permissionLogging expects every ``setMode`` to be observable
        (replaying a trail should reproduce ``setMode`` calls 1:1).
        """
        previous = self._mode
        self._mode = mode
        self._trail.append((previous, mode, reason))
        self._stats["mode_transitions"] += 1

    def audit_trail(self) -> list[tuple[PermissionMode, PermissionMode, str]]:
        """Return a copy of the (from, to, reason) transition log."""
        return list(self._trail)

    # ----- rule management -------------------------------------------------

    def add_rule(self, rule: PermissionRule) -> None:
        self._rules.append(rule)

    def remove_rule(self, pattern: str) -> bool:
        """Remove the first rule whose pattern equals ``pattern``.

        Returns True if a rule was removed, False otherwise. Removal is
        idempotent: removing an absent pattern is a no-op that returns
        False rather than raising.
        """
        for i, rule in enumerate(self._rules):
            if rule.pattern == pattern:
                del self._rules[i]
                return True
        return False

    def list_rules(self) -> list[PermissionRule]:
        return list(self._rules)

    # ----- main dispatch ---------------------------------------------------

    def check(self, request: PermissionRequest) -> PermissionVerdict:
        """Dispatch a permission request through mode + rule + hook.

        Dispatch order:

          1. Update ``checks_total``.
          2. BYPASS_PERMISSIONS short-circuits everything (auto-allow).
          3. PLAN hard-blocks WRITE/EXEC (unless override allow).
          4. READ_ONLY_TOOLS always allow (unless override deny).
          5. ACCEPT_EDITS auto-allows WRITE_TOOLS (not EXEC).
          6. Rule scan: deny > allow within same scope, scoped > null.
          7. Fallthrough: AUTO + safe → allow; else → prompt path.
          8. Prompt path → hook or ``no_hook_ambiguous_deny``.

        Never raises. Denial is returned as a verdict.
        """
        self._stats["checks_total"] += 1

        if self._mode == PermissionMode.BYPASS_PERMISSIONS:
            return self._finalize(self._verdict_bypass())

        matched = self._scan_rules(request)

        plan_verdict = self._check_plan(request, matched)
        if plan_verdict is not None:
            return self._finalize(plan_verdict)

        readonly_verdict = self._check_read_only(request, matched)
        if readonly_verdict is not None:
            return self._finalize(readonly_verdict)

        accept_verdict = self._check_accept_edits(request, matched)
        if accept_verdict is not None:
            return self._finalize(accept_verdict)

        if matched is not None:
            return self._resolve_matched_rule(request, matched)

        if self._mode == PermissionMode.AUTO:
            if request.tool_name in AUTO_SAFE_TOOLS:
                return self._finalize(
                    PermissionVerdict(
                        decision="allow",
                        matched_rule=None,
                        reason="auto_mode_safe_whitelist",
                        mode=self._mode,
                    )
                )
        return self._finalize_prompt(request, None)

    # ----- per-mode branches (keep `check` under the 120-line cap) ---------

    def _verdict_bypass(self) -> PermissionVerdict:
        if not self._bypass_warned:
            self._bypass_warned = True
            warn_reason = "bypass_permissions_auto_allow_first_warning"
        else:
            warn_reason = "bypass_permissions_auto_allow"
        return PermissionVerdict(
            decision="allow",
            matched_rule=None,
            reason=warn_reason,
            mode=self._mode,
        )

    def _check_plan(
        self,
        request: PermissionRequest,
        matched: PermissionRule | None,
    ) -> PermissionVerdict | None:
        """PLAN hard-blocks WRITE/EXEC unless an override-allow matches."""
        if self._mode != PermissionMode.PLAN:
            return None
        if (
            request.tool_name not in WRITE_TOOLS
            and request.tool_name not in EXEC_TOOLS
        ):
            return None
        if (
            matched is not None
            and matched.decision == "allow"
            and matched.override
        ):
            return PermissionVerdict(
                decision="allow",
                matched_rule=matched,
                reason=matched.reason or "plan_override_allow",
                mode=self._mode,
            )
        return PermissionVerdict(
            decision="deny",
            matched_rule=None,
            reason="plan_mode_read_only",
            mode=self._mode,
        )

    def _check_read_only(
        self,
        request: PermissionRequest,
        matched: PermissionRule | None,
    ) -> PermissionVerdict | None:
        """READ_ONLY_TOOLS allow unless an override-deny rule matches."""
        if request.tool_name not in READ_ONLY_TOOLS:
            return None
        if (
            matched is not None
            and matched.decision == "deny"
            and matched.override
        ):
            return PermissionVerdict(
                decision="deny",
                matched_rule=matched,
                reason=matched.reason or "override_deny_read_only",
                mode=self._mode,
            )
        return PermissionVerdict(
            decision="allow",
            matched_rule=None,
            reason="read_only_tool_auto_allow",
            mode=self._mode,
        )

    def _check_accept_edits(
        self,
        request: PermissionRequest,
        matched: PermissionRule | None,
    ) -> PermissionVerdict | None:
        """ACCEPT_EDITS auto-allows WRITE tools unless override-deny."""
        if self._mode != PermissionMode.ACCEPT_EDITS:
            return None
        if request.tool_name not in WRITE_TOOLS:
            return None
        if (
            matched is not None
            and matched.decision == "deny"
            and matched.override
        ):
            return PermissionVerdict(
                decision="deny",
                matched_rule=matched,
                reason=matched.reason or "accept_edits_override_deny",
                mode=self._mode,
            )
        return PermissionVerdict(
            decision="allow",
            matched_rule=None,
            reason="accept_edits_auto_allow",
            mode=self._mode,
        )

    def _resolve_matched_rule(
        self,
        request: PermissionRequest,
        matched: PermissionRule,
    ) -> PermissionVerdict:
        if matched.decision == "allow":
            return self._finalize(
                PermissionVerdict(
                    decision="allow",
                    matched_rule=matched,
                    reason=matched.reason or "rule_allow",
                    mode=self._mode,
                )
            )
        if matched.decision == "deny":
            return self._finalize(
                PermissionVerdict(
                    decision="deny",
                    matched_rule=matched,
                    reason=matched.reason or "rule_deny",
                    mode=self._mode,
                )
            )
        return self._finalize_prompt(request, matched)

    # ----- CC idiom: dontAsk → deny transform ------------------------------

    def dontAsk_to_deny(self, request: PermissionRequest) -> PermissionRule:
        """Turn a "don't ask me this again" user reflex into a deny rule.

        Mirrors CC's ``permissions.ts`` ``dontAsk`` path: when a user
        clicks "deny and don't ask again", the current request is
        materialized into a permanent null-scope deny rule so future
        matching requests short-circuit without prompting. The rule is
        added to the FSM *and* returned so callers can persist it.

        For Bash the rule key is ``Tool:command``; for Edit/Write-style
        tools it's ``Tool:path``; for tools with neither it degrades to
        ``Tool`` alone.
        """
        key = request.command or request.path
        pattern = f"{request.tool_name}:{key}" if key else request.tool_name
        rule = PermissionRule(
            pattern=pattern,
            decision="deny",
            scope=None,
            override=False,
            reason="user_dont_ask",
        )
        self.add_rule(rule)
        return rule

    # ----- stats -----------------------------------------------------------

    def stats(self) -> dict[str, int]:
        return dict(self._stats)

    # ----- internals -------------------------------------------------------

    def _scan_rules(
        self, request: PermissionRequest
    ) -> PermissionRule | None:
        """Return the highest-priority matching rule, or None.

        Ordering:
          1. Rules with ``scope == self._mode`` outrank ``scope=None``.
          2. Within one scope bucket, ``override=True`` allow beats
             every deny; otherwise deny beats allow.
          3. Within a bucket of same decision+override, first-added
             wins.
        """
        scoped_matches: list[PermissionRule] = []
        null_matches: list[PermissionRule] = []
        for rule in self._rules:
            if not self._rule_matches(rule, request):
                continue
            if rule.scope == self._mode:
                scoped_matches.append(rule)
            elif rule.scope is None:
                null_matches.append(rule)
            # rules scoped to a different mode are skipped entirely.

        for bucket in (scoped_matches, null_matches):
            if not bucket:
                continue
            # Override-allow wins outright.
            for rule in bucket:
                if rule.decision == "allow" and rule.override:
                    return rule
            # Then first deny.
            for rule in bucket:
                if rule.decision == "deny":
                    return rule
            # Then first allow.
            for rule in bucket:
                if rule.decision == "allow":
                    return rule
            # Then first explicit prompt.
            for rule in bucket:
                if rule.decision == "prompt":
                    return rule
        return None

    @staticmethod
    def _rule_matches(
        rule: PermissionRule, request: PermissionRequest
    ) -> bool:
        """True iff ``rule.pattern`` matches ``request`` shape.

        Pattern grammar:
            TOOL                    → match tool name only
            TOOL:GLOB               → match tool + (command|path) glob
            *:GLOB                  → match any tool + glob
        """
        pattern = rule.pattern
        if ":" in pattern:
            tool_part, glob = pattern.split(":", 1)
        else:
            tool_part, glob = pattern, ""

        if tool_part != "*" and tool_part != request.tool_name:
            return False

        if not glob:
            # Bare tool pattern: match iff there's no request-side
            # specificity we'd otherwise have to ignore. We still match
            # even when the request has a command/path — a bare
            # ``Bash`` rule is the "all Bash" rule.
            return True

        # Choose the request field to glob against. Bash → command,
        # everything else → path (falling back to command if the path
        # field is empty, covering custom/MCP tools that carry their
        # target in `command`).
        if request.tool_name in EXEC_TOOLS:
            target = request.command
        elif request.path:
            target = request.path
        else:
            target = request.command
        return fnmatch.fnmatchcase(target, glob)

    def _finalize(self, verdict: PermissionVerdict) -> PermissionVerdict:
        if verdict.decision == "allow":
            self._stats["allow_count"] += 1
        elif verdict.decision == "deny":
            self._stats["deny_count"] += 1
        else:
            self._stats["prompt_count"] += 1
        return verdict

    def _finalize_prompt(
        self,
        request: PermissionRequest,
        matched: PermissionRule | None,
    ) -> PermissionVerdict:
        """Resolve a ``prompt`` path through the hook or auto-deny."""
        if self._hook is None:
            return self._finalize(
                PermissionVerdict(
                    decision="deny",
                    matched_rule=matched,
                    reason="no_hook_ambiguous_deny",
                    mode=self._mode,
                )
            )
        # We count the intermediate "prompt" as a prompt regardless of
        # what the hook ultimately returns — the prompt happened.
        self._stats["prompt_count"] += 1
        hook_answer = self._hook.prompt(request)
        if hook_answer == "allow":
            self._stats["allow_count"] += 1
            return PermissionVerdict(
                decision="allow",
                matched_rule=matched,
                reason="hook_allow",
                mode=self._mode,
            )
        # any non-"allow" return → deny (defensive)
        self._stats["deny_count"] += 1
        return PermissionVerdict(
            decision="deny",
            matched_rule=matched,
            reason="hook_deny",
            mode=self._mode,
        )

    # ----- 11-variant decision reason bridge (CC parity) ------------------

    def check_result(self, request: PermissionRequest) -> PermissionResult:
        """Run ``check`` and return a CC-parity ``PermissionResult``.

        Wraps the legacy ``PermissionVerdict`` in the 11-variant typed
        reason discriminated union. Existing callers keep using
        ``check()``; new callers that want observability use this.

        Mapping rules:
          - BYPASS_PERMISSIONS or mode-driven outcomes → MODE variant
          - Matched rule outcomes → RULE variant with rule_id
          - Hook-driven outcomes → HOOK variant
          - Ambiguous deny without hook → SAFETY_CHECK variant
          - Fallthrough prompt → OTHER variant
        """
        verdict = self.check(request)
        reason_text = verdict.reason
        action: PermissionAction
        if verdict.decision == "allow":
            action = "allow"
        elif verdict.decision == "deny":
            action = "deny"
        else:
            action = "ask"

        # Hook-driven
        if reason_text in ("hook_allow", "hook_deny"):
            pdr = PermissionDecisionReason(
                type=PermissionDecisionReasonType.HOOK,
                detail=reason_text,
            )
            return PermissionResult(action=action, reason=pdr)

        # No-hook ambiguous deny is a safety fallback.
        if reason_text == "no_hook_ambiguous_deny":
            pdr = PermissionDecisionReason(
                type=PermissionDecisionReasonType.SAFETY_CHECK,
                detail=reason_text,
            )
            return PermissionResult(action=action, reason=pdr)

        # Matched-rule outcomes always carry the rule.
        if verdict.matched_rule is not None:
            pdr = PermissionDecisionReason(
                type=PermissionDecisionReasonType.RULE,
                detail=reason_text or "rule_match",
                rule_id=verdict.matched_rule.pattern,
                metadata={
                    "rule_decision": verdict.matched_rule.decision,
                    "rule_scope": (
                        verdict.matched_rule.scope.value
                        if verdict.matched_rule.scope is not None
                        else ""
                    ),
                    "rule_override": str(verdict.matched_rule.override),
                },
            )
            return PermissionResult(action=action, reason=pdr)

        # Everything else is mode-driven (BYPASS, PLAN, READ_ONLY,
        # ACCEPT_EDITS auto-allow, AUTO safe-whitelist, etc.).
        pdr = PermissionDecisionReason(
            type=PermissionDecisionReasonType.MODE,
            detail=f"mode={verdict.mode.value}, {reason_text}",
            metadata={"mode": verdict.mode.value},
        )
        return PermissionResult(action=action, reason=pdr)


# --------------------------------------------------------------------------- #
# 11-variant PermissionDecisionReason (CC parity)
# --------------------------------------------------------------------------- #
#
# Ported from Claude Code's TypeScript source:
#   types/permissions.ts lines 271-324 (PermissionDecisionReason union)
#
# CC carries a typed reason on every permission allow/deny/ask so that
# logs, UIs, and debuggers can explain *why* a call was gated. The
# TypeScript side uses a discriminated union; Python doesn't have one
# natively, so we model it as an Enum tag + a dataclass with variant-
# specific optional fields. The helper ``make_decision`` is the ergonomic
# constructor; it always produces a frozen ``PermissionResult``.


class PermissionDecisionReasonType(str, Enum):
    """11 discriminated reasons CC carries on every permission decision.

    Source: E:/Cursor/src/src(CC源碼)/types/permissions.ts:271-324

    Enum values mirror CC's TypeScript ``type`` field verbatim (snake
    case here, camelCase on the TS side; the mapping is documented per
    variant). String values are stable and safe to serialize.
    """

    RULE = "rule"
    """Explicit rule match — a PermissionRule's allow/deny fired."""

    MODE = "mode"
    """FSM mode drove the decision (BYPASS, PLAN, READ_ONLY, etc.)."""

    SUBCOMMAND_RESULTS = "subcommand_results"
    """Bash subcommand classifier — multi-command line, per-part verdict.

    CC TypeScript name: ``subcommandResults``.
    """

    PERMISSION_PROMPT_TOOL = "permission_prompt_tool"
    """User answered a prompt tool (interactive allow/deny).

    CC TypeScript name: ``permissionPromptTool``.
    """

    HOOK = "hook"
    """A PreToolUse hook returned the decision."""

    ASYNC_AGENT = "async_agent"
    """A background async agent decided (non-interactive).

    CC TypeScript name: ``asyncAgent``.
    """

    SANDBOX_OVERRIDE = "sandbox_override"
    """Sandbox config override (e.g. excludedCommand).

    CC TypeScript name: ``sandboxOverride``.
    """

    CLASSIFIER = "classifier"
    """LLM classifier — Bash 2-stage stage 2 verdict."""

    WORKING_DIR = "working_dir"
    """Working-directory-scoped rule match.

    CC TypeScript name: ``workingDir``.
    """

    SAFETY_CHECK = "safety_check"
    """Safety guard fired (destruction_guard, butterfly, etc.).

    CC TypeScript name: ``safetyCheck``.
    """

    OTHER = "other"
    """Catch-all. MUST include ``detail`` for observability."""


PermissionAction = Literal["allow", "deny", "ask", "passthrough"]
"""Four possible outcomes a ``PermissionResult`` can carry.

- ``allow``        — proceed with the tool call.
- ``deny``         — block with a reason.
- ``ask``          — surface to the user / hook.
- ``passthrough``  — no opinion; let the next layer decide.
"""

_ALLOWED_ACTIONS: frozenset[str] = frozenset(
    {"allow", "deny", "ask", "passthrough"}
)


@dataclass(frozen=True)
class PermissionDecisionReason:
    """Typed reason attached to every ``PermissionResult``.

    Fields:
        type: one of the 11 enum variants
        detail: human-readable explanation (always filled)
        rule_id: populated for RULE / WORKING_DIR variants
        classifier_score: populated for CLASSIFIER variant (0..1)
        metadata: variant-specific extras (frozen dict semantics: the
            dataclass is frozen but the dict itself is mutable at build
            time — callers SHOULD treat it as read-only once constructed)
    """

    type: PermissionDecisionReasonType
    detail: str = ""
    rule_id: Optional[str] = None
    classifier_score: Optional[float] = None
    metadata: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Enforce CC's contract: OTHER variant MUST carry a detail string
        # (otherwise the reason is invisible in logs).
        if self.type == PermissionDecisionReasonType.OTHER and not self.detail:
            msg = "PermissionDecisionReason.OTHER requires non-empty detail"
            raise ValueError(msg)


@dataclass(frozen=True)
class PermissionResult:
    """CC-parity permission outcome: action + typed reason + optional ask text.

    ``action`` is restricted to the four ``PermissionAction`` literals
    at runtime via ``__post_init__`` so bogus strings like ``"maybe"``
    raise immediately rather than propagating through logs.
    """

    action: PermissionAction
    reason: PermissionDecisionReason
    suggested_message: str = ""

    def __post_init__(self) -> None:
        if self.action not in _ALLOWED_ACTIONS:
            msg = (
                f"PermissionResult.action must be one of "
                f"{sorted(_ALLOWED_ACTIONS)}, got {self.action!r}"
            )
            raise ValueError(msg)

    def legacy_reason(self) -> str:
        """Stable string form for callers that expect ``str`` reasons.

        Format:
            [TYPE] detail                                (bare)
            [TYPE rule_id=<id>] detail                   (RULE/WORKING_DIR)
            [TYPE score=<score>] detail                  (CLASSIFIER)

        The format is deliberately bracket-prefixed so a caller can
        ``startswith("[MODE")`` to route without parsing JSON. Stable
        across versions — covered by snapshot tests.
        """
        tag = self.reason.type.name
        extras: list[str] = []
        if self.reason.rule_id is not None:
            extras.append(f"rule_id={self.reason.rule_id}")
        if self.reason.classifier_score is not None:
            extras.append(f"score={self.reason.classifier_score}")
        head = f"[{tag}]" if not extras else f"[{tag} {' '.join(extras)}]"
        if self.reason.detail:
            return f"{head} {self.reason.detail}"
        return head


def make_decision(
    action: PermissionAction,
    reason_type: PermissionDecisionReasonType,
    *,
    detail: str = "",
    rule_id: Optional[str] = None,
    classifier_score: Optional[float] = None,
    metadata: Optional[dict[str, Any]] = None,
    suggested_message: str = "",
) -> PermissionResult:
    """Ergonomic constructor for ``PermissionResult``.

    Example::

        result = make_decision(
            "deny",
            PermissionDecisionReasonType.RULE,
            detail="blocked by bash:rm-rf",
            rule_id="bash:rm-rf",
        )
    """
    reason = PermissionDecisionReason(
        type=reason_type,
        detail=detail,
        rule_id=rule_id,
        classifier_score=classifier_score,
        metadata=dict(metadata) if metadata else {},
    )
    return PermissionResult(
        action=action,
        reason=reason,
        suggested_message=suggested_message,
    )
