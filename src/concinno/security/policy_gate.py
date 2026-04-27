"""concinno.security.policy_gate — Layer 9 policy-as-code + 4.3.0 guard base.

@module security.policy_gate
@responsibility Two related primitives in one module, kept together because
    they share the same fail-mode taxonomy and audit-log conventions:

    1. **PolicyEngine** (Layer 9 of the 9-layer security stack — original
       4.2.x feature). Defines security policies as DATA (dict/YAML schemas),
       not as code scattered across 55 guards. Each policy is a named rule
       with: a threat category (OWASP LLM01-LLM10 + NIST AI 800-2), a match
       predicate, and a deny/allow/audit action. The PolicyEngine loads a
       set of policies and evaluates every tool call against them.
       Fail-closed: if the engine can't evaluate a policy (missing field,
       exception), it denies.

    2. **PolicyGate** (4.3.0 Plan B Step 2 — shared base class). Concrete
       security guards (pii / deserialize / circuit_breaker / rce_injection /
       http_client / sql_injection) inherit ``PolicyGate`` and implement
       ``scan(payload) -> list[Finding]``. The base supplies the 4-tier
       fail-mode chain (silent / warn / warn+log / hard_deny), profile-aware
       fail-mode resolution via :func:`feature_config.get_fail_mode`, an
       escape hatch via the ``# CONCINNO_DISABLE:<reason>`` payload comment,
       JSON-line audit log to ``~/.concinno/audit/<guard>.jsonl`` with size
       rotation, and an opt-in ZIQ outcome bus emit hook for online learning.

@dependencies stdlib only (re, fnmatch, dataclasses, enum, typing, json,
    pathlib, os, sys, time)
@exports
    PolicyEngine layer:
        ThreatCategory, PolicyAction, PolicyRule, PolicyContext,
        PolicyVerdict, EngineResult, PolicyMatcher, ToolNameMatcher,
        ContentPatternMatcher, MetadataMatcher, CompositeMatcher,
        CallableMatcher, OWASP_LLM_BASELINE, PolicyEngine
    Guard base layer (4.3.0):
        FailMode (re-export), Severity, Decision, Finding,
        PolicyGateResult, PolicyGate
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, Sequence

# ── Threat taxonomy ───────────────────────────────────────────

class ThreatCategory(str, Enum):
    """OWASP LLM Top 10 (2025 edition) + NIST AI 800-2 additions."""

    LLM01_PROMPT_INJECTION = "LLM01"
    LLM02_INSECURE_OUTPUT = "LLM02"
    LLM03_TRAINING_POISONING = "LLM03"
    LLM04_MODEL_DOS = "LLM04"
    LLM05_SUPPLY_CHAIN = "LLM05"
    LLM06_SENSITIVE_DISCLOSURE = "LLM06"
    LLM07_INSECURE_PLUGIN = "LLM07"
    LLM08_EXCESSIVE_AGENCY = "LLM08"
    LLM09_OVERRELIANCE = "LLM09"
    LLM10_UNBOUNDED_CONSUMPTION = "LLM10"
    NIST_DATA_INTEGRITY = "NIST_DI"
    NIST_PRIVACY = "NIST_PV"
    NIST_ACCOUNTABILITY = "NIST_AC"


PolicyAction = Literal["deny", "allow", "audit"]


# ── Matchers ──────────────────────────────────────────────────

class PolicyMatcher:
    """Base matcher. Subclass for specific match logic."""

    def matches(self, ctx: PolicyContext) -> bool:
        """Return True if the policy condition matches the given context."""
        return False  # pragma: no cover — base is never used directly


class ToolNameMatcher(PolicyMatcher):
    """Match by tool name glob (fnmatch)."""

    def __init__(self, pattern: str) -> None:
        self.pattern = pattern

    def matches(self, ctx: PolicyContext) -> bool:
        return fnmatch.fnmatch(ctx.tool_name, self.pattern)

    def __repr__(self) -> str:
        return f"ToolNameMatcher({self.pattern!r})"


class ContentPatternMatcher(PolicyMatcher):
    """Match by regex pattern in tool_input values or tool_result."""

    def __init__(
        self,
        pattern: str,
        *,
        fields: tuple[str, ...] = ("tool_input", "tool_result"),
    ) -> None:
        self.pattern = pattern
        self.fields = fields
        self._compiled = re.compile(pattern, re.IGNORECASE)

    def matches(self, ctx: PolicyContext) -> bool:
        for fld in self.fields:
            if fld == "tool_input":
                for value in ctx.tool_input.values():
                    text = value if isinstance(value, str) else json.dumps(value)
                    if self._compiled.search(text):
                        return True
            elif fld == "tool_result":
                if self._compiled.search(ctx.tool_result):
                    return True
        return False

    def __repr__(self) -> str:
        return f"ContentPatternMatcher({self.pattern!r}, fields={self.fields})"


class MetadataMatcher(PolicyMatcher):
    """Match by session_metadata key=value."""

    def __init__(self, key: str, value: str) -> None:
        self.key = key
        self.value = value

    def matches(self, ctx: PolicyContext) -> bool:
        return ctx.session_metadata.get(self.key) == self.value

    def __repr__(self) -> str:
        return f"MetadataMatcher({self.key!r}={self.value!r})"


class CompositeMatcher(PolicyMatcher):
    """AND/OR composition of child matchers."""

    def __init__(
        self,
        children: Sequence[PolicyMatcher],
        *,
        mode: Literal["all", "any"] = "all",
    ) -> None:
        self.children = list(children)
        self.mode = mode

    def matches(self, ctx: PolicyContext) -> bool:
        if self.mode == "all":
            return all(c.matches(ctx) for c in self.children)
        return any(c.matches(ctx) for c in self.children)

    def __repr__(self) -> str:
        return f"CompositeMatcher(mode={self.mode!r}, n={len(self.children)})"


class CallableMatcher(PolicyMatcher):
    """Escape hatch: arbitrary callable. Use sparingly."""

    def __init__(
        self,
        fn: Callable[[PolicyContext], bool],
        *,
        name: str = "custom",
    ) -> None:
        self.fn = fn
        self.name = name

    def matches(self, ctx: PolicyContext) -> bool:
        return self.fn(ctx)

    def __repr__(self) -> str:
        return f"CallableMatcher({self.name!r})"


# ── Data classes ──────────────────────────────────────────────

@dataclass(frozen=True)
class PolicyRule:
    """A single policy-as-code rule."""

    name: str
    threat: ThreatCategory
    action: PolicyAction
    description: str
    match: PolicyMatcher
    severity: Literal["critical", "high", "medium", "low"] = "high"
    fail_closed: bool = True


@dataclass
class PolicyContext:
    """What the engine evaluates against — one tool call."""

    tool_name: str
    tool_input: dict[str, Any] = field(default_factory=dict)
    tool_result: str = ""
    session_metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PolicyVerdict:
    """Result of evaluating one rule against one context."""

    rule_name: str
    threat: ThreatCategory
    action: PolicyAction
    matched: bool
    reason: str
    severity: str = "high"


@dataclass
class EngineResult:
    """Aggregated result across all rules."""

    denied: bool
    verdicts: list[PolicyVerdict]
    deny_reasons: list[str]
    audit_log: list[PolicyVerdict]


# ── Built-in OWASP LLM baseline ──────────────────────────────

OWASP_LLM_BASELINE: tuple[PolicyRule, ...] = (
    # LLM01 — Prompt injection (indirect via fetched content)
    PolicyRule(
        name="owasp_lm01_prompt_injection",
        threat=ThreatCategory.LLM01_PROMPT_INJECTION,
        action="deny",
        description="Block indirect prompt injection in fetched content",
        match=ContentPatternMatcher(
            r"ignore\s+(all\s+)?previous|system\s+prompt|you\s+are\s+now",
            fields=("tool_result",),
        ),
        severity="critical",
    ),
    # LLM02 — Insecure output handling (XSS patterns)
    PolicyRule(
        name="owasp_lm02_insecure_output",
        threat=ThreatCategory.LLM02_INSECURE_OUTPUT,
        action="deny",
        description="Block XSS / script injection in tool output",
        match=ContentPatternMatcher(
            r"<script|javascript:|on\w+=",
            fields=("tool_result",),
        ),
        severity="high",
    ),
    # LLM03 — Training data poisoning (audit only)
    PolicyRule(
        name="owasp_lm03_training_poisoning",
        threat=ThreatCategory.LLM03_TRAINING_POISONING,
        action="audit",
        description="Audit references to training data or fine-tuning payloads",
        match=ContentPatternMatcher(
            r"training[_\s]?data|fine[_\s]?tun(e|ing)|\.jsonl\b.*\b(train|dataset)",
            fields=("tool_input", "tool_result"),
        ),
        severity="medium",
    ),
    # LLM04 — Model DoS (brace expansion / extreme repetition)
    PolicyRule(
        name="owasp_lm04_model_dos",
        threat=ThreatCategory.LLM04_MODEL_DOS,
        action="deny",
        description="Block DoS via brace expansion or extreme repetition",
        match=ContentPatternMatcher(
            r"\{[\d,]{20,}\}|\.{100,}",
            fields=("tool_input",),
        ),
        severity="high",
    ),
    # LLM05 — Supply chain (untrusted registries)
    PolicyRule(
        name="owasp_lm05_supply_chain",
        threat=ThreatCategory.LLM05_SUPPLY_CHAIN,
        action="deny",
        description="Block installs from untrusted registries",
        match=ContentPatternMatcher(
            r"pip\s+install\s+--index-url|npm\s+install\s+--registry",
            fields=("tool_input",),
        ),
        severity="critical",
    ),
    # LLM06 — Sensitive information disclosure (write to secret files)
    PolicyRule(
        name="owasp_lm06_sensitive_disclosure",
        threat=ThreatCategory.LLM06_SENSITIVE_DISCLOSURE,
        action="deny",
        description="Block writes to sensitive files (.env, .key, .pem, credentials)",
        match=CompositeMatcher(
            [
                ToolNameMatcher("Write"),
                ContentPatternMatcher(
                    r"\.(env|key|pem|credentials)",
                    fields=("tool_input",),
                ),
            ],
            mode="all",
        ),
        severity="critical",
    ),
    # LLM07 — Insecure plugin execution (MCP tools)
    PolicyRule(
        name="owasp_lm07_insecure_plugin",
        threat=ThreatCategory.LLM07_INSECURE_PLUGIN,
        action="audit",
        description="Audit MCP tool calls for unknown plugin execution",
        match=ContentPatternMatcher(
            r"^mcp__",
            fields=("tool_input",),
        ),
    ),
    # LLM07 — also match tool_name directly
    PolicyRule(
        name="owasp_lm07_insecure_plugin_toolname",
        threat=ThreatCategory.LLM07_INSECURE_PLUGIN,
        action="audit",
        description="Audit tool calls with mcp__ prefix in tool name",
        match=ToolNameMatcher("mcp__*"),
        severity="medium",
    ),
    # LLM08 — Excessive agency (agent spawns)
    PolicyRule(
        name="owasp_lm08_excessive_agency",
        threat=ThreatCategory.LLM08_EXCESSIVE_AGENCY,
        action="deny",
        description="Block agent spawns when parallel count exceeded",
        match=CompositeMatcher(
            [
                ToolNameMatcher("Agent"),
                MetadataMatcher("agent_limit_exceeded", "true"),
            ],
            mode="all",
        ),
        severity="high",
    ),
    # LLM09 — Overreliance (audit only — no executable gate)
    PolicyRule(
        name="owasp_lm09_overreliance",
        threat=ThreatCategory.LLM09_OVERRELIANCE,
        action="audit",
        description="Audit patterns suggesting blind trust in LLM output",
        match=ContentPatternMatcher(
            r"(?:as\s+(?:an?\s+)?AI|I\s+cannot\s+(?:verify|confirm))",
            fields=("tool_result",),
        ),
        severity="low",
    ),
    # LLM10 — Unbounded consumption (infinite loops)
    PolicyRule(
        name="owasp_lm10_unbounded_consumption",
        threat=ThreatCategory.LLM10_UNBOUNDED_CONSUMPTION,
        action="deny",
        description="Block commands with unbounded output patterns",
        match=ContentPatternMatcher(
            r"while\s+true|for\s*\(\s*;\s*;\s*\)|yes\s*\|",
            fields=("tool_input",),
        ),
        severity="high",
    ),
)


# ── Matcher registry for from_dict deserialization ────────────

_MATCHER_REGISTRY: dict[str, type[PolicyMatcher]] = {
    "ToolNameMatcher": ToolNameMatcher,
    "ContentPatternMatcher": ContentPatternMatcher,
    "MetadataMatcher": MetadataMatcher,
    "CompositeMatcher": CompositeMatcher,
}


def _build_matcher(match_type: str, match_args: dict[str, Any]) -> PolicyMatcher:
    """Construct a matcher from its type name and arguments dict."""
    if match_type not in _MATCHER_REGISTRY:
        msg = f"Unknown matcher type: {match_type!r}"
        raise ValueError(msg)

    cls = _MATCHER_REGISTRY[match_type]

    if cls is CompositeMatcher:
        # Recursively build child matchers
        children_data = match_args.get("children", [])
        children = [
            _build_matcher(c["match_type"], c.get("match_args", {}))
            for c in children_data
        ]
        mode = match_args.get("mode", "all")
        return CompositeMatcher(children, mode=mode)

    return cls(**match_args)


# ── Policy engine ─────────────────────────────────────────────

class PolicyEngine:
    """Evaluate tool calls against a set of policy-as-code rules.

    Fail-closed by default: if a matcher raises an exception and the
    rule has ``fail_closed=True``, the evaluation treats it as a deny.
    """

    def __init__(
        self,
        *,
        rules: Sequence[PolicyRule] = OWASP_LLM_BASELINE,
        fail_closed: bool = True,
    ) -> None:
        self._rules: list[PolicyRule] = list(rules)
        self._fail_closed = fail_closed
        self._stats = {
            "evaluations": 0,
            "denies": 0,
            "audits": 0,
            "allows": 0,
            "errors_fail_closed": 0,
        }

    # ── Core evaluation ───────────────────────────────────────

    def evaluate(self, ctx: PolicyContext) -> EngineResult:
        """Run all rules against *ctx*.

        - First deny wins (short-circuit).
        - Audit rules always run (no short-circuit).
        - Allow is permissive — it simply does not deny.
        - On matcher exception + fail_closed → treat as deny.
        """
        self._stats["evaluations"] += 1
        verdicts: list[PolicyVerdict] = []
        deny_reasons: list[str] = []
        audit_log: list[PolicyVerdict] = []
        denied = False

        for rule in self._rules:
            verdict = self._evaluate_rule(rule, ctx)
            verdicts.append(verdict)

            if verdict.matched and verdict.action == "deny":
                denied = True
                deny_reasons.append(verdict.reason)
                self._stats["denies"] += 1
                # Short-circuit: stop evaluating deny rules but still
                # collect remaining audit rules
                break

            if verdict.matched and verdict.action == "audit":
                audit_log.append(verdict)
                self._stats["audits"] += 1

            if verdict.matched and verdict.action == "allow":
                self._stats["allows"] += 1

        # If we short-circuited on deny, still run remaining audit rules
        if denied:
            start_idx = len(verdicts)
            for rule in self._rules[start_idx:]:
                if rule.action == "audit":
                    v = self._evaluate_rule(rule, ctx)
                    verdicts.append(v)
                    if v.matched:
                        audit_log.append(v)
                        self._stats["audits"] += 1

        return EngineResult(
            denied=denied,
            verdicts=verdicts,
            deny_reasons=deny_reasons,
            audit_log=audit_log,
        )

    def _evaluate_rule(
        self, rule: PolicyRule, ctx: PolicyContext
    ) -> PolicyVerdict:
        """Evaluate a single rule. Fail-closed on exception."""
        try:
            matched = rule.match.matches(ctx)
        except Exception as exc:
            fc = rule.fail_closed if rule.fail_closed else self._fail_closed
            if fc:
                self._stats["errors_fail_closed"] += 1
                return PolicyVerdict(
                    rule_name=rule.name,
                    threat=rule.threat,
                    action="deny",
                    matched=True,
                    reason=(
                        f"Fail-closed: matcher raised {type(exc).__name__}: {exc}"
                    ),
                    severity=rule.severity,
                )
            # Fail-open: treat as not matched
            return PolicyVerdict(
                rule_name=rule.name,
                threat=rule.threat,
                action=rule.action,
                matched=False,
                reason=f"Fail-open: matcher raised {type(exc).__name__}",
                severity=rule.severity,
            )

        reason = ""
        if matched:
            reason = f"[{rule.threat.value}] {rule.description}"

        return PolicyVerdict(
            rule_name=rule.name,
            threat=rule.threat,
            action=rule.action,
            matched=matched,
            reason=reason,
            severity=rule.severity,
        )

    # ── Batch evaluation ──────────────────────────────────────

    def evaluate_batch(
        self, contexts: Sequence[PolicyContext]
    ) -> list[EngineResult]:
        """Evaluate multiple contexts. One EngineResult per context."""
        return [self.evaluate(ctx) for ctx in contexts]

    # ── Rule management ───────────────────────────────────────

    def add_rule(self, rule: PolicyRule) -> None:
        """Add a rule to the engine."""
        self._rules.append(rule)

    def remove_rule(self, name: str) -> bool:
        """Remove a rule by name. Returns True if found and removed."""
        for i, r in enumerate(self._rules):
            if r.name == name:
                self._rules.pop(i)
                return True
        return False

    def list_rules(self) -> list[PolicyRule]:
        """Return a copy of the current rule list."""
        return list(self._rules)

    # ── Reporting ─────────────────────────────────────────────

    def coverage_report(self) -> dict[str, list[str]]:
        """Return ``{threat_category: [rule_names]}``.

        Callers can audit "do I have LLM01 coverage?" by checking
        whether a threat category has any rules.
        """
        report: dict[str, list[str]] = {}
        for rule in self._rules:
            key = rule.threat.value
            if key not in report:
                report[key] = []
            report[key].append(rule.name)
        return report

    def stats(self) -> dict[str, int]:
        """Return accumulated evaluation statistics."""
        return dict(self._stats)

    # ── Factory ───────────────────────────────────────────────

    @classmethod
    def from_dict(
        cls, rules_data: Sequence[Mapping[str, Any]]
    ) -> PolicyEngine:
        """Load rules from a list of dicts (e.g. parsed YAML/JSON).

        Each dict must have: name, threat, action, match_type, match_args,
        description. Optional: severity, fail_closed.
        """
        rules: list[PolicyRule] = []
        for rd in rules_data:
            threat = ThreatCategory(rd["threat"])
            matcher = _build_matcher(rd["match_type"], rd.get("match_args", {}))
            rule = PolicyRule(
                name=rd["name"],
                threat=threat,
                action=rd["action"],
                description=rd["description"],
                match=matcher,
                severity=rd.get("severity", "high"),
                fail_closed=rd.get("fail_closed", True),
            )
            rules.append(rule)
        return cls(rules=rules)


# ══════════════════════════════════════════════════════════════════
#  4.3.0 — Plan B Step 2: shared PolicyGate base class
# ══════════════════════════════════════════════════════════════════
#
# Concrete security guards (pii / deserialize / circuit_breaker /
# rce_injection / http_client / sql_injection) all need the same four
# things:
#
#   1. A 4-tier fail-mode chain (silent / warn / warn+log / hard_deny)
#      whose resolved value depends on the active feature-toggle profile
#      (lite / mainstream / strict / paranoid) plus a per-feature
#      override on disk.
#   2. An escape hatch — operators paste ``# CONCINNO_DISABLE:<reason>``
#      into the payload to opt out of a single check (mirrors the
#      ``#DESTROY_CONFIRMED:<reason>`` pattern used by destruction_guard).
#   3. A JSON-line audit log at ``~/.concinno/audit/<guard>.jsonl`` with
#      10 MB rotation.
#   4. An optional ZIQ outcome-bus emit so the online-learning loop can
#      tune any ZIQ-autotunable parameters the guard owns.
#
# Implementing those four things six times = six chances to drift. So
# we centralise here. Concrete guards subclass ``PolicyGate`` and
# implement ``scan(payload) -> list[Finding]``; the base owns the
# fail-mode chain, escape detection, audit log and ZIQ emit.
#
# Design notes
# ------------
# * **Inheritance, not Protocol** — the spec called for a Protocol
#   pattern but the base genuinely needs to *own* the fail-mode chain,
#   audit log, and ZIQ emit (template-method shape: subclass provides
#   ``scan``; base orchestrates everything else). A Protocol forces
#   every guard to re-implement ``evaluate``, which is exactly the
#   duplication this module exists to prevent.
# * **No runtime deps** — ``feature_config.get_fail_mode`` and
#   ``ziq_outcome_bus.get_bus`` are *lazy-imported* inside the methods
#   that need them. Importing :mod:`concinno.security.policy_gate` at
#   module load does not pull either, so the L9 PolicyEngine path
#   stays as cheap as it is today.
# * **Audit log lazy mkdir** — we only ``mkdir`` when the first audit
#   entry actually needs to be written. Tests that never trigger the
#   ``warn+log`` branch leave no filesystem artefact.
# * **Thread safety** — audit-log append serialises through a
#   per-path :class:`threading.Lock`. POSIX append mode is atomic for
#   small writes, but NTFS does not give the same guarantee for
#   unsynchronised concurrent writers; the lock keeps Windows + POSIX
#   on equal footing. The lock is process-local — multi-process
#   writers to the same audit file are out of scope.
# * **Graceful ZIQ degrade** — if ``ziq_outcome_bus`` is missing,
#   raises an exception, or the bus is disabled via env, the emit
#   silently no-ops. Security guards never block on telemetry.

# ── Type aliases shared with feature_config ──────────────────

# Re-export FailMode here so guards don't need a second import line.
# Kept as a Literal alias so mypy strict still narrows correctly.
FailMode = Literal["silent", "warn", "warn+log", "hard_deny"]

_VALID_FAIL_MODES: frozenset[str] = frozenset({
    "silent", "warn", "warn+log", "hard_deny",
})

# Severity classification for individual ``Finding`` instances. The
# canonical mapping for security guards (independent of the L9
# PolicyEngine which uses string severities directly).
Severity = Literal["low", "medium", "high", "critical"]

_VALID_SEVERITIES: frozenset[str] = frozenset({
    "low", "medium", "high", "critical",
})

# Public outcome of an :meth:`PolicyGate.evaluate` call.
Decision = Literal["accept", "warn", "deny"]


# ── Audit log defaults ───────────────────────────────────────

# 10 MB before rotation — matches destruction_guard convention so
# operators only learn one number for both audit streams.
_AUDIT_ROTATE_MAX_BYTES = 10 * 1024 * 1024

# Default escape pattern. Mirrors destruction_guard's
# ``#DESTROY_CONFIRMED:<reason>`` — same shape so muscle memory carries
# over. Subclasses can override per-call via ``evaluate(escape_pattern=)``.
_DEFAULT_ESCAPE_PATTERN = "# CONCINNO_DISABLE:"


def _audit_dir() -> Path:
    """Return ``~/.concinno/audit/`` honouring the env override.

    The override (``CONCINNO_AUDIT_DIR``) lets tests redirect writes
    into a tmp path without monkeypatching :func:`pathlib.Path.home`.
    """
    override = os.environ.get("CONCINNO_AUDIT_DIR")
    if override:
        return Path(override)
    return Path.home() / ".concinno" / "audit"


# Per-file write lock. Append-mode is **not** atomic on Windows for
# concurrent writers (NTFS does not provide POSIX O_APPEND semantics
# for unsynchronised file handles), so we serialise audit appends per
# log path. The lock is process-local — multiple processes writing to
# the same audit file would still tear, but that is out of scope: each
# guard runs in one Python process per CC session.
_audit_locks: dict[str, threading.Lock] = {}
_audit_locks_guard = threading.Lock()


def _lock_for(path: Path) -> threading.Lock:
    """Return the lock guarding ``path``, creating one on first call."""
    key = str(path)
    with _audit_locks_guard:
        lock = _audit_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _audit_locks[key] = lock
        return lock


def _rotate_if_needed(path: Path) -> None:
    """Rotate ``path`` to ``path.1`` when it exceeds 10 MB.

    Best-effort: every failure is swallowed. Audit logging never
    breaks the call path that triggered it. We keep exactly one
    rotated archive (``<name>.1``) — older content is overwritten on
    the next rotation. Concrete guards needing deeper history can
    plug their own retention by overriding :meth:`_emit_audit`.
    """
    try:
        if not path.exists():
            return
        if path.stat().st_size < _AUDIT_ROTATE_MAX_BYTES:
            return
        archive = path.with_name(f"{path.name}.1")
        if archive.exists():
            try:
                archive.unlink()
            except OSError:
                pass
        path.rename(archive)
    except OSError:
        # Filesystem hiccup — fall through; the next append will retry.
        return


def _redact_snippet(snippet: str, max_len: int = 80) -> str:
    """Truncate snippets so audit lines stay readable.

    Concrete guards that match raw secrets should pre-redact their
    findings (replace the matched span with ``***``) before passing
    the snippet to ``Finding``; this helper only enforces a length
    cap as a defence-in-depth measure so a buggy guard never leaks
    a megabyte of payload into the audit log.
    """
    if len(snippet) <= max_len:
        return snippet
    return snippet[: max_len - 3] + "..."


# ── Data classes ─────────────────────────────────────────────


@dataclass(frozen=True)
class Finding:
    """One detection result emitted by a concrete guard's ``scan``.

    Attributes:
        type: Stable identifier for the finding kind, e.g. ``"ssn"``,
            ``"credit_card"``, ``"pickle.loads"``. Used as a stable
            key by audit-log consumers and ZIQ outcome consumers.
        span: ``(start, end)`` byte/char offset into the original
            payload. Stays as a 2-tuple of ints for JSON friendliness;
            both ``-1`` if the guard cannot localise the match (e.g.
            an AST-level finding with no source position).
        snippet: Already-redacted excerpt suitable for an audit log.
            Subclasses are responsible for redaction; this dataclass
            only enforces a length cap on serialisation.
        severity: ``low`` / ``medium`` / ``high`` / ``critical``.
            Validated at ``__post_init__``.
        message: Optional human-readable detail. Empty by default.
    """

    type: str
    span: tuple[int, int]
    snippet: str
    severity: Severity
    message: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.type, str) or not self.type:
            raise ValueError("Finding.type must be a non-empty string")
        if (
            not isinstance(self.span, tuple)
            or len(self.span) != 2
            or not all(isinstance(x, int) for x in self.span)
        ):
            raise TypeError("Finding.span must be a 2-tuple of int")
        if self.severity not in _VALID_SEVERITIES:
            raise ValueError(
                f"Finding.severity {self.severity!r} not in "
                f"{sorted(_VALID_SEVERITIES)}"
            )

    def to_audit_dict(self) -> dict[str, Any]:
        """Serialise to a dict ready for ``json.dumps``.

        The snippet is re-redacted to the audit cap defensively so
        callers cannot accidentally enlarge it via a custom subclass.
        """
        return {
            "type": self.type,
            "span": list(self.span),
            "snippet": _redact_snippet(self.snippet),
            "severity": self.severity,
            "message": self.message,
        }


@dataclass(frozen=True)
class PolicyGateResult:
    """Outcome of one :meth:`PolicyGate.evaluate` call.

    Attributes:
        decision: ``accept`` (allow), ``warn`` (allow + stderr warn),
            or ``deny`` (caller must abort the operation).
        reason: One-line explanation suitable for stderr / audit.
        fail_mode: The resolved fail-mode that produced ``decision``.
        escaped: ``True`` when the escape pattern was hit and overrode
            an otherwise-blocking decision.
        findings: All findings the scan returned (zero on a clean
            payload). Caller may inspect for richer reporting.
        audit_entry: The dict that gets / would get appended to the
            audit log. Always populated, even when ``fail_mode`` is
            ``silent`` and we don't actually write — useful for tests
            and for callers that want to ship audit through their own
            sink instead of (or in addition to) the file.
    """

    decision: Decision
    reason: str
    fail_mode: FailMode
    escaped: bool
    findings: tuple[Finding, ...]
    audit_entry: dict[str, Any]


# ── PolicyGate base class ────────────────────────────────────


class PolicyGate:
    """Base class for Concinno security guards.

    Subclasses **must** set the class attribute :attr:`name` (used as
    the audit-log filename and the ZIQ outcome source) and implement
    :meth:`scan`. Everything else — fail-mode resolution, escape-hatch
    detection, audit-log write, ZIQ emit — is provided by the base
    via :meth:`evaluate`, the public entry point.

    Example::

        class PIIGuard(PolicyGate):
            name = "pii_guard"

            def scan(self, payload):
                findings = []
                for m in _SSN_RE.finditer(str(payload)):
                    findings.append(Finding(
                        type="ssn",
                        span=m.span(),
                        snippet="***",
                        severity="high",
                    ))
                return findings

        result = PIIGuard().evaluate("123-45-6789")
        if result.decision == "deny":
            sys.exit(2)
    """

    #: Stable, file-system-safe identifier. Subclasses **must** override.
    name: str = ""

    def __init__(
        self,
        profile: str = "lite",
        fail_mode_override: FailMode | None = None,
    ) -> None:
        if not self.name:
            raise TypeError(
                f"{type(self).__name__}.name is empty. Subclasses must set "
                "a non-empty class attribute (used for audit-log filename "
                "and ZIQ outcome source)."
            )
        if fail_mode_override is not None and fail_mode_override not in _VALID_FAIL_MODES:
            raise ValueError(
                f"fail_mode_override {fail_mode_override!r} not in "
                f"{sorted(_VALID_FAIL_MODES)}"
            )
        self._profile = profile
        self._fail_mode_override = fail_mode_override

    # ── API for subclasses ────────────────────────────────────

    def scan(self, payload: str | bytes | dict[str, Any]) -> list[Finding]:
        """Subclass entry point. Return findings; empty list = clean.

        Concrete guards must not raise on a malformed payload — they
        should return a single ``Finding`` of severity ``low`` with
        ``type="malformed_payload"`` so the policy chain still has
        something to evaluate. Raising here is a programmer error and
        will fail the audit pipeline because ``evaluate`` does not
        catch :class:`Exception` from ``scan``.
        """
        raise NotImplementedError(
            f"{type(self).__name__} must implement scan(payload)"
        )

    # ── Public entry point ────────────────────────────────────

    def evaluate(
        self,
        payload: str | bytes | dict[str, Any],
        *,
        escape_pattern: str = _DEFAULT_ESCAPE_PATTERN,
    ) -> PolicyGateResult:
        """Run :meth:`scan`, apply fail-mode, write audit, return result.

        This is the *only* method most callers should touch. The
        4-step pipeline:

          1. Detect the escape pattern. If present, short-circuit to
             ``accept`` with ``escaped=True``. The audit entry still
             records the escape so post-hoc review can spot abuse.
          2. Run :meth:`scan`. Empty findings → ``accept``.
          3. Resolve the effective fail-mode (override > profile-meta
             chain) and map it to ``decision``:

             - ``silent``    → ``accept`` (no stderr, no log file)
             - ``warn``      → ``warn``   (stderr only)
             - ``warn+log``  → ``warn``   (stderr + audit log)
             - ``hard_deny`` → ``deny``   (audit log + caller aborts)

          4. Emit the audit dict through :meth:`_emit_audit` (writes
             only when the fail-mode is ``warn+log`` or ``hard_deny``)
             and through :meth:`_emit_ziq_outcome` (always tries; the
             bus is opt-in and degrades gracefully).
        """
        # 1. Escape hatch — works even when scan would have flagged
        #    something. We record the override in the audit entry so
        #    a downstream auditor can grep for abuse patterns.
        payload_text = self._payload_to_text(payload)
        escaped = bool(escape_pattern) and escape_pattern in payload_text

        # 2. Run subclass scan. Allowing exceptions to propagate is
        #    intentional — a guard that crashes is a bug, not a
        #    routine event we want to swallow.
        findings: list[Finding]
        if escaped:
            # Skip scan entirely — operator opted out of this check.
            findings = []
        else:
            findings = list(self.scan(payload))

        # 3. Decide.
        fail_mode = self._resolve_fail_mode()
        decision: Decision
        reason: str

        if escaped:
            decision = "accept"
            reason = f"{self.name}: escape pattern {escape_pattern!r} detected"
        elif not findings:
            decision = "accept"
            reason = f"{self.name}: clean ({fail_mode})"
        elif fail_mode == "silent":
            decision = "accept"
            reason = (
                f"{self.name}: {len(findings)} finding(s) "
                f"suppressed (silent mode)"
            )
        elif fail_mode in ("warn", "warn+log"):
            decision = "warn"
            reason = (
                f"{self.name}: {len(findings)} finding(s) "
                f"({fail_mode})"
            )
        else:  # hard_deny
            decision = "deny"
            reason = (
                f"{self.name}: hard_deny — {len(findings)} finding(s)"
            )

        audit_entry = self._build_audit_entry(
            decision=decision,
            reason=reason,
            fail_mode=fail_mode,
            escaped=escaped,
            findings=findings,
        )

        result = PolicyGateResult(
            decision=decision,
            reason=reason,
            fail_mode=fail_mode,
            escaped=escaped,
            findings=tuple(findings),
            audit_entry=audit_entry,
        )

        # 4. Emit. Order: audit first (durable), ZIQ second (best-effort).
        self._emit_audit(result)
        self._emit_ziq_outcome(result)

        # Stderr warn happens here so unit tests of `evaluate`
        # observe the side-effect even when they never check the audit
        # log file. ``silent`` and ``accept`` paths skip stderr.
        if decision == "warn":
            print(reason, file=sys.stderr)

        return result

    # ── Hook points (overridable but not abstract) ────────────

    def _resolve_fail_mode(self) -> FailMode:
        """Resolve the effective fail-mode.

        Order (later wins):
          1. Profile catch-all + per-feature override (delegated to
             :func:`feature_config.get_fail_mode`, which itself runs
             the 6-source chain when ``cfg`` is supplied — we omit
             ``cfg`` here so the result is purely profile-driven and
             cheap; concrete guards that *want* the user-override
             chain can pass their own ``cfg`` by overriding this
             method).
          2. Constructor ``fail_mode_override`` — last wins.

        Lazy-imports ``feature_config`` to avoid a hot-path cost on
        the L9 PolicyEngine which doesn't need this resolver.
        """
        base: FailMode
        try:
            # Local import — keeps the security/__init__.py cycle-free
            # and avoids paying for feature_config import on the L9
            # path that doesn't use the guard base.
            from concinno.feature_config import get_fail_mode  # noqa: PLC0415
            raw = get_fail_mode(self.name, profile=self._profile)
            # ``feature_config.get_fail_mode`` already validates against
            # ``VALID_FAIL_MODES``; we re-check defensively so mypy can
            # narrow the str → Literal here.
            if raw not in _VALID_FAIL_MODES:
                base = "warn"
            else:
                # Membership in _VALID_FAIL_MODES proves the value is
                # one of the four FailMode literals; mypy narrows
                # automatically because _VALID_FAIL_MODES is a frozenset
                # of the same literal strings.
                base = raw
        except Exception:
            # Profile-resolver failed (corrupted config, missing
            # profile, etc). Default to ``warn`` — non-blocking but
            # visible. Better than silent (we'd swallow the breakage)
            # or hard_deny (we'd block legitimate work on a config bug).
            base = "warn"

        if self._fail_mode_override is not None:
            return self._fail_mode_override
        return base

    def _emit_audit(self, result: PolicyGateResult) -> None:
        """Append the audit entry to ``~/.concinno/audit/<name>.jsonl``.

        Only writes when ``fail_mode`` is ``warn+log`` or ``hard_deny``.
        Lazily creates the audit directory on first write so a fresh
        install with no incidents leaves no filesystem trace.

        Best-effort: filesystem errors are swallowed. The audit dict
        is still attached to the returned result, so callers that
        want stricter durability (e.g. ship to a SIEM) can subscribe
        through ``_emit_ziq_outcome`` or override this method.
        """
        if result.fail_mode not in ("warn+log", "hard_deny"):
            return
        try:
            audit_dir = _audit_dir()
            audit_dir.mkdir(parents=True, exist_ok=True)
            log_path = audit_dir / f"{self.name}.jsonl"
            line = json.dumps(result.audit_entry, ensure_ascii=False)
            # Per-path lock keeps rotation + append atomic relative to
            # other in-process writers. Without the lock NTFS append
            # mode tears under concurrent threads (Windows ≠ POSIX
            # O_APPEND).
            with _lock_for(log_path):
                _rotate_if_needed(log_path)
                with log_path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
        except OSError:
            # Audit write failure must not break the guard chain.
            # Concrete guards that need stricter durability can
            # override this method.
            return

    def _emit_ziq_outcome(self, result: PolicyGateResult) -> None:
        """Best-effort ZIQ outcome bus emit.

        The bus is opt-in (silent no-op when:

          * ``concinno.ziq_outcome_bus`` import fails (it shouldn't,
            but lazy-import is defensive),
          * the bus is disabled via ``CONCINNO_ZIQ_BUS_DISABLED=1``,
          * the bus emit raises (we swallow to keep the guard chain
            unblockable).

        The reward convention: ``1.0`` when the guard accepted (no
        threat or escape), ``0.0`` when the guard denied. ``warn`` is
        ``0.5`` so FTRL learns gentler signals. Subclasses with
        domain-specific reward shaping can override.
        """
        try:
            # Lazy import — bus may not exist in stripped-down installs.
            # ``get_bus`` returns the process-wide singleton; ``emit`` in
            # this module is a *decorator*, not a direct emit function,
            # so we go through the bus instance.
            from concinno.ziq_outcome_bus import (  # noqa: PLC0415
                Outcome,
                get_bus,
                is_bus_disabled,
            )
        except ImportError:
            return

        if is_bus_disabled():
            return

        reward = {"accept": 1.0, "warn": 0.5, "deny": 0.0}[result.decision]
        try:
            get_bus().emit(
                Outcome(
                    tunable=f"security.{self.name}",
                    value=reward,
                    reward=reward,
                    metadata={
                        "decision": result.decision,
                        "fail_mode": result.fail_mode,
                        "escaped": result.escaped,
                        "n_findings": len(result.findings),
                    },
                    source=f"PolicyGate.{self.name}",
                )
            )
        except Exception:
            return

    # ── Internals ─────────────────────────────────────────────

    def _build_audit_entry(
        self,
        *,
        decision: Decision,
        reason: str,
        fail_mode: FailMode,
        escaped: bool,
        findings: list[Finding],
    ) -> dict[str, Any]:
        """Assemble the JSON-line audit dict.

        The schema is stable contract — bumping it breaks log
        consumers. Add new keys by appending; do not rename or remove.
        """
        return {
            "ts": time.time(),
            "guard": self.name,
            "profile": self._profile,
            "decision": decision,
            "fail_mode": fail_mode,
            "escaped": escaped,
            "reason": reason,
            "findings": [f.to_audit_dict() for f in findings],
        }

    @staticmethod
    def _payload_to_text(payload: str | bytes | dict[str, Any]) -> str:
        """Coerce ``payload`` into a string for escape-pattern scanning.

        ``bytes`` are decoded UTF-8 with replacement so binary blobs
        never raise here. ``dict`` round-trips through ``json.dumps``
        with ``default=str`` so non-serialisable values become their
        ``repr`` and the escape-pattern scan still works on values
        nested inside the dict.
        """
        if isinstance(payload, str):
            return payload
        if isinstance(payload, bytes):
            return payload.decode("utf-8", errors="replace")
        try:
            return json.dumps(payload, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return repr(payload)
