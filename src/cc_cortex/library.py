"""cc_cortex.library — CCC as a Python library (non-hook entry point).

@module library
@responsibility Expose CCC guard pipeline, routing, and prompt assembly
    as direct Python function calls, independent of Claude Code's
    stdin/stdout hook protocol.
@dependencies cc_cortex.guards.*, cc_cortex.c0_router, cc_cortex.prompt_engine
@exports CortexLibrary, LibraryResult

Use case: Aegis framework (or any Python app) that wants CCC's 55+
guards and cognition without running inside Claude Code.

    from cc_cortex.library import CortexLibrary

    cortex = CortexLibrary(workspace="/path/to/project")
    result = cortex.check_safety("Bash", {"command": "rm -rf /"})
    if result.denied:
        print(f"Blocked: {result.reason}")
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from typing import Optional

from cc_cortex.guards.base import GuardContext
from cc_cortex.guards.pipeline import GuardPipeline
from cc_cortex.guards.registry import create_default_pipeline


@dataclass
class LibraryResult:
    """Clean result type for library consumers (no hook JSON)."""

    action: str  # "allow" | "deny" | "rewrite"
    denied: bool
    reason: str = ""
    context: str = ""
    rewritten_input: Optional[dict] = None
    raw: dict = field(default_factory=dict)

    @staticmethod
    def from_hook_dict(d: dict) -> LibraryResult:
        """Convert pipeline's hook dict to LibraryResult."""
        decision = d.get("permissionDecision", "allow")
        return LibraryResult(
            action=decision,
            denied=decision == "deny",
            reason=d.get("reason", ""),
            context=d.get("additionalContext", ""),
            rewritten_input=d.get("updatedInput"),
            raw=d,
        )


class CortexLibrary:
    """CCC as a Python library — guards, routing, prompts without hooks.

    Usage::

        cortex = CortexLibrary(workspace="/path/to/project")

        # Run 55+ guards on an action
        result = cortex.check_safety("Bash", {"command": "rm -rf /"})
        assert result.denied

        # Classify task complexity
        complexity = cortex.classify_complexity("refactor the auth module")
        # → "complicated"

        # Assemble prompt with anti-drift
        prompt = cortex.assemble_prompt("fix the login bug")
    """

    def __init__(
        self,
        workspace: str = "",
        cache_dir: str = "",
        session_id: str = "",
    ):
        self.workspace = workspace or os.environ.get("CLAUDE_PROJECT_DIR", "")
        self.cache_dir = cache_dir or self._default_cache_dir()
        self.session_id = session_id or f"aegis-{os.getpid()}"
        self._pipeline: Optional[GuardPipeline] = None

    def _default_cache_dir(self) -> str:
        """Create a valid cache directory for stateful guards."""
        if self.workspace:
            d = os.path.join(self.workspace, ".cc_cortex_cache")
        else:
            d = os.path.join(tempfile.gettempdir(), "cc_cortex_cache")
        os.makedirs(d, exist_ok=True)
        return d

    @property
    def pipeline(self) -> GuardPipeline:
        """Lazy-init guard pipeline."""
        if self._pipeline is None:
            self._pipeline = create_default_pipeline()
        return self._pipeline

    def _make_context(
        self,
        tool_name: str,
        tool_input: dict,
        hook_event: str = "PreToolUse",
        tool_result: str = "",
    ) -> GuardContext:
        """Build GuardContext with valid fields for library use."""
        return GuardContext(
            tool_name=tool_name,
            tool_input=tool_input,
            session_id=self.session_id,
            cache_dir=self.cache_dir,
            hook_event=hook_event,
            tool_result=tool_result,
            workspace=self.workspace,
        )

    def check_safety(
        self,
        tool_name: str,
        tool_input: dict,
    ) -> LibraryResult:
        """Run 55+ guard pipeline on a tool action.

        Args:
            tool_name: Tool being called (e.g. "Bash", "Write", "Edit")
            tool_input: Tool parameters (e.g. {"command": "rm -rf /"})

        Returns:
            LibraryResult with denied=True/False, reason, and context.
        """
        ctx = self._make_context(tool_name, tool_input)
        raw = self.pipeline.run_pre_tool(ctx)
        return LibraryResult.from_hook_dict(raw)

    def check_post_tool(
        self,
        tool_name: str,
        tool_input: dict,
        tool_result: str,
    ) -> str:
        """Run post-tool guards (returns additionalContext string)."""
        ctx = self._make_context(
            tool_name, tool_input,
            hook_event="PostToolUse",
            tool_result=tool_result,
        )
        raw = self.pipeline.run_post_tool(ctx)
        return raw.get("additionalContext", "")

    def classify_complexity(self, task: str) -> str:
        """Classify task complexity using C0 router.

        Returns: "simple" | "complicated" | "complex" | "chaotic"
        """
        try:
            from cc_cortex.c0_router import C0Router
            router = C0Router()
            result = router.classify(task, cache_dir=self.cache_dir)
            return result.complexity if result else "complicated"
        except Exception:
            return "complicated"  # Safe default

    def assemble_prompt(
        self,
        task: str,
        complexity: str = "standard",
    ) -> str:
        """Assemble dynamic prompt with anti-drift.

        Returns: Assembled prompt string within 1500-3000t budget.
        """
        try:
            from cc_cortex.prompt_engine import PromptEngine
            engine = PromptEngine(workspace=self.workspace)
            return engine.assemble(task_prompt=task, complexity=complexity)
        except Exception:
            return ""

    def get_weights(self) -> dict:
        """Get ZIQ retrieval weights (for adaptive RAG)."""
        try:
            from cc_cortex.ziq_retrieval import ZIQRetrieval
            ziq = ZIQRetrieval(self.cache_dir)
            return ziq.get_weights()
        except Exception:
            return {}
