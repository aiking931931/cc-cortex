"""concinno.hallucination_guard — Detect unsourced assertions in written content.

@module hallucination_guard
@responsibility CBUA Law #6 (誠實定律) + B4 幻覺偵測: scan Write/Edit content
    for specific claims (numbers, percentages, dates, URLs, citations) that
    lack supporting Read/Search evidence in the current session.
@dependencies concinno.guards.base, concinno.core.state_store
@exports HallucinationGuard

PostToolUse guard (inject warning, never deny). Tracks which sources have
been read in the session, then flags written content containing specific
claims without corresponding evidence.
"""

from __future__ import annotations

import re
from typing import Optional

from concinno.constants import WRITE_TOOLS_EXT
from concinno.core.state_store import StateStore
from concinno.guards.base import BaseGuard, GuardCategory, GuardContext, GuardResult

_NS = "hallucination_guard"

# Bash output signatures that indicate a real fetch / inspection happened
# and should count as evidence (alongside Read / WebFetch / Grep). Without
# this, sessions that gather data via curl / gh api / git show were stuck
# at evidence_count = 0 and triggered the warning on every write even
# though the operator was clearly verifying every claim.
_BASH_EVIDENCE_PATTERNS = re.compile(
    r"HTTP/[12](?:\.[01])?\s+200"
    r"|^200\s+OK"
    r'|"status_code"\s*:\s*200'
    r'|"status"\s*:\s*200'
    r"|^\s*\{[\s\S]*\}\s*$",  # raw JSON response body
    re.IGNORECASE | re.MULTILINE,
)

# Patterns indicating specific claims that should have sources
_SPECIFIC_CLAIM_PATTERNS = re.compile(
    r"(?:"
    # Percentages and numbers in non-code context
    r"(?:約|roughly|approximately|around)\s*\d+[%％]|"
    r"(?:提升|improve|increase|decrease|降低)\s*(?:了\s*)?\d+[%％]|"
    # Citation-style claims
    r"(?:根據|according to|based on|研究顯示|studies show)\s+\S+|"
    # URLs that might be fabricated
    r"https?://(?!localhost|127\.0\.0\.1|github\.com/anthropics)\S{20,}|"
    # Date claims (specific future/past dates)
    r"(?:截止|deadline|due)\s*(?:日期|date)?[：:]\s*\d{4}[-/]\d{1,2}[-/]\d{1,2}"
    r")",
    re.IGNORECASE,
)

# File extensions that are pure code (skip scanning)
_CODE_EXTENSIONS = frozenset({
    ".py", ".js", ".ts", ".tsx", ".jsx", ".rs", ".go", ".java",
    ".c", ".cpp", ".h", ".hpp", ".cs", ".rb", ".php", ".swift",
    ".kt", ".scala", ".sh", ".bash", ".zsh", ".ps1",
    ".json", ".yaml", ".yml", ".toml", ".xml", ".csv",
    ".sql", ".graphql", ".proto",
})

# Config file patterns (skip scanning)
_CONFIG_PATTERNS = re.compile(
    r"(?:config|settings|\.env|package\.json|tsconfig|pyproject|Cargo\.toml)",
    re.IGNORECASE,
)


def _is_code_file(path: str) -> bool:
    """Check if the file is pure code (skip hallucination scanning)."""
    if not path:
        return False
    lower = path.lower().replace("\\", "/")
    # Check extension
    for ext in _CODE_EXTENSIONS:
        if lower.endswith(ext):
            return True
    # Check config patterns
    if _CONFIG_PATTERNS.search(lower):
        return True
    return False


def _extract_written_text(tool_input: dict) -> str:
    """Extract the text content being written."""
    return (
        tool_input.get("content", "")
        or tool_input.get("new_string", "")
        or tool_input.get("new_source", "")
    )


class HallucinationGuard(BaseGuard):
    """Detect unsourced specific claims in written content.

    CBUA Law #6 hardening: "禁止幻覺、禁止編造".
    PostToolUse: after Write/Edit, scan content for specific claims
    (percentages, citations, URLs, dates) and check if supporting
    evidence exists in the session's read history.

    Inject warning (never deny) — the AI should verify the claim.
    """

    name = "hallucination_guard"
    category = GuardCategory.QUALITY

    def check(self, ctx: GuardContext) -> Optional[GuardResult]:
        """Not used for PreToolUse. See on_post_tool."""
        return None

    def on_post_tool(self, ctx: GuardContext) -> Optional[GuardResult]:
        """Scan written content for unsourced claims."""
        if not ctx.cache_dir:
            return None

        # Track reads as evidence sources
        if ctx.tool_name in ("Read", "WebFetch", "WebSearch", "Grep"):
            self._track_evidence(ctx)
            return None

        # Bash with a successful HTTP fetch / JSON response counts as
        # evidence too — sessions that verify claims via curl / gh api /
        # git show were previously stuck at evidence_count=0.
        if ctx.tool_name == "Bash":
            self._maybe_track_bash_evidence(ctx)
            return None

        # Only scan write tools
        if ctx.tool_name not in WRITE_TOOLS_EXT:
            return None

        # Skip code files
        path = ctx.tool_input.get("file_path", "")
        if _is_code_file(path):
            return None

        content = _extract_written_text(ctx.tool_input)
        if not content or len(content) < 30:
            return None

        # Find specific claims
        claims = _SPECIFIC_CLAIM_PATTERNS.findall(content)
        if not claims:
            return None

        # Check evidence history (no blanket exemption — always check)
        store = StateStore(ctx.cache_dir)
        state = store.read(_NS, ctx.session_id, default={})
        evidence_count = state.get("evidence_count", 0)

        # Flag unsourced claims
        sample_claims = [c[:60] for c in claims[:3]]
        claim_list = "\n".join(f"  - {c}" for c in sample_claims)

        # Record for B4 metacognition tracking
        flags = state.get("hallucination_flags", 0)
        state["hallucination_flags"] = flags + 1
        state["last_flagged_claims"] = sample_claims
        store.write(_NS, ctx.session_id, state)

        return GuardResult.allow(
            context=(
                f"⚠ CBUA Law #6 幻覺偵測：寫入內容含 {len(claims)} 個"
                "可能未驗證的具體斷言：\n"
                f"{claim_list}\n\n"
                f"本 session 只有 {evidence_count} 次讀取作為證據。"
                "請確認這些斷言有來源依據，或標註為推測。"
            ),
        )

    def _track_evidence(self, ctx: GuardContext) -> None:
        """Track read/search actions as evidence sources."""
        store = StateStore(ctx.cache_dir)
        state = store.read(_NS, ctx.session_id, default={})
        count = state.get("evidence_count", 0)
        state["evidence_count"] = count + 1
        store.write(_NS, ctx.session_id, state)

    def _maybe_track_bash_evidence(self, ctx: GuardContext) -> None:
        """Credit Bash output that looks like a real fetch as evidence.

        Matches HTTP 200 status lines, JSON success bodies, and raw
        JSON responses — all signs the operator pulled real data the
        write step can legitimately cite.
        """
        result = getattr(ctx, "tool_result", "") or ""
        if not isinstance(result, str) or not result:
            return
        if _BASH_EVIDENCE_PATTERNS.search(result):
            self._track_evidence(ctx)
