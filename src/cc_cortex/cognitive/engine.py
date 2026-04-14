"""Cognitive engine — orchestrator + hooks + guard adapter.

@module cognitive.engine
@responsibility Integrate SessionProfile/Journal/Thresholds, provide hook entry points
@dependencies cognitive._base, cognitive.session_profile, cognitive.journal, cognitive.thresholds
@exports CognitiveEngine, CognitiveGuard, check_session_start, check_pre_tool, check_stop,
    on_stop, on_post_tool
"""

from __future__ import annotations

import os
from typing import Optional

from cc_cortex.guards.base import BaseGuard, GuardCategory, GuardContext, GuardResult

from ._base import cognitive_dir
from .journal import DecisionJournal
from .session_profile import SessionProfile
from .thresholds import AdaptiveThresholds

# ── Weakness → Framework mapping ─────────────────────────

_WEAKNESS_FRAMEWORKS: dict[str, tuple[str, str]] = {
    # decision_type → (framework_name, one-line remedy)
    "tool_selection": (
        "three-layer-thinking",
        "L1 root cause → L2 sweet spot → L3 escalate",
    ),
    "file_edit": (
        "verification-obligation",
        "Read before edit. Verify after edit.",
    ),
    "user_correction": (
        "auto-knowledge-distillation",
        "Capture → distill → verify → automate",
    ),
    "method": (
        "first-principles",
        "Decompose to fundamentals, rebuild from truth",
    ),
    "scope": (
        "consequence-first",
        "Who calls it? Remove it — what breaks?",
    ),
}


def _map_weakness_to_framework(
    weak_spots: list[dict],
) -> str:
    """Map journal weak spots to specific cognitive frameworks."""
    lines = ["⚠ Cognitive weak spots:"]
    for ws in weak_spots:
        dt = ws.get("decision_type", "unknown")
        quality = ws.get("quality", 0)
        fw_name, remedy = _WEAKNESS_FRAMEWORKS.get(
            dt, ("three-layer-thinking", "Root cause first"),
        )
        lines.append(
            f"  {dt} ({quality:.0%}) → {remedy} "
            f"[knowledge/{fw_name}.md]"
        )
    return "\n".join(lines)


class CognitiveEngine:
    """Orchestrator that integrates sentinel, knowledge, and evolution.

    Provides hook entry points and CLI inspection methods.
    Ties together SessionProfile, DecisionJournal, AdaptiveThresholds,
    and bridges to sentinel/knowledge/evolution modules.
    """

    def __init__(self, session_id: str = "", base_dir: Optional[str] = None):
        self._dir = cognitive_dir(base_dir)
        self.session_id = session_id
        self.profile = SessionProfile(session_id, self._dir) if session_id else None
        self.journal = DecisionJournal(self._dir)
        self.thresholds = AdaptiveThresholds(self._dir)

    def on_session_start(self, user_message: str = "") -> Optional[str]:
        """Hook: called at session start.

        Initializes profile, checks for insights from past sessions.
        Returns context string for the AI, or None.
        """
        if self.profile and user_message:
            self.profile.record_user_message(user_message)
            self.profile.classify()

        # Check decision quality and map to frameworks
        insights: list[str] = []
        weak_spots = self.journal.get_weak_spots()
        if weak_spots:
            remedies = _map_weakness_to_framework(weak_spots[:3])
            insights.append(remedies)

        # Check if thresholds have adapted
        status = self.thresholds.status()
        adapted = [k for k, v in status.items() if v["deviation"] != 0]
        if adapted:
            insights.append(
                f"🧠 Cognitive: {len(adapted)} threshold(s) adapted from learning: "
                + ", ".join(adapted[:5])
            )

        return "\n".join(insights) if insights else None

    def get_sentinel_params(self) -> dict[str, int]:
        """Get sentinel threshold params from adaptive thresholds."""
        t = self.thresholds
        return {
            "repeat_threshold": t.get("sentinel_repeat"),
            "stale_threshold": t.get("sentinel_diminish"),
            "paralysis_threshold": t.get("sentinel_paralysis"),
            "scope_threshold": t.get("sentinel_scope"),
            "drift_threshold": t.get("sentinel_drift"),
            "diminish_threshold": t.get("sentinel_diminish"),
        }

    def get_tidy_params(self) -> dict[str, int]:
        """Get tidy balance params from adaptive thresholds."""
        t = self.thresholds
        return {
            "md_threshold": t.get("tidy_md_lines"),
            "code_threshold": t.get("tidy_code_lines"),
        }

    def check_sentinel(
        self,
        tool_name: str,
        tool_input: dict,
        state_dir: str,
    ) -> Optional[str]:
        """Run sentinel checks with adaptive thresholds."""
        try:
            from cc_cortex.sentinel import check_sentinel  # type: ignore[attr-defined]
        except ImportError:
            return None

        params = self.get_sentinel_params()
        return check_sentinel(
            self.session_id,
            tool_name,
            tool_input,
            state_dir,
            **params,
        )

    def check_tidy(self, tool_name: str, tool_input: dict) -> Optional[str]:
        """Run tidy balance check with adaptive thresholds."""
        try:
            from cc_cortex.evolution import check_tidy_balance
        except ImportError:
            return None

        params = self.get_tidy_params()
        return check_tidy_balance(tool_name, tool_input, **params)

    def ingest_corrections(
        self,
        corrections: list[dict],
        source: str = "knowledge",
    ) -> int:
        """Ingest corrections from knowledge module into DecisionJournal.

        Each correction becomes a 'corrected' decision entry.

        Returns:
            Number of corrections ingested.
        """
        count = 0
        for c in corrections:
            self.journal.record(
                session_id=c.get("session_id", self.session_id),
                decision_type="user_correction",
                context=c.get("assistant_before", "")[:200],
                action=c.get("user_correction", "")[:200],
                confidence=1.0 - c.get("confidence", 1.0),
                tags=[source, "auto_extracted"],
            )
            count += 1
        return count

    def on_tool_use(self, tool_name: str, tool_input: dict) -> Optional[str]:
        """Hook: called before each tool use. Records tool in profile."""
        if self.profile:
            self.profile.record_tool(tool_name, tool_input)
        return None

    def on_correction(self, correction_text: str, context: str = "") -> None:
        """Hook: called when a user correction is detected."""
        self.journal.record(
            session_id=self.session_id,
            decision_type="user_correction",
            context=context[:200],
            action=correction_text[:200],
            confidence=0.0,
            tags=["correction"],
        )

    def on_session_end(self, transcript_path: str = "") -> None:
        """Hook: called at session end.

        Finalizes and saves profile, triggers threshold learning,
        and ingests corrections from transcript via knowledge module.
        """
        if self.profile:
            self.profile.classify()
            self.profile.save()

        # Periodically learn from accumulated profiles
        self.thresholds.learn_from_profiles(self._dir)

        # Extract and ingest corrections from transcript
        if transcript_path:
            try:
                from cc_cortex.knowledge import extract_corrections

                corrections = extract_corrections(transcript_path)
                if corrections:
                    self.ingest_corrections(corrections)
            except ImportError:
                pass

    def get_dashboard(self) -> dict:
        """Get comprehensive cognitive dashboard for CLI display."""
        return {
            "session_profile": self.profile.to_dict() if self.profile else None,
            "decision_quality": self.journal.stats(),
            "adaptive_thresholds": self.thresholds.status(),
            "session_type_distribution": SessionProfile.get_type_distribution(self._dir),
        }

    def get_summary(self) -> str:
        """Get a human-readable cognitive summary."""
        lines: list[str] = []
        lines.append("🧠 cc-cortex Cognitive Layer")
        lines.append("")

        # Decision quality
        stats = self.journal.stats()
        quality = stats["quality_score"]
        quality_icon = "🟢" if quality >= 0.7 else "🟡" if quality >= 0.4 else "🔴"
        lines.append(
            f"  {quality_icon} Decision quality: {quality:.0%} "
            f"({stats['scored_decisions']} scored / {stats['total_decisions']} total)"
        )

        if stats["weak_spots"]:
            for ws in stats["weak_spots"][:3]:
                lines.append(
                    f"     ⚠ Weak: {ws['decision_type']} ({ws['quality']:.0%}, n={ws['count']})"
                )

        # Thresholds
        thresholds = self.thresholds.status()
        adapted = {k: v for k, v in thresholds.items() if v["deviation"] != 0}
        if adapted:
            lines.append(f"  🔧 Adapted thresholds: {len(adapted)}")
            for k, v in adapted.items():
                sign = "+" if v["deviation"] > 0 else ""
                lines.append(
                    f"     {k}: {v['default']} → {v['current']} ({sign}{v['deviation']})"
                )
        else:
            lines.append("  🔧 Thresholds: all at defaults")

        # Session types
        dist = SessionProfile.get_type_distribution(self._dir)
        if dist:
            total = sum(dist.values())
            top = sorted(dist.items(), key=lambda x: x[1], reverse=True)[:5]
            dist_str = ", ".join(f"{k}:{v}" for k, v in top)
            lines.append(f"  📊 Session types ({total} sessions): {dist_str}")
        else:
            lines.append("  📊 Session types: no data yet")

        # Current session
        if self.profile:
            p = self.profile
            lines.append(
                f"  📍 Current: {p.session_type} | "
                f"{len(p.files_touched)} files | "
                f"R/W ratio: {p.read_write_ratio:.1f}"
            )

        return "\n".join(lines)


# ── Hook entry points ──────────────────────────────────────


def check_session_start(payload: dict) -> Optional[dict]:
    """Hook entry: SessionStart event."""
    session_id = os.environ.get("CC_SESSION_ID", "")
    if not session_id:
        return None

    engine = CognitiveEngine(session_id)
    context = engine.on_session_start()
    if context:
        return {"additionalContext": context}
    return None


def check_pre_tool(payload: dict) -> Optional[dict]:
    """Hook entry: PreToolUse event."""
    session_id = os.environ.get("CC_SESSION_ID", "")
    if not session_id:
        return None

    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input", {})

    engine = CognitiveEngine(session_id)
    context = engine.on_tool_use(tool_name, tool_input)
    if context:
        return {"additionalContext": context}
    return None


def check_stop(payload: dict) -> Optional[dict]:
    """Hook entry: Stop event."""
    from cc_cortex.core.path_utils import find_transcript

    session_id = os.environ.get("CC_SESSION_ID", "")
    if not session_id:
        return None

    transcript_path = find_transcript(session_id)
    engine = CognitiveEngine(session_id)
    engine.on_session_end(transcript_path)
    return None


def on_stop(hook_data: dict) -> None:
    """Hook entry alias for check_stop — called by CCC on_stop pipeline."""
    check_stop(hook_data or {})


def on_post_tool(hook_data: dict) -> Optional[str]:
    """Hook entry: PostToolUse — record tool use in cognitive profile.

    Returns additionalContext string or None.
    """
    result = check_pre_tool(hook_data or {})
    if result and result.get("additionalContext"):
        return result["additionalContext"]
    return None


# ── BaseGuard adapter ───────────────────────────────────────────


class CognitiveGuard(BaseGuard):
    """Knowledge injection on ALLOW. Layer 3 — never denies."""

    name = "cognitive"
    category = GuardCategory.COGNITIVE

    def check(self, ctx: GuardContext) -> GuardResult | None:
        """Inject cognitive insights (weak spots, adapted thresholds) on ALLOW."""
        result = check_pre_tool({
            "tool_name": ctx.tool_name,
            "tool_input": ctx.tool_input,
        })
        if result and result.get("additionalContext"):
            return GuardResult.allow(context=result["additionalContext"])
        return None

    def on_stop(self, ctx: GuardContext) -> GuardResult | None:
        """Finalize session profile, trigger threshold learning, ingest corrections."""
        check_stop({})
        return None
