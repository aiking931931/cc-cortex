"""cc_cortex.prompt_engine — Dynamic prompt assembly with token budgets.

@module prompt_engine
@responsibility Assemble optimal system prompts from static + dynamic layers.
    Token-aware: respects sweet spot (1500-3000t total inject), compresses
    when over budget. Supports anti-drift re-injection every N tool calls.
@dependencies cc_cortex.cognitive_inject, cc_cortex.token_zone
@exports PromptEngine, assemble_prompt, should_reinject

Architecture:
  Static cache (identity + iron laws + tool desc) = ~500t, never changes.
  Dynamic slots (task context + L1 rules + memory + handoff) = ~1000-2500t.
  Inject budget = ≤350t per guard/SOP (cognitive anchor A1).

  Sweet spot: 1500-3000t total. Beyond 3000t → compliance drops sharply
  (Liu et al. 2023 "Lost in the Middle": U-shape attention curve).

Anti-drift:
  Long conversations cause identity drift. Re-inject core identity every
  N tool calls (default 15). This is NOT the same as CLAUDE.md (which
  survives compact). This targets the inject layer that gets diluted.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Optional

from cc_cortex.cognitive_inject import (
    build_delivery_standards,
    build_rag_context,
    build_thinking_directives,
)
from cc_cortex.field_read import build_field_context

# ── Constants ──────────────────────────────────────────────

# Cognitive anchor A1: inject budget per layer
MAX_INJECT_TOKENS = 350
# Sweet spot for total system prompt injection
SWEET_SPOT_MIN = 1500
SWEET_SPOT_MAX = 3000
# Anti-drift: re-inject every N tool calls
DRIFT_INTERVAL = 15
# Approximate tokens per character (English + CJK mix)
CHARS_PER_TOKEN = 3.5

# ── State file ─────────────────────────────────────────────

_STATE_FILE = os.path.join(
    os.path.expanduser("~"), ".claude", ".prompt_engine_state.json",
)

# Default location for FTRL learning corrections (promotion-eligible entries).
_DEFAULT_LEARNINGS_PATH = os.path.join(
    os.path.expanduser("~"), ".claude", "cognitive", "learnings.json",
)


def _build_ftrl_context(
    learnings_path: str = "",
    max_items: int = 3,
    ftrl_threshold: float = 3.0,
) -> Optional[str]:
    """Build FTRL-weighted learning reminder from ``learnings.json``.

    Ranks non-promoted corrections by FTRL weight (count × recency decay)
    and returns the top-N as a concise reminder fragment suitable for
    injection as a dynamic prompt slot.

    Args:
        learnings_path: Override path to learnings file. Empty string uses
            ``~/.claude/cognitive/learnings.json``.
        max_items: Maximum number of learnings to include.
        ftrl_threshold: Minimum FTRL weight to include.

    Returns:
        Formatted reminder string, or ``None`` when no qualifying entry.
    """
    path = learnings_path or _DEFAULT_LEARNINGS_PATH
    if not os.path.isfile(path):
        return None
    try:
        from cc_cortex.knowledge import ftrl_weight

        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError, ImportError):
        return None

    items = data.get("learnings", []) if isinstance(data, dict) else []
    if not isinstance(items, list) or not items:
        return None

    scored: list[tuple[float, dict]] = []
    for item in items:
        if not isinstance(item, dict) or item.get("promoted"):
            continue
        try:
            w = ftrl_weight(
                int(item.get("count", 0)),
                str(item.get("last_seen", "")),
            )
        except (TypeError, ValueError):
            continue
        if w >= ftrl_threshold:
            scored.append((w, item))

    if not scored:
        return None

    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:max_items]

    lines = ["⚡ FTRL 學習提醒（近期高頻糾正）:"]
    for _w, item in top:
        text = str(item.get("correction_text", ""))[:80]
        count = item.get("count", 0)
        lines.append(f"  - [{count}x] {text}")
    return "\n".join(lines)


def _estimate_tokens(text: str) -> int:
    """Estimate token count from text length. CJK-aware."""
    if not text:
        return 0
    # CJK characters are ~1.5 tokens each, ASCII ~0.25
    cjk = sum(1 for c in text if ord(c) > 0x2E80)
    ascii_chars = len(text) - cjk
    return int(cjk * 1.5 + ascii_chars / 4)


# ── Static Cache ───────────────────────────────────────────

@dataclass
class StaticCache:
    """Immutable prompt components. Loaded once, cached forever."""

    identity: str = ""
    iron_laws: str = ""
    tool_desc: str = ""
    _total_tokens: int = 0

    def load(self, workspace: str) -> None:
        """Load static components from workspace.

        Looks up two optional files via explicit overrides + sensible
        defaults, not hard-coded author paths. Nothing is required —
        strangers can instantiate `PromptEngine(workspace="")` and the
        static cache stays empty, so `dynamic_budget` falls back to the
        full sweet-spot window (this is fine for library consumers).

        Override precedence:
          1. ``CC_CORTEX_IDENTITY_PATH`` env var (explicit file path)
          2. ``CC_CORTEX_L0_RULES_PATH`` env var (explicit file path)
          3. ``<workspace>/.cc_cortex/identity.md``
          4. ``<workspace>/.cc_cortex/l0.md``
          5. ``<workspace>/CLAUDE.md`` (as iron-laws source)

        Red team #3 — the previous author-specific defaults
        (``projects/cc-cortex/src/cc_cortex/cognitive_anchor.py`` and
        ``.claude/rules/00-L0.md``) violated CCC hard rule #1 and
        silently left the cache empty for any non-author workspace.
        """
        identity_path = os.environ.get("CC_CORTEX_IDENTITY_PATH", "")
        if not identity_path and workspace:
            cand = os.path.join(workspace, ".cc_cortex", "identity.md")
            if os.path.isfile(cand):
                identity_path = cand
        if identity_path and os.path.isfile(identity_path):
            try:
                with open(identity_path, encoding="utf-8") as f:
                    self.identity = f.read(4096).strip()
            except OSError:
                pass

        l0_path = os.environ.get("CC_CORTEX_L0_RULES_PATH", "")
        if not l0_path and workspace:
            for cand in (
                os.path.join(workspace, ".cc_cortex", "l0.md"),
                os.path.join(workspace, "CLAUDE.md"),
            ):
                if os.path.isfile(cand):
                    l0_path = cand
                    break
        if l0_path and os.path.isfile(l0_path):
            self.iron_laws = _extract_iron_laws(l0_path)

        self._total_tokens = (
            _estimate_tokens(self.identity)
            + _estimate_tokens(self.iron_laws)
            + _estimate_tokens(self.tool_desc)
        )

    @property
    def total_tokens(self) -> int:
        return self._total_tokens

    def render(self) -> str:
        """Render static cache as single string."""
        parts = [p for p in [self.identity, self.iron_laws] if p]
        return "\n\n".join(parts)


def _extract_iron_laws(path: str) -> str:
    """Extract iron laws (⛔ lines) from L0 rules."""
    try:
        with open(path, encoding="utf-8") as f:
            content = f.read(3000)
        laws = []
        for line in content.splitlines():
            if line.strip().startswith("⛔") or (
                line.strip().startswith("1.") and "⛔" in line
            ):
                laws.append(line.strip())
        return "\n".join(laws[:5]) if laws else ""
    except OSError:
        return ""


# ── Dynamic Slots ──────────────────────────────────────────

@dataclass
class DynamicSlots:
    """Per-request dynamic prompt components."""

    task_context: str = ""
    l1_rules: str = ""
    memory_hits: str = ""
    ftrl_learnings: str = ""
    handoff_summary: str = ""
    delivery: str = ""
    thinking: str = ""

    def total_tokens(self) -> int:
        return sum(
            _estimate_tokens(getattr(self, f))
            for f in [
                "task_context", "l1_rules", "memory_hits",
                "ftrl_learnings",
                "handoff_summary", "delivery", "thinking",
            ]
        )

    def render(self, budget: int) -> str:
        """Render dynamic slots within token budget.

        Priority order (highest first):
        1. thinking directives (core cognition)
        2. ftrl learnings (recent high-weight corrections — prevent regression)
        3. memory hits (prevent regression)
        4. delivery standards (quality)
        5. task context
        6. L1 rules
        7. handoff summary (lowest priority, largest)
        """
        priority = [
            ("thinking", self.thinking),
            ("ftrl_learnings", self.ftrl_learnings),
            ("memory_hits", self.memory_hits),
            ("delivery", self.delivery),
            ("task_context", self.task_context),
            ("l1_rules", self.l1_rules),
            ("handoff_summary", self.handoff_summary),
        ]

        parts: list[str] = []
        used = 0
        for _name, text in priority:
            if not text:
                continue
            t = _estimate_tokens(text)
            if used + t <= budget:
                parts.append(text)
                used += t
            else:
                # Truncate to fit remaining budget
                remaining = budget - used
                if remaining > 50:  # Worth including partial
                    chars = int(remaining * CHARS_PER_TOKEN)
                    parts.append(text[:chars] + "\n[...truncated]")
                    used = budget
                break

        return "\n\n".join(parts)


# ── Anti-Drift ─────────────────────────────────────────────

@dataclass
class DriftTracker:
    """Track tool calls since last identity re-injection."""

    calls_since_reinject: int = 0
    last_reinject_time: float = 0.0

    def tick(self) -> None:
        """Record a tool call."""
        self.calls_since_reinject += 1

    def should_reinject(self, interval: int = DRIFT_INTERVAL) -> bool:
        """Check if identity should be re-injected."""
        return self.calls_since_reinject >= interval

    def reset(self) -> None:
        """Reset after re-injection."""
        self.calls_since_reinject = 0
        self.last_reinject_time = time.time()


# ── Main Engine ────────────────────────────────────────────

@dataclass
class PromptEngine:
    """Dynamic prompt assembly engine.

    Assembles optimal system prompts from static + dynamic layers
    within the sweet spot of 1500-3000 tokens.

    Usage::

        engine = PromptEngine(workspace="/path/to/project")
        engine.load_static()

        # Per-request assembly
        prompt = engine.assemble(
            task_prompt="Fix the auth bug",
            complexity="standard",
        )
    """

    workspace: str = ""
    static: StaticCache = field(default_factory=StaticCache)
    drift: DriftTracker = field(default_factory=DriftTracker)
    _loaded: bool = False

    def load_static(self) -> None:
        """Load static cache (call once per session)."""
        if self.workspace:
            self.static.load(self.workspace)
        self._loaded = True

    def build_ftrl_context(
        self,
        learnings_path: str = "",
        max_items: int = 3,
        ftrl_threshold: float = 3.0,
    ) -> Optional[str]:
        """Expose FTRL-weighted learning reminder for the dynamic slot.

        Thin wrapper around :func:`_build_ftrl_context` so external callers
        (hooks, CLI) can populate the ``ftrl_learnings`` slot — or fetch
        the raw fragment — without reimplementing the weight pipeline.
        """
        return _build_ftrl_context(
            learnings_path=learnings_path,
            max_items=max_items,
            ftrl_threshold=ftrl_threshold,
        )

    def assemble(
        self,
        task_prompt: str = "",
        complexity: str = "standard",
        include_drift: bool = False,
        learnings_path: str = "",
    ) -> str:
        """Assemble optimal prompt within sweet spot budget.

        Args:
            task_prompt: Current task description.
            complexity: "minimal" | "standard" | "full".
            include_drift: Force include identity (anti-drift).
            learnings_path: Override path to FTRL learnings file. Empty
                string uses the default location; set to a non-existent
                path to skip the FTRL slot.

        Returns:
            Assembled prompt string within 1500-3000t budget.
        """
        if not self._loaded:
            self.load_static()

        # Budget allocation
        static_tokens = self.static.total_tokens
        dynamic_budget = SWEET_SPOT_MAX - static_tokens

        # Build dynamic slots
        dynamic = DynamicSlots()
        dynamic.thinking = build_thinking_directives(complexity)
        if task_prompt and self.workspace:
            dynamic.memory_hits = build_rag_context(
                task_prompt, self.workspace,
            )
            dynamic.delivery = build_delivery_standards(task_prompt)
            dynamic.handoff_summary = build_field_context(
                self.workspace, task_prompt,
            )

        # Redteam gate: if C0 classified this as needing red team, inject reminder
        if task_prompt and self.workspace:
            try:
                from cc_cortex.c0_router import C0Router
                from cc_cortex.cbua_ux import CbuaCode, cbua_format
                from cc_cortex.hooks.io_utils import cache_path

                c_dir = cache_path()
                if c_dir:
                    route = C0Router().load(c_dir, "")
                    if route and route.redteam_required:
                        dynamic.task_context += "\n" + cbua_format(
                            CbuaCode.U1_RED,
                            "⛔ 本任務需紅隊壓測後才能執行。先 U0 藍隊自爆，再派 Opus 紅隊。",
                        )
            except Exception:
                pass

        # FTRL-weighted learning slot — participates in sweet-spot budget
        ftrl = self.build_ftrl_context(learnings_path=learnings_path)
        if ftrl:
            dynamic.ftrl_learnings = ftrl

        # Anti-drift: include identity in dynamic if due
        if include_drift or self.drift.should_reinject():
            dynamic.task_context = (
                self.static.identity + "\n" + dynamic.task_context
            )
            self.drift.reset()

        # Assemble
        parts: list[str] = []

        # Static (always first — primacy bias)
        static_text = self.static.render()
        if static_text:
            parts.append(static_text)

        # Dynamic (within remaining budget)
        dynamic_text = dynamic.render(dynamic_budget)
        if dynamic_text:
            parts.append(dynamic_text)

        return "\n\n".join(parts)

    def on_tool_call(self) -> Optional[str]:
        """Called on each tool call. Returns re-inject text if due.

        Hook integration: call this from PostToolUse hook.
        If returns non-empty string, inject as additionalContext.
        """
        self.drift.tick()
        if self.drift.should_reinject():
            self.drift.reset()
            # Re-inject core identity + iron laws
            return self.static.render()
        return None


# ── Module-level convenience ───────────────────────────────

_engine: Optional[PromptEngine] = None


def get_engine(workspace: str = "") -> PromptEngine:
    """Get or create the singleton PromptEngine.

    R1 fix: recreate if workspace changed (prevents stale singleton).
    """
    global _engine
    if _engine is None or (workspace and _engine.workspace != workspace):
        _engine = PromptEngine(workspace=workspace)
        _engine.load_static()
    return _engine


def assemble_prompt(
    task_prompt: str = "",
    complexity: str = "standard",
    workspace: str = "",
) -> str:
    """Convenience: assemble a prompt using the singleton engine."""
    engine = get_engine(workspace)
    return engine.assemble(task_prompt=task_prompt, complexity=complexity)


def should_reinject(workspace: str = "") -> Optional[str]:
    """Convenience: check if anti-drift re-injection is due."""
    engine = get_engine(workspace)
    return engine.on_tool_call()
