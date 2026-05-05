"""concinno.sentinel — Behavioral pattern detection for loops.

@module sentinel
@responsibility Detect stuck/brute-force/hijack patterns in tool calls
@dependencies concinno.constants, concinno.core.log,
    concinno.core.path_utils, concinno.core.state_store,
    concinno.guards.base
@exports check, hijack_score, gate_sentinel,
    HijackGuard, ConsecutiveFailGuard, SentinelGuard

Tracks recent tool calls per session and detects:
1. Tool Repeat: same write tool + same file N times in a row
2. Edit Stagnation: identical diffs attempted repeatedly
3. Analysis Paralysis: too many consecutive read-only operations
4. Scope Creep: modifying too many distinct files
5. Split Detection: suggest task splitting when file count is high
6. Bash Retry: similar Bash commands repeated without progress
7. Consecutive Failure: tool failures without strategy change
8. Hijack Detection: attention hijack via entropy/convergence/failure/repetition

All messages are configurable via the ``messages`` parameter.
"""

from __future__ import annotations

import os
import re
import time
from collections import Counter
from math import log2
from typing import Optional

from concinno.constants import READ_TOOLS, WRITE_TOOLS, make_deny
from concinno.core.log import get_logger
from concinno.core.path_utils import extract_file_path, normalize_path
from concinno.core.state_store import StateStore
from concinno.guards.base import BaseGuard, GuardCategory, GuardContext, GuardResult

READ_ONLY_TOOLS = READ_TOOLS  # alias for backward compat
logger = get_logger(__name__)

_NS = "sentinel"  # StateStore namespace

DEFAULT_THRESHOLDS = {
    "repeat": 3,
    "repeat_force": 5,
    "stale": 2,
    "paralysis": 7,
    "scope": 10,
    "split": 4,
    "bash_retry": 3,
    "consecutive_fail": 3,
}

# Default English messages (can be overridden by caller)
DEFAULT_MESSAGES = {
    "repeat": (
        "Sentinel: {tool} on {file} repeated {count} times. "
        "A locksmith who keeps trying the same key knows it's time to examine the lock. "
        "Three hypotheses — which one hasn't been tested yet?"
    ),
    "repeat_force": (
        "Sentinel: {tool} on {file} repeated {count}x — "
        "a path worn this deep leads nowhere new. "
        "The strategist who recognizes a dead end saves more time than "
        "the one who keeps walking. Different file, different tool, different angle."
    ),
    "stagnation": (
        "Sentinel: identical Edit diffs detected (stagnation). "
        "Repeating the same stroke and expecting a different line — "
        "step back, re-read the canvas, find what's actually missing."
    ),
    "paralysis_start": (
        "Sentinel: {count} consecutive read-only operations. "
        "A scout who never reports back isn't scouting — they're wandering. "
        "Enough intel to act, or time to change direction?"
    ),
    "paralysis_end": (
        "Attention recovered: "
        "switched from analysis to action mode."
    ),
    "scope": (
        "Sentinel: {count} distinct files modified this session. "
        "Is scope too large? "
        "Confirm all changes are within task scope."
    ),
    "split": (
        "Sentinel: {count} distinct files edited. "
        "Consider splitting into sub-tasks "
        "if touching 3+ subsystems."
    ),
    "bash_retry": (
        "Sentinel: similar Bash command repeated {count}x. "
        "The command is likely failing — stop retrying "
        "and diagnose the root cause first."
    ),
    "consecutive_fail": (
        "Sentinel: {count} consecutive tool failures without strategy change. "
        "You may be stuck without realizing it. "
        "STOP and re-examine: 1) Is your approach fundamentally wrong? "
        "2) Try a completely different tool or path. "
        "3) If unsure, use Step-Back to list hypotheses."
    ),
}

# Bash command prefix length for similarity detection
_BASH_CMD_PREFIX_LEN = 60


def _bash_cmd_prefix(tool_input: dict) -> str:
    """Extract normalized command prefix for similarity check."""
    cmd = ""
    if isinstance(tool_input, dict):
        cmd = tool_input.get("command", "")
    return cmd.strip()[:_BASH_CMD_PREFIX_LEN]


# ─── Detector Helpers (extracted from check()) ────────────


def _detect_tool_repeat(
    warnings: list, calls: list, tool_name: str,
    file_path: str, th: dict, msgs: dict,
) -> None:
    """Check 1: Write tool + same file N times in a row."""
    if not file_path or tool_name not in WRITE_TOOLS:
        return
    consec = 0
    for c in reversed(calls):
        if c["tool"] == tool_name and c["path"] == file_path:
            consec += 1
        else:
            break
    rt_force = th.get("repeat_force", 5)
    basename = os.path.basename(file_path)
    if consec >= rt_force:
        warnings.append({"type": "repeat_force", "message": msgs["repeat_force"].format(
            tool=tool_name, file=basename, count=consec)})
    elif consec >= th["repeat"]:
        warnings.append({"type": "repeat", "message": msgs["repeat"].format(
            tool=tool_name, file=basename, count=consec)})


def _detect_stagnation(
    warnings: list, calls: list, tool_name: str,
    edit_sig: str, th: dict, msgs: dict,
) -> None:
    """Check 2: Identical Edit diffs repeated."""
    st = th["stale"]
    if tool_name != "Edit" or not edit_sig or len(calls) < st:
        return
    edit_calls = [c for c in calls[-6:] if c["tool"] == "Edit" and c["edit_sig"]]
    if len(edit_calls) >= st:
        sigs = [c["edit_sig"] for c in edit_calls[-st:]]
        if len(set(sigs)) == 1:
            warnings.append({"type": "stagnation", "message": msgs["stagnation"]})


def _detect_paralysis(
    warnings: list, calls: list, tool_name: str,
    state: dict, store: StateStore, session_id: str,
    th: dict, msgs: dict,
) -> None:
    """Check 3: Too many consecutive read-only operations."""
    consecutive_reads = 0
    for c in reversed(calls):
        if c["tool"] in READ_ONLY_TOOLS:
            consecutive_reads += 1
        else:
            break
    was_paralyzed = state.get("paralysis_warned", False)
    if was_paralyzed and tool_name not in READ_ONLY_TOOLS:
        state["paralysis_warned"] = False
        store.write(_NS, session_id, state)
        warnings.append({"type": "paralysis_end", "message": msgs["paralysis_end"]})
    elif consecutive_reads >= th["paralysis"] and not was_paralyzed:
        state["paralysis_warned"] = True
        store.write(_NS, session_id, state)
        warnings.append({"type": "paralysis_start", "message": msgs["paralysis_start"].format(
            count=consecutive_reads)})


def _detect_scope_creep(
    warnings: list, calls: list, th: dict, msgs: dict,
) -> None:
    """Check 4: Modifying too many distinct files."""
    modified = {c["path"] for c in calls if c["tool"] in WRITE_TOOLS and c.get("path")}
    if len(modified) >= th["scope"]:
        warnings.append({"type": "scope", "message": msgs["scope"].format(count=len(modified))})


def _detect_bash_retry(
    warnings: list, calls: list, tool_name: str,
    bash_prefix: str, th: dict, msgs: dict,
) -> None:
    """Check 6: Similar Bash commands repeated without progress."""
    if tool_name != "Bash" or not bash_prefix:
        return
    consec_bash = 0
    for c in reversed(calls):
        if c.get("tool") == "Bash" and c.get("bash_pfx") == bash_prefix:
            consec_bash += 1
        else:
            break
    if consec_bash >= th["bash_retry"]:
        warnings.append({"type": "bash_retry", "message": msgs["bash_retry"].format(
            count=consec_bash)})


def _detect_consecutive_fail(
    warnings: list, calls: list, success: Optional[bool],
    th: dict, msgs: dict,
) -> None:
    """Check 7: Consecutive failures without strategy change."""
    if success is not False:
        return
    consec_fail = 0
    for c in reversed(calls):
        if c.get("ok") is False:
            consec_fail += 1
        elif c.get("ok") is True:
            break
    if consec_fail >= th["consecutive_fail"]:
        warnings.append({"type": "consecutive_fail", "message": msgs["consecutive_fail"].format(
            count=consec_fail)})


# ─── Main Check ───────────────────────────────────────────


def check(
    session_id: str,
    tool_name: str,
    tool_input: dict,
    state_dir: str,
    *,
    thresholds: Optional[dict] = None,
    messages: Optional[dict] = None,
    max_calls: int = 10,
    success: Optional[bool] = None,
) -> Optional[list[dict]]:
    """Detect behavioral anti-patterns from recent tool calls.

    Args:
        session_id: Current session ID.
        tool_name: Name of the tool being called.
        tool_input: Tool input dict.
        state_dir: Directory for persisting call history.
        thresholds: Override default thresholds.
        messages: Override default message templates.
        max_calls: Number of recent calls to keep.
        success: Whether the tool call succeeded (None = unknown).

    Returns:
        List of {type, message} dicts, or None if no warnings.
    """
    if not session_id:
        return None

    store = StateStore(state_dir)
    th = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    msgs = {**DEFAULT_MESSAGES, **(messages or {})}
    state = store.read(_NS, session_id, default={"calls": []})
    calls = state.get("calls", [])

    # Extract file path
    file_path = extract_file_path(tool_input)

    # Edit signature for stagnation detection
    edit_sig = ""
    if tool_name == "Edit" and isinstance(tool_input, dict):
        old_s = tool_input.get("old_string", "")[:80]
        new_s = tool_input.get("new_string", "")[:80]
        edit_sig = f"{old_s}||{new_s}"

    # Bash command prefix for retry detection
    bash_prefix = ""
    if tool_name == "Bash":
        bash_prefix = _bash_cmd_prefix(tool_input)

    # Record call
    call_record: dict = {
        "tool": tool_name,
        "path": file_path,
        "edit_sig": edit_sig,
        "bash_pfx": bash_prefix,
        "ts": time.time(),
    }
    if success is not None:
        call_record["ok"] = success
    calls.append(call_record)
    calls = calls[-max_calls:]
    state["calls"] = calls

    # Track edited files for split detection (capped to prevent unbounded growth)
    _MAX_EDITED_FILES = 200
    split_warn = False
    if tool_name in WRITE_TOOLS and file_path:
        ef = state.get("edited_files", [])
        if file_path not in ef:
            if len(ef) >= _MAX_EDITED_FILES:
                ef = ef[-(_MAX_EDITED_FILES - 1):]
            ef.append(file_path)
            state["edited_files"] = ef
        if (
            len(ef) >= th["split"]
            and not state.get("split_warned")
        ):
            state["split_warned"] = True
            split_warn = True

    # Save state before checks
    store.write(_NS, session_id, state)

    warnings: list[dict] = []
    _detect_tool_repeat(warnings, calls, tool_name, file_path, th, msgs)
    _detect_stagnation(warnings, calls, tool_name, edit_sig, th, msgs)
    _detect_paralysis(warnings, calls, tool_name, state, store, session_id, th, msgs)
    _detect_scope_creep(warnings, calls, th, msgs)
    if split_warn:
        ef_count = len(state.get("edited_files", []))
        warnings.append({"type": "split", "message": msgs["split"].format(count=ef_count)})
    _detect_bash_retry(warnings, calls, tool_name, bash_prefix, th, msgs)
    _detect_consecutive_fail(warnings, calls, success, th, msgs)
    return warnings if warnings else None


# ── Hijack Score (TADS-1) ────────────────────────────────


def hijack_score(calls: list[dict]) -> float:
    """Compute attention hijack score 0-1.

    Four signals: entropy, convergence, failures, repetition.
    """
    if len(calls) < 3:
        return 0.0

    recent = calls[-8:]
    scores: list[float] = []

    # 1. Tool selection entropy (normal = diverse tools; hijacked = one tool)
    tool_counts = Counter(c.get("tool", "") for c in recent)
    if len(tool_counts) > 1:
        total = sum(tool_counts.values())
        entropy = -sum(
            (v / total) * log2(v / total) for v in tool_counts.values()
        )
        max_ent = log2(len(tool_counts))
        scores.append(1 - entropy / max_ent if max_ent > 0 else 0.8)
    else:
        scores.append(0.8)  # only one tool type — highly suspicious

    # 2. Path convergence (normal = many files; hijacked = same file)
    paths = [c.get("path", "") for c in recent if c.get("path")]
    if paths:
        unique_ratio = len(set(paths)) / len(paths)
        scores.append(1 - unique_ratio)

    # 3. Consecutive failure rate (last 5)
    last5 = calls[-5:]
    if last5:
        fail_count = sum(1 for c in last5 if c.get("ok") is False)
        scores.append(fail_count / len(last5))

    # 4. Description/signature repetition
    sigs = [
        c.get("edit_sig") or c.get("bash_pfx") or c.get("tool", "")
        for c in recent
    ]
    if len(sigs) >= 3:
        unique_sigs = len(set(sigs))
        scores.append(1 - unique_sigs / len(sigs))

    return sum(scores) / len(scores) if scores else 0.0


# ── Hard Gate: Hijack Circuit Breaker (DENY) ─────────────


# TADS L3 context reset prompt template
_L3_RESET_TEMPLATE = (
    "Cognitive Reset ({count} repeats). History cleared.\n"
    "Restate problem fresh. List 3 new hypotheses. Pick most different."
)


def _hijack_level(score: float, thresholds: tuple = (0.3, 0.6, 0.8)) -> int:
    """Map hijack score to TADS level (0/2/3/4). Skips L1."""
    if score >= thresholds[2]:
        return 4
    if score >= thresholds[1]:
        return 3
    if score >= thresholds[0]:
        return 2
    return 0


_HIJACK_MSGS: dict[int, tuple[str, str]] = {
    4: ("force stop required",
        "Unrecoverable. STOP → write handoff → tell user to start new session."),
    3: ("context reset required", ""),  # Uses _L3_RESET_TEMPLATE
    2: ("alternative hypotheses required",
        "List 2 alternative root-cause hypotheses. Pick the most different one."),
}


def gate_hijack(
    session_id: str, state_dir: str,
    *, l2_threshold: float = 0.3, l3_threshold: float = 0.6, l4_threshold: float = 0.8,
) -> Optional[dict]:
    """TADS four-level circuit breaker. L0→allow, L2/L3/L4→deny. Skips L1."""
    if not session_id:
        return None
    store = StateStore(state_dir)
    calls = store.read(_NS, session_id, default={"calls": []}).get("calls", [])
    if len(calls) < 3:
        return None
    score = hijack_score(calls)
    level = _hijack_level(score, (l2_threshold, l3_threshold, l4_threshold))
    if level == 0:
        return None
    recent = [c.get("tool", "") for c in calls[-8:]]
    mc = Counter(recent).most_common(1)
    desc, count = (mc[0][0], mc[0][1]) if mc else ("unknown", 0)
    reason_suffix, ctx = _HIJACK_MSGS[level]
    if level == 3:
        ctx = _L3_RESET_TEMPLATE.format(count=count)
    else:
        ctx = (
            f"TADS L{level} ({score:.2f}): {desc} "
            f"{'loop' if level == 4 else 'repeated'} {count}/8.\n{ctx}"
        )
    reason = f"TADS L{level}: hijack_score={score:.2f} — {reason_suffix}"
    return make_deny(reason, additionalContext=ctx)


# ── Outcome Recorder (PostToolUse) ───────────────────────


def _extract_error_signature(tool_name: str, tool_result: str) -> str:
    """Extract a stable error signature from tool result for same-problem grouping.

    Normalizes away line numbers, timestamps, hashes so that
    the same root-cause error produces the same signature across retries.

    Returns empty string if no error detected or unknown.
    Performance: <0.5ms (regex).
    """
    if not tool_result:
        return ""
    lower = tool_result.lower()

    if tool_name == "Edit":
        for pattern, sig in (
            ("not found in file", "edit:old_string_not_found"),
            ("old_string not found", "edit:old_string_not_found"),
            ("not unique", "edit:not_unique"),
            ("no match found", "edit:no_match"),
        ):
            if pattern in lower:
                return sig
        return ""
    if tool_name == "Bash":
        return _bash_error_sig(tool_result, lower)
    if tool_name == "Write":
        return "write:error" if ("error" in lower or "failed" in lower) else ""
    return ""


def _bash_error_sig(raw: str, lower: str) -> str:
    """Extract error signature from Bash output."""
    m = re.search(r"(TS\d{4})", raw)
    if m:
        return f"bash:tsc:{m.group(1)}"
    m = re.search(r"(\w+Error):", raw)
    if m:
        return f"bash:python:{m.group(1)}"
    if "command not found" in lower:
        m = re.search(r"(\S+):\s*command not found", raw)
        return f"bash:cmd_not_found:{m.group(1) if m else 'unknown'}"
    if "permission denied" in lower:
        return "bash:permission_denied"
    if "exit code" in lower or "returned non-zero" in lower:
        return "bash:nonzero_exit"
    return ""


def record_outcome(
    session_id: str,
    tool_name: str,
    tool_input: dict,
    state_dir: str,
    *,
    success: Optional[bool] = None,
    tool_result: str = "",
    max_calls: int = 10,
) -> None:
    """Record a tool call outcome for gate_consecutive_fail.

    Lightweight: only writes state, no checks, no return value.
    Call from PostToolUse to build history for PreToolUse gate.

    Now includes error_sig for same-problem tracking:
    - Same error_sig appearing 3+ times = same problem recurring
    - Different error_sig = new problem, counter resets for that sig
    - Success clears all sig counters (problem was fixed)

    Performance: <2ms (read + write JSON).
    """
    if not session_id:
        return

    store = StateStore(state_dir)
    state = store.read(_NS, session_id, default={"calls": []})
    calls = state.get("calls", [])

    file_path = extract_file_path(tool_input)
    error_sig = ""
    if success is False and tool_result:
        error_sig = _extract_error_signature(tool_name, tool_result)

    call_record: dict = {
        "tool": tool_name,
        "path": file_path,
        "ts": time.time(),
    }
    if success is not None:
        call_record["ok"] = success
    if error_sig:
        call_record["sig"] = error_sig
    calls.append(call_record)
    calls = calls[-max_calls:]
    state["calls"] = calls

    # Track generated media artifacts from Bash output
    if tool_name == "Bash" and success is not False:
        _track_media_artifacts(state, tool_input, tool_result)

    store.write(_NS, session_id, state)


# ── Media Artifact Tracking ─────────────────────────────────

_MEDIA_EXTS = frozenset({
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".avif",  # image
    ".mp4", ".mov", ".avi", ".mkv", ".webm",                    # video
    ".mp3", ".wav", ".flac", ".ogg", ".aac", ".m4a",            # audio
})

_MEDIA_API_PATTERNS: dict[str, tuple[str, ...]] = {
    "image": ("fal-ai/", "fal.run/", "kontext", "text-to-image",
              "flux", "stable-diffusion", "dall-e", "replicate"),
    "video": ("kling", "runway", "pika", "hedra", "luma",
              "video-generation", "ffmpeg"),
    "audio": ("suno", "elevenlabs", "tts", "text-to-speech",
              "bark", "whisper"),
}

_FILE_PATH_RE = re.compile(
    r"""(?:saved?\s+(?:to|as|at|→|->)\s*|output[:\s]+|wrote\s+|created\s+|"""
    r"""generated\s+|downloaded?\s+(?:to\s+)?|file[:\s]+)"""
    r"""["\']?([^\s"\'<>|]+\.(?:"""
    + "|".join(ext.lstrip(".") for ext in _MEDIA_EXTS)
    + r"""))["\']?""",
    re.IGNORECASE,
)

_MAX_GENERATED = 50


def _track_media_artifacts(
    state: dict, tool_input: dict, tool_result: str,
) -> None:
    """Extract generated media file paths from Bash output.

    Two tracking lists in state:
    - generated_artifacts: actual file paths found in output
    - media_tasks: detected media API task types (image/video/audio)
    """
    cmd = ""
    if isinstance(tool_input, dict):
        cmd = (tool_input.get("command") or "").lower()

    # Track media task type from command
    for task_type, patterns in _MEDIA_API_PATTERNS.items():
        if any(p in cmd for p in patterns):
            mt = state.setdefault("media_tasks", [])
            if task_type not in mt:
                mt.append(task_type)

    # Extract file paths from output
    if not tool_result:
        return
    matches = _FILE_PATH_RE.findall(tool_result[:10_000])
    if not matches:
        return

    ga = state.setdefault("generated_artifacts", [])
    for path in matches:
        norm = os.path.normpath(path)
        if norm not in ga and os.path.isfile(norm):
            ga.append(norm)
    # Cap to prevent unbounded growth
    if len(ga) > _MAX_GENERATED:
        state["generated_artifacts"] = ga[-_MAX_GENERATED:]


_FAIL_PATTERNS: dict[str, tuple[str, ...]] = {
    "Edit": ("not found in file", "not unique", "old_string not found", "no match found"),
    "Bash": ("command not found", "no such file or directory", "permission denied",
             "syntax error", "exit code", "returned non-zero"),
    "Write": ("error", "failed"),
}


def infer_success(tool_name: str, tool_result: str) -> Optional[bool]:
    """Infer tool success from result text. True/False/None (unknown)."""
    if not tool_result:
        return None
    patterns = _FAIL_PATTERNS.get(tool_name)
    if patterns is None:
        return None
    lower = tool_result.lower()
    return not any(p in lower for p in patterns)


# ── Hard Gate: Consecutive Fail (DENY) ───────────────────


def _three_strikes_action() -> str:
    from concinno.i18n import msg
    return msg("sentinel.three_strikes")


def _count_sig_failures(calls: list) -> dict[str, int]:
    """Count error signature occurrences since last success."""
    counts: dict[str, int] = {}
    for c in reversed(calls):
        if c.get("ok") is True:
            break
        if c.get("ok") is False and c.get("sig"):
            sig = c["sig"]
            counts[sig] = counts.get(sig, 0) + 1
    return counts


def _emit_consec_fail_outcome(
    max_fails: int, tripped: bool, observed: int, *, mode: str = "raw",
) -> None:
    """ZIQ outcome emit for `consecutive_fail_gate.max_fails`.

    Tripped = True → low reward (gate fired), scaled by how aggressive
    the threshold is. Tripped = False → reward grows with the headroom
    the threshold left unused (smaller observed/max_fails ratio).
    """
    try:
        from concinno.ziq_outcome_bus import Outcome
        from concinno.ziq_outcome_bus import get_bus as _gbus

        if tripped:
            # Lower max_fails = earlier interruption = worse signal
            # unless observed >> max_fails (proves gate caught real loop).
            reward = max(0.0, min(0.5, max_fails / 10.0))
        else:
            used_ratio = observed / max(1, max_fails)
            reward = max(0.5, 1.0 - used_ratio * 0.5)
        _gbus().emit(
            Outcome(
                tunable="consecutive_fail_gate.max_fails",
                value=max_fails,
                reward=reward,
                source="concinno.sentinel.gate_consecutive_fail",
                metadata={
                    "tripped": tripped,
                    "observed": observed,
                    "mode": mode,
                },
            )
        )
    except Exception:
        pass


def gate_consecutive_fail(
    session_id: str, state_dir: str, *, max_fails: int = 3,
) -> Optional[dict]:
    """DENY when same problem fails >= max_fails. Sig-based or raw fallback."""
    if not session_id:
        return None
    calls = StateStore(state_dir).read(_NS, session_id, default={"calls": []}).get("calls", [])
    if not calls:
        return None

    sig_counts = _count_sig_failures(calls)
    if sig_counts:
        worst_sig = max(sig_counts, key=sig_counts.get)  # type: ignore[arg-type]
        worst_count = sig_counts[worst_sig]
        if worst_count >= max_fails:
            _emit_consec_fail_outcome(max_fails, True, worst_count, mode="sig")
            return make_deny(
                f"Three Strikes: same problem '{worst_sig}' failed {worst_count} times",
                additionalContext=(
                    f"Same problem `{worst_sig}` failed {worst_count} times.\n"
                    f"{_three_strikes_action()}"
                ),
            )
        _emit_consec_fail_outcome(max_fails, False, worst_count, mode="sig")
        return None  # Different problems, none recurring enough

    # Fallback: no sigs available, count raw consecutive fails
    consec = 0
    for c in reversed(calls):
        if c.get("ok") is False:
            consec += 1
        elif c.get("ok") is True:
            break
    if consec >= max_fails:
        _emit_consec_fail_outcome(max_fails, True, consec, mode="raw")
        return make_deny(
            f"Three Strikes: {consec} consecutive failures",
            additionalContext=(
                f"{consec} consecutive failures (no specific signature).\n{_three_strikes_action()}"
            ),
        )
    if consec > 0:
        _emit_consec_fail_outcome(max_fails, False, consec, mode="raw")
    return None


# ── Hard Gate: Repeat Edit (DENY) ───────────────────────


def _has_lint_errors(file_path: str) -> bool:
    """Check if file currently has lint errors (allows edit exception)."""
    try:
        proj = os.environ.get("CLAUDE_PROJECT_DIR", ".")
        ux_path = os.path.join(proj, ".concinno_cache", "streak_ux.json")
        if os.path.isfile(ux_path):
            from concinno.core.atomic import read_json
            errs = read_json(ux_path, default={}).get("errors", {})
            return errs.get(normalize_path(file_path), 0) > 0
    except Exception:
        logger.debug("lint exception check failed for %s", file_path)
    return False


def _emit_sentinel_outcome(max_repeats: int, observed: int, tripped: bool) -> None:
    """ZIQ outcome emit for `sentinel_gate.max_repeats`."""
    try:
        from concinno.ziq_outcome_bus import Outcome
        from concinno.ziq_outcome_bus import get_bus as _gbus

        if tripped:
            reward = max(0.0, min(0.5, max_repeats / 15.0))
        else:
            used_ratio = observed / max(1, max_repeats)
            reward = max(0.5, 1.0 - used_ratio * 0.5)
        _gbus().emit(
            Outcome(
                tunable="sentinel_gate.max_repeats",
                value=max_repeats,
                reward=reward,
                source="concinno.sentinel.gate_sentinel",
                metadata={"observed": observed, "tripped": tripped},
            )
        )
    except Exception:
        pass


def gate_sentinel(
    session_id: str, tool_name: str, tool_input: dict, state_dir: str,
    *, max_repeats: int = 5, lint_exception: bool = True,
) -> Optional[dict]:
    """DENY write tool on same file when count >= max_repeats."""
    if tool_name not in WRITE_TOOLS or not session_id:
        return None
    file_path = extract_file_path(tool_input)
    if not file_path:
        return None
    calls = StateStore(state_dir).read(_NS, session_id, default={"calls": []}).get("calls", [])
    consec = 0
    for c in reversed(calls):
        if c.get("tool") == tool_name and c.get("path") == file_path:
            consec += 1
        else:
            break
    if consec < max_repeats:
        if consec > 0:
            _emit_sentinel_outcome(max_repeats, consec, tripped=False)
        return None
    if lint_exception and _has_lint_errors(file_path):
        return None
    _emit_sentinel_outcome(max_repeats, consec, tripped=True)
    basename = os.path.basename(file_path)
    return make_deny(
        f"Sentinel Gate: {tool_name} on {basename} repeated {consec}x — stuck loop detected",
        additionalContext=(
            f"{basename} edited {consec}x — the groove is getting deeper, not closer.\n"
            "A craftsman who keeps sanding the same spot knows when to flip the piece over."
        ),
    )


# ── Prescription Map (9C-1) ─────────────────────────────────────
# Cognitive strategy prescriptions for each stuck type.
# Sentinel deny messages now include a specific prescription (9C-2).

_RX_KEYS = (
    "repeat", "repeat_force", "stagnation", "paralysis_start",
    "consecutive_fail", "bash_retry", "scope",
    "hijack_l2", "hijack_l3", "hijack_l4",
)


def _build_prescription_map() -> dict[str, str]:
    from concinno.i18n import msg
    return {k: msg(f"sentinel.rx.{k}") for k in _RX_KEYS}


PRESCRIPTION_MAP: dict[str, str] = {}  # Lazy-populated


def get_prescription(warning_type: str) -> str:
    """Get cognitive prescription for a stuck type. Returns '' if unknown."""
    if not PRESCRIPTION_MAP:
        PRESCRIPTION_MAP.update(_build_prescription_map())
    return PRESCRIPTION_MAP.get(warning_type, "")


# ── BaseGuard adapters (with prescriptions — 9C-2) ──────────────


class HijackGuard(BaseGuard):
    """TADS four-level circuit breaker based on hijack_score."""

    name = "hijack_guard"
    feature_name = "hijack_gate"
    category = GuardCategory.QUALITY
    step_back_reason = "attention hijack loop detected"

    def check(self, ctx: GuardContext) -> GuardResult | None:
        """Deny when attention hijack score crosses L2/L3/L4 thresholds.

        Args:
            ctx: Guard context with session_id and cache_dir.

        Returns:
            GuardResult.deny with cognitive prescription per level, or None.
        """
        if not ctx.session_id or not ctx.cache_dir:
            return None
        result = gate_hijack(ctx.session_id, ctx.cache_dir)
        if result is None:
            return None
        reason = result.get("reason", self.name)
        base_ctx = result.get("additionalContext", "")
        # Determine hijack level from reason for prescription
        rx_type = "hijack_l2"
        if "L4" in reason:
            rx_type = "hijack_l4"
        elif "L3" in reason:
            rx_type = "hijack_l3"
        return GuardResult.deny(
            reason,
            context=base_ctx + get_prescription(rx_type),
        )


class ConsecutiveFailGuard(BaseGuard):
    """DENY when consecutive tool failures exceed threshold."""

    name = "consecutive_fail"
    feature_name = "consecutive_fail_gate"
    category = GuardCategory.QUALITY
    step_back_reason = "consecutive tool failures"

    def check(self, ctx: GuardContext) -> GuardResult | None:
        """Block all tools after N consecutive failures without strategy change.

        Args:
            ctx: Guard context with session_id and cache_dir.

        Returns:
            GuardResult.deny with strategy-change prescription, or None.
        """
        if not ctx.session_id or not ctx.cache_dir:
            return None
        result = gate_consecutive_fail(ctx.session_id, ctx.cache_dir)
        if result is None:
            return None
        return GuardResult.deny(
            result.get("reason", self.name),
            context=(
                result.get("additionalContext", "")
                + get_prescription("consecutive_fail")
            ),
        )


class SentinelGuard(BaseGuard):
    """DENY Edit on same file when repeated too many times (stuck loop)."""

    name = "sentinel"
    feature_name = "sentinel_gate"
    category = GuardCategory.QUALITY
    step_back_reason = "repeated edits on same file"

    def check(self, ctx: GuardContext) -> GuardResult | None:
        """Block write tools on the same file after too many consecutive edits.

        Args:
            ctx: Guard context with session_id, tool_name, tool_input, cache_dir.

        Returns:
            GuardResult.deny with stuck-loop prescription, or None.
        """
        if not ctx.session_id or not ctx.cache_dir:
            return None
        result = gate_sentinel(
            ctx.session_id, ctx.tool_name, ctx.tool_input, ctx.cache_dir,
        )
        if result is None:
            return None
        return GuardResult.deny(
            result.get("reason", self.name),
            context=(
                result.get("additionalContext", "")
                + get_prescription("repeat_force")
            ),
        )
