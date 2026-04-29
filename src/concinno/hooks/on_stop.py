#!/usr/bin/env python3
"""concinno Stop hook — async pipeline with per-module timeout + circuit breaker.

Fail-open: any crash -> exit silently. No module failure blocks others.

Architecture (F4):
- All modules run in parallel via asyncio.gather + asyncio.to_thread
- Each module has an independent timeout (default 10s)
- Circuit breaker: 3 consecutive failures -> skip module for 60s
- State persisted to ~/.claude/hook_circuit_state.json
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable

# ── Circuit Breaker ─────────────────────────────────────────

_CIRCUIT_STATE_PATH = os.path.join(
    os.path.expanduser("~"), ".claude", "hook_circuit_state.json"
)
_CIRCUIT_FAIL_THRESHOLD = 3
_CIRCUIT_COOLDOWN_S = 60.0


@dataclass
class _CircuitState:
    consecutive_failures: int = 0
    last_failure_ts: float = 0.0

    def is_open(self) -> bool:
        """True = skip this module (circuit is open/tripped)."""
        if self.consecutive_failures < _CIRCUIT_FAIL_THRESHOLD:
            return False
        return (time.time() - self.last_failure_ts) < _CIRCUIT_COOLDOWN_S

    def record_success(self) -> None:
        self.consecutive_failures = 0

    def record_failure(self) -> None:
        self.consecutive_failures += 1
        self.last_failure_ts = time.time()


def _load_circuit_states() -> dict[str, _CircuitState]:
    """Load circuit breaker state from disk."""
    if not os.path.isfile(_CIRCUIT_STATE_PATH):
        return {}
    try:
        with open(_CIRCUIT_STATE_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        states: dict[str, _CircuitState] = {}
        for name, data in raw.items():
            states[name] = _CircuitState(
                consecutive_failures=data.get("consecutive_failures", 0),
                last_failure_ts=data.get("last_failure_ts", 0.0),
            )
        return states
    except Exception:
        return {}


def _save_circuit_states(states: dict[str, _CircuitState]) -> None:
    """Persist circuit breaker state to disk."""
    try:
        os.makedirs(os.path.dirname(_CIRCUIT_STATE_PATH), exist_ok=True)
        data = {
            name: {
                "consecutive_failures": s.consecutive_failures,
                "last_failure_ts": s.last_failure_ts,
            }
            for name, s in states.items()
            if s.consecutive_failures > 0  # only persist non-zero
        }
        with open(_CIRCUIT_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception:
        pass


# ── Module Registry ─────────────────────────────────────────


@dataclass
class _StopModule:
    """A module in the stop pipeline."""

    name: str
    func: Callable[[], Any]
    timeout_s: float = 10.0
    # Runtime
    result: Any = field(default=None, repr=False)
    error: str = field(default="", repr=False)
    elapsed_ms: float = field(default=0.0, repr=False)
    skipped: bool = field(default=False, repr=False)
    timed_out: bool = field(default=False, repr=False)


# ── Async Runner ────────────────────────────────────────────


async def _run_module(
    mod: _StopModule,
    circuit: _CircuitState,
) -> None:
    """Run a single module with timeout + circuit breaker."""
    if circuit.is_open():
        mod.skipped = True
        return

    t0 = time.monotonic()
    try:
        mod.result = await asyncio.wait_for(
            asyncio.to_thread(mod.func),
            timeout=mod.timeout_s,
        )
        mod.elapsed_ms = (time.monotonic() - t0) * 1000
        circuit.record_success()
    except asyncio.TimeoutError:
        mod.elapsed_ms = (time.monotonic() - t0) * 1000
        mod.timed_out = True
        mod.error = f"timeout after {mod.timeout_s}s"
        circuit.record_failure()
    except Exception as exc:
        mod.elapsed_ms = (time.monotonic() - t0) * 1000
        mod.error = str(exc)[:200]
        circuit.record_failure()


async def _run_pipeline(modules: list[_StopModule]) -> list[_StopModule]:
    """Run all modules in parallel with independent timeouts."""
    states = _load_circuit_states()

    # Ensure every module has a circuit state
    for mod in modules:
        if mod.name not in states:
            states[mod.name] = _CircuitState()

    tasks = [
        _run_module(mod, states[mod.name])
        for mod in modules
    ]
    await asyncio.gather(*tasks, return_exceptions=True)

    _save_circuit_states(states)
    return modules


# ── Module Builders (lazy import) ───────────────────────────


def _build_knowledge(hook_data: dict) -> Callable[[], Any]:
    def _run() -> None:
        from concinno.knowledge import on_stop
        on_stop(hook_data)
    return _run


def _build_cognitive(hook_data: dict) -> Callable[[], Any]:
    def _run() -> None:
        from concinno.cognitive import on_stop as cog_stop
        cog_stop(hook_data)
    return _run


def _build_multi_instance() -> Callable[[], Any]:
    def _run() -> None:
        from concinno.multi_instance import release_lock
        release_lock()
    return _run


def _build_stop_guard(hook_data: dict) -> Callable[[], str | None]:
    def _run() -> str | None:
        from concinno.stop_guard import on_stop as sg_stop
        return sg_stop(hook_data)
    return _run


def _build_auto_delivery(hook_data: dict) -> Callable[[], str | None]:
    def _run() -> str | None:
        from concinno.core.config import get_config
        from concinno.delivery import auto_delivery_gate
        # ``/hook delivery_gate off`` skips the WIREDO auto-checker
        # so teams that don't want delivery nagging can opt out.
        if not get_config().feature("delivery_gate", "enabled"):
            return None
        sid = hook_data.get("session_id", "")
        return auto_delivery_gate(session_id=sid)
    return _run


def _build_wiredo_block(hook_data: dict) -> Callable[[], str | None]:
    """Multi-type WIREDO verification via ArtifactPipeline.

    Checks ALL asset types (code, image, video, audio, document),
    not just code. Blocks stop if any configured dimension fails.
    """
    def _run() -> str | None:
        from concinno.core.config import get_config
        from concinno.delivery.artifact_pipeline import ArtifactPipeline

        cfg = get_config()
        if not cfg.raw("wiredo", {}).get("enabled", True):
            return None

        project_dir = os.environ.get("CLAUDE_PROJECT_DIR", ".")
        cache_dir = os.path.join(project_dir, ".concinno_cache")
        sid = hook_data.get("session_id", "")

        block_dims = cfg.raw("wiredo", {}).get("block_dimensions")
        pipeline = ArtifactPipeline(
            cache_dir=cache_dir,
            session_id=sid,
            workspace=project_dir,
            block_dimensions=block_dims,
        )

        should_block, reason = pipeline.run_and_gate()
        if not should_block:
            return None

        return f"WIREDO_BLOCK:{reason}"
    return _run


def _build_mcp_cleanup() -> Callable[[], None]:
    def _run() -> None:
        _cleanup_mcp_servers()
    return _run


def _build_handoff_claim(hook_data: dict) -> Callable[[], str | None]:
    """Block stop when assistant claims handoff was written but git shows no change."""
    def _run() -> str | None:
        from concinno.handoff_claim_guard import on_stop as hcg_stop
        return hcg_stop(hook_data)
    return _run


def _build_handoff_required(hook_data: dict) -> Callable[[], str | None]:
    """Block stop when session has substantive work but no handoff file updated."""
    def _run() -> str | None:
        from concinno.handoff_required_guard import on_stop as hr_stop
        return hr_stop(hook_data)
    return _run


def _build_orphan_scan(hook_data: dict) -> Callable[[], None]:
    def _run() -> None:
        if not _has_active_delivery():
            return
        _orphan_scan(hook_data)
    return _run


def _build_session_summary(hook_data: dict) -> Callable[[], None]:
    def _run() -> None:
        _session_summary(hook_data)
    return _run


def _build_notify(hook_data: dict) -> Callable[[], None]:
    def _run() -> None:
        _notify_stop(hook_data)
    return _run


def _build_token_audit(hook_data: dict) -> Callable[[], None]:
    """Flush per-session token audit jsonl on Stop.

    Default-OFF per 4.0.0 opt-in. Honours
    ``token_audit_autopilot.enabled``. Best-effort — disk failures /
    feature off both no-op silently.
    """
    def _run() -> None:
        try:
            from concinno.core.config import get_config
            from concinno.observability.token_audit.audit import get_audit

            cfg = get_config()
            if not cfg.feature("token_audit_autopilot", "enabled"):
                return
            sid = hook_data.get("session_id", "")
            audit = get_audit(session_id=sid or None)
            audit.end_session()
        except Exception:
            return
    return _run


def _build_sweep_guard(hook_data: dict) -> Callable[[], str | None]:
    """Warn (or block) when .git residual state is present at stop.

    Residuals = interrupted git operations (rebase/merge/cherry-pick/
    revert/bisect) left unfinished. Silent inheritance by the next
    session is the typical bleed path. WARN by default; upgradable to
    BLOCK via ``feature_config.sweep_guard.block = true``.
    """
    def _run() -> str | None:
        from concinno.sweep_guard import on_stop as sweep_stop
        return sweep_stop(hook_data)
    return _run


# ── Main Entry Point ────────────────────────────────────────


_BLOCK_PREFIXES = {
    "stop_guard": "STOP_BLOCK:",
    "wiredo_block": "WIREDO_BLOCK:",
    "handoff_claim": "HANDOFF_CLAIM_BLOCK:",
    "handoff_required": "HANDOFF_REQUIRED_BLOCK:",
    "sweep_guard": "SWEEP_BLOCK:",
}

_BLOCK_REASONS = {
    "STOP_BLOCK": lambda reason: reason,
    "WIREDO_BLOCK": lambda dims: (
        f"WIREDO verification failed: {dims}. "
        f"Fix failed dimensions before stopping."
    ),
    "HANDOFF_CLAIM_BLOCK": lambda reason: reason,
    "HANDOFF_REQUIRED_BLOCK": lambda reason: reason,
    "SWEEP_BLOCK": lambda reason: reason,
}


def _check_block_decisions(modules: list[_StopModule]) -> None:
    """Check modules for block decisions and output to stdout if found."""
    for mod in modules:
        if mod.error or not mod.result:
            continue
        prefix = _BLOCK_PREFIXES.get(mod.name)
        if not prefix:
            continue
        result_str = str(mod.result)
        if not result_str.startswith(prefix):
            continue
        tag = prefix.rstrip(":")
        payload = result_str.split(":", 1)[1]
        reason = _BLOCK_REASONS[tag](payload)
        block_out = json.dumps(
            {"decision": "block", "reason": reason}, ensure_ascii=False,
        )
        sys.stdout.write(block_out)
        sys.stdout.flush()
        return  # First block wins


def _emit_stderr_outputs(modules: list[_StopModule]) -> None:
    """Print stderr outputs from modules that return info strings."""
    for mod in modules:
        if mod.error or mod.skipped or not mod.result:
            continue
        result_str = str(mod.result)
        # Skip block results (already handled by _check_block_decisions)
        if any(result_str.startswith(p) for p in _BLOCK_PREFIXES.values()):
            continue
        # Whitelist modules whose result is a user-facing status/warning.
        # auto_commit + its inline-squash log go here so silent git failures
        # (dirty tree, stale rebase-merge, lock contention, squash aborts)
        # actually surface — previously they went to /dev/null via
        # asyncio.to_thread, making "keep 3 commits" rule a no-op for months.
        if mod.name in (
            "stop_guard",
            "auto_delivery",
            "auto_commit",
            "inline_squash",
            "sweep_guard",
        ):
            print(result_str, file=sys.stderr)


def _should_skip_blocks(hook_data: dict) -> bool:
    """Decide whether to skip all block gates and only run cleanup modules.

    Skip blocks when:
    1. ``stop_hook_active=true`` — CC's standard signal that this is a retry
       after a previous block; per CC hook spec, hooks must NOT block again
       (otherwise /logout, /exit, and force-stop become impossible).
    2. ``CONCINNO_FORCE_STOP=1`` env var — user escape valve for manual
       override (e.g., `CONCINNO_FORCE_STOP=1 claude` then /logout).
    """
    if hook_data.get("stop_hook_active"):
        return True
    return os.environ.get("CONCINNO_FORCE_STOP") == "1"


def main(hook_data: dict | None = None) -> None:
    """Entry point — runs all stop modules in parallel."""
    try:
        if hook_data is None:
            hook_data = json.loads(sys.stdin.read())
    except Exception:
        return

    # Force-stop path: run cleanup only, skip all block gates.
    # This is what makes /logout work when WIREDO/sedimentation/handoff
    # gates would otherwise block session termination indefinitely.
    if _should_skip_blocks(hook_data):
        cleanup_only = [
            _StopModule("knowledge", _build_knowledge(hook_data), timeout_s=15.0),
            _StopModule("multi_instance", _build_multi_instance(), timeout_s=5.0),
            _StopModule("mcp_cleanup", _build_mcp_cleanup(), timeout_s=10.0),
            _StopModule("session_summary", _build_session_summary(hook_data), timeout_s=5.0),
            _StopModule("notify", _build_notify(hook_data), timeout_s=15.0),
        ]
        try:
            asyncio.run(_run_pipeline(cleanup_only))
        except Exception:
            pass
        return  # No block decisions emitted — CC may stop freely

    modules = [
        _StopModule("knowledge", _build_knowledge(hook_data), timeout_s=15.0),
        _StopModule("cognitive", _build_cognitive(hook_data), timeout_s=5.0),
        _StopModule("multi_instance", _build_multi_instance(), timeout_s=5.0),
        _StopModule("stop_guard", _build_stop_guard(hook_data), timeout_s=5.0),
        _StopModule("auto_delivery", _build_auto_delivery(hook_data), timeout_s=10.0),
        _StopModule("wiredo_block", _build_wiredo_block(hook_data), timeout_s=15.0),
        _StopModule("handoff_claim", _build_handoff_claim(hook_data), timeout_s=5.0),
        _StopModule("handoff_required", _build_handoff_required(hook_data), timeout_s=5.0),
        _StopModule("sweep_guard", _build_sweep_guard(hook_data), timeout_s=5.0),
        _StopModule("mcp_cleanup", _build_mcp_cleanup(), timeout_s=10.0),
        _StopModule("orphan_scan", _build_orphan_scan(hook_data), timeout_s=15.0),
        _StopModule("token_audit", _build_token_audit(hook_data), timeout_s=5.0),
        _StopModule("session_summary", _build_session_summary(hook_data), timeout_s=5.0),
        _StopModule("notify", _build_notify(hook_data), timeout_s=15.0),
    ]

    try:
        asyncio.run(_run_pipeline(modules))
    except Exception:
        _fallback_sequential(hook_data)
        return

    _check_block_decisions(modules)
    _emit_stderr_outputs(modules)

    # --- Optional: concinno-skills-memory lifecycle (0.2.0+) ---
    # Two-step: ``on_stop`` evaluates pending candidate snippets (default
    # defer = no auto-write), then ``on_session_end`` prunes stale rows
    # past the 90-day retention window. CC has no native ``SessionEnd``
    # hook event, so the prune step rides Stop with the standard
    # try/except wrapper. Either side raising ImportError or any runtime
    # error is silently swallowed so a hook subscriber bug cannot crash
    # the host pipeline.
    _memory_lifecycle_stop(hook_data)
    _memory_lifecycle_session_end(hook_data)

    # --- Optional: concinno-skills-session-search lifecycle (0.1.0+) ---
    # ``on_stop`` indexes this session's transcript text (best-effort
    # Haiku summary inside the sub-pkg). ``on_session_end`` rides the
    # same Stop event because CC has no native ``SessionEnd`` hook —
    # mirrors the concinno-skills-memory wiring above.
    _session_search_lifecycle_stop(hook_data)
    _session_search_lifecycle_session_end(hook_data)


def _session_search_lifecycle_stop(hook_data: dict) -> None:
    """Optional concinno-skills-session-search wiring; absence is silent.

    4.6.0 hybrid-capture: we resolve the CC transcript file path from
    ``session_id + cwd`` and pass it as ``transcript_path`` to the
    sub-pkg. The sub-pkg's ``on_stop`` enqueues a tiny pointer record
    instead of reading the (potentially multi-MB) transcript inline,
    keeping this hook hot-path-fast. The deferred read+index lands
    later via ``on_session_end`` drain or the explicit
    ``concinno-session-search drain`` CLI.
    """
    try:
        from concinno_skills_session_search import (
            transcript_resolver as _resolver,
        )
        from concinno_skills_session_search.lifecycle import (
            LifecycleContext as _SearchCtx,
        )
        from concinno_skills_session_search.lifecycle import (
            on_stop as _search_on_stop,
        )

        session_id = hook_data.get("session_id", "")
        cwd = hook_data.get("cwd") or None
        # ``resolve`` returns a Path or None. None gracefully falls
        # back to the legacy id-only noop inside the sub-pkg.
        resolved = _resolver.resolve(session_id, cwd=cwd) if session_id else None
        transcript_path = str(resolved) if resolved is not None else None
        _search_on_stop(
            _SearchCtx(
                session_id=session_id,
                transcript_path=transcript_path,
            )
        )
    except (ImportError, Exception):
        pass


def _session_search_lifecycle_session_end(hook_data: dict) -> None:
    """Optional concinno-skills-session-search retention prune; absence is silent."""
    try:
        from concinno_skills_session_search.lifecycle import (
            LifecycleContext as _SearchCtx,
        )
        from concinno_skills_session_search.lifecycle import (
            on_session_end as _search_on_session_end,
        )

        _search_on_session_end(
            _SearchCtx(session_id=hook_data.get("session_id", ""))
        )
    except (ImportError, Exception):
        pass


def _memory_lifecycle_stop(hook_data: dict) -> None:
    """Optional concinno-skills-memory wiring; absence is silent."""
    try:
        from concinno_skills_memory.lifecycle import (
            LifecycleContext as _MemoryCtx,
        )
        from concinno_skills_memory.lifecycle import (
            on_stop as _memory_on_stop,
        )

        _memory_on_stop(
            _MemoryCtx(session_id=hook_data.get("session_id", ""))
        )
    except (ImportError, Exception):
        pass


def _memory_lifecycle_session_end(hook_data: dict) -> None:
    """Optional concinno-skills-memory retention prune; absence is silent."""
    try:
        from concinno_skills_memory.lifecycle import (
            LifecycleContext as _MemoryCtx,
        )
        from concinno_skills_memory.lifecycle import (
            on_session_end as _memory_on_session_end,
        )

        _memory_on_session_end(
            _MemoryCtx(session_id=hook_data.get("session_id", ""))
        )
    except (ImportError, Exception):
        pass


def _fallback_sequential(hook_data: dict) -> None:
    """Emergency fallback: run critical modules sequentially (no asyncio).

    Includes stop_guard (may block stop) and knowledge (avoid knowledge loss)
    in addition to instance cleanup and notification.
    """
    # stop_guard first — it may output a block decision
    try:
        result = _build_stop_guard(hook_data)()
        if result and isinstance(result, str):
            for prefix, tag in _BLOCK_PREFIXES.items():
                if prefix == "stop_guard" and result.startswith(tag):
                    payload = result.split(":", 1)[1]
                    reason = _BLOCK_REASONS[tag.rstrip(":")](payload)
                    block_out = json.dumps(
                        {"decision": "block", "reason": reason},
                        ensure_ascii=False,
                    )
                    sys.stdout.write(block_out)
                    sys.stdout.flush()
                    return
    except Exception:
        pass

    for func in [
        lambda: _build_knowledge(hook_data)(),
        lambda: _build_multi_instance()(),
        lambda: _build_notify(hook_data)(),
    ]:
        try:
            func()
        except Exception:
            pass


# ── Pipeline Report (for debugging) ────────────────────────


def pipeline_report(modules: list[_StopModule]) -> str:
    """Generate human-readable pipeline execution report."""
    lines = ["on-stop pipeline:"]
    for mod in modules:
        if mod.skipped:
            status = "⏭ SKIPPED (circuit open)"
        elif mod.timed_out:
            status = f"⏱ TIMEOUT ({mod.timeout_s}s)"
        elif mod.error:
            status = f"❌ ERROR: {mod.error[:80]}"
        else:
            status = "✅"
        lines.append(f"  {mod.name}: {status} [{mod.elapsed_ms:.0f}ms]")
    return "\n".join(lines)


# ── Helpers (unchanged from original) ──────────────────────

_ORPHAN_SCAN_EXTENSIONS = {".ts", ".tsx", ".js", ".jsx", ".py"}
_ORPHAN_SCAN_MAX_FILES = 10


def _has_active_delivery() -> bool:
    """Check if there are active delivery gate tasks (worth running orphan scan)."""
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", ".")
    state_path = os.path.join(project_dir, ".concinno_cache", "delivery_state.json")
    if not os.path.isfile(state_path):
        return False
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return bool(data)  # non-empty = has active tasks
    except Exception:
        return False


def _get_git_changed_files(project_dir: str) -> list[str]:
    """Get source files changed in git (unstaged + staged), max 10."""
    import subprocess
    import sys as _sys

    _NO_WIN = 0x08000000 if _sys.platform == "win32" else 0
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "HEAD"],
            capture_output=True, text=True, cwd=project_dir,
            timeout=5, creationflags=_NO_WIN,
        )
        staged = subprocess.run(
            ["git", "diff", "--name-only", "--cached"],
            capture_output=True, text=True, cwd=project_dir,
            timeout=5, creationflags=_NO_WIN,
        )
        files = set(result.stdout.strip().splitlines())
        files.update(staged.stdout.strip().splitlines())
    except Exception:
        return []

    source_files = [
        f for f in files if os.path.splitext(f)[1] in _ORPHAN_SCAN_EXTENSIONS
    ]
    return source_files[:_ORPHAN_SCAN_MAX_FILES]


def _orphan_scan(hook_data: dict) -> None:
    """D8: Scan session-changed source files for orphan exports -> stderr."""
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", "")
    if not project_dir:
        return

    source_files = _get_git_changed_files(project_dir)
    if not source_files:
        return

    from concinno.delivery import scan_orphans_batch

    orphans = scan_orphans_batch(source_files, project_dir)
    if not orphans:
        return

    lines = [f"\033[93m⚠ [OrphanExport] {len(orphans)} orphan export(s):\033[0m"]
    for o in orphans[:10]:
        rel = os.path.relpath(o.file_path, project_dir)
        lines.append(f"  - {o.symbol} ({rel})")
    if len(orphans) > 10:
        lines.append(f"  ... and {len(orphans) - 10} more")
    lines.append("  → orphan exports = island code, unused by the system")
    sys.stderr.write("\n".join(lines) + "\n")


def _session_summary(hook_data: dict) -> None:
    """Output visual session summary to stderr (user-visible). <10ms.

    Respects ``/hook session_summary off`` — users who find the
    end-of-session recap noisy can silence it without touching hook
    registration.
    """
    session_id = hook_data.get("session_id", "")
    if not session_id:
        return

    try:
        from concinno.core.config import get_config
        if not get_config().feature("session_summary", "enabled"):
            return
    except Exception:  # noqa: BLE001 — fail-open
        pass

    streak = 0
    try:
        project_dir = os.environ.get("CLAUDE_PROJECT_DIR", ".")
        ux_path = os.path.join(project_dir, ".concinno_cache", "streak_ux.json")
        if os.path.isfile(ux_path):
            with open(ux_path, "r", encoding="utf-8") as f:
                ux = json.load(f)
            streak = ux.get("streak", 0)
    except Exception:
        pass

    try:
        from concinno.handoff_engine import generate_session_summary

        summary = generate_session_summary(session_id, streak=streak)
        if summary:
            sys.stderr.write(f"\n{summary}\n")
            sys.stderr.flush()
    except (ImportError, Exception):
        pass


def _resolve_session_info(
    session_id: str, project_dir: str,
) -> tuple[str, str]:
    """Resolve session name + task from instance lock or transcript."""
    from concinno.core.config import get_config
    brain_dir = get_config().brain_dir
    lock_path = os.path.join(
        project_dir, brain_dir, "cognition_shared", "instance_lock.json",
    )
    session_name = ""
    task = ""
    if os.path.isfile(lock_path):
        try:
            with open(lock_path, "r", encoding="utf-8") as f:
                lock = json.load(f)
            for name, s in lock.get("sessions", {}).items():
                sid = s.get("session_id", "")
                if sid == session_id or not session_id:
                    session_name = name
                    task = s.get("task", "")
                    break
        except Exception:
            pass

    if not task:
        try:
            from concinno.core.notify import extract_first_user_message
            home = os.path.expanduser("~")
            proj_dir = os.path.join(home, ".claude", "projects")
            for d in os.listdir(proj_dir):
                t_path = os.path.join(proj_dir, d, f"{session_id}.jsonl")
                if os.path.isfile(t_path):
                    task = extract_first_user_message(t_path, 40)
                    break
        except Exception:
            pass

    return session_name or "Session", task


def _notify_stop(hook_data: dict) -> None:
    """Show toast: session name + task + git status, locale-aware.

    Toast-first pipeline (2.14.0 fix): the 2-line core (session + task +
    response_ready) fires immediately in a daemon thread so the banner
    always appears within ~100ms regardless of how long git_assist takes.
    Once auto_commit + generate_report finish, a second toast with the
    same tag+group *replaces* the first with the full 5-line spec.

    Before this split, git operations (auto_commit on a large working
    tree, generate_report on a stale index) routinely pushed the entire
    notify module past its 5s async timeout, tripping the circuit breaker
    after 3 consecutive failures — the user-visible symptom was "hook
    fires but no banner appears" even though the toast code itself was
    fine. See MEMORY #70 (feedback_toast_3opus_triangulation) +
    feedback_toast_module_timeout.md.
    """
    import threading

    from concinno.core.notify import _get_locale, _t, show_toast

    locale = _get_locale()
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", "")
    session_id = hook_data.get("session_id", "")

    line1, task = _resolve_session_info(session_id, project_dir)
    line2 = f"{task[:40]} — {_t('response_ready')}" if task else _t("response_ready")

    def _fire(body: str) -> None:
        try:
            show_toast(
                "Claude Code", body,
                enabled=True, tag="claude-stop", group="claude-code",
            )
        except Exception:
            pass

    def _git_then_full_toast() -> None:
        """Compute git info then fire the 5-line replacement toast.

        Runs off the pipeline's critical path so ``auto_commit`` /
        ``generate_report`` delays never trip the notify module's async
        timeout. Same tag+group as the core toast → Windows replaces the
        earlier 2-line banner with this full version.
        """
        git_lines: list[str] = []
        try:
            from concinno.git_assist import auto_commit, generate_report
            committed = auto_commit(cwd=project_dir or None)
            if committed:
                git_lines.append(f"✅ {committed}")
            report = generate_report(cwd=project_dir or None, locale=locale)
            if report:
                git_lines.extend(report.splitlines()[:2])
        except Exception:
            return
        if git_lines:
            full_body = f"{line1}\n{line2}\n" + "\n".join(git_lines)
            _fire(full_body)

    # Fire the core 2-line toast immediately — user sees it within ~100ms.
    core_body = f"{line1}\n{line2}"
    threading.Thread(
        target=_fire, args=(core_body,),
        name="concinno-stop-toast-core", daemon=True,
    ).start()

    # Off-load git_assist (slow, can exceed any module timeout in a
    # rebase-stuck state) onto a second daemon thread. _notify_stop
    # itself returns in <200 ms so the notify module never times out.
    threading.Thread(
        target=_git_then_full_toast,
        name="concinno-stop-toast-git", daemon=True,
    ).start()


def _find_claude_cli_pid(proc_map: dict[int, dict]) -> int:
    """Walk up from current PID to find the ancestor claude.exe."""
    current = os.getpid()
    for _ in range(10):
        info = proc_map.get(current)
        if not info:
            break
        name = info.get("name", "").lower()
        if name in ("claude", "claude.exe"):
            return current
        ppid = info.get("ppid", 0)
        if ppid <= 0 or ppid == current:
            break
        current = ppid
    return 0


def _kill_mcp_children(cli_pid: int, all_procs: list, proc_map: dict) -> int:
    """Find and kill MCP server children of the CLI process."""
    import re
    import subprocess as _sp

    _NO_WIN = 0x08000000 if sys.platform == "win32" else 0
    from concinno.process_guard import _get_child_tree

    children = _get_child_tree(cli_pid, all_procs)
    mcp_pattern = re.compile(r"mcp[_\-]?server", re.IGNORECASE)
    killed = 0

    for child_pid in children:
        child_info = proc_map.get(child_pid, {})
        child_name = child_info.get("name", "").lower()
        cmdline = child_info.get("cmdline", "")

        if child_name not in ("python", "python.exe", "python3", "python3.exe"):
            continue
        if not mcp_pattern.search(cmdline):
            continue

        try:
            if sys.platform == "win32":
                _sp.run(
                    ["taskkill", "/F", "/T", "/PID", str(child_pid)],
                    capture_output=True, timeout=5, creationflags=_NO_WIN,
                )
            else:
                os.kill(child_pid, 15)  # SIGTERM
            killed += 1
        except Exception:
            pass
    return killed


def _cleanup_mcp_servers() -> None:
    """Kill MCP server processes spawned by this session's parent claude.exe."""
    try:
        from concinno.process_guard import _get_all_processes
    except ImportError:
        return

    all_procs = _get_all_processes()
    proc_map = {p["pid"]: p for p in all_procs}
    cli_pid = _find_claude_cli_pid(proc_map)
    if not cli_pid:
        return

    killed = _kill_mcp_children(cli_pid, all_procs, proc_map)
    if killed:
        sys.stderr.write(
            f"\033[90m[mcp_cleanup] terminated {killed} MCP server(s)\033[0m\n"
        )


if __name__ == "__main__":
    main()
