"""concinno.guards.base — Base classes for the unified guard pipeline.

@module base
@responsibility Guard contract: BaseGuard ABC, result/context types,
    two-outcome (ALLOW/DENY) enforcement
@dependencies (none — stdlib only)
@exports GuardAction, GuardCategory, GuardContext, GuardResult,
    BaseGuard
"""

from __future__ import annotations

import fnmatch
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class GuardAction(Enum):
    """Guard outcome.

    ALLOW and DENY are the two core states — no WARN.

    REWRITE is a third state for PreToolUse only: the guard rewrites
    ``tool_input`` in place (e.g. ``rm -rf *`` → ``rm -rf --dry-run *``)
    and the pipeline passes the new input to the remaining guards,
    then emits ``hookSpecificOutput.updatedInput`` so Claude Code runs
    the modified tool call. REWRITE is a form of ALLOW — the call
    proceeds, just with safer parameters.
    """

    ALLOW = "allow"
    DENY = "deny"
    REWRITE = "rewrite"


class GuardCategory(Enum):
    """Execution layer. Pipeline sorts guards by category value.

    SECURITY (1) runs first — hard deny, no step-back.
    QUALITY  (2) runs second — hard deny + step-back middleware.
    COGNITIVE (3) runs last — knowledge injection on ALLOW.
    """

    SECURITY = 1
    QUALITY = 2
    COGNITIVE = 3


@dataclass(frozen=True)
class GuardContext:
    """Everything a guard needs. Built once per hook invocation.

    Guards receive this instead of raw (tool_name, tool_input, **kwargs).
    Single source of truth — no guard reads stdin or env vars directly.
    """

    tool_name: str
    tool_input: dict
    session_id: str
    cache_dir: str
    hook_event: str  # "PreToolUse" | "PostToolUse" | "Stop"
    # Optional extras for specific guards
    tool_result: str = ""  # PostToolUse only
    workspace: str = ""  # CLAUDE_PROJECT_DIR

    @staticmethod
    def from_hook_data(data: dict) -> GuardContext:
        """Build from Claude Code hook JSON. THE entry point."""
        if not isinstance(data, dict):
            data = {}

        tool_name = data.get("tool_name", "")
        tool_input = data.get("tool_input", {})
        if not isinstance(tool_input, dict):
            tool_input = {}

        session_id = (
            data.get("session_id")
            or os.environ.get("CLAUDE_SESSION_ID", "")
            or os.environ.get("CC_SESSION_ID", "")
        )

        workspace = os.environ.get("CLAUDE_PROJECT_DIR", "")
        cache_dir = os.path.join(workspace, ".concinno_cache") if workspace else ""

        hook_event = data.get("hook_event", "PreToolUse")

        tool_result = ""
        if isinstance(data.get("tool_result"), str):
            tool_result = data["tool_result"]
        elif isinstance(data.get("tool_result"), dict):
            tool_result = data["tool_result"].get("stdout", "")

        return GuardContext(
            tool_name=tool_name,
            tool_input=tool_input,
            session_id=session_id,
            cache_dir=cache_dir,
            hook_event=hook_event,
            tool_result=tool_result,
            workspace=workspace,
        )


@dataclass(frozen=True)
class GuardResult:
    """Immutable guard outcome.

    Three factory methods: allow() / deny() / rewrite().
    - ``context`` carries additionalContext for knowledge injection
      (NOT warnings).
    - ``updated_input`` is populated only by ``rewrite()`` and carries
      the replacement ``tool_input`` dict. The pipeline hands this dict
      to the remaining guards and emits it as
      ``hookSpecificOutput.updatedInput``.
    - ``metadata`` carries guard-specific extras (audit IDs, risk
      levels, etc).
    - ``advisory`` marks the result as silenceable prose (CBUA /
      WIREDO / Read:Edit ratio nags). In the ``competition`` profile
      the pipeline routes advisory contexts to audit log instead of
      injecting them into the LLM prompt. Defaults to False so all
      existing callers (positional or keyword) stay unaffected.
    """

    action: GuardAction
    reason: str = ""
    context: str = ""
    updated_input: Optional[dict] = None
    metadata: dict = field(default_factory=dict)
    advisory: bool = False

    @staticmethod
    def allow(context: str = "", **metadata: Any) -> GuardResult:
        """Allow the tool call, optionally injecting knowledge context."""
        return GuardResult(
            action=GuardAction.ALLOW,
            context=context,
            metadata=dict(metadata) if metadata else {},
        )

    @staticmethod
    def allow_advisory(
        *,
        context: str,
        reason: str = "",
        **metadata: Any,
    ) -> GuardResult:
        """Allow with an advisory (silenceable) context payload.

        Use this for prose intended to coach the LLM (B1/C1/U1 markers,
        WIREDO six-dim reminders, Read:Edit ratio warnings, token zone
        advice, UX streak bursts). The pipeline will suppress the
        context when the active profile is ``competition``, keeping
        the guard itself active and audit-logged. Safety guards
        (destruction, bash validators, permission FSM, secret scan,
        premise gate, butterfly, handoff required) MUST NOT use this
        helper — their messages must always reach the LLM.
        """
        return GuardResult(
            action=GuardAction.ALLOW,
            reason=reason,
            context=context,
            metadata=dict(metadata) if metadata else {},
            advisory=True,
        )

    @staticmethod
    def deny(reason: str, context: str = "", **metadata: Any) -> GuardResult:
        """Deny the tool call with a reason."""
        return GuardResult(
            action=GuardAction.DENY,
            reason=reason,
            context=context,
            metadata=dict(metadata) if metadata else {},
        )

    @staticmethod
    def rewrite(
        updated_input: dict,
        reason: str = "",
        context: str = "",
        **metadata: Any,
    ) -> GuardResult:
        """Rewrite ``tool_input`` to a safer / canonical form.

        Requires a non-empty dict; pipeline replaces ``ctx.tool_input``
        with this value and continues executing the remaining guards
        against the rewritten version. The rewritten dict is emitted
        as ``hookSpecificOutput.updatedInput`` so Claude Code runs the
        modified tool call. ``reason`` becomes a visible rewrite note
        in additionalContext so the user understands what changed.
        """
        if not isinstance(updated_input, dict) or not updated_input:
            msg = (
                "GuardResult.rewrite requires a non-empty dict. Use "
                "allow() if there is nothing to change."
            )
            raise ValueError(msg)
        return GuardResult(
            action=GuardAction.REWRITE,
            reason=reason,
            context=context,
            updated_input=dict(updated_input),
            metadata=dict(metadata) if metadata else {},
        )

    def to_hook_dict(self) -> dict:
        """Convert to Claude Code hook JSON format.

        ALLOW / DENY produce the classic ``permissionDecision``-based
        shape. REWRITE produces a hook-spec-output payload with
        ``updatedInput`` so the CC runtime runs the modified call.
        """
        if self.action == GuardAction.REWRITE:
            hso: dict[str, Any] = {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
            }
            if self.updated_input:
                hso["updatedInput"] = dict(self.updated_input)
            d: dict[str, Any] = {"hookSpecificOutput": hso}
            # Surface the rewrite reason as additionalContext so the
            # user sees what changed.
            notes: list[str] = []
            if self.reason:
                notes.append(f"↻ rewritten: {self.reason}")
            if self.context:
                notes.append(self.context)
            if notes:
                d["additionalContext"] = "\n".join(notes)
            for k, v in self.metadata.items():
                if k not in d:
                    d[k] = v
            return d

        d = {"permissionDecision": self.action.value}
        if self.reason:
            d["reason"] = self.reason
        if self.context:
            d["additionalContext"] = self.context
        # Merge metadata (audit_id, risk_level, etc.)
        for k, v in self.metadata.items():
            if k not in d:
                d[k] = v
        return d

    @staticmethod
    def from_legacy_dict(d: dict) -> Optional[GuardResult]:
        """Convert legacy guard dict to GuardResult. For migration."""
        if not isinstance(d, dict):
            return None
        decision = d.get("permissionDecision", "allow")
        if decision == "deny":
            return GuardResult.deny(
                reason=d.get("reason", ""),
                context=d.get("additionalContext", ""),
            )
        return GuardResult.allow(context=d.get("additionalContext", ""))


def _extract_target_path(ctx: GuardContext) -> str:
    """Extract the file path being operated on from tool context."""
    inp = ctx.tool_input
    # Read/Write/Edit have file_path
    path = inp.get("file_path", "")
    if path:
        return path
    # Glob has path + pattern
    if inp.get("pattern"):
        return inp.get("path", "")
    # Grep has path
    if inp.get("path"):
        return inp.get("path", "")
    # Bash — extract first path-like argument
    cmd = inp.get("command", "")
    if cmd:
        for part in cmd.split():
            if "/" in part or "\\" in part:
                return part
    return ""


class BaseGuard(ABC):
    """Abstract base for all guards. THE template.

    Subclass this, set name/category, implement check().
    Pipeline.register() enforces isinstance(guard, BaseGuard).

    Attributes:
        name: Unique identifier (e.g. "git_safety"). Used for health tracking.
        category: Execution layer — determines run order in pipeline.
        step_back_reason: Non-empty = pipeline auto-applies step-back on DENY.
                         Empty = immediate hard deny (security guards).
    """

    name: str = ""
    category: GuardCategory = GuardCategory.QUALITY
    step_back_reason: str = ""
    path_scope: list[str] = []  # glob patterns, empty = always active

    def matches_path_scope(self, ctx: GuardContext) -> bool:
        """Check if this guard should run for the given context.

        Empty path_scope = always active (backward compatible).
        Non-empty = only active when tool operates on matching paths.
        """
        if not self.path_scope:
            return True

        target = _extract_target_path(ctx)
        if not target:
            return True  # no path info → run guard (safe default)

        target = target.replace("\\", "/")
        return any(fnmatch.fnmatch(target, p) for p in self.path_scope)

    @abstractmethod
    def check(self, ctx: GuardContext) -> Optional[GuardResult]:
        """PreToolUse check. Return None = no opinion (pass through).

        This is the ONLY method guards MUST implement.
        Pipeline calls this during run_pre_tool().
        """

    def on_post_tool(self, ctx: GuardContext) -> Optional[GuardResult]:
        """PostToolUse processing. Override if guard needs post-tool logic.

        Pipeline calls this during run_post_tool().
        Return GuardResult.allow(context=...) to inject feedback.
        """
        return None

    def on_stop(self, ctx: GuardContext) -> Optional[GuardResult]:
        """Stop processing. Override if guard needs session-end logic.

        Pipeline calls this during run_stop().
        """
        return None

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} name={self.name!r} category={self.category.name}>"
