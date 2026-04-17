"""concinno.premise_gate — Verify external premises before execution.

@module premise_gate
@responsibility CBUA Law #3 (前提驗證): block first write-tool when external
    constraints (competition rules, specs, requirements) are detected in the
    user prompt but no evidence of reading the source material exists.
@dependencies concinno.guards.base, concinno.core.state_store
@exports PremiseGate

Lesson learned: user lost a week + significant money because AI assumed
competition requirements instead of reading the actual rules first.
"""

from __future__ import annotations

import re
from typing import Optional

from concinno.constants import WRITE_TOOLS_EXT
from concinno.core.state_store import StateStore
from concinno.guards.base import BaseGuard, GuardCategory, GuardContext, GuardResult

_NS = "premise_gate"

# External constraint keywords — trigger premise verification
# Uses co-occurrence pattern to reduce false positives:
# requires domain word + action/context word nearby
_DOMAIN_WORDS = re.compile(
    r"(?:"
    r"\bcompetition\b|\bhackathon\b|\bcontest\b|"
    r"\bRFP\b|\bRFC\b|\bPRD\b|\bleaderboard\b|"
    r"比賽|競賽|黑客松|排行榜|投標|招標"
    r")",
    re.IGNORECASE,
)
_CONTEXT_WORDS = re.compile(
    r"(?:"
    r"\brule[s]?\b|\bguideline[s]?\b|\bsubmission\b|\bdeadline\b|"
    r"\bcriteria\b|\bspec(?:ification)?[s]?\b|\bevaluation\b|"
    r"\brequirement[s]?\b|\bregulation[s]?\b|"
    r"規則|規範|截止|提交|評分|基準|需求|合規|法規"
    r")",
    re.IGNORECASE,
)

# Platform-limit ceiling detection — extends CBUA Law #3 with a "version
# ceiling" check. Fires when the assistant or user references a platform
# limitation (e.g. "hook can't call LLM", "L3", "CC doesn't support X")
# without having verified against current docs first. See:
#   feedback_ceiling_misalignment.md — CCC 1.3.0 KILLed H1 because of
#   limitations that had already been removed several versions ago.
_CEILING_WORDS = re.compile(
    r"(?:"
    # English / ASCII — \b works fine
    r"\bL[1-8]\b(?!\w)|"                              # L1-L8 limit ids
    r"\bupdatedInput\b|\bhookSpecificOutput\b|"       # specific CC features
    r"\bSubagentStart\s+(?:payload|prompt|data)\b|"
    r"\btype:\s*[\"']?prompt[\"']?\s+hook\b|"
    r"(?:hook|subagent|skill)\s+(?:can[' ]?t|cannot|doesn[' ]?t\s+support)\b|"
    # CJK — no \b because 中文 chars are all \w, so \b between two CJK
    # chars never matches. Rely on the verb itself being specific enough.
    r"(?:hook|subagent|skill)\s*(?:無法|不支援|沒辦法|只能)|"
    r"CC\s*(?:不支援|無法|沒辦法)|"
    r"\bCC\s+doesn[' ]?t\s+support\b|"
    r"平台限制|CC\s*限制|api\s*限制"
    r")",
    re.IGNORECASE,
)

# Tools whose output counts as "platform-limit verification" — must hit
# official CC docs, not just any URL.
_OFFICIAL_DOC_HOSTS = re.compile(
    r"(?:code\.claude\.com|docs\.claude\.com|docs\.anthropic\.com|"
    r"github\.com/anthropics/claude-code)",
    re.IGNORECASE,
)


def _has_external_constraints(text: str) -> str:
    """Detect external constraints via co-occurrence (domain + context word).

    Returns matched sample string, or "" if no match.
    Single domain word (competition/hackathon/etc.) is enough.
    Context words alone (rules/requirements) are too common — need domain word nearby.
    """
    domain_match = _DOMAIN_WORDS.search(text)
    if domain_match:
        return domain_match.group()[:50]
    # Fallback: two context words co-occurring = likely external constraint
    context_matches = _CONTEXT_WORDS.findall(text)
    if len(context_matches) >= 2:
        return " + ".join(context_matches[:2])[:50]
    return ""

# Evidence that source material was read (tool names + content patterns)
_VERIFICATION_TOOLS = frozenset({"Read", "WebFetch", "WebSearch"})

_VERIFICATION_EVIDENCE = re.compile(
    r"(?:"
    r"\bread.*(?:rule|spec|requirement|guideline|規則|需求|規範)\b|"
    r"\bverif(?:y|ied|ication)\b|\bconfirm(?:ed)?\b|"
    r"\baccording to\b|\bbased on.*(?:official|source)\b|"
    r"根據.*(?:官方|原始|規則)|已確認|已驗證|已閱讀"
    r")",
    re.IGNORECASE,
)


class PremiseGate(BaseGuard):
    """Block execution when external constraints or platform-ceiling claims
    exist but premises unverified.

    CBUA Law #3 hardening: "先驗證前提再行動".

    Two deny modes:
      1. Classic "external constraints" (competition/hackathon/spec without
         having read the source rules) — triggers if _DOMAIN_WORDS +
         _CONTEXT_WORDS co-occur in tool content.
      2. "Ceiling misalignment" (assistant references a CC platform limit
         without having WebFetched official docs first) — triggers if
         _CEILING_WORDS hit and no recent WebFetch to
         _OFFICIAL_DOC_HOSTS exists. See feedback_ceiling_misalignment.md.

    Both modes require Complexity >= Complicated. Once verification evidence
    is detected, sets the corresponding flag and that mode never triggers
    again in the session.
    """

    name = "premise_gate"
    category = GuardCategory.QUALITY
    step_back_reason = "external constraints detected but premises not verified"

    def check(self, ctx: GuardContext) -> Optional[GuardResult]:
        """Deny first write when external constraints or ceiling claim unverified."""
        # Only trigger on write tools
        if ctx.tool_name not in WRITE_TOOLS_EXT and ctx.tool_name != "Bash":
            return None

        if not ctx.cache_dir:
            return None

        store = StateStore(ctx.cache_dir)
        state = store.read(_NS, ctx.session_id, default={})

        # Check complexity — skip Simple tasks (applies to both modes)
        c0_state = store.read("c0_route", ctx.session_id, default={})
        complexity = c0_state.get("complexity", "complicated")
        simple_task = complexity == "simple"

        # ── Mode 1: external constraints ─────────────────────────
        ext_pending = (
            state.get("has_external_constraints")
            and not state.get("premise_verified")
        )
        if ext_pending and simple_task:
            state["premise_verified"] = True
            state["skip_reason"] = "simple_task"
            store.write(_NS, ctx.session_id, state)
            ext_pending = False

        if ext_pending:
            return GuardResult.deny(
                "External constraints detected but source material not read yet.",
                context=(
                    "⚠ CBUA Law #3 前提驗證：偵測到外部約束"
                    f"（{state.get('constraint_sample', '...')}）"
                    "但尚未讀取原始規則/需求文件。\n\n"
                    "下一步：\n"
                    "  1. 用 Read/WebFetch 讀取原始規則/需求文件\n"
                    "  2. 確認關鍵約束（評分標準、截止日期、提交格式等）\n"
                    "  3. 然後再開始實作\n\n"
                    "先驗證前提再行動 — 防止方向性錯誤浪費時間和資源。"
                ),
            )

        # ── Mode 2: ceiling misalignment ─────────────────────────
        ceiling_pending = (
            state.get("has_ceiling_claim")
            and not state.get("ceiling_verified")
        )
        if ceiling_pending and simple_task:
            state["ceiling_verified"] = True
            state["ceiling_skip_reason"] = "simple_task"
            store.write(_NS, ctx.session_id, state)
            ceiling_pending = False

        if ceiling_pending:
            return GuardResult.deny(
                "Platform limitation referenced but official CC docs not checked.",
                context=(
                    "⚠ CBUA Law #3 天花板驗證：引用了 CC 平台限制"
                    f"（{state.get('ceiling_sample', '...')}）"
                    "但尚未 WebFetch 當前官方 docs 驗證。\n\n"
                    "下一步：\n"
                    "  1. WebFetch https://code.claude.com/docs/en/hooks\n"
                    "  2. WebFetch https://code.claude.com/docs/en/skills\n"
                    "  3. 確認該限制是否仍存在於當前 CC 版本\n"
                    "  4. 若已解除：更新 CCC 限制登記簿並據此設計\n\n"
                    "歷史教訓：CCC 1.3.0 誤把 updatedInput + type:\"prompt\" "
                    "當限制 KILL H1，實際是舊版錯認（feedback_ceiling_"
                    "misalignment.md）。不重複同樣錯誤。"
                ),
            )

        return None

    def on_post_tool(self, ctx: GuardContext) -> Optional[GuardResult]:
        """Track constraint + ceiling detection and verification evidence."""
        if not ctx.cache_dir:
            return None

        store = StateStore(ctx.cache_dir)
        state = store.read(_NS, ctx.session_id, default={})

        # Scan text available from this tool call once, reuse for both modes.
        # tool_input is stringified because Write/Edit content + Bash command
        # are where the assistant most often references CC limitations.
        scan_text = (ctx.tool_result or "") + " " + str(ctx.tool_input)

        # ── Mode 1: external constraints ─────────────────────────
        if not state.get("premise_verified"):
            # Phase 1: Detect external constraints via co-occurrence
            if not state.get("has_external_constraints"):
                text_to_scan = ctx.tool_result or ""
                if ctx.tool_name in _VERIFICATION_TOOLS:
                    text_to_scan += " " + str(ctx.tool_input)

                sample = _has_external_constraints(text_to_scan)
                if sample:
                    state["has_external_constraints"] = True
                    state["constraint_sample"] = sample
                    store.write(_NS, ctx.session_id, state)

            # Phase 2: Detect verification evidence (content must match constraints)
            if state.get("has_external_constraints"):
                if ctx.tool_name in _VERIFICATION_TOOLS:
                    result_text = ctx.tool_result or ""
                    constraint_in_result = (
                        _DOMAIN_WORDS.search(result_text)
                        or _CONTEXT_WORDS.search(result_text)
                    )
                    if (
                        _VERIFICATION_EVIDENCE.search(result_text)
                        and constraint_in_result
                    ):
                        state["premise_verified"] = True
                        state["verified_via"] = ctx.tool_name
                        store.write(_NS, ctx.session_id, state)

        # ── Mode 2: ceiling misalignment ─────────────────────────
        if not state.get("ceiling_verified"):
            # Phase 1: Detect platform-limit claim in scan_text
            if not state.get("has_ceiling_claim"):
                ceiling_match = _CEILING_WORDS.search(scan_text)
                if ceiling_match:
                    state["has_ceiling_claim"] = True
                    state["ceiling_sample"] = ceiling_match.group()[:80]
                    store.write(_NS, ctx.session_id, state)

            # Phase 2: Detect official-docs WebFetch as verification
            if state.get("has_ceiling_claim"):
                if ctx.tool_name in ("WebFetch", "WebSearch"):
                    # WebFetch URL lives in tool_input["url"]; fall back to
                    # stringified tool_input for WebSearch / other shapes.
                    url_source = ""
                    if isinstance(ctx.tool_input, dict):
                        url_source = str(ctx.tool_input.get("url", ""))
                    if not url_source:
                        url_source = str(ctx.tool_input)
                    # Also accept official-docs hits in the result text
                    # (e.g. WebSearch snippet linking to code.claude.com)
                    combined = url_source + " " + (ctx.tool_result or "")
                    if _OFFICIAL_DOC_HOSTS.search(combined):
                        state["ceiling_verified"] = True
                        state["ceiling_verified_via"] = ctx.tool_name
                        store.write(_NS, ctx.session_id, state)

        return None
