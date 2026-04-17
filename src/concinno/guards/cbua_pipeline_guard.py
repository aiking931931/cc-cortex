"""concinno.guards.cbua_pipeline_guard — Hardened CBUA pipeline enforcement.

Closes text-only CBUA gaps via PostToolUse state tracking + context injection.
Uses StateStore.read_modify_write for atomic cross-tick persistence.

@module cbua_pipeline_guard
@responsibility Enforce CBUA B1/C1/U1/A4/A5 steps. Uses behavioral signals
    (edit count, read count, agent dispatches) + text markers as secondary.
@dependencies concinno.guards.base, concinno.core.state_store
@exports CbuaPipelineGuard

CC ceiling notes:
- L1 (Agent spawn unmonitored): red team dispatch can only be DETECTED
  (by Agent tool name), not ENFORCED. When L1 unlocks → upgrade to enforce.
- L4 (Hook can't read conversation): guard scans tool_input/tool_result only,
  not Claude's reasoning text. When L4 unlocks → scan full response.
- L6 (PostToolUse can't DENY): guard is COGNITIVE (warn only).
  When L6 unlocks → upgrade A5 red-team-not-dispatched to QUALITY (deny).
"""

from __future__ import annotations

import re
from typing import Callable, Optional

from concinno.core.state_store import StateStore
from concinno.guards.base import (
    BaseGuard,
    GuardCategory,
    GuardContext,
    GuardResult,
)

# ── Detection patterns ──────────────────────────────────────

_B1_MARKERS = re.compile(
    r"B1[_\s]*(根因|甜蜜點|策略|root|sweet|strategy)"
    r"|根因.*甜蜜點.*策略",
    re.IGNORECASE,
)

_C1_MARKERS = re.compile(
    r"我知道.*我不知道.*我假設"
    r"|known.*unknown.*assumed",
    re.IGNORECASE,
)

_U1_MARKERS = re.compile(
    r"反例|counter.?example|edge.?case|what.?if"
    r"|失敗場景|failure.?mode",
    re.IGNORECASE,
)

# A4: only match in Agent/conversational context, not code content
_A4_ASK_PATTERNS = re.compile(
    r"要做嗎|要不要繼續|需要我.*嗎|要繼續嗎",
)

_A5_REDTEAM = re.compile(
    r"紅隊|red.?team|壓測|pressure.?test",
    re.IGNORECASE,
)

_WIREDO_TABLE = re.compile(
    r"W.*Wired.*I.*Inherited|WIREDO|W/I/R/E/D/O",
    re.IGNORECASE,
)

# Dichotomy framing detector: catches A-vs-B language that often
# hides a third option — "integrate A and B at a higher level."
# RLHF-trained models tend to pattern-match to comparative analysis
# templates and miss integrative synthesis. Example failure mode:
# experiments disprove method A, model frames the fix as "keep A's
# brand or switch to B" instead of "dual-mode framework where A is
# zero-shot mode and B is non-zero-shot mode."
#
# Patterns target explicit binary choice language. Kept narrow to
# avoid false positives on ordinary comparison tables.
_DICHOTOMY_MARKERS = re.compile(
    r"保留\s*or\s*改|保留\s*vs\s*改"
    r"|(?:路線|方案|選項)\s*[AB1２2]\s*or\s*(?:路線|方案|選項)\s*[AB1２2]"
    r"|選(?:一個|一邊|擇)"
    r"|二選一|非\s*A\s*即\s*B"
    r"|keep\s+or\s+(?:change|switch|replace)"
    r"|either\s+A\s+or\s+B",
    re.IGNORECASE,
)

# Integrative synthesis patterns — if present, silence the dichotomy
# reminder because the model is already thinking integratively.
_INTEGRATIVE_MARKERS = re.compile(
    r"A\+B|A\s*\+\s*B"
    r"|共存|融合|整合成|多模式|dual.?mode|multi.?mode|unified\s+framework"
    r"|同一(?:個)?(?:框架|framework)(?:下|內|裡)?.*(?:兩|2)(?:個)?模式"
    r"|higher\s+level",
    re.IGNORECASE,
)

# Delivery-phase signals: ONLY shell commands that actually ship.
# Pure text keywords (完成/交付/ready/done) were removed because
# documentation, comments, handoff files, even THIS docstring trigger
# false positives — exactly the "事前查證六維 亂七八糟" failure mode.
#
# Implementation: we split the Bash command on shell separators
# (&&, ||, ;, |, newline) and check each SEGMENT's leading token.
# This avoids matching delivery verbs that appear inside quoted
# arguments of meta-commands like `python -c "...git commit..."`,
# which is exactly what burned us in smoke testing.
_DELIVERY_SEPARATORS = re.compile(r"&&|\|\||;|\||\n")

_DELIVERY_VERB = re.compile(
    r"^\s*(?:git\s+commit(?!\s+--dry-run)"
    r"|git\s+push(?!\s+--dry-run)"
    r"|gh\s+pr\s+create"
    r"|gh\s+release\s+create"
    r"|twine\s+upload"
    r"|npm\s+publish"
    r"|cargo\s+publish"
    r"|python\s+-m\s+build"
    r"|docker\s+push"
    r"|kubectl\s+apply"
    r"|deploy\.py(?!\s+--dry-run)"
    r"|rsync.*--delete"
    r"|scp\s.*@"
    r")",
    re.IGNORECASE,
)


def _is_delivery_command(cmd: str) -> bool:
    """True if any top-level segment of cmd starts with a delivery verb."""
    if not cmd:
        return False
    for segment in _DELIVERY_SEPARATORS.split(cmd):
        if _DELIVERY_VERB.match(segment):
            return True
    return False


def _update_cbua_state(
    state: dict,
    *,
    ctx: GuardContext,
    text: str,
    early_result: "list[Optional[GuardResult]]",
    classify: "Callable[[str, str], tuple[str, bool]]",
    silent_ack: "Callable[[dict], None]",
) -> dict:
    """Mutate CBUA state for one PostToolUse tick.

    Extracted from ``CbuaPipelineGuard.on_post_tool``'s inner
    closure so the dispatcher method stays under the structural
    ``func_length`` budget. Callers inject ``classify`` and
    ``silent_ack`` rather than importing the guard class here
    to avoid a cycle.

    Contract: returns the updated ``state`` dict. When an A4
    ask-user violation is detected, populates ``early_result[0]``
    with an advisory :class:`GuardResult` so the caller can
    short-circuit before the reminder sweep fires.
    """
    complexity = state.get("complexity", "")
    redteam_required = state.get("redteam_required", False)

    # C0: classify if not yet done or periodically reclassify
    edit_count = state.get("edit_count", 0)
    if not complexity or (edit_count > 0 and edit_count % 20 == 0):
        complexity, redteam_required = classify(ctx.cache_dir, ctx.session_id)
        state["complexity"] = complexity
        state["redteam_required"] = redteam_required

    if complexity == "simple":
        return state

    # Behavioural counters
    if ctx.tool_name in ("Edit", "Write", "NotebookEdit"):
        state["edit_count"] = edit_count + 1
        state["polling_streak"] = 0
    if ctx.tool_name in ("Read", "Glob", "Grep"):
        state["read_count"] = state.get("read_count", 0) + 1
    if ctx.tool_name == "Bash":
        state["bash_count"] = state.get("bash_count", 0) + 1
    if ctx.tool_name == "Agent":
        state["agent_count"] = state.get("agent_count", 0) + 1

    # Polling detection: same Bash cmd repeating without Edit/Write
    # between ticks → operator is monitoring a process. Signature =
    # first 3 tokens of the command, normalised.
    if ctx.tool_name == "Bash" and isinstance(ctx.tool_input, dict):
        raw_cmd = ctx.tool_input.get("command", "") or ""
        if isinstance(raw_cmd, str):
            sig = " ".join(raw_cmd.strip().split()[:3])
            last_sig = state.get("last_bash_sig", "")
            if sig and sig == last_sig:
                state["polling_streak"] = state.get("polling_streak", 0) + 1
            else:
                state["polling_streak"] = 0
            state["last_bash_sig"] = sig

    # Text markers (secondary signals)
    if _B1_MARKERS.search(text):
        state["b1_shown"] = True
    if _C1_MARKERS.search(text):
        state["c1_shown"] = True
    if _U1_MARKERS.search(text):
        state["u1_shown"] = True
    if _WIREDO_TABLE.search(text):
        state["wiredo_shown"] = True
    if _DICHOTOMY_MARKERS.search(text):
        state["dichotomy_seen"] = True
    if _INTEGRATIVE_MARKERS.search(text):
        state["integrative_shown"] = True

    silent_ack(state)

    # Delivery-phase signal: ONLY shell commands. Pure-text keywords
    # would false-positive on docstrings / handoff markdown.
    if ctx.tool_name == "Bash" and isinstance(ctx.tool_input, dict):
        cmd = ctx.tool_input.get("command", "")
        if isinstance(cmd, str) and _is_delivery_command(cmd):
            state["delivery_keyword_seen"] = True
    if ctx.tool_name == "Agent" and _A5_REDTEAM.search(text):
        state["redteam_dispatched"] = True

    # WIREDO one-shot trigger consumed by _generate_reminder this tick
    state["wiredo_just_fired"] = False
    if not state.get("wiredo_reminded"):
        fire_now = (
            state.get("edit_count", 0) >= 20
            or state.get("delivery_keyword_seen", False)
        )
        if fire_now:
            state["wiredo_reminded"] = True
            state["wiredo_just_fired"] = True

    # A4: ask-user violation (Agent tool only)
    if ctx.tool_name == "Agent" and _A4_ASK_PATTERNS.search(text):
        state["ask_violations"] = state.get("ask_violations", 0) + 1
        early_result[0] = GuardResult.allow_advisory(
            context="⛔ A4 違規：信心 ≥70% 直接做，<70% 升 B1/B2 自己決策。",
        )

    return state


# StateStore namespace
_NAMESPACE = "cbua_pipeline"


class CbuaPipelineGuard(BaseGuard):
    """PostToolUse: enforce CBUA pipeline steps via state tracking.

    State persisted via StateStore.read_modify_write (atomic file lock).
    Uses behavioral signals (edit/read count, agent dispatch) + text markers.

    Competition-mode bypass: when ``handoff_mode == "competition"``,
    ``on_post_tool`` short-circuits BEFORE any state mutation or
    reminder generation. No B1 / C1 / U1 / WIREDO chatter is emitted
    and no state is persisted. See
    ``concinno.handoff_engine.HANDOFF_MODES`` for the full policy and
    the warning that competition mode is benchmark/bounty-only.
    """

    name = "cbua_pipeline"
    category = GuardCategory.COGNITIVE

    def check(self, ctx: GuardContext) -> Optional[GuardResult]:
        """PreToolUse: no-op."""
        return None

    def on_post_tool(self, ctx: GuardContext) -> Optional[GuardResult]:
        """PostToolUse: scan for CBUA compliance evidence.

        All state mutations happen inside StateStore.read_modify_write
        to prevent concurrent-subprocess data loss.
        """
        # Competition mode: silence reminders and skip state bookkeeping.
        # Benchmark/bounty iterations explicitly waive cognitive anchors;
        # see handoff_engine.HANDOFF_MODES.
        try:
            from concinno.handoff_engine import is_competition_mode
            if is_competition_mode():
                return None
        except Exception:
            pass

        if not ctx.cache_dir:
            return None

        store = StateStore(ctx.cache_dir)
        text = self._get_scannable_text(ctx)
        early_result: list[Optional[GuardResult]] = [None]

        def _update(state: dict) -> dict:
            return _update_cbua_state(
                state,
                ctx=ctx, text=text, early_result=early_result,
                classify=self._classify,
                silent_ack=self._behavioral_silent_ack,
            )

        try:
            final_state = store.read_modify_write(
                _NAMESPACE, ctx.session_id, _update,
            )
        except RuntimeError:
            return None

        if early_result[0] is not None:
            return early_result[0]

        # Polling suppression: if we've seen the same Bash command
        # repeat 3+ times without any Edit/Write between, the operator
        # is monitoring something (build, test, log, status). Skip
        # reminders so we don't spam B1/C1/U1 every poll tick.
        if final_state.get("polling_streak", 0) >= 3:
            return None

        return self._generate_reminder(
            final_state,
            final_state.get("complexity", "complicated"),
            final_state.get("redteam_required", False),
        )

    def _classify(
        self, cache_dir: str, session_id: str,
    ) -> tuple[str, bool]:
        """Load or reclassify C0. Fail-safe: complicated."""
        try:
            from concinno.c0_router import C0Router
            result = C0Router().load(cache_dir, session_id)
            if result:
                return result.complexity, result.redteam_required
        except Exception:
            pass
        return "complicated", False

    @staticmethod
    def _behavioral_silent_ack(state: dict) -> None:
        """Silently mark B1 as shown when the session shows research.

        Two acknowledgement paths:

        Path A — research-heavy sessions
        --------------------------------
        CC L4 hides Claude's markdown reasoning from the hook, so a
        text-only B1 scan never sees real "research-before-write"
        sessions. As soon as the session has accumulated **at least
        3 Read calls**, the structural pattern the marker anchors is
        already happening (you cannot read 3 files without thinking
        about which file to read next). Old condition was
        ``reads >= edits``, which silently degenerated to "permanent
        false positive" on heavy-edit sessions like handoff/test/doc
        churn where edits run 50-200 but reads only 3-10. The new
        threshold respects that any non-trivial session reads 3+
        files at the start of work.

        Path B — bash-heavy verification sessions
        ------------------------------------------
        Sessions that are mostly running tests / smokes / git ops
        accumulate Bash calls instead of Reads. **8+ Bash calls** is
        the same proof of structured iteration: you can't run 8
        commands without observing → adjusting → running again,
        which IS the loop B1 anchors. Without this, ratio drift on
        verification-heavy sessions kept the reminder firing on
        every edit even though the user was watching the model
        actually iterate.

        C1/U1 stay strict: intelligence inventory and counter-
        example attack carry semantic weight a ratio cannot prove.
        Only B1 (root-cause/sweet-spot/strategy) is silenced — the
        other markers anchor harder cognitive moves.
        """
        edits = state.get("edit_count", 0)
        reads = state.get("read_count", 0)
        bashes = state.get("bash_count", 0)
        if edits >= 3 and (reads >= 3 or bashes >= 8):
            state["b1_shown"] = True

    # Per-string truncation limit when scanning marker text. Long edits
    # (docstrings, multi-section markdown) used to be skipped entirely
    # via `len(v) < 2000`, which made every marker that lived inside a
    # large Edit invisible to the scanner — the root cause of the
    # permanent "B1 marker 未見" false positive on heavy-edit sessions.
    # Truncating to a generous-but-bounded prefix keeps regex cost
    # capped while letting the leading marker text actually count.
    _SCAN_TEXT_CAP = 4000

    @staticmethod
    def _get_scannable_text(ctx: GuardContext) -> str:
        """Extract text from tool input + result.

        Long values are truncated (not skipped) so marker text inside
        large edits still feeds the scanner. See `_SCAN_TEXT_CAP`.
        """
        parts: list[str] = []
        if isinstance(ctx.tool_input, dict):
            for v in ctx.tool_input.values():
                if isinstance(v, str):
                    parts.append(v[:CbuaPipelineGuard._SCAN_TEXT_CAP])
        # GuardContext field is `tool_result`, not `tool_output`
        if hasattr(ctx, "tool_result") and isinstance(ctx.tool_result, str):
            parts.append(ctx.tool_result[:CbuaPipelineGuard._SCAN_TEXT_CAP])
        return " ".join(parts)

    @staticmethod
    def _generate_reminder(
        state: dict, complexity: str, redteam_required: bool,
    ) -> Optional[GuardResult]:
        """Generate reminders based on accumulated state.

        2026-04-13 restore (用戶糾正「人性河床論」):
        B1/C1/U1 marker warnings are restored. Rationale: even if L4
        blocks hook from reading reasoning text and markers look like
        Goodhart theater, the "未見" reminder still anchors cognition
        toward the desired format. Cognition is fluid — context shapes
        thinking direction, not just verifies it. The earlier Goodhart
        concern (stuffing keywords into tool args) is accepted as the
        cost of cognitive anchoring.

        WIREDO keeps its upgraded D-dimension pointer (from commit
        78711974). Ratio signal stays in ThinkingDepthGuard (single
        source of truth, no duplication here).

        Signals:
        - B1 marker: "未見（根因→甜蜜點→策略）" once at 3+ edits
        - C1 marker: "未見（我知道/不知道/假設）" once at 5+ edits (Complex+)
        - U1 marker: "未見（反例攻擊）" once at 8+ edits (Complex+)
        - A5 redteam-not-dispatched at 10+ edits (if required)
        - WIREDO D-dimension delivery reminder at 5-edit multiples
        """
        edit_count = state.get("edit_count", 0)
        missing: list[str] = []

        # B1: structured thinking marker (after 3+ edits, all Complicated+)
        if not state.get("b1_shown") and edit_count >= 3:
            missing.append("B1 結構思考未見（根因→甜蜜點→策略）")

        # Dichotomy framing: fires when binary A-or-B frame appears
        # without accompanying integrative synthesis language. RLHF
        # bias — models pattern-match to comparative analysis and
        # miss "A+B at higher level" as a third option. Anchor:
        # force the model to ask the integration question before
        # accepting the dichotomy.
        if (
            state.get("dichotomy_seen")
            and not state.get("integrative_shown")
            and edit_count >= 2
        ):
            missing.append(
                "🔀 Dichotomy 框架偵測 — 先問「A+B 在更高層級共存？」"
                "（RLHF bias: 偏好 comparative 而非 integrative synthesis；"
                "多模式 framework / dual-mode / 融合 是常被跳過的第三選項）"
            )

        # C1: intelligence inventory marker (Complex+ only, 5+ edits)
        if (
            not state.get("c1_shown")
            and edit_count >= 5
            and complexity in ("complex", "chaotic")
        ):
            missing.append("C1 情報盤點未見（我知道/不知道/假設）")

        # U1: counter-example attack marker (Complex+ only, 8+ edits)
        if (
            not state.get("u1_shown")
            and edit_count >= 8
            and complexity in ("complex", "chaotic")
        ):
            missing.append("U1 反例攻擊未見")

        # A5: redteam required but not dispatched (Agent tool dispatch
        # is observable via tool_name; this stays behavioral).
        if (
            redteam_required
            and not state.get("redteam_dispatched")
            and edit_count >= 10
        ):
            missing.append("⛔ A5 紅隊未派出")

        # WIREDO delivery reminder: ONE-SHOT, fires only at delivery
        # signals. 2026-04-13 fix (用戶糾正「時機判斷要正確 不然會
        # 變成事前查證六維 亂七八糟」):
        #   - removed every-5-edits trigger (was firing during work)
        #   - now fires once via wiredo_just_fired flag set in _update
        #   - trigger conditions (in _update):
        #     (a) 20+ edits accumulated (heavy work session)
        #     (b) delivery keyword in tool input
        #         (commit/ship/done/release/部署/完成/PR)
        # The actual six-dim ✓/✗ checklist runs at Stop event via
        # prompt_hooks.WIREDO_JUDGE, not here. This is the "delivery
        # is near, don't forget WIREDO" nudge — fires exactly once.
        if state.get("wiredo_just_fired"):
            missing.append(
                "📐 WIREDO 六維（W:接線/I:母版/R:響應/E:可配置/"
                "D:驗證/O:可觀測）— 交付時機到了。D 維最強：跑 "
                "tests / build / 截圖才算驗證，tsc/lint 不算。"
                "Stop event 會跑 WIREDO_JUDGE 強制六維打勾"
            )

        if not missing:
            return None

        severity = "⚠" if len(missing) <= 2 else "⛔"
        return GuardResult.allow_advisory(
            context=(
                f"{severity} CBUA ({complexity}, {edit_count} edits):\n"
                + "\n".join(f"  - {m}" for m in missing)
            ),
        )
