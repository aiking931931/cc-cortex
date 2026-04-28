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


def _stage_minus_1_anchor(
    user_prompt: str,
    cache_dir: str = "",
    session_id: str = "",
) -> str | None:
    """Inject a Stage -1 intent anchor on first turn for non-Simple tasks.

    CBUA v3.2 minimal Stage -1 (concinno 2.35.0): extract ``done_spec`` +
    ``constraints`` heuristically from the user's first prompt and persist
    them on the ``intent_anchor`` namespace so the existing
    :class:`concinno.intent_anchor_guard.IntentAnchorGuard` can fold them
    into every periodic re-injection. Returns the rendered anchor block
    as additionalContext so the model also sees it once at submit time.

    Skipped:
        * Simple complexity (ZIQ whitelist) — overhead exceeds the lift
          on trivial prompts.
        * Empty / trivially short prompts.
        * Subsequent turns — anchor already present means a previous
          first-turn submit captured it; further turns are handled by
          :class:`concinno.intent_anchor_guard.IntentAnchorGuard`.
    """
    if not user_prompt or len(user_prompt.strip()) < 8:
        return None
    if not cache_dir or not session_id:
        return None

    try:
        from concinno.core.state_store import StateStore
        from concinno.intent_anchor import extract_anchor, render_anchor_block

        store = StateStore(cache_dir)
        existing = store.read("intent_anchor", session_id, default={})

        # First-turn detection — back-compat with v2.9 'intent' key.
        if existing.get("summary") or existing.get("intent"):
            return None

        # ZIQ Simple whitelist: respect classification set by Step 6
        # earlier in the same submit handler. C0Router persists at
        # `c0_route` namespace; missing → assume Complicated to be safe.
        c0_state = store.read("c0_route", session_id, default=None)
        complexity = (c0_state or {}).get("complexity", "complicated")
        if complexity == "simple":
            return None

        anchor = extract_anchor(user_prompt)
        if anchor.is_empty():
            return None

        merged = dict(existing)
        merged.update(anchor.to_dict())
        merged["intent"] = anchor.summary  # v2.9 back-compat alias
        merged["intent_source"] = "stage_minus_1"
        store.write("intent_anchor", session_id, merged)

        return render_anchor_block(
            anchor,
            title="🎯 Stage -1 意圖錨定（首輪）",
        )
    except Exception:
        # Stage -1 anchor must never break submit flow.
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

    # 7. CBUA Stage -1 intent anchor (concinno 2.35.0 — minimal v3.2 ship)
    #    First-turn only, ZIQ Simple whitelist skip, additive on top of
    #    the existing intent_anchor namespace so v2.9 readers stay happy.
    stage_neg1_ctx = _stage_minus_1_anchor(user_prompt, cache_dir, session_id)
    if stage_neg1_ctx:
        contexts.append(stage_neg1_ctx)

    # 8. Auto-capture session goal from first prompt (side-effect only)
    _auto_capture_goal(user_prompt, cache_dir, session_id)

    # 9. Handoff resume — when user says "交接 X" / "/handoff resume X",
    #    detect intent and inject §0/§1/§5 from the existing handoff.
    handoff_ctx = _handoff_resume_inject(user_prompt)
    if handoff_ctx:
        contexts.append(handoff_ctx)

    # 10. time_steward — DAG visualiser / pre-spawn contention / idle
    #     detection / sub-agent budget tracker / re-triage on completion /
    #     cancel-restart heuristic. Wired in Phase 2 of 3.2.0 ship-prep
    #     (replaces the older single-purpose parallel_spawn_reminder).
    time_steward_ctx = _time_steward_inject(
        user_prompt, session_id=session_id,
    )
    if time_steward_ctx:
        contexts.append(time_steward_ctx)

    # 11. polling-watcher — surface active waits + drained alerts
    #     so the agent sees pending async work at every prompt without
    #     depending on sub-agent notifications.
    try:
        from concinno.hooks.wait_inject import build_context as _wi_build
        polling_ctx = _wi_build()
        if polling_ctx:
            contexts.append(polling_ctx)
    except Exception:
        pass  # never block prompt submission on this

    # 12. proactive Skill router — surface "/<skill> matches your
    #     request" advisories when the user's prompt semantically maps
    #     to a registered Skill. FATAL-1 fix wave (sub-agent O): the
    #     module shipped without a production caller — this wiring
    #     activates it and lets the ZIQ FTRL loop earn its reward.
    skill_ctx = _skill_proactive_router_inject(user_prompt)
    if skill_ctx:
        contexts.append(skill_ctx)

    # 13. progressive Skill disclosure (HP7 — 4.5.0) — three-layer L1/L2/L3
    #     router. Cheap inverted-index list_l1() + ZIQ-routed top-k.
    #     Off by default (DEFAULT_OFF_4_0_0); when enabled, surfaces a
    #     short advisory listing the top-k disclosable skills for the
    #     current prompt without forcing a load_l2.
    disclosure_ctx = _skill_disclosure_inject(user_prompt)
    if disclosure_ctx:
        contexts.append(disclosure_ctx)

    return {"contexts": contexts}


def _handoff_resume_inject(user_prompt: str) -> str | None:
    """Best-effort §0/§1/§5 inject for resumed handoffs.

    Catches every exception so a missing handoff dir / unreadable file
    cannot break prompt submission. The handoff path is derived from
    ``$HOME/_AI_BRAIN/06_Handoffs/<project>/`` glob; if no file matches
    the project name the underlying ``build_resume_inject`` returns the
    "not found" advisory which is still useful to the agent.
    """
    try:
        from pathlib import Path

        from concinno.handoff_resume_hook import (
            build_resume_inject,
            detect_resume_intent,
        )
        proj = detect_resume_intent(user_prompt)
        if not proj:
            return None
        # Resolve handoff dir: $HOME/_AI_BRAIN/06_Handoffs/<proj>/.
        # In environments without _AI_BRAIN (e.g. fresh strangers),
        # advisory still fires with a "not found" message.
        base = Path.home() / "_AI_BRAIN" / "06_Handoffs" / proj
        if base.is_dir():
            candidates = sorted(base.glob("交接_*.md"), reverse=True)
            if not candidates:
                candidates = sorted(base.glob("*.md"), reverse=True)
            handoff_path = candidates[0] if candidates else (base / "交接.md")
        else:
            handoff_path = base / "交接.md"
        return build_resume_inject(proj, handoff_path)
    except Exception:
        return None


def _skill_proactive_router_inject(user_prompt: str) -> str | None:
    """Best-effort proactive Skill router inject (FATAL-1 fix sub-agent O).

    Wires :func:`concinno.skill_proactive_router.propose_skills` into the
    UserPromptSubmit hook chain so a registered Skill that semantically
    matches the user's request surfaces as an advisory line. Failure-
    safe: any exception swallows silently — the router itself never
    raises but the import or feature lookup might.

    Gated by ``feature_config['skill_proactive_router']['enabled']``.
    """
    try:
        from concinno.core.config import get_config
        from concinno.skill_proactive_router import propose_skills

        cfg = get_config()
        if not cfg.feature("skill_proactive_router", "enabled"):
            return None
        result = propose_skills(user_prompt)
        text = (result.additional_context or "").strip()
        return text or None
    except Exception:
        return None


def _skill_disclosure_inject(user_prompt: str) -> str | None:
    """Best-effort progressive Skill disclosure inject (HP7 — 4.5.0).

    Wires :class:`concinno.skills.disclosure.SkillDisclosure` into the
    UserPromptSubmit hook chain. Surfaces top-k disclosable Skills for
    the current prompt as a short advisory line; does not force L2 load
    so the prompt stays cheap.

    Failure-safe: any exception (missing skills dir, malformed
    SKILL.md frontmatter, ZIQ bus down) swallows silently — the
    underlying class never raises but the import path might.

    Gated by ``feature_config['skill_disclosure']['enabled']`` (default
    False per the 4.0.0 opt-in policy).
    """
    if not user_prompt or not user_prompt.strip():
        return None
    try:
        from concinno.core.config import get_config
        from concinno.skills.disclosure import SkillDisclosure

        cfg = get_config()
        if not cfg.feature("skill_disclosure", "enabled"):
            return None
        top_k = int(cfg.feature("skill_disclosure", "top_k_routing") or 5)
        sd = SkillDisclosure(top_k=top_k)
        results = sd.route(user_prompt, top_k=top_k)
        if not results:
            return None
        lines = [
            f"  /{r.name} (score {r.score:.2f})"
            for r in results
        ]
        return "Skill disclosure (L1) — candidates:\n" + "\n".join(lines)
    except Exception:
        return None


def _time_steward_inject(
    user_prompt: str, *, session_id: str | None = None,
) -> str | None:
    """Best-effort time_steward inject.

    Calls ``run_time_steward`` with the current prompt + session id and
    returns the inject text (or ``None`` if no advice this turn). Catches
    every exception so a state-file glitch / missing dir cannot break
    prompt submission. Supersedes the older single-purpose
    ``parallel_spawn_reminder`` (now collapsed into one of six
    time_steward capabilities — idle detection — alongside DAG
    visualiser, pre-spawn contention, budget tracker, re-triage,
    cancel-restart, and the polling watchdog landed in Phase 3).
    """
    try:
        from concinno.time_steward import run_time_steward
        result = run_time_steward(
            agent_recent_turns=[user_prompt] if user_prompt else [],
            session_id=session_id or "unknown",
            turn_index=0,
        )
        if isinstance(result, dict):
            return result.get("inject") or None
        return None
    except Exception:
        return None
