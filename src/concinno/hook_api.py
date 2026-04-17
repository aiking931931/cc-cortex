"""concinno.hook_api — Public composition API for Claude Code hooks.

@module hook_api
@responsibility Provide HookResult builder and Pipeline guard chain with
               short-circuit-on-deny semantics for PreToolUse/PostToolUse hooks.
@dependencies (none — standalone)
@exports HookResult, Pipeline
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Union

__all__ = ["HookResult", "Pipeline", "GuardFn"]

# ── Types ─────────────────────────────────────────────────

# A guard function: receives (tool_name, tool_input, **ctx)
# Returns HookResult, dict, str (warning text), or None (no opinion).
GuardFn = Callable[..., Optional[Union["HookResult", dict, str]]]


# ── HookResult ────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class HookResult:
    """Typed result for Claude Code hooks.

    Use the class methods instead of constructing directly::

        HookResult.deny("dangerous command")
        HookResult.allow()
        HookResult.warn("file not read yet")
    """

    decision: str  # "allow" | "deny"
    reason: str = ""
    context: str = ""

    # ── Constructors ──────────────────────────────────────

    @classmethod
    def allow(cls, context: str = "") -> HookResult:
        """Allow the tool call, optionally injecting context."""
        return cls("allow", context=context)

    @classmethod
    def deny(cls, reason: str, context: str = "") -> HookResult:
        """Deny the tool call with a reason shown to the model."""
        return cls("deny", reason=reason, context=context)

    @classmethod
    def warn(cls, context: str) -> HookResult:
        """Allow but inject a warning into additionalContext."""
        return cls("allow", context=context)

    # ── Predicates ────────────────────────────────────────

    @property
    def denied(self) -> bool:
        return self.decision == "deny"

    @property
    def allowed(self) -> bool:
        return self.decision == "allow"

    # ── Conversion ────────────────────────────────────────

    def to_dict(self) -> dict[str, str]:
        """Convert to Claude Code hook JSON format."""
        d: dict[str, str] = {"permissionDecision": self.decision}
        if self.reason:
            d["reason"] = self.reason
        if self.context:
            d["additionalContext"] = self.context
        return d


# ── Pipeline ──────────────────────────────────────────────


def _normalize(result: Any) -> Optional[HookResult]:
    """Convert guard return value to HookResult (or None)."""
    if result is None:
        return None
    if isinstance(result, HookResult):
        return result
    if isinstance(result, dict):
        decision = result.get("permissionDecision", "allow")
        return HookResult(
            decision=decision,
            reason=result.get("reason", ""),
            context=result.get("additionalContext", ""),
        )
    if isinstance(result, str):
        # Bare string = warning text
        return HookResult.warn(result)
    return None


@dataclass
class Pipeline:
    """Ordered guard chain with short-circuit-on-deny.

    Two phases:
      1. **Deny guards** — run in order; first deny short-circuits.
      2. **Warn guards** — run all; warnings collected into additionalContext.

    Fail-open: any guard that raises an exception is silently skipped.

    Example::

        pipe = Pipeline()
        pipe.add_deny_guard("destruction", evaluate)
        pipe.add_warn_guard("read_first", check_read_first)
        result = pipe.run("Bash", {"command": "rm -rf /"})
        # result == {"permissionDecision": "deny", "reason": "..."}
    """

    _deny_guards: list[tuple[str, GuardFn]] = field(default_factory=list)
    _warn_guards: list[tuple[str, GuardFn]] = field(default_factory=list)

    # ── Registration ──────────────────────────────────────

    def add_deny_guard(self, name: str, fn: GuardFn) -> "Pipeline":
        """Add a guard that can DENY tool calls (runs in phase 1)."""
        self._deny_guards.append((name, fn))
        return self

    def add_warn_guard(self, name: str, fn: GuardFn) -> "Pipeline":
        """Add a guard that collects warnings (runs in phase 2)."""
        self._warn_guards.append((name, fn))
        return self

    # ── Execution ─────────────────────────────────────────

    def run(self, tool_name: str, tool_input: dict, **ctx: Any) -> dict:
        """Execute the pipeline and return a Claude Code hook JSON dict.

        Args:
            tool_name: The tool being called (e.g. "Bash", "Edit").
            tool_input: The tool's input parameters.
            **ctx: Extra context passed to every guard (session_id, etc).

        Returns:
            Dict ready for ``json.dump`` to stdout::

                {"permissionDecision": "allow|deny", ...}
        """
        # Phase 1: deny guards (short-circuit on first deny)
        for _name, guard in self._deny_guards:
            try:
                raw = guard(tool_name, tool_input, **ctx)
            except Exception:
                continue  # fail-open
            result = _normalize(raw)
            if result is not None and result.denied:
                return result.to_dict()

        # Phase 2: warn guards (collect all)
        warnings: list[str] = []
        for _name, guard in self._warn_guards:
            try:
                raw = guard(tool_name, tool_input, **ctx)
            except Exception:
                continue  # fail-open
            result = _normalize(raw)
            if result is not None and result.context:
                warnings.append(result.context)

        # Build output
        out: dict[str, str] = {"permissionDecision": "allow"}
        if warnings:
            out["additionalContext"] = "\n".join(warnings)
        return out

    # ── Introspection ─────────────────────────────────────

    def list_guards(self) -> dict[str, list[str]]:
        """Return registered guard names by phase."""
        return {
            "deny": [name for name, _ in self._deny_guards],
            "warn": [name for name, _ in self._warn_guards],
        }
