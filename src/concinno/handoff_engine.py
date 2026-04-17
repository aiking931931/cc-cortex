"""concinno.handoff_engine — Token-aware hard gate and handoff guidance.

@module handoff_engine
@responsibility Hard-block Agent spawns when context tokens approach limit;
               generate structured handoff guidance; manage handoff modes.
               Performance: <5ms.
@dependencies concinno.constants
@exports check_token_gate, check_handoff_reminder,
         generate_session_summary, reset_handoff_reminder_state,
         get_handoff_mode, set_handoff_mode, HANDOFF_MODES,
         is_full_autonomous, is_competition_mode,
         is_autonomous_or_competition
"""

from __future__ import annotations

import json
import os
from typing import Optional

from concinno.constants import make_deny

# ── Thresholds (model-aware) ──────────────────────────────


def _model_thresholds() -> dict:
    """Derive gate thresholds from MODEL_PROFILES dynamically.

    save-token: agent gate = C2, critical = C3
    phase: gate = C4, reminder = C3
    Fallback: Haiku 200K era values.
    """
    try:
        from concinno.token_zone import (
            CHECKPOINT_THRESHOLDS,
            _parse_model_key,
            detect_model,
        )
        profile = detect_model()
        key = _parse_model_key(profile.get("display", "opus"))
        cp = CHECKPOINT_THRESHOLDS.get(key, CHECKPOINT_THRESHOLDS["opus"])
        return {
            "gate_agent": cp[1][0] if len(cp) > 1 else 140_000,
            "gate_critical": cp[2][0] if len(cp) > 2 else 160_000,
            "reminder_min": cp[0][0] if cp else 80_000,
            "phase_gate": cp[3][0] if len(cp) > 3 else 180_000,
            "phase_reminder": cp[2][0] if len(cp) > 2 else 150_000,
        }
    except Exception:
        return {
            "gate_agent": 140_000,
            "gate_critical": 160_000,
            "reminder_min": 80_000,
            "phase_gate": 180_000,
            "phase_reminder": 150_000,
        }


# Legacy constants — kept for backward compat, but functions
# use _model_thresholds() at runtime.
GATE_AGENT = 140_000
GATE_CRITICAL = 160_000
REMINDER_TOKEN_MIN = 80_000
REMINDER_FILE_MIN = 3

# ── Handoff Modes ─────────────────────────────────────────
#
# save-token (default) — Conservative, token-first.
#   - Gate Agent spawn at 140K, critical at 160K
#   - Fire handoff reminder at 80K+ with ≥3 modified files
#   - Philosophy: preserve tokens, interrupt early, hand off often
#
# phase — Balanced, task-boundary aware.
#   - Complete the current task list before any gate
#   - Gate only at 180K hard ceiling (safety net)
#   - Late reminder at 150K (no file-count requirement)
#   - Philosophy: ride out the current task, then hand off
#
# full — Maximum autonomous execution. CBUA-driven, user-delegated.
#   - NO token gating (Agent spawns always allowed)
#   - NO handoff reminders (user has explicitly delegated execution)
#   - Autonomous decision authority: the agent runs the CBUA pipeline
#     (C0 route → B1/B2 think → C1-A5 act) for every choice point without
#     pausing to ask the user. Only the following still interrupt execution:
#       (a) Destructive, irreversible actions (DestructionGuard R0-R4)
#       (b) Butterfly-effect rule violations
#       (c) Genuinely unknown unknowns the agent cannot resolve via
#           more research / ablation within its current toolset
#   - Execution discipline: decide fast, act fast, verify fast. No
#     "should I continue?" questions. No waiting for review between
#     sub-tasks. Unblocked todos are picked up automatically.
#   - Handoff writing is still allowed at any moment — full mode lifts
#     forcing mechanisms, not the option to persist state when useful.
#   - Philosophy: the user has pre-authorised autonomous execution of
#     the entire project flow. The agent's job is to judge and move.
#
# competition — Benchmark / bounty / short-horizon iteration mode.
#   Strict superset of `full` autonomy with two extra silencers:
#   - NO handoff-required block at session end
#     (handoff_required_guard short-circuits to None)
#   - NO CBUA cognitive reminders (B1 / C1 / U1 / WIREDO chatter
#     is silenced — cbua_pipeline_guard returns None and skips
#     state mutation entirely)
#   - Inherits ALL `full`-mode behaviour: no token gating, no
#     handoff reminders, no ask-user prompts, ZIQ / C0 routing
#     still active and trusted.
#   - Advertises the FieldRead / auto-compression hint flag for
#     downstream consumers (no consumer wired in CCC core today —
#     documented for future hookup).
#   - ⛔ WARNING: Competition mode deliberately suppresses cognitive
#     anchors. Reflective pacing is counter-productive when the
#     user is rapid-fire iterating against a benchmark scoreboard.
#     Do NOT use for production work or long-horizon architecture
#     tasks — those still need B1/C1/U1/WIREDO friction.
#   - Philosophy: trust ZIQ + the user's pre-flight strategy doc;
#     remove every interruption that costs scoreboard cycles.
#
HANDOFF_MODES = ("save-token", "phase", "full", "competition")
# Legacy constants — callers use _model_thresholds() at runtime
_PHASE_GATE = 180_000
_PHASE_REMINDER = 150_000


def is_full_autonomous() -> bool:
    """Return True when the current handoff mode is 'full'.

    Consumers use this to short-circuit ask-user prompts, skip
    soft reminders, and default to direct execution. See the
    ``HANDOFF_MODES`` block above for the full ``full``-mode policy.
    """
    return get_handoff_mode() == "full"


def is_competition_mode() -> bool:
    """Return True when the current handoff mode is 'competition'.

    Competition mode is a superset of ``full`` with additional
    cognitive-reminder suppression. Callers that want to short-
    circuit BOTH full-autonomous AND competition should use this
    in combination with :func:`is_full_autonomous`.
    """
    return get_handoff_mode() == "competition"


def is_autonomous_or_competition() -> bool:
    """Return True when either autonomous execution mode is active.

    Convenience predicate for callers that only care that the
    user has pre-authorised minimal-interruption execution (full
    or competition). Use this in preference to
    ``is_full_autonomous() or is_competition_mode()`` at call sites.
    """
    return get_handoff_mode() in ("full", "competition")


def get_handoff_mode() -> str:
    """Read handoff_mode from cc_config.json. Returns 'phase' if unset."""
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", "")
    cfg_path = os.path.join(project_dir, ".claude", "hooks", "cc_config.json")
    if not os.path.isfile(cfg_path):
        return "phase"
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        mode = cfg.get("handoff_mode", "phase")
        return mode if mode in HANDOFF_MODES else "phase"
    except Exception:
        return "phase"


def set_handoff_mode(mode: str) -> bool:
    """Write handoff_mode to cc_config.json. Returns True on success."""
    if mode not in HANDOFF_MODES:
        return False
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", "")
    cfg_path = os.path.join(project_dir, ".claude", "hooks", "cc_config.json")
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        cfg["handoff_mode"] = mode
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
            f.write("\n")
        return True
    except Exception:
        return False

# ── Handoff Reminder State (module-level, per-process) ────

_reminder_fired_sessions: set[str] = set()   # sessions already reminded

# ── Transcript Finder (cached) ────────────────────────────


def _find_transcript(session_id: str) -> str:
    """Delegate to unified transcript lookup in core.path_utils."""
    from concinno.core.path_utils import find_transcript
    return find_transcript(session_id)


# ── Token Gate (PreToolUse) ───────────────────────────────


def check_token_gate(
    session_id: str,
    tool_name: str,
    *,
    gate_agent: int = 0,
    gate_critical: int = 0,
) -> Optional[dict]:
    """Check if tool should be blocked due to high token usage.

    Returns deny dict for PreToolUse hook, or None if allowed.
    Only blocks Agent tool (other tools need to work for handoff writing).
    Thresholds are model-aware via _model_thresholds().

    Performance: <5ms (20KB tail read + JSON parse).
    """
    if tool_name != "Agent":
        return None

    mode = get_handoff_mode()

    if mode in ("full", "competition"):
        return None

    # Resolve model-aware thresholds
    mt = _model_thresholds()
    if not gate_agent:
        gate_agent = mt["gate_agent"]
    if not gate_critical:
        gate_critical = mt["gate_critical"]

    transcript = _find_transcript(session_id)
    if not transcript:
        return None

    try:
        from concinno.token_monitor import read_real_token_usage

        usage = read_real_token_usage(transcript)
        if not usage:
            return None

        context_k = usage["context_tokens"] // 1000
        cost_k = usage["cost_tokens"] // 1000
        ctx = usage["context_tokens"]

        if mode == "phase":
            phase_gate = mt["phase_gate"]
            if ctx >= phase_gate:
                from concinno.i18n import msg
                reason = msg(
                    "handoff_engine.token_gate_phase",
                    context_k=context_k,
                )
                return make_deny(
                    reason,
                    additionalContext=_handoff_guidance(
                        context_k, cost_k, critical=True,
                    ),
                )
            return None

        # save-token mode (default)
        if ctx >= gate_critical:
            return make_deny(
                f"🚨 Token Gate: {context_k}K tokens — CRITICAL. "
                f"Agent spawn blocked.",
                additionalContext=_handoff_guidance(context_k, cost_k, critical=True),
            )

        if ctx >= gate_agent:
            return make_deny(
                f"⚠️ Token Gate: {context_k}K tokens — Agent spawn blocked.",
                additionalContext=_handoff_guidance(context_k, cost_k, critical=False),
            )
    except Exception:
        pass

    return None


# ── Handoff Reminder (PreToolUse additionalContext) ───────


def check_handoff_reminder(
    session_id: str,
    token_usage: int,
    *,
    modified_count: int = 0,
    handoff_written: bool = False,
    token_min: int = 0,
    file_min: int = REMINDER_FILE_MIN,
) -> Optional[str]:
    """Return an additionalContext reminder if handoff is overdue.

    Thresholds are model-aware via _model_thresholds().

    Conditions (ALL must be true):
      1. token_usage >= threshold (model-aware C1)
      2. modified_count >= file_min (save-token only)
      3. handoff_written is False
      4. Reminder not already fired for this session_id
    """
    if session_id in _reminder_fired_sessions:
        return None
    if handoff_written:
        return None

    mode = get_handoff_mode()

    if mode in ("full", "competition"):
        return None

    mt = _model_thresholds()
    if not token_min:
        token_min = mt["reminder_min"]

    if mode == "phase":
        phase_reminder = mt["phase_reminder"]
        if token_usage < phase_reminder:
            return None
    else:
        # save-token mode
        if token_usage < token_min:
            return None
        if modified_count < file_min:
            return None

    # Fire once
    _reminder_fired_sessions.add(session_id)

    token_k = token_usage // 1000

    from concinno.i18n import msg
    return msg("handoff_engine.reminder", token_k=token_k, count=modified_count)


def reset_handoff_reminder_state() -> None:
    """Reset reminder state. Useful for testing."""
    _reminder_fired_sessions.clear()


# ── Auto Checkpoint ────────────────────────────────────────

_checkpoint_fired: set[str] = set()


def auto_checkpoint(
    session_id: str,
    token_usage: int,
    *,
    modified_files: list[str] | None = None,
    handoff_dir: str = "",
    next_step: str = "",
) -> str | None:
    """Auto-write checkpoint to the most relevant handoff file.

    Upgrades HandoffGuard from warn-only to write-action.
    Triggers when: token ≥ yellow zone OR ≥5 files modified OR >30min.

    Writes a compact checkpoint block (next_step + changed files summary)
    into the handoff file's next_step section. Idempotent per session.

    Returns the path written to, or None if skipped.
    """
    if session_id in _checkpoint_fired:
        return None
    if not handoff_dir or not os.path.isdir(handoff_dir):
        return None

    files = modified_files or []
    mt = _model_thresholds()

    # Trigger conditions (any one)
    yellow_zone = mt.get("reminder_min", 80_000)
    should_fire = (
        token_usage >= yellow_zone
        or len(files) >= 5
    )
    if not should_fire:
        return None

    _checkpoint_fired.add(session_id)

    # Find best handoff file
    target = _find_best_handoff(handoff_dir, files)
    if not target:
        return None

    # Build checkpoint block
    token_k = token_usage // 1000
    file_summary = ", ".join(
        os.path.basename(f) for f in files[:5]
    )
    if len(files) > 5:
        file_summary += f" +{len(files) - 5} more"

    step = next_step or "繼續未完成任務"
    block = (
        f"\n### auto-checkpoint (ctx {token_k}K, {len(files)} files)\n"
        f"- next_step: {step}\n"
        f"- changed: {file_summary}\n"
    )

    # Append to handoff file
    try:
        with open(target, "a", encoding="utf-8") as f:
            f.write(block)
        return target
    except OSError:
        return None


def _is_archive_path(root: str, handoff_dir: str) -> bool:
    """True if any path component between handoff_dir and root marks
    an archive directory.

    Naive ``"_archive" in root`` substring matches collapse on:
      - ``test_skips_archive_dir`` (pytest tmp_path) → false positive
      - ``project_archived_v1`` → false positive on word boundary
    Component-level check restricts to actual directory name suffixes
    so only real archive dirs get filtered.
    """
    try:
        rel = os.path.relpath(root, handoff_dir)
    except ValueError:
        # Different drives on Windows — treat as outside, not archived.
        return False
    if rel == ".":
        return False
    for part in rel.split(os.sep):
        if part.endswith("_archive") or part == "archive":
            return True
    return False


def _find_best_handoff(
    handoff_dir: str,
    modified_files: list[str],
) -> str | None:
    """Find the handoff file most relevant to the modified files.

    Scoring:
      +1 per modified file whose path contains the handoff file's
         immediate parent directory name (the "project tag", e.g.
         ``concinno``). Empty/dot tags do not score.
      +5 if the handoff file body contains ``next_step`` or ``⬜``
         (active markers — more likely the live handoff).

    Highest score wins. Ties resolve to first walk order.
    """
    best: tuple[int, str] = (0, "")
    # Pre-split modified files into path components for boundary-safe
    # project-tag matching. Substring `in mf` would false-positive on
    # one-letter tags ("a" in "/path/b/foo.py" → True via "path").
    modified_parts: list[set[str]] = [
        set(os.path.normpath(mf).split(os.sep)) for mf in modified_files
    ]

    handoff_root_norm = os.path.normpath(handoff_dir)
    handoff_root_name = os.path.basename(handoff_root_norm)

    for root, _dirs, files in os.walk(handoff_dir):
        if _is_archive_path(root, handoff_dir):
            continue
        # Use the immediate parent dir of the handoff file (== root)
        # as the project tag. When the handoff sits directly under
        # handoff_dir its tag would equal the handoff dir basename
        # (often a generic root like "06_Handoffs") — that has no
        # routing power, so we suppress scoring in that case.
        project_tag = os.path.basename(os.path.normpath(root))
        is_root = os.path.normpath(root) == handoff_root_norm
        for fname in files:
            if not fname.startswith("交接_") or not fname.endswith(".md"):
                continue
            path = os.path.join(root, fname)
            score = 0
            if (
                project_tag
                and project_tag not in (".", "", handoff_root_name)
                and not is_root
            ):
                score += sum(
                    1 for parts in modified_parts if project_tag in parts
                )
            # Prefer files with next_step (more likely active)
            try:
                with open(path, encoding="utf-8") as fh:
                    content = fh.read(2000)
                if "next_step" in content or "⬜" in content:
                    score += 5
            except OSError:
                pass
            if score > best[0]:
                best = (score, path)

    return best[1] if best[0] > 0 else None


def reset_checkpoint_state() -> None:
    """Reset checkpoint state. Useful for testing."""
    _checkpoint_fired.clear()


def _handoff_guidance(context_k: int, cost_k: int, *, critical: bool) -> str:
    """Generate handoff guidance text injected into additionalContext."""
    from concinno.i18n import msg

    key = "handoff_engine.guidance_critical" if critical else "handoff_engine.guidance_normal"
    return msg(key, context_k=context_k, cost_k=cost_k)


# ── Emergency Handoff (crash/kill/token-exhaustion) ──────


_EMERGENCY_MARKER = "<!-- auto-generated emergency handoff, needs review -->"
_EMERGENCY_MAX_LINES = 20


def emergency_handoff(
    session_id: str,
    *,
    modified_files: list[str] | None = None,
    reason: str = "unknown",
    handoff_dir: str = "",
) -> str | None:
    """Write emergency handoff snippet when session dies without proper handoff.

    Triggers: token exhaustion / process kill / failure with >=3 file changes
    and no handoff file modified this session.

    Writes ≤20 lines into the most relevant handoff file's next_step section.
    Returns the path written to, or None if skipped.
    """
    if not modified_files or len(modified_files) < 3:
        return None

    if not handoff_dir or not os.path.isdir(handoff_dir):
        return None

    # Find which handoff file is most relevant based on modified file paths
    handoff_files: list[tuple[str, int]] = []
    for root, _dirs, files in os.walk(handoff_dir):
        for fn in files:
            if fn.startswith("交接_") and fn.endswith(".md"):
                fpath = os.path.join(root, fn)
                # Score: how many modified files match this project's directory
                project_name = os.path.basename(root)
                score = sum(
                    1 for mf in modified_files
                    if project_name.lower() in mf.lower()
                )
                handoff_files.append((fpath, score))

    if not handoff_files:
        return None

    # Pick best match, fallback to evolution (global sync)
    handoff_files.sort(key=lambda x: x[1], reverse=True)
    target = handoff_files[0][0]

    # If no match at all, use evolution handoff
    if handoff_files[0][1] == 0:
        for fpath, _ in handoff_files:
            if "evolution" in fpath or "進化" in os.path.basename(fpath):
                target = fpath
                break

    # Build emergency snippet
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M")
    file_list = modified_files[:8]  # cap at 8
    snippet_lines = [
        "",
        _EMERGENCY_MARKER,
        f"### 緊急交接（{now}，{reason}）",
        "",
        f"**Session**: `{session_id}`",
        f"**原因**: {reason}",
        f"**修改檔案** ({len(modified_files)} 個):",
    ]
    for mf in file_list:
        snippet_lines.append(f"- `{mf}`")
    if len(modified_files) > 8:
        snippet_lines.append(f"- ... 還有 {len(modified_files) - 8} 個")
    snippet_lines.append("")
    snippet_lines.append("**next_step**: 審查上述變更，確認是否需要回滾或繼續")
    snippet_lines.append("")

    # Cap at max lines
    snippet_lines = snippet_lines[:_EMERGENCY_MAX_LINES]
    snippet = "\n".join(snippet_lines)

    # Append to target handoff file
    try:
        with open(target, "r", encoding="utf-8") as f:
            content = f.read()

        # Don't stack multiple emergency handoffs
        if _EMERGENCY_MARKER in content:
            return None

        with open(target, "a", encoding="utf-8") as f:
            f.write(snippet)

        return target
    except Exception:
        return None


def check_handoff_line_budget(
    handoff_path: str,
    *,
    budget: int = 300,
) -> dict | None:
    """Gate: deny writing to handoff file if it exceeds line budget.

    Returns deny dict for PreToolUse hook, or None if within budget.
    Escape hatch: tool_input contains '#HANDOFF_OVERFLOW'.
    """
    try:
        if not os.path.isfile(handoff_path):
            return None
        with open(handoff_path, "r", encoding="utf-8") as f:
            line_count = sum(1 for _ in f)
        if line_count <= budget:
            return None
        from concinno.constants import make_deny
        return make_deny(
            f"🔴 交接檔 {line_count} 行，超出 {budget} 行預算。"
            f"先清理到 ≤{budget} 行再寫入。"
            f"緊急情況用 #HANDOFF_OVERFLOW 跳過。",
        )
    except Exception:
        return None


# ── Session Summary (Stop hook UX) ───────────────────────


def generate_session_summary(
    session_id: str,
    *,
    streak: int = 0,
    start_time: str = "",
) -> str:
    """Generate a visual session end summary for stderr (user-visible).

    Performance: <10ms (reads cached token data + streak state).
    """
    # Token usage
    token_line = ""
    transcript = _find_transcript(session_id)
    if transcript:
        try:
            from concinno.token_monitor import read_real_token_usage

            usage = read_real_token_usage(transcript)
            if usage:
                ctx_k = usage["context_tokens"] // 1000
                cost_k = usage["cost_tokens"] // 1000
                cache_k = usage["cache_read_tokens"] // 1000
                token_line = f"💰 Token: {ctx_k}K (cost {cost_k}K, cache {cache_k}K)"
        except Exception:
            pass

    # Streak
    streak_line = ""
    if streak > 0:
        if streak >= 25:
            streak_line = f"🔥 Streak: {streak} — ON FIRE"
        elif streak >= 10:
            streak_line = f"🔥 Streak: {streak} — solid run"
        elif streak >= 5:
            streak_line = f"✨ Streak: {streak}"
        else:
            streak_line = f"📝 Streak: {streak}"

    # Modified files count
    files_line = ""
    try:
        project_dir = os.environ.get("CLAUDE_PROJECT_DIR", "")
        from concinno.core.config import get_config
        brain_dir = get_config().brain_dir
        lock_path = os.path.join(
            project_dir, brain_dir, "cognition_shared", "instance_lock.json",
        )
        if os.path.isfile(lock_path):
            with open(lock_path, "r", encoding="utf-8") as f:
                lock = json.load(f)
            for _name, s in lock.get("sessions", {}).items():
                if s.get("session_id") == session_id:
                    files = s.get("files", [])
                    if files:
                        files_line = f"📂 Files: {len(files)} touched"
                    break
    except Exception:
        pass

    # Assemble
    lines = [x for x in [token_line, streak_line, files_line] if x]
    if not lines:
        return ""

    # Box drawing
    w = max(len(x) for x in lines) + 4
    w = max(w, 30)

    from concinno.i18n import msg
    title = msg("handoff_engine.session_title")
    border_top = f"╔{'═' * w}╗"
    border_mid = f"╠{'═' * w}╣"
    border_bot = f"╚{'═' * w}╝"

    result = [border_top]
    result.append(f"║  {title:<{w - 2}}║")
    result.append(border_mid)
    for line in lines:
        result.append(f"║  {line:<{w - 2}}║")
    result.append(border_bot)

    return "\n".join(result)
