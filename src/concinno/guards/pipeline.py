"""concinno.guards.pipeline — Guard execution engine.

@module pipeline
@responsibility Ordered guard chain: Security > Quality > Cognitive,
    short-circuit DENY, SECURITY fail-closed / QUALITY+COGNITIVE fail-open,
    health tracking, step-back
@dependencies concinno.guards.base
@exports GuardPipeline
"""

from __future__ import annotations

import dataclasses
import json
import os
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from concinno.equilibrium_breaker import EquilibriumBreaker

from concinno.guards.base import (
    BaseGuard,
    GuardAction,
    GuardCategory,
    GuardContext,
    GuardResult,
)


class GuardPipeline:
    """Ordered guard chain. First DENY wins. ALLOW collects context.

    Fail policy:
      SECURITY guards → fail-closed (crash = deny, secrets must not leak)
      QUALITY/COGNITIVE → fail-open (crash = allow, don't block dev workflow)

    Equilibrium Circuit Breaker:
      When deny pressure >= 5, QUALITY+COGNITIVE gates are suspended
      for 10 steps (SECURITY always on). Prevents deny storms.
    """

    def __init__(self, *, step_back_state_dir: str = "", max_failures: int = 5):
        self._guards: list[BaseGuard] = []
        self._health: dict[str, int] = {}  # name → consecutive failure count
        self._max_failures = max_failures
        self._step_back_dir = step_back_state_dir
        self._breaker: "EquilibriumBreaker | None" = None
        # Advisory results that were filtered out in competition mode.
        # Kept on the pipeline instance so tests + audit inspection can
        # assert on the routing decision without re-running guards.
        self._advisory_audit: list[GuardResult] = []

    # ── Advisory routing (competition mode silencer) ─────────

    @staticmethod
    def _active_profile() -> str:
        """Resolve the active profile. Fail-soft to ``standard``.

        Wrapped in a staticmethod (not a module-level call) so tests
        can monkeypatch ``GuardPipeline._active_profile`` directly.
        """
        try:
            from concinno.feature_config import get_active_profile
            return get_active_profile()
        except Exception:
            return "standard"

    def _should_silence(self, result: GuardResult) -> bool:
        """Return True if ``result`` is advisory AND profile silences it.

        Only ALLOW-action advisories are silenceable — DENY / REWRITE
        are structural decisions and bypass this filter entirely.
        """
        if result.action != GuardAction.ALLOW:
            return False
        if not getattr(result, "advisory", False):
            return False
        return self._active_profile() == "competition"

    def _collect_context(self, result: GuardResult, sink: list[str]) -> None:
        """Append ``result.context`` to *sink* unless it is silenced.

        Silenced advisories are appended to ``_advisory_audit`` so
        downstream audit tooling (and tests) can still observe them.
        """
        if not result.context:
            return
        if self._should_silence(result):
            self._advisory_audit.append(result)
            return
        sink.append(result.context)

    @property
    def advisory_audit(self) -> list[GuardResult]:
        """Read-only view of advisories silenced during this pipeline run.

        Cleared lazily — callers that care about per-invocation deltas
        should snapshot length before invoking the pipeline.
        """
        return list(self._advisory_audit)

    # ── Registration ──────────────────────────────────────────

    def register(self, guard: BaseGuard) -> "GuardPipeline":
        """Register a guard. Auto-sorts by category. Enforces BaseGuard."""
        if not isinstance(guard, BaseGuard):
            raise TypeError(
                f"Expected BaseGuard subclass, got {type(guard).__name__}. "
                f"All guards must inherit from BaseGuard."
            )
        if not guard.name:
            raise ValueError(
                f"{type(guard).__name__}.name is empty. "
                f"Every guard must have a unique name."
            )
        self._guards.append(guard)
        self._guards.sort(key=lambda g: g.category.value)
        return self

    # ── PreToolUse ────────────────────────────────────────────

    def _is_static_mode(self) -> bool:
        """Check if pipeline_mode is 'static' (skip all guards)."""
        try:
            from concinno.core.config import get_config
            cfg = get_config()
            return cfg.feature("pipeline_mode", "mode") == "static"
        except Exception:
            return False

    def run_pre_tool(self, ctx: GuardContext) -> dict:
        """Run all guards for PreToolUse. First DENY short-circuits.

        Returns Claude Code hook JSON dict:
          {"permissionDecision": "allow|deny", "reason": "...", "additionalContext": "..."}

        Equilibrium Circuit Breaker: when tripped, QUALITY+COGNITIVE guards
        are skipped. SECURITY always runs.
        """
        if self._is_static_mode():
            return {"permissionDecision": "allow"}

        # Record Read/Glob/Grep for ThinkingDepthGuard ratio window.
        # PostToolUse matcher typically excludes read tools, so we record here
        # at the pipeline level so every caller (CCC entry point + project-level
        # wrappers that invoke run_pre_tool directly) gets consistent tracking.
        # Without this, Read:Edit ratio stays 0R → false CRITICAL on every Edit.
        try:
            if ctx.tool_name in ("Read", "Glob", "Grep"):
                from concinno.thinking_depth_guard import ThinkingDepthGuard
                ThinkingDepthGuard().record(ctx)
        except Exception:
            pass

        breaker = self._get_breaker(ctx)
        if breaker:
            breaker.tick()  # once per tool-call, not per guard-check
        contexts: list[str] = []
        # Track rewrite chain: each REWRITE replaces ctx.tool_input and
        # contributes its reason so the user sees the full chain.
        pending_rewrite: dict | None = None
        rewrite_notes: list[str] = []

        for guard in self._guards:
            if self._is_disabled(guard.name):
                continue

            # Path-scope: skip guard if operating on non-matching path
            if not guard.matches_path_scope(ctx):
                continue

            # Equilibrium: skip non-SECURITY guards when breaker is tripped
            if breaker and breaker.should_skip(guard.category):
                continue

            try:
                result = guard.check(ctx)
                self._reset_health(guard.name)
            except Exception as exc:
                self._record_failure(guard.name)
                if guard.category == GuardCategory.SECURITY:
                    # SECURITY fail-closed: crash → deny (secrets/exfil must not leak)
                    print(
                        f"[concinno] SECURITY guard {guard.name} crashed: {exc}",
                        file=sys.stderr,
                    )
                    return GuardResult.deny(
                        reason=f"Security guard {guard.name} error — blocking for safety",
                    ).to_hook_dict()
                continue  # QUALITY/COGNITIVE fail-open: don't block user due to guard bug

            if result is None:
                if breaker:
                    breaker.record_allow()
                continue

            if result.action == GuardAction.DENY:
                if breaker:
                    breaker.record_deny()
                return self._handle_deny(guard, result, ctx)

            if result.action == GuardAction.REWRITE and result.updated_input:
                # Apply the rewrite in-place: replace ctx.tool_input and
                # let remaining guards run against the new version. This
                # means a later guard can DENY a rewritten call that
                # still looks dangerous — rewrite is not a trump card.
                pending_rewrite = dict(result.updated_input)
                ctx = dataclasses.replace(
                    ctx, tool_input=pending_rewrite,
                )
                if result.reason:
                    rewrite_notes.append(
                        f"↻ {guard.name}: {result.reason}",
                    )
                # REWRITE results are structural — not silenceable.
                if result.context:
                    contexts.append(result.context)
                if breaker:
                    breaker.record_allow()
                continue

            # ALLOW with context = knowledge injection.
            # Advisory results (CBUA / WIREDO / Read:Edit ratio) are
            # routed to audit log when the active profile is
            # "competition"; safety guards stay loud because their
            # results are not marked advisory.
            self._collect_context(result, contexts)

        # All guards passed → clear step-back state
        self._clear_step_back(ctx.session_id)

        # If one or more guards rewrote the input, emit it via the
        # hookSpecificOutput.updatedInput channel so Claude Code runs
        # the modified call. Otherwise return the classic allow dict.
        if pending_rewrite is not None:
            hso: dict = {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "updatedInput": pending_rewrite,
            }
            out: dict = {"hookSpecificOutput": hso}
            merged_notes = rewrite_notes + contexts
            if merged_notes:
                out["additionalContext"] = "\n".join(merged_notes)
            return out

        out = {"permissionDecision": "allow"}
        if contexts:
            out["additionalContext"] = "\n".join(contexts)
        return out

    # ── PostToolUse ───────────────────────────────────────────

    def run_post_tool(self, ctx: GuardContext) -> dict:
        """Run all guards' on_post_tool(). Collect context.

        Advisory results are filtered in competition mode via
        ``_collect_context``.
        """
        contexts: list[str] = []

        for guard in self._guards:
            if self._is_disabled(guard.name):
                continue
            try:
                result = guard.on_post_tool(ctx)
                self._reset_health(guard.name)
            except Exception:
                self._record_failure(guard.name)
                continue

            if result is not None:
                self._collect_context(result, contexts)

        if not contexts:
            return {}
        return {"additionalContext": "\n".join(contexts)}

    # ── Stop ──────────────────────────────────────────────────

    def run_stop(self, ctx: GuardContext) -> dict:
        """Run all guards' on_stop(). Collect context.

        Advisory results are filtered in competition mode via
        ``_collect_context``.
        """
        contexts: list[str] = []

        for guard in self._guards:
            if self._is_disabled(guard.name):
                continue
            try:
                result = guard.on_stop(ctx)
                self._reset_health(guard.name)
            except Exception:
                self._record_failure(guard.name)
                continue

            if result is not None:
                self._collect_context(result, contexts)

        if not contexts:
            return {}
        return {"additionalContext": "\n".join(contexts)}

    # ── Equilibrium Circuit Breaker ─────────────────────────

    def _get_breaker(self, ctx: GuardContext) -> EquilibriumBreaker | None:
        """Lazy-init the equilibrium circuit breaker."""
        if not ctx.cache_dir:
            return None
        if self._breaker is None:
            from concinno.core.state_store import StateStore
            from concinno.equilibrium_breaker import EquilibriumBreaker as _TB

            store = StateStore(ctx.cache_dir)
            self._breaker = _TB(store, ctx.session_id)
        return self._breaker

    # ── Deny Handling (step-back middleware) ───────────────────

    def _handle_deny(
        self, guard: BaseGuard, result: GuardResult, ctx: GuardContext
    ) -> dict:
        """Process a DENY result. Applies step-back if configured."""
        self._audit_deny(guard.name, result.reason, ctx)

        # Step-back: Quality guards with step_back_reason get two-tier buffer
        if guard.step_back_reason and self._step_back_dir:
            try:
                from concinno.step_back import wrap_gate

                wrapped = wrap_gate(
                    guard.name,
                    result.to_hook_dict(),
                    ctx.session_id,
                    self._step_back_dir,
                    reason=guard.step_back_reason,
                )
                if wrapped is not None:
                    self._emit_deny_stderr(guard.name, result.reason)
                    return wrapped
                # wrap_gate returned None = gate mode is "off"
                return {"permissionDecision": "allow"}
            except Exception as exc:
                print(
                    f"[concinno] step-back failed for {guard.name}: {exc}",
                    file=sys.stderr,
                )
                # fall through to hard deny

        self._emit_deny_stderr(guard.name, result.reason)
        return result.to_hook_dict()

    def _clear_step_back(self, session_id: str) -> None:
        """Clear step-back state when all guards pass."""
        if not self._step_back_dir or not session_id:
            return
        try:
            from concinno.step_back import clear_state

            clear_state(session_id, self._step_back_dir)
        except Exception:
            pass

    # ── Audit ─────────────────────────────────────────────────

    @staticmethod
    def _audit_deny(guard_name: str, reason: str, ctx: GuardContext) -> None:
        """Append guard deny event to audit JSONL for reporting."""
        from datetime import datetime, timezone

        project_dir = os.environ.get("CLAUDE_PROJECT_DIR", "")
        if not project_dir:
            return
        audit_dir = os.path.join(project_dir, ".concinno_cache", "audit")
        try:
            os.makedirs(audit_dir, exist_ok=True)
            entry = json.dumps({
                "guard": guard_name,
                "reason": reason[:200] if reason else "",
                "tool": ctx.tool_name,
                "ts": datetime.now(timezone.utc).isoformat(),
                "session": ctx.session_id[:8] if ctx.session_id else "",
            }, ensure_ascii=False)
            with open(
                os.path.join(audit_dir, "guard_denies.jsonl"),
                "a", encoding="utf-8",
            ) as f:
                f.write(entry + "\n")
        except Exception:
            pass

    # ── Health Tracking ───────────────────────────────────────

    def _is_disabled(self, name: str) -> bool:
        """Guard auto-disabled after max_failures consecutive crashes."""
        return self._health.get(name, 0) >= self._max_failures

    def _record_failure(self, name: str) -> None:
        prev = self._health.get(name, 0)
        self._health[name] = prev + 1
        if prev + 1 == self._max_failures:
            try:
                sys.stderr.write(
                    f"\033[93m⚠ [concinno] guard '{name}' disabled after "
                    f"{self._max_failures} consecutive failures\033[0m\n",
                )
                sys.stderr.flush()
            except Exception:
                pass

    def _reset_health(self, name: str) -> None:
        if name in self._health:
            del self._health[name]

    def load_health(self, path: str) -> None:
        """Load guard health state from disk."""
        if not path or not os.path.isfile(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self._health = {
                    k: v for k, v in data.items() if isinstance(v, int)
                }
        except Exception:
            pass

    def save_health(self, path: str) -> None:
        """Persist guard health state to disk."""
        if not path:
            return
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._health, f)
            os.replace(tmp, path)
        except Exception:
            pass

    # ── UX ────────────────────────────────────────────────────

    @staticmethod
    def _emit_deny_stderr(name: str, reason: str) -> None:
        """Red ANSI deny notification → stderr (user sees in VS Code)."""
        try:
            short = reason[:80] if reason else name
            sys.stderr.write(f"\033[91m✖ {name}: {short}\033[0m\n")
            sys.stderr.flush()
        except Exception:
            pass

    # ── Introspection ─────────────────────────────────────────

    def list_guards(self) -> dict[str, list[str]]:
        """Return guard names grouped by category."""
        result: dict[str, list[str]] = {}
        for guard in self._guards:
            cat = guard.category.name.lower()
            result.setdefault(cat, []).append(guard.name)
        return result

    @property
    def guard_count(self) -> int:
        return len(self._guards)

    def __repr__(self) -> str:
        cats = self.list_guards()
        parts = [f"{k}={len(v)}" for k, v in cats.items()]
        return f"<GuardPipeline {' '.join(parts)} total={self.guard_count}>"
