"""concinno.security.policy_gate — Layer 9 policy-as-code deny gates.

@module security.policy_gate
@responsibility Define security policies as DATA (dict/YAML schemas), not as
    code scattered across 55 guards. Each policy is a named rule with: a threat
    category (OWASP LLM01-LLM10 + NIST AI 800-2), a match predicate, and a
    deny/allow/audit action. The PolicyEngine loads a set of policies and
    evaluates every tool call against them. Fail-closed: if the engine can't
    evaluate a policy (missing field, exception), it denies.

    Layer 9 of the 9-layer security stack — BEYOND Hermes. This is the first
    governance-level security primitive in the agent ecosystem. Where Hermes
    has 7 layers of implementation-level security, this is the capstone
    policy-as-code engine that compiles OWASP LLM Top 10 / NIST AI 800-2
    threat categories into executable deny rules.

@dependencies stdlib only (re, fnmatch, dataclasses, enum, typing, json)
@exports ThreatCategory, PolicyAction, PolicyRule, PolicyContext,
    PolicyVerdict, EngineResult, PolicyMatcher, ToolNameMatcher,
    ContentPatternMatcher, MetadataMatcher, CompositeMatcher,
    CallableMatcher, OWASP_LLM_BASELINE, PolicyEngine
"""

from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import dataclass, field
from enum import Enum
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
