"""concinno.hooks.on_prompt_submit — CCC-side prompt submit handler.

@module on_prompt_submit
@responsibility Combine clarity gate + multi-question injection + insight engine
    into a single CCC handler. CC hook stays thin (stdin parse + output).
@dependencies concinno.prompt_guard, concinno.insight_engine
@exports handle_prompt_submit
"""

from __future__ import annotations


def _ftrl_learning_injection(
    cache_dir: str = "",
    max_items: int = 3,
    ftrl_threshold: float = 3.0,
) -> str | None:
    """Inject top FTRL-weighted learnings as session reminders.

    Thin adapter — delegates to :class:`concinno.prompt_engine.PromptEngine`
    so FTRL learnings flow through the shared dynamic-slot pipeline (budget
    aware, priority-ordered). ``cache_dir`` is retained for ABI stability
    but currently unused: learnings live at
    ``~/.claude/cognitive/learnings.json``.

    Args:
        cache_dir: Kept for backwards compatibility; ignored.
        max_items: Maximum learnings to inject.
        ftrl_threshold: Minimum FTRL weight to include.

    Returns:
        Formatted string of top learnings, or None.
    """
    try:
        from concinno.prompt_engine import PromptEngine

        return PromptEngine().build_ftrl_context(
            max_items=max_items,
            ftrl_threshold=ftrl_threshold,
        )
    except Exception:
        return None


def _first_keyword_pos(text: str, keywords: list[str]) -> int:
    """Return position of the first matching keyword in *text*, or -1."""
    for kw in keywords:
        pos = text.find(kw.lower())
        if pos >= 0:
            return pos
    return -1


def rule_injection(
    user_prompt: str,
    *,
    trigger_map: dict[str, list[str]] | None = None,
    rules_dir: str = "",
    max_files: int = 2,
) -> str | None:
    """Keyword-triggered L1 rule injection for three-layer rule architecture.

    Scans user prompt for trigger keywords (multi-language via i18n.patterns)
    and injects matching rule files as additionalContext. This enables:
    L0 (always loaded, minimal) → L1 (keyword-triggered) → L2 (manual).

    Args:
        user_prompt: The user's input.
        trigger_map: Maps rule file basenames to i18n pattern keys.
            e.g. {"handoff.md": "rule_trigger.handoff"}
            If a pattern key is not found in i18n, the value list is used
            as literal keywords (fallback for custom setups).
        rules_dir: Base directory for rule files.
        max_files: Maximum number of rule files to inject (default 2).

    Returns:
        Combined content of matched rule files, or None.
    """
    import os

    from concinno.i18n import patterns as i18n_patterns

    if not trigger_map or not user_prompt:
        return None

    prompt_lower = user_prompt.lower()
    matched: list[tuple[int, str, str]] = []  # (position, basename, content)

    for rule_file, kw_source in trigger_map.items():
        # Try i18n patterns first (multi-language), fallback to literal list
        keywords = (
            i18n_patterns(kw_source) or [kw_source]
            if isinstance(kw_source, str)
            else list(kw_source)
        )
        pos = _first_keyword_pos(prompt_lower, keywords)
        if pos < 0:
            continue
        full_path = (
            os.path.join(rules_dir, rule_file) if rules_dir else rule_file
        )
        if not os.path.isfile(full_path):
            continue
        try:
            with open(full_path, "r", encoding="utf-8") as f:
                content = f.read(4096)  # Cap at 4KB per file
        except OSError:
            continue
        matched.append((pos, os.path.basename(rule_file), content))

    if not matched:
        return None

    # Sort by position in prompt (earliest mention = most relevant), cap
    matched.sort(key=lambda x: x[0])
    matched = matched[:max_files]

    parts = [f"📋 {name}:\n{content}" for _, name, content in matched]
    header = "📋 L1 rules auto-loaded (keyword trigger):"
    return header + "\n\n" + "\n\n---\n\n".join(parts)


def _auto_capture_goal(
    user_prompt: str,
    cache_dir: str,
    session_id: str,
    max_len: int = 80,
) -> None:
    """Seed TaskOrchestrator with the first user prompt as the session goal.

    Pure side-effect, failure-safe. Behavior:
      - No-op when cache_dir / session_id missing.
      - No-op when a goal is already set (don't clobber explicit goals).
      - No-op when prompt is empty after strip / trivially short.
      - Truncates long prompts to *max_len* chars so the goal line stays
        readable in ``cc aegis status``.

    This makes every fresh session automatically trackable via the
    existing aegis CLI without requiring the user to call ``cc aegis goal``
    manually.
    """
    if not cache_dir or not session_id:
        return
    text = (user_prompt or "").strip()
    if len(text) < 3:
        return
    try:
        from concinno.task_orchestrator import TaskOrchestrator

        orch = TaskOrchestrator(
            cache_dir=cache_dir,
            session_id=session_id,
        )
        if orch.progress().get("goal"):
            return  # Already set — don't clobber
        goal = text[:max_len]
        if len(text) > max_len:
            goal = goal.rstrip() + "..."
        orch.set_goal(goal)
    except Exception:
        # Auto-capture must never break prompt submission
        pass


def _cbua_forced_classification(
    user_prompt: str,
    cache_dir: str = "",
    session_id: str = "",
) -> str | None:
    """Force CBUA C0 classification into additionalContext.

    When complexity ≥ Complicated, injects a mandatory format that Claude
    must follow before acting. This closes the gap where CBUA B1/B2
    thinking depth was text-only SOP (ignorable).

    The injection forces Claude to:
    1. Show C0 classification result
    2. If ≥ Complicated: show B1 structured thinking before first tool call
    3. If redteam_required: remind to dispatch Opus red team before delivery
    """
    if not user_prompt or len(user_prompt) < 3:
        return None

    try:
        from concinno.c0_router import C0Router
        from concinno.cbua_ux import CbuaCode, cbua_format
        from concinno.hooks.io_utils import cache_path

        c_dir = cache_dir or cache_path() or ""
        if not c_dir:
            return None

        router = C0Router()
        result = router.classify(user_prompt)
        router.persist(result, c_dir, session_id or "")

        parts: list[str] = []

        # Always show classification
        parts.append(cbua_format(
            CbuaCode.C0,
            f"{result.complexity} (budget={result.prompt_budget}t, "
            f"guard={result.guard_level})",
        ))

        # ≥ Complicated: force structured thinking
        if result.complexity != "simple":
            parts.append(
                "⛔ C0≥Complicated — 回應前必須先輸出："
                " B1根因(為什麼) → B1甜蜜點(最佳CP) → B1策略(怎麼做)。"
                " 跳過 B1 直接做 = CBUA 違規。"
            )

        # Red team required
        if result.redteam_required:
            parts.append(
                "⛔ A5 交付紅隊：本任務 C0 標記 redteam_required=True。"
                " 交付前必派 1-3 Opus 子代理壓測可交付性。"
                " 不派紅隊不交付。"
            )

        # A2A suggested
        if result.a2a_suggested:
            parts.append(cbua_format(
                CbuaCode.C0,
                "A2A 建議：此任務適合多代理協作（已偵測到協作關鍵詞）",
            ))

        return "\n".join(parts) if parts else None

    except Exception:
        return None


def handle_prompt_submit(
    user_prompt: str,
    *,
    prefs_path: str = "",
    cache_dir: str = "",
    session_id: str = "",
    trigger_map: dict[str, list[str]] | None = None,
    rules_dir: str = "",
) -> dict:
    """Run CCC prompt-submit pipeline.

    Returns:
        dict with optional keys:
        - "deny": hook deny dict (if clarity gate rejects)
        - "contexts": list[str] of additionalContext fragments
    """
    from concinno.core.config import get_config
    from concinno.insight_engine import check_insight
    from concinno.prompt_guard import (
        multi_question_injection,
        run_clarity_gate,
    )

    cfg = get_config()

    # 1. TADS-3 clarity gate — respect /hook clarity_gate off.
    if cfg.feature("clarity_gate", "enabled"):
        deny = run_clarity_gate(user_prompt, prefs_path=prefs_path)
        if deny:
            return {"deny": deny}

    contexts: list[str] = []

    # 2. Multi-question checklist injection — gated by prompt_guard.
    if cfg.feature("prompt_guard", "enabled"):
        mq = multi_question_injection(user_prompt)
        if mq:
            contexts.append(mq)

    # 3. Proactive Insight Engine — gated by insight_engine feature.
    if cfg.feature("insight_engine", "enabled"):
        insight = check_insight(
            user_prompt,
            cache_dir=cache_dir,
            session_id=session_id,
        )
        if insight:
            contexts.append(insight)

    # 4. FTRL-weighted learning injection (top corrections by recency × count)
    ftrl_ctx = _ftrl_learning_injection(cache_dir=cache_dir)
    if ftrl_ctx:
        contexts.append(ftrl_ctx)

    # 5. L1 Rule injection (three-layer architecture)
    if trigger_map:
        rule_ctx = rule_injection(
            user_prompt,
            trigger_map=trigger_map,
            rules_dir=rules_dir,
        )
        if rule_ctx:
            contexts.append(rule_ctx)

    # 6. CBUA B1 forced classification (hardened — no more text-only SOP)
    b1_ctx = _cbua_forced_classification(user_prompt, cache_dir, session_id)
    if b1_ctx:
        contexts.append(b1_ctx)

    # 7. Auto-capture session goal from first prompt (side-effect only)
    _auto_capture_goal(user_prompt, cache_dir, session_id)

    return {"contexts": contexts}
