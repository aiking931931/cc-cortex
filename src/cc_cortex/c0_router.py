"""cc_cortex.c0_router — CBUA C0 複雜度分類器。

Always On。從 task prompt 分類複雜度，驅動全鏈路：
- Simple → prompt_engine minimal (800t), guards 寬鬆
- Complicated → prompt_engine standard (1500t), guards 正常
- Complex → prompt_engine full (3000t), guards 嚴格
- Chaotic → prompt_engine full (3000t), guards 嚴格

Wraps cognitive.router (classification engine) and adds:
1. Prompt token budget mapping
2. Guard level mapping
3. Tool-history dynamic escalation
4. StateStore persistence for cross-module queries

@module c0_router
@responsibility Bridge between raw classification and operational config.
    Other modules read ``c0_route`` from StateStore instead of re-classifying.
@dependencies cc_cortex.cognitive.router, cc_cortex.core.state_store
@exports C0Router
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

from cc_cortex.cognitive.router import (
    classify_complexity,
)

# ── Constants ────────────────────────────────────────────

_PROMPT_BUDGET: dict[str, int] = {
    "simple": 800,
    "complicated": 1500,
    "complex": 3000,
    "chaotic": 3000,
}

_GUARD_LEVEL: dict[str, str] = {
    "simple": "relaxed",
    "complicated": "normal",
    "complex": "strict",
    "chaotic": "strict",
}

# Tool-history thresholds for dynamic escalation
_TOOL_ESCALATION_THRESHOLD = 30  # >30 calls → at least complicated
_TOOL_COMPLEX_THRESHOLD = 60     # >60 calls → complex

# File-count thresholds
_FILE_COMPLICATED_THRESHOLD = 5   # >5 files → at least complicated
_FILE_COMPLEX_THRESHOLD = 15      # >15 files → complex

# Context-token thresholds (Aegis ZIQ PTME "memory" signal port)
_CTX_COMPLICATED_THRESHOLD = 100_000  # >100k tokens → at least complicated
_CTX_COMPLEX_THRESHOLD = 250_000      # >250k tokens → complex

# Pattern for architecture/migration keywords (strong complex signal)
_HEAVY_KEYWORDS = re.compile(
    r"(?:refactor|architecture|migration|redesign|重構|架構|遷移|大改)",
    re.IGNORECASE,
)

# Pattern for tasks that mandate red team before acting
_REDTEAM_KEYWORDS = re.compile(
    r"(?:patent|專利|論文|paper|release|發布|開源|schema|"
    r"不可逆|irreversible|架構級|重大決策|database|drop|delete.*prod)",
    re.IGNORECASE,
)

# Pattern for tasks that benefit from A2A agent collaboration
_A2A_KEYWORDS = re.compile(
    r"(?:multi.?agent|delegate|協作|分工|red.?team|紅隊|壓測|"
    r"parallel|並行|cross.?project|跨專案|外部.*agent|deploy|部署)",
    re.IGNORECASE,
)

# StateStore namespace
NAMESPACE = "c0_route"


# ── Data ─────────────────────────────────────────────────

@dataclass
class C0Result:
    """Classification result with operational config."""

    complexity: str          # simple | complicated | complex | chaotic
    prompt_budget: int       # token budget for prompt_engine
    guard_level: str         # relaxed | normal | strict
    signals: dict            # transparency: what drove the classification
    escalation_reason: str   # "" if no escalation happened
    redteam_required: bool = False  # True → mandatory red team before Act
    a2a_suggested: bool = False     # True → task benefits from A2A agent collab

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> C0Result:
        return cls(
            complexity=d.get("complexity", "simple"),
            prompt_budget=d.get("prompt_budget", 800),
            guard_level=d.get("guard_level", "relaxed"),
            signals=d.get("signals", {}),
            escalation_reason=d.get("escalation_reason", ""),
            redteam_required=bool(d.get("redteam_required", False)),
            a2a_suggested=bool(d.get("a2a_suggested", False)),
        )


# ── Router ───────────────────────────────────────────────

class C0Router:
    """分類任務複雜度，驅動 CBUA 全鏈路。

    Pure heuristic — no LLM calls, no network, ~0 latency.
    """

    def classify(
        self,
        task_prompt: str,
        tool_history: list[str] | None = None,
        file_paths: list[str] | None = None,
        context_tokens: int | None = None,
    ) -> C0Result:
        """分類複雜度並返回操作配置。

        Signals (pure heuristic, no LLM):
        1. task_prompt 內容 → cognitive.router.classify_complexity
        2. 關鍵詞偵測 → refactor/architecture/migration → complex
        3. file_paths 數量 → >5 → complicated+, >15 → complex
        4. tool_history 長度 → >30 → complicated+, >60 → complex
        """
        # Base classification from cognitive.router
        domain, signals = classify_complexity(task_prompt)
        base_complexity = domain.value

        # Dynamic escalation
        escalation_reason = ""
        final_complexity = base_complexity

        # Signal: heavy keywords override
        if _HEAVY_KEYWORDS.search(task_prompt):
            signals["heavy_keywords"] = True
            if final_complexity == "simple":
                final_complexity = "complicated"
                escalation_reason = "heavy_keywords"

        # Signal: file count
        file_count = len(file_paths) if file_paths else 0
        signals["file_count"] = file_count
        if file_count > _FILE_COMPLEX_THRESHOLD:
            if _complexity_rank(final_complexity) < _complexity_rank("complex"):
                final_complexity = "complex"
                escalation_reason = f"file_count={file_count}"
        elif file_count > _FILE_COMPLICATED_THRESHOLD:
            if _complexity_rank(final_complexity) < _complexity_rank("complicated"):
                final_complexity = "complicated"
                escalation_reason = f"file_count={file_count}"

        # Signal: tool history length
        tool_count = len(tool_history) if tool_history else 0
        signals["tool_count"] = tool_count
        if tool_count > _TOOL_COMPLEX_THRESHOLD:
            if _complexity_rank(final_complexity) < _complexity_rank("complex"):
                final_complexity = "complex"
                escalation_reason = f"tool_count={tool_count}"
        elif tool_count > _TOOL_ESCALATION_THRESHOLD:
            if _complexity_rank(final_complexity) < _complexity_rank("complicated"):
                final_complexity = "complicated"
                escalation_reason = f"tool_count={tool_count}"

        # Signal: context token count (Aegis ZIQ PTME "memory" signal)
        if context_tokens is not None:
            signals["context_tokens"] = context_tokens
            if context_tokens > _CTX_COMPLEX_THRESHOLD:
                if _complexity_rank(final_complexity) < _complexity_rank("complex"):
                    final_complexity = "complex"
                    escalation_reason = f"context_tokens={context_tokens}"
            elif context_tokens > _CTX_COMPLICATED_THRESHOLD:
                if _complexity_rank(final_complexity) < _complexity_rank("complicated"):
                    final_complexity = "complicated"
                    escalation_reason = f"context_tokens={context_tokens}"

        # Determine if red team is mandatory
        redteam = (
            _complexity_rank(final_complexity) >= _complexity_rank("complex")
            and bool(_REDTEAM_KEYWORDS.search(task_prompt))
        )
        signals["redteam_required"] = redteam

        # Determine if A2A collaboration is beneficial
        a2a = (
            _complexity_rank(final_complexity) >= _complexity_rank("complicated")
            and bool(_A2A_KEYWORDS.search(task_prompt))
        )
        signals["a2a_suggested"] = a2a

        return C0Result(
            complexity=final_complexity,
            prompt_budget=self.get_prompt_budget(final_complexity),
            guard_level=self.get_guard_level(final_complexity),
            signals=signals,
            escalation_reason=escalation_reason,
            redteam_required=redteam,
            a2a_suggested=a2a,
        )

    @staticmethod
    def get_prompt_budget(complexity: str) -> int:
        """根據複雜度返回 prompt token 預算。"""
        return _PROMPT_BUDGET.get(complexity, 1500)

    @staticmethod
    def get_guard_level(complexity: str) -> str:
        """根據複雜度返回 guard 強度。"""
        return _GUARD_LEVEL.get(complexity, "normal")

    def persist(
        self,
        result: C0Result,
        cache_dir: str,
        session_id: str,
    ) -> None:
        """Save classification to StateStore for cross-module queries."""
        from cc_cortex.core.state_store import StateStore

        store = StateStore(cache_dir)
        store.write(NAMESPACE, session_id, result.to_dict())

    @staticmethod
    def set_stage(
        cache_dir: str,
        session_id: str,
        stage: str,
        sub: str = "",
    ) -> None:
        """Record current CBUA pipeline stage (C/B/U/A + sub-code).

        Other modules can query this to know what stage the agent is in.
        Enables "can skip, can go back" tracking.

        Args:
            stage: One of "C", "B", "U", "A".
            sub: Sub-code like "0", "1", "3.D", etc.
        """
        from cc_cortex.core.state_store import StateStore

        store = StateStore(cache_dir)
        store.write("cbua_stage", session_id, {
            "stage": stage,
            "sub": sub,
            "code": f"{stage}{sub}",
        })

    @staticmethod
    def get_stage(
        cache_dir: str,
        session_id: str,
    ) -> dict | None:
        """Read current CBUA pipeline stage."""
        from cc_cortex.core.state_store import StateStore

        store = StateStore(cache_dir)
        return store.read("cbua_stage", session_id, default=None)

    def load(
        self,
        cache_dir: str,
        session_id: str,
    ) -> C0Result | None:
        """Load last classification from StateStore. Returns None if absent."""
        from cc_cortex.core.state_store import StateStore

        store = StateStore(cache_dir)
        data = store.read(NAMESPACE, session_id, default=None)
        if not data:
            return None
        return C0Result.from_dict(data)


# ── Helpers ──────────────────────────────────────────────

_RANK = {"simple": 0, "complicated": 1, "complex": 2, "chaotic": 3}


def _complexity_rank(complexity: str) -> int:
    """Numeric rank for complexity comparison (higher = more complex)."""
    return _RANK.get(complexity, 1)
