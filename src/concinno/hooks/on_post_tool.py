#!/usr/bin/env python3
"""concinno PostToolUse hook — Guard Pipeline + streak UX + token monitor.

Output: JSON {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "..."}}

Architecture (mirrors on_pre_tool.py):
  1. Guard Pipeline  — run_post_tool() executes all registered PostToolUse guards
     (AgentArtifactGuard, CodeGuard, LintGuard, StructuralGuard, SSOTGuard,
      HandoffGuard, EquilibriumGuard, DeliveryGuard, DesignTheoryGuard,
      WiredoEnforcementGuard) with health tracking + circuit breaker
  2. Streak UX       — clean edit streak tracking (reads pipeline output for errors)
  3. Knowledge       — learning loop (corrections → kb)
  4. Cognitive       — adaptive thresholds
  5. Token monitor   — real API usage monitoring
  6. Sentinel        — tool outcome recording

Three-tier classification prevents model habituation:
  CRITICAL  — errors/fixes: always relay with [SHOW USER VERBATIM]
  MILESTONE — streak fire: only at intervals (every N)
  INFO      — pass through as context
"""

from __future__ import annotations

import json
import os
import re
import sys

from concinno.constants import WRITE_TOOLS_EXT as _WRITE_TOOLS
from concinno.hooks.io_utils import cache_path, get_project_dir

_MILESTONE_INTERVAL = 5

# ── Paths (shared with on_pre_tool.py pattern) ──────────

_WORKSPACE = get_project_dir()
_CACHE_DIR = cache_path() if _WORKSPACE else ""
_HEALTH_PATH = cache_path("guard_health.json") if _CACHE_DIR else ""
_STEP_BACK_DIR = _CACHE_DIR

# ── Streak UX State ──────────────────────────────────────

_UX_STATE_FILE = cache_path("streak_ux.json")

_MAX_ERRORS = 50


def _norm_path(p: str) -> str:
    """Normalize path for consistent dict key (lowercase + forward slash)."""
    return p.lower().replace("\\", "/") if p else ""


def _resolve_session_id(session_id: str = "") -> str:
    """Resolve session ID from explicit arg, env var, or fallback."""
    sid = (
        session_id
        or os.environ.get("CLAUDE_SESSION_ID", "")
        or os.environ.get("CC_SESSION_ID", "")
        or "unknown"
    )
    return sid[:12]


def _load_ux(session_id: str = "") -> dict:
    sid = _resolve_session_id(session_id)
    try:
        with open(_UX_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("session_id") != sid:
            data["streak"] = 0
            data["session_id"] = sid
        raw_errors = data.get("errors", {})
        normed: dict[str, int] = {}
        for k, v in raw_errors.items():
            nk = _norm_path(k)
            normed[nk] = v
        if len(normed) > _MAX_ERRORS:
            normed = dict(list(normed.items())[-_MAX_ERRORS:])
        data["errors"] = normed
        return data
    except Exception:
        return {"errors": {}, "streak": 0, "session_id": sid}


def _save_ux(state: dict):
    try:
        d = os.path.dirname(_UX_STATE_FILE)
        os.makedirs(d, exist_ok=True)
        tmp = _UX_STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp, _UX_STATE_FILE)
    except Exception:
        pass


# ── Three-tier Classification ────────────────────────────


def _classify(line: str) -> str:
    low = line.lower()
    if "\u2705" in line:  # ✅
        return "CRITICAL"
    from concinno.i18n import patterns as i18n_patterns
    err_kw = i18n_patterns("classify_error_keywords") or ["error", "warning", "fix"]
    if any(k in low for k in err_kw) or any(c in line for c in ("⚠", "❌")):
        return "CRITICAL"
    if "\U0001f525" in line:  # 🔥
        return "MILESTONE"
    return "INFO"


def _extract_streak_count(line: str) -> int:
    m = re.search(r"x(\d+)", line)
    return int(m.group(1)) if m else 0


def _emit_stderr(msg: str) -> None:
    """Write UX message to stderr so the user sees it directly."""
    try:
        if hasattr(sys.stderr, "buffer"):
            sys.stderr.buffer.write(f"  {msg}\n".encode("utf-8"))
            sys.stderr.buffer.flush()
        else:
            print(f"  {msg}", file=sys.stderr)
    except Exception:
        pass


def _is_token_display_enabled() -> bool:
    try:
        from concinno.core.config import get_config
        return bool(get_config().feature("token_display"))
    except (ImportError, Exception):
        return True


def _stderr_summary(line: str) -> str:
    first = line.split("\n")[0]
    return first[:160]


def _throttle(lines: list[str], session_id: str = "") -> list[str]:
    token_suffix = ""
    if _is_token_display_enabled():
        token_info = _get_token_info(session_id)
        if token_info is not None:
            token_suffix = " | " + _format_token_suffix(token_info)
            _record_cost_tick(session_id, token_info)

    output = []
    for line in lines:
        level = _classify(line)
        if level == "CRITICAL":
            _emit_stderr(_stderr_summary(line) + token_suffix)
            output.append(f"[SHOW USER VERBATIM] {line}{token_suffix}")
            output.append(
                "⚠ 蝴蝶效應鐵律：完成當前子任務後，"
                "立即回頭修復上述問題（含 pre-existing）。"
                "不修不能開始下一個任務。"
            )
        elif level == "MILESTONE":
            count = _extract_streak_count(line)
            is_named = count in (25, 50, 100)
            is_interval = count > 0 and count % _MILESTONE_INTERVAL == 0
            if is_named or is_interval:
                display = f"{line}{token_suffix}"
                _emit_stderr(display)
                output.append(f"[SHOW USER VERBATIM] {display}")
        else:
            output.append(line)
    return output


# ── Streak UX ────────────────────────────────────────────


def _get_streak_config() -> dict:
    try:
        from concinno.core.config import get_config
        return get_config().raw("streak_ux", {})
    except Exception:
        return {}


def _extract_error_count(lint_msg: str | None) -> int:
    if not lint_msg:
        return 1
    m = re.search(r"(\d+)\s+(?:issues?|errors?)", lint_msg)
    return int(m.group(1)) if m else 1


def _token_state_path(session_id: str) -> str:
    """Return the per-session token-state JSON path, or '' if unavailable."""
    if not session_id:
        return ""
    state_dir = cache_path("token_state")
    if not state_dir:
        return ""
    return os.path.join(state_dir, f"{session_id[:8]}.json")


def _read_token_state(state_path: str) -> dict:
    """Load persisted token state (prior est_tokens + last_warned). Safe on failure."""
    if not state_path or not os.path.isfile(state_path):
        return {}
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _write_token_state(state_path: str, *, est_tokens: int, last_warned: int) -> None:
    """Persist current reading. Preserves last_warned to avoid resetting dedup."""
    if not state_path:
        return
    try:
        os.makedirs(os.path.dirname(state_path), exist_ok=True)
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump({"last_warned": last_warned, "est_tokens": est_tokens}, f)
    except Exception:
        pass


def _resolve_transcript_path(session_id: str) -> tuple[str, str]:
    """Return (path, source) where source ∈ {'session','latest',''}."""
    try:
        from concinno.core.path_utils import (
            find_latest_transcript,
            find_transcript,
        )
    except Exception:
        return "", ""
    if session_id:
        p = find_transcript(session_id)
        if p:
            return p, "session"
    p = find_latest_transcript()
    return (p, "latest") if p else ("", "")


def _get_token_info(session_id: str = "") -> dict | None:
    """Robust token usage reader — session-aware, delta-tracking, compact-detecting.

    Resolution priority for transcript lookup:
      1. find_transcript(session_id) — exact session match (no guessing)
      2. find_latest_transcript() — mtime fallback when session_id missing

    Delta tracking: compares context_tokens against the previous value
    persisted under ``token_state/<session_id_prefix>.json`` and detects
    autocompact events (drop > 30K from prior record).

    Returns dict or None:
        context_k / context / delta_k / autocompact / source
    """
    try:
        from concinno.token_monitor import read_real_token_usage
    except Exception:
        return None

    path, source = _resolve_transcript_path(session_id)
    if not path:
        return None
    usage = read_real_token_usage(path)
    if not usage:
        return None
    context = usage["context_tokens"]

    state_path = _token_state_path(session_id)
    prior_state = _read_token_state(state_path)
    prior = int(prior_state.get("est_tokens", 0) or 0)
    last_warned = int(prior_state.get("last_warned", 0) or 0)

    delta = context - prior if prior > 0 else 0
    delta_k = delta // 1000
    autocompact = prior > 50_000 and delta < -30_000

    _write_token_state(state_path, est_tokens=context, last_warned=last_warned)

    return {
        "context_k": context // 1000,
        "context": context,
        "delta_k": delta_k,
        "autocompact": autocompact,
        "source": source,
        # Raw fields for downstream cost tracking — context is the cumulative
        # input-side token count, output_tokens is the last assistant turn.
        "input_total": context,
        "output_total": int(usage.get("output_tokens", 0) or 0),
    }


def _record_cost_tick(session_id: str, info: dict) -> None:
    """Feed the latest token snapshot into CostTracker for this session.

    Pure side-effect: silently no-ops if the cache dir is unavailable or
    CostTracker fails to import. Called once per PostToolUse tick.
    """
    if not _CACHE_DIR or not session_id:
        return
    try:
        from concinno.cost_tracker import CostTracker

        tracker = CostTracker(
            cache_dir=_CACHE_DIR,
            session_id=session_id,
        )
        tracker.update_snapshot(
            cumulative_input=int(info.get("input_total", 0)),
            cumulative_output=int(info.get("output_total", 0)),
        )
    except Exception:
        # Cost tracking must never break the hook
        pass


def _format_token_suffix(info: dict) -> str:
    """Render token info as a compact status suffix.

    Examples:
        "ctx 280k"                    (no prior baseline)
        "ctx 285k Δ+5k"               (normal growth)
        "ctx 120k Δ-160k ↓COMPACT"   (autocompact detected)
        "ctx 280k [latest]"           (session_id fallback to mtime)
    """
    parts = [f"ctx {info['context_k']}k"]
    delta_k = info.get("delta_k", 0)
    if delta_k != 0:
        sign = "+" if delta_k > 0 else ""
        parts.append(f"Δ{sign}{delta_k}k")
    if info.get("autocompact"):
        parts.append("↓COMPACT")
    if info.get("source") == "latest":
        parts.append("[latest]")
    return " ".join(parts)


# Backward-compat shim — old callers may import _get_token_k
def _get_token_k(session_id: str = "") -> int | None:
    info = _get_token_info(session_id)
    return info["context_k"] if info else None


def _streak_fix_msg(fname: str, streak: int, prev_errors: int) -> str:
    """Format a 'file fixed' streak message."""
    cfg = _get_streak_config()
    fmt = cfg.get(
        "fix_fmt",
        "✅ {fname} fixed {fixed}/{total} | 🔥x{streak} clean edits",
    )
    return fmt.format(
        fname=fname, streak=streak,
        fixed_label="fixed", fixed=prev_errors, total=prev_errors,
    )


def _streak_milestone_msg(streak: int) -> str | None:
    """Format a milestone streak message, or None if not at milestone."""
    cfg = _get_streak_config()
    interval = int(cfg.get("milestone_interval", _MILESTONE_INTERVAL))
    if streak > 0 and streak % interval == 0:
        fmt = cfg.get("streak_fmt", "🔥x{streak} clean edits")
        return fmt.format(streak=streak, label="", stats="").strip(" |")
    return None


def _build_streak_msg(
    has_errors: bool, file_path: str, lint_msg: str | None = None,
    *, session_id: str = "",
) -> str | None:
    """Track streak and generate UX message."""
    ux = _load_ux(session_id)
    fname = os.path.basename(file_path) if file_path else ""
    norm = _norm_path(file_path)

    if has_errors:
        if norm:
            ux.setdefault("errors", {})[norm] = _extract_error_count(lint_msg)
        ux["streak"] = 0
        _save_ux(ux)
        return None

    prev_errors = ux.get("errors", {}).get(norm, 0) if norm else 0
    ux["streak"] = ux.get("streak", 0) + 1
    if prev_errors > 0 and norm:
        ux["errors"].pop(norm, None)
    _save_ux(ux)
    streak = ux["streak"]

    if prev_errors > 0 and norm:
        return _streak_fix_msg(fname, streak, prev_errors)
    return _streak_milestone_msg(streak)


# ── Token Warning i18n ────────────────────────────────────

_TOKEN_ICONS = {160_000: "🚨", 140_000: "⚠️", 100_000: "📊"}
_TOKEN_MSG_KEYS = {
    160_000: "token_warning.160000",
    140_000: "token_warning.140000",
    100_000: "token_warning.100000",
}


def _token_thresholds(lang: str = "en") -> list[tuple]:
    """Model-aware token thresholds from CHECKPOINT_THRESHOLDS."""
    from concinno.i18n import msg
    try:
        from concinno.token_zone import CHECKPOINT_THRESHOLDS, _parse_model_key, detect_model
        profile = detect_model()
        model_key = _parse_model_key(profile.get("display", "opus"))
        checkpoints = CHECKPOINT_THRESHOLDS.get(model_key, CHECKPOINT_THRESHOLDS["opus"])
        # Use C3 (quality_boundary), C2 (mid), C1 (early)
        # Map to existing 160K/140K/100K message keys
        thresholds = []
        msg_tpl = msg  # alias for line length
        k_max = "{k}", "{max}"
        if len(checkpoints) >= 3:
            t = checkpoints[2][0]
            m = msg_tpl(_TOKEN_MSG_KEYS[160_000], k=k_max[0], max=k_max[1])
            thresholds.append((t, "🚨", m, False))
        if len(checkpoints) >= 2:
            t = checkpoints[1][0]
            m = msg_tpl(_TOKEN_MSG_KEYS[140_000], k=k_max[0], max=k_max[1])
            thresholds.append((t, "⚠️", m, False))
        if len(checkpoints) >= 1:
            t = checkpoints[0][0]
            m = msg_tpl(_TOKEN_MSG_KEYS[100_000], k=k_max[0], max=k_max[1])
            thresholds.append((t, "📊", m, False))
        return thresholds
    except Exception:
        # Fallback to legacy hardcoded values
        return [
            (160_000, "🚨", msg(_TOKEN_MSG_KEYS[160_000], k="{k}", max="{max}"), False),
            (140_000, "⚠️", msg(_TOKEN_MSG_KEYS[140_000], k="{k}", max="{max}"), False),
            (100_000, "📊", msg(_TOKEN_MSG_KEYS[100_000], k="{k}", max="{max}"), False),
        ]


def _format_token_warning(result: dict, lang: str = "en") -> str:
    from concinno.i18n import msg
    threshold = result["threshold"]
    est_k = result["est_k"]
    icon = _TOKEN_ICONS.get(threshold, "📊")
    key = _TOKEN_MSG_KEYS.get(threshold, "token_warning.100000")
    # Use model-aware quality_zone as denominator
    try:
        from concinno.token_zone import detect_model
        profile = detect_model()
        max_k = profile["quality_zone"] // 1000
    except Exception:
        max_k = 200
    return icon + " " + msg(key, k=est_k, max=max_k)


# ── Pipeline ─────────────────────────────────────────────


def _run_pipeline(hook_data: dict) -> str:
    """Run GuardPipeline.run_post_tool() with health tracking. Returns context."""
    try:
        from concinno.guards.base import GuardContext
        from concinno.guards.registry import create_default_pipeline

        ctx = GuardContext.from_hook_data(hook_data)
        pipe = create_default_pipeline(step_back_state_dir=_STEP_BACK_DIR)
        if _HEALTH_PATH:
            pipe.load_health(_HEALTH_PATH)
        pipe_result = pipe.run_post_tool(ctx)
        if _HEALTH_PATH:
            pipe.save_health(_HEALTH_PATH)
        return pipe_result.get("additionalContext", "")
    except Exception:
        return ""


# ── Module-level post-tool calls ─────────────────────────


def _run_knowledge(hook_data: dict) -> str | None:
    """Knowledge learning loop (no guard, module function)."""
    try:
        from concinno.knowledge import on_post_tool as know_post
        return know_post(hook_data)
    except (ImportError, Exception):
        return None


def _run_cognitive(hook_data: dict) -> str | None:
    """Cognitive adaptive thresholds (module function)."""
    try:
        from concinno.cognitive import on_post_tool as cog_post
        return cog_post(hook_data)
    except (ImportError, Exception):
        return None


def _find_transcript(session_id: str) -> str:
    """Delegate to unified transcript lookup in core.path_utils."""
    from concinno.core.path_utils import find_transcript
    return find_transcript(session_id)


def _get_handoff_mode() -> str:
    """Read current handoff mode (full/phase/save-token)."""
    try:
        from concinno.handoff_engine import get_handoff_mode
        return get_handoff_mode()
    except (ImportError, Exception):
        return "phase"


def _append_token_fragments(
    result: dict, token_msg: str, mode: str, fragments: list[str],
) -> None:
    """Append token warning fragments based on handoff mode.

    Full mode: token display only (no handoff guidance, no stop).
    Other modes: display + handoff guidance at high thresholds.
    """
    threshold = result["threshold"]
    fragments.append(f"[SHOW USER VERBATIM] {token_msg}")
    # Full mode: display context usage but never inject
    # handoff guidance — user delegated autonomous execution.
    if mode in ("full", "competition"):
        return
    if threshold >= 140_000:
        try:
            from concinno.handoff_engine import _handoff_guidance
            guidance = _handoff_guidance(
                result["est_k"], result["cost_k"],
                critical=threshold >= 160_000,
            )
            fragments.append(guidance)
        except (ImportError, Exception):
            pass


def _run_token_monitor(
    hook_data: dict, fragments: list[str],
) -> None:
    """Token monitor: 100K/140K/160K threshold warnings (real API data only)."""
    try:
        from concinno.token_monitor import check_threshold

        session_id = hook_data.get("session_id", "")
        transcript = _find_transcript(session_id)
        if not transcript:
            return
        lang = os.environ.get("CC_UX_LANG", "en")
        result = check_threshold(
            transcript, _token_thresholds(lang),
            state_dir=cache_path("token_state"), session_id=session_id,
        )
        if not result:
            return
        token_msg = _format_token_warning(result, lang)
        _append_token_fragments(result, token_msg, _get_handoff_mode(), fragments)
    except (ImportError, Exception):
        pass


def _run_sentinel(hook_data: dict, tool_name: str) -> None:
    """Sentinel outcome recording (observation, not a guard)."""
    try:
        from concinno.sentinel import infer_success, record_outcome

        session_id = hook_data.get("session_id", "")
        tool_result = hook_data.get("tool_result", "")
        if isinstance(tool_result, dict):
            tool_result = str(tool_result)
        success = infer_success(tool_name, tool_result or "")
        state_dir = cache_path("sentinel")
        tool_input = hook_data.get("tool_input") or {}
        if not isinstance(tool_input, dict):
            tool_input = {}
        record_outcome(
            session_id, tool_name, tool_input, state_dir,
            success=success,
            tool_result=tool_result or "",
        )
    except (ImportError, Exception):
        pass


def _emit_output(fragments: list[str], session_id: str = "") -> None:
    """Throttle + JSON stdout via io_utils.write_json_stdout.

    Goes through write_json_stdout so silent mode (CONCINNO_SILENT or
    profile_settings.silent) wraps additionalContext with suppression
    directive — LLM still sees the info but is instructed not to mention it.
    Previously this function bypassed silent infra by writing stdout directly.
    """
    combined = "\n\n".join(fragments)
    lines = combined.split("\n\n")
    throttled = _throttle(lines, session_id=session_id)
    if not throttled:
        return
    final_ctx = "\n\n".join(throttled)
    from concinno.hooks.io_utils import write_json_stdout
    write_json_stdout({
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": final_ctx,
        }
    })
    if hasattr(sys.stdout, "buffer"):
        sys.stdout.buffer.flush()
    else:
        sys.stdout.flush()


# ── Pre-main helpers ─────────────────────────────────────


def _check_context_compressed(hook_data: dict, tool_name: str) -> bool:
    """Detect context compression ("file has not been read") and emit warning.

    Returns True if compressed (caller should early-exit).
    """
    if tool_name not in ("Edit", "Write"):
        return False
    tool_result = hook_data.get("tool_result", "")
    if isinstance(tool_result, dict):
        tool_result = str(tool_result)
    result_lower = str(tool_result).lower()
    if "not been read" not in result_lower and "must read" not in result_lower:
        return False
    msg = (
        "⛔ Context compressed — file read records lost. "
        "Stop all Edit/Write. Write handoff NOW."
    )
    _emit_stderr(msg)
    _emit_output(
        [f"[SHOW USER VERBATIM] {msg}"],
        session_id=hook_data.get("session_id", ""),
    )
    return True


def _run_auto_checkpoint(
    hook_data: dict, tool_name: str, tool_input: dict,
) -> None:
    """Accumulate modified files and fire auto_checkpoint when threshold hit.

    Uses StateStore to track files across subprocess calls.
    Requires CC_HANDOFF_DIR env var (CC-specific, not hardcoded).
    """
    if not _CACHE_DIR:
        return
    handoff_dir = os.environ.get("CC_HANDOFF_DIR", "")
    if not handoff_dir:
        return

    from concinno.core.state_store import StateStore

    store = StateStore(_CACHE_DIR)
    session_id = _resolve_session_id(hook_data.get("session_id", ""))
    ns = "auto_checkpoint"

    # Accumulate modified file paths from write tools
    if tool_name in _WRITE_TOOLS:
        fp = tool_input.get("file_path") or tool_input.get("path") or ""
        if fp:
            def _add_file(state: dict) -> dict:
                files = state.get("files", [])
                if fp not in files:
                    files.append(fp)
                state["files"] = files
                return state
            store.read_modify_write(ns, session_id, _add_file)

    # Check if we should fire (piggyback on token monitor's transcript read)
    state = store.read(ns, session_id, default={})
    files = state.get("files", [])
    if len(files) < 5:
        return

    # Get token usage from transcript
    path, _source = _resolve_transcript_path(session_id)
    if not path:
        return
    from concinno.token_monitor import read_real_token_usage
    usage = read_real_token_usage(path)
    token_usage = usage["context_tokens"] if usage else 0

    from concinno.handoff_engine import auto_checkpoint
    result = auto_checkpoint(
        session_id, token_usage,
        modified_files=files, handoff_dir=handoff_dir,
    )
    if result:
        _emit_stderr(f"auto-checkpoint → {os.path.basename(result)}")


def _run_streak_ux(
    tool_name: str, tool_input: dict, guard_ctx: str,
    hook_data: dict, fragments: list[str],
) -> None:
    """Track clean-edit streak and append UX message if applicable."""
    if tool_name not in _WRITE_TOOLS:
        return
    fp = tool_input.get("file_path") or tool_input.get("path") or ""
    has_errors = "\U0001f534" in guard_ctx  # 🔴
    streak_msg = _build_streak_msg(
        has_errors, fp, guard_ctx if has_errors else None,
        session_id=hook_data.get("session_id", ""),
    )
    if streak_msg:
        fragments.append(streak_msg)


# ── Main ─────────────────────────────────────────────────


def main(hook_data: dict | None = None) -> None:
    """PostToolUse entry point. Mirrors on_pre_tool.py thin-wrapper pattern."""
    try:
        if hook_data is None:
            hook_data = json.loads(sys.stdin.read())
    except Exception:
        return

    tool_name = hook_data.get("tool_name", "")
    tool_input = hook_data.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}

    # 0. Context compression — early exit
    if _check_context_compressed(hook_data, tool_name):
        return

    fragments: list[str] = []

    # 1. Guard Pipeline (lint/structure/SSOT/handoff/UIverify/design)
    guard_ctx = _run_pipeline(hook_data)
    if guard_ctx:
        fragments.append(guard_ctx)

    # 2. Streak UX (reads pipeline output for lint errors)
    _run_streak_ux(tool_name, tool_input, guard_ctx, hook_data, fragments)

    # 3. Knowledge + Cognitive (module functions, not guards)
    for extra_ctx in (_run_knowledge(hook_data), _run_cognitive(hook_data)):
        if extra_ctx:
            fragments.append(extra_ctx)

    # 4. Token monitor
    _run_token_monitor(hook_data, fragments)

    # 5. Sentinel outcome recording
    _run_sentinel(hook_data, tool_name)

    # 5.5 ThinkingDepthGuard — record ALL tools, warn only on Edit
    try:
        if _CACHE_DIR:
            from concinno.thinking_depth_guard import ThinkingDepthGuard
            guard = ThinkingDepthGuard()
            from concinno.guards.base import GuardContext
            # Use the same session_id as PreToolUse (from hook_data).
            # Falling back to _resolve_session_id() without hook_data would
            # route PostToolUse records to "unknown" while PreToolUse records
            # go to the real session id — splitting the ratio window.
            ctx = GuardContext(
                tool_name=tool_name,
                tool_input=tool_input,
                cache_dir=_CACHE_DIR,
                session_id=_resolve_session_id(
                    hook_data.get("session_id", "")
                ),
                hook_event="PostToolUse",
            )
            if tool_name in ("Edit", "Write", "NotebookEdit"):
                # Edit tools: record + check ratio
                result = guard.check(ctx)
                if result and result.context:
                    fragments.append(result.context)
            else:
                # All other tools: record only (no ratio warning)
                guard.record(ctx)
    except Exception:
        pass

    # 5.7 Anti-Drift identity re-injection
    try:
        from concinno.prompt_engine import should_reinject
        drift_text = should_reinject(_WORKSPACE or "")
        if drift_text:
            fragments.append(drift_text)
    except Exception:
        pass

    # 5.75 WIREDO mid-session → moved to CbuaPipelineGuard (StateStore-persisted)
    # The old _wiredo_mid_reminder used module-level global that reset per subprocess.
    # CbuaPipelineGuard now handles this with disk-persisted edit count.

    # 5.8 ZIQ feedback — record which files were actually read
    try:
        if tool_name == "Read" and _CACHE_DIR:
            fp = tool_input.get("file_path") or ""
            if fp:
                from concinno.ziq_retrieval import record_feedback
                record_feedback(_CACHE_DIR, [fp])
    except Exception:
        pass

    # 5.9 Auto checkpoint — accumulate modified files + fire when threshold hit
    try:
        _run_auto_checkpoint(hook_data, tool_name, tool_input)
    except Exception:
        pass

    # 6. Output
    if fragments:
        _emit_output(fragments, session_id=hook_data.get("session_id", ""))


if __name__ == "__main__":
    os.environ.setdefault("CC_UX_LANG", "en")
    main()
