"""CLI commands for the cognitive subsystem.

@module cognitive.cli
@responsibility User-facing CLI for inspecting cognitive state
@dependencies cognitive.engine, cognitive.journal, cognitive.thresholds, cognitive.session_profile
"""

from __future__ import annotations

import os

from ._base import cognitive_dir
from .engine import CognitiveEngine
from .journal import DecisionJournal
from .session_profile import SessionProfile
from .thresholds import AdaptiveThresholds


def cli_status() -> str:
    """CLI: show cognitive layer status."""
    engine = CognitiveEngine()
    return engine.get_summary()


def cli_thresholds() -> str:
    """CLI: show adaptive thresholds."""
    thresholds = AdaptiveThresholds()
    status = thresholds.status()
    lines = ["🔧 Adaptive Thresholds", ""]
    for key, info in status.items():
        dev = info["deviation"]
        marker = f" ({'+' if dev > 0 else ''}{dev})" if dev != 0 else ""
        bounds = info["bounds"]
        lines.append(
            f"  {key:30s} {info['current']:>3d}{marker:>8s}"
            f"  [default: {info['default']},"
            f" bounds: {bounds[0]}-{bounds[1]}]"
        )
    return "\n".join(lines)


def cli_journal(limit: int = 10) -> str:
    """CLI: show recent decisions."""
    journal = DecisionJournal()
    entries = journal.get_recent(limit)
    if not entries:
        return "📓 Decision Journal: empty"

    lines = [f"📓 Decision Journal (last {limit})", ""]
    for e in entries:
        outcome = e.get("outcome", "pending")
        outcome_icons = {
            "accepted": "✅",
            "corrected": "❌",
            "reverted": "↩",
            "ignored": "⏭",
        }
        icon = outcome_icons.get(outcome, "⏳")
        lines.append(
            f"  {icon} [{e.get('decision_type', '?')}] {e.get('action', '')[:60]}"
            f"  (conf: {e.get('confidence', '?')}, {outcome})"
        )
    return "\n".join(lines)


def cli_profiles(limit: int = 10) -> str:
    """CLI: show recent session profiles."""
    profiles = SessionProfile.load_history(limit=limit)
    if not profiles:
        return "📊 Session Profiles: no data yet"

    lines = [f"📊 Session Profiles (last {limit})", ""]
    for p in profiles[-limit:]:
        dur = p.get("duration_seconds", 0)
        dur_str = f"{dur / 60:.0f}m" if dur > 60 else f"{dur:.0f}s"
        lines.append(
            f"  {p.get('short_id', '?'):8s} {p.get('session_type', '?'):10s} "
            f"{p.get('files_touched_count', 0):>3d} files  "
            f"R/W {p.get('read_write_ratio', 0):.1f}  "
            f"{dur_str}"
        )
    return "\n".join(lines)


def cli_reset_thresholds() -> str:
    """CLI: reset all thresholds to defaults."""
    AdaptiveThresholds().reset()
    return "🔧 All thresholds reset to defaults."


def cli_promotions(threshold: int = 3) -> str:
    """CLI: show learnings ready for promotion to rules."""
    try:
        from cc_cortex.knowledge import suggest_rule_promotions
    except ImportError:
        return "❌ knowledge module not available"

    learnings_path = os.path.join(
        cognitive_dir(),
        "learnings.json",
    )
    suggestions = suggest_rule_promotions(learnings_path, threshold)
    if not suggestions:
        return "📋 No learnings ready for promotion (need count ≥ {}).".format(threshold)

    lines = [f"📋 Rule Promotion Suggestions ({len(suggestions)} pending)", ""]
    for s in suggestions:
        lines.append(f"  🔺 [{s['learning_id']}] (×{s['count']}, conf={s['confidence']:.0%})")
        lines.append(f"     Correction: {s['correction_text'][:80]}")
        if s.get("context"):
            lines.append(f"     Context: {s['context'][:60]}...")
        lines.append(f"     → Target: .claude/rules/{s['target_file']}")
        lines.append("")

    lines.append("Use `cc-cortex cognitive promote <id>` to create a rule file.")
    return "\n".join(lines)


def cli_promote(learning_id: str, rules_dir: str = "") -> str:
    """CLI: promote a learning to a rule file."""
    try:
        from cc_cortex.knowledge import (
            mark_promoted,
            suggest_rule_promotions,
        )
    except ImportError:
        return "❌ knowledge module not available"

    learnings_path = os.path.join(cognitive_dir(), "learnings.json")
    suggestions = suggest_rule_promotions(learnings_path, threshold=1)

    target = next((s for s in suggestions if s["learning_id"] == learning_id), None)
    if not target:
        return f"❌ Learning '{learning_id}' not found or already promoted."

    if not rules_dir:
        rules_dir = os.path.join(os.path.expanduser("~"), ".claude", "rules")

    os.makedirs(rules_dir, exist_ok=True)
    rule_path = os.path.join(rules_dir, target["target_file"])

    content = (
        f"# Learned Rule: {target['correction_text'][:60]}\n\n"
        f"> Auto-promoted from {target['count']}x recurring correction "
        f"(confidence: {target['confidence']:.0%})\n\n"
        f"{target['correction_text']}\n"
    )
    if target.get("context"):
        content += f"\n## Context\n\n{target['context']}\n"

    with open(rule_path, "w", encoding="utf-8") as f:
        f.write(content)

    mark_promoted(learnings_path, learning_id)
    return f"✅ Rule created: {rule_path}"
