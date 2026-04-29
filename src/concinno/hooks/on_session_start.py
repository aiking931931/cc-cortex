#!/usr/bin/env python3
"""concinno SessionStart hook — session init, zombie cleanup.

Fail-open: any crash -> exit silently.
"""

from __future__ import annotations

import json
import sys


def main(hook_data: dict | None = None) -> None:
    """Entry point."""
    try:
        if hook_data is None:
            hook_data = json.loads(sys.stdin.read())
    except Exception:
        return

    # --- Module: multi_instance (acquire lock + zombie GC) ---
    try:
        from concinno.multi_instance import acquire_lock

        acquire_lock()
    except (ImportError, Exception):
        pass

    # --- Module: 軌 B 件 1 dedup_layer — clear cache at session boundary
    # New session id => the previous session's dedup state must not
    # leak forward (otherwise the first identical warning would still
    # be silently dropped). Best-effort; never blocks session start.
    try:
        from concinno.hooks.dedup_layer import clear_session

        sid = (hook_data or {}).get("session_id", "")
        if sid:
            clear_session(sid)
    except (ImportError, Exception):
        pass

    # --- Module: token_audit_autopilot (begin per-session audit) ---
    # Default-OFF per 4.0.0 opt-in policy. Honours feature flag.
    try:
        from concinno.core.config import get_config
        from concinno.observability.token_audit.audit import get_audit

        cfg = get_config()
        if cfg.feature("token_audit_autopilot", "enabled"):
            sid = (hook_data or {}).get("session_id", "")
            audit = get_audit(session_id=sid or None, enabled=True)
            audit.begin_session()
        else:
            # Force singleton into disabled mode so any in-session
            # record_* call is a cheap no-op.
            get_audit(enabled=False)
    except (ImportError, Exception):
        pass

    # --- Module: cognitive (start session profiling) ---
    try:
        from concinno.cognitive import on_session_start

        on_session_start(hook_data)
    except (ImportError, Exception):
        pass

    # --- Module: git_assist (ensure git repo exists) ---
    try:
        from concinno.git_assist import ensure_git_repo

        result = ensure_git_repo()
        if result.get("initialized"):
            print(
                "🔀 Git repo initialized by CC Cortex. "
                "Auto-commit enabled for session backups.",
                file=sys.stderr,
            )
        elif result.get("error"):
            print(
                f"⚠ Git init skipped: {result['error']}",
                file=sys.stderr,
            )
    except (ImportError, Exception):
        pass

    # --- Module: notify_health (reset Win11 AUMID counter) ---
    # Win11 suppresses toast banners after ~3 notifications per 24h per
    # AUMID (PeriodicNotificationCount threshold). Reset on every session
    # start so banners always work when the user actually starts working.
    # Root-causes "通知又沒了" regression (kb_notify_health Mode 1).
    try:
        from concinno.notify_health import auto_reset_on_session_start

        auto_reset_on_session_start(verbose=True)
    except (ImportError, Exception):
        pass

    # --- Module: auto_update.tier1_registry (refresh plugin caches) ---
    # Cheap registry refresh (entry-points re-scan) on SessionStart.
    # Hard 300ms budget; fail-soft. Digest-hit fast path means 99% of
    # session starts are sub-50ms once the cache is warm.
    # See ``concinno.auto_update.tier1_registry`` for the full spec.
    try:
        from concinno.auto_update import refresh_tier1_registry

        result = refresh_tier1_registry(timeout_ms=300)
        if result.error:
            sys.stderr.write(
                f"concinno: tier1 refresh error (non-fatal): {result.error}\n"
            )
        elif result.timed_out and not result.digest_hit:
            sys.stderr.write(
                "concinno: tier1 refresh timed out at "
                f"{result.elapsed_ms:.0f}ms (300ms budget)\n"
            )
        elif not result.digest_hit:
            sys.stderr.write(
                f"concinno: tier1 refreshed {result.skills_count} skills + "
                f"{result.features_count} features in "
                f"{result.elapsed_ms:.0f}ms\n"
            )
    except (ImportError, Exception):
        pass

    # --- Module: skill_tier1_mount (auto-mount Tier1 skills) ---
    # Surface the curated Tier1 skill list (default 10) into the agent's
    # primacy window so high-value Skills (memoria / kb_handoff /
    # claude-api / awareness / ...) do not depend on the user typing the
    # exact slash command. Hard 500 ms wall-clock budget; over-budget =
    # silent skip. Honours ``CONCINNO_SKILL_TIER1_MOUNT_DISABLED``.
    _emit_tier1_mount()

    # --- narrower-scope v4: inject active preset into agent context ---
    _emit_active_preset()

    # --- Optional: concinno-skills-memory lifecycle (0.2.0+) ---
    _memory_lifecycle_session_start(hook_data)

    # --- Optional: concinno-skills-session-search lifecycle (0.1.0+) ---
    _session_search_lifecycle_session_start(hook_data)


def _memory_lifecycle_session_start(hook_data: dict | None) -> None:
    """Optional concinno-skills-memory wiring; absence is silent."""
    try:
        from concinno_skills_memory.lifecycle import (
            LifecycleContext as _MemoryCtx,
        )
        from concinno_skills_memory.lifecycle import (
            on_session_start as _memory_on_session_start,
        )

        _memory_on_session_start(
            _MemoryCtx(session_id=(hook_data or {}).get("session_id", ""))
        )
    except (ImportError, Exception):
        pass


def _session_search_lifecycle_session_start(hook_data: dict | None) -> None:
    """Optional concinno-skills-session-search wiring; absence is silent.

    SessionStart hook is read-only for cross-session search — the agent
    queries the index on demand later. We still call ``on_session_start``
    so the sub-pkg has a consistent five-hook protocol shape and any
    future warm-up (priming, capability check) lands without another
    wiring patch in concinno main.
    """
    try:
        from concinno_skills_session_search.lifecycle import (
            LifecycleContext as _SearchCtx,
        )
        from concinno_skills_session_search.lifecycle import (
            on_session_start as _search_on_session_start,
        )

        _search_on_session_start(
            _SearchCtx(session_id=(hook_data or {}).get("session_id", ""))
        )
    except (ImportError, Exception):
        pass


def _emit_tier1_mount() -> None:
    """Emit Tier1 skill mount block as ``hookSpecificOutput.additionalContext``.

    Concinno 4.4.0 task #5: SessionStart auto-mount. The mount module
    runs under a hard 500 ms wall-clock budget and never raises; this
    wrapper handles the additional ``json`` framing for the hook
    contract and tags the output with the same ``hookEventName`` as
    :func:`_emit_active_preset`.

    Multiple SessionStart hook emits are concatenated by CC, so this
    call sites alongside ``_emit_active_preset`` without coordination.
    """
    try:
        from concinno.skill_tier1_mount import mount_tier1_skills

        result = mount_tier1_skills()
    except Exception:
        return
    try:
        if result.error:
            sys.stderr.write(
                f"concinno: tier1 mount error (non-fatal): {result.error}\n"
            )
            return
        if result.timed_out:
            sys.stderr.write(
                "concinno: tier1 mount timed out at "
                f"{result.elapsed_ms:.0f}ms (500ms budget)\n"
            )
        if not result.additional_context:
            return
        payload = {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": result.additional_context,
            },
        }
        sys.stdout.write(json.dumps(payload, ensure_ascii=False))
    except Exception:
        pass


def _emit_active_preset() -> None:
    """Emit active v4 preset name as hookSpecificOutput.additionalContext.

    The agent reads this on its first turn so primacy-bias can't drop
    the user's cascade choice (MEMORY #71 Switch-First directive).
    Fail-soft: any error -> silent, the hook contract stays intact.
    """
    try:
        from concinno.preset_cascade import get_active_preset

        name = get_active_preset()
    except Exception:
        return
    try:
        payload = {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": f"concinno: active_preset={name}\n",
            },
        }
        sys.stdout.write(json.dumps(payload, ensure_ascii=False))
    except Exception:
        pass


if __name__ == "__main__":
    main()
