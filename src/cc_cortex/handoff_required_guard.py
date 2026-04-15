"""cc_cortex.handoff_required_guard — Block stop when session has substantive
work but no handoff file was modified or created.

@module handoff_required_guard
@responsibility Enforce "session-end requires handoff update" rule (L0 #2).
@dependencies cc_cortex.i18n (handoff_prefixes), cc_cortex.core.config
@exports on_stop

Trigger condition (ALL must be true):
- Session id is set
- Git diff shows >= N source files modified (default 3)
  OR >= 1 commit was made this session
- No handoff file (matching i18n handoff_prefixes) appears in
  staged/unstaged/recent commits since session start
- Not already blocked once for this session (circuit breaker)

Escape valves:
- CC_CORTEX_FORCE_STOP=1 env (handled at on_stop.py dispatch level — this
  module is bypassed entirely in force-stop mode)
- stop_hook_active=true (CC retry signal — same dispatch-level bypass)
- feature_config: handoff_required_guard.enabled = false
- feature_config: handoff_required_guard.min_files (default 3)
- handoff_mode = "competition" (benchmark/bounty mode short-circuits
  this guard so scoreboard iteration is not interrupted by handoff
  requirements; see handoff_engine.HANDOFF_MODES docstring).

Returns:
- "HANDOFF_REQUIRED_BLOCK:<reason>" if handoff missing
- None otherwise
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from typing import Iterable, Optional

# ── Circuit breaker ──────────────────────────────────────────

_BLOCK_STATE_PATH = os.path.join(
    os.path.expanduser("~"), ".claude", "handoff_required_block.json",
)
_BLOCK_COOLDOWN_S = 300.0

# Source-like files that count toward "substantive work".
_SOURCE_EXTS: frozenset[str] = frozenset({
    ".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java",
    ".md", ".toml", ".yml", ".yaml", ".json",
})

# Git lookback window — only count commits/changes from the last 4 hours.
# This approximates "this session" without depending on session start time.
_SESSION_LOOKBACK_S = 14400  # 4 hours


def _already_blocked(session_id: str) -> bool:
    if not session_id or not os.path.isfile(_BLOCK_STATE_PATH):
        return False
    try:
        with open(_BLOCK_STATE_PATH, "r", encoding="utf-8") as f:
            state = json.load(f)
        if state.get("session_id") != session_id:
            return False
        return (time.time() - state.get("ts", 0)) < _BLOCK_COOLDOWN_S
    except Exception:
        return False


def _record_block(session_id: str) -> None:
    try:
        os.makedirs(os.path.dirname(_BLOCK_STATE_PATH), exist_ok=True)
        with open(_BLOCK_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump({"session_id": session_id, "ts": time.time()}, f)
    except Exception:
        pass


# ── Core logic ───────────────────────────────────────────────


def _handoff_prefixes() -> tuple[str, ...]:
    """Load handoff file prefixes from i18n."""
    try:
        from cc_cortex.i18n import patterns as i18n_patterns
        result = i18n_patterns("handoff_prefixes")
        return tuple(result) if result else ("交接_", "handoff_")
    except Exception:
        return ("交接_", "handoff_")


def _run_git(args: list[str], project_dir: str) -> str:
    """Run a git command safely, returning stdout (empty on failure).

    Force UTF-8 decoding because git always outputs UTF-8 for filenames
    (especially when ``-c core.quotepath=false`` is set), regardless of
    the platform's default codec. On Windows the default is gbk/cp936,
    which barfs on CJK paths and returns empty — silently breaking the
    whole guard. errors='replace' is a belt-and-braces fallback so a
    single odd byte can never crash the hook.
    """
    _NO_WIN = 0x08000000 if sys.platform == "win32" else 0
    try:
        result = subprocess.run(
            args, capture_output=True, text=True,
            cwd=project_dir, timeout=5,
            creationflags=_NO_WIN,
            encoding="utf-8",
            errors="replace",
        )
        return result.stdout or ""
    except Exception:
        return ""


def _git_session_changed_files(
    project_dir: str, since_seconds: int = _SESSION_LOOKBACK_S,
) -> list[str]:
    """Union of files changed in the current working tree and in commits made
    within the recent window. Deduplicated, order-insensitive.

    NOTE: every git command MUST pass ``-c core.quotepath=false`` so that
    non-ASCII filenames (CJK handoff files like ``交接_*.md``) come back
    as UTF-8 instead of git's default octal-escaped form
    (``"\\344\\272\\244..."``). Without this, ``_filter_handoff_files``
    cannot prefix-match Chinese names and the stop guard never sees the
    handoff updates the user actually wrote — causing infinite stop-block.
    """
    files: set[str] = set()
    quotepath = ["-c", "core.quotepath=false"]

    # Unstaged vs HEAD
    for line in _run_git(
        ["git", *quotepath, "diff", "--name-only", "HEAD"], project_dir,
    ).splitlines():
        line = line.strip()
        if line:
            files.add(line)

    # Staged
    for line in _run_git(
        ["git", *quotepath, "diff", "--name-only", "--cached"], project_dir,
    ).splitlines():
        line = line.strip()
        if line:
            files.add(line)

    # Recent commits within window
    since = f"{since_seconds} seconds ago"
    log_out = _run_git(
        [
            "git", *quotepath, "log",
            f"--since={since}",
            "--name-only",
            "--pretty=format:",
        ],
        project_dir,
    )
    for line in log_out.splitlines():
        line = line.strip()
        if line:
            files.add(line)

    return sorted(files)


def _filter_handoff_files(
    files: Iterable[str], prefixes: tuple[str, ...],
) -> list[str]:
    """Filter files down to handoff markdown docs."""
    out: list[str] = []
    for f in files:
        basename = os.path.basename(f)
        if any(basename.startswith(p) for p in prefixes) and basename.endswith(".md"):
            out.append(f)
    return out


def _session_has_handoff_changes(project_dir: str) -> bool:
    """True if any handoff file appears in session-window git changes."""
    prefixes = _handoff_prefixes()
    files = _git_session_changed_files(project_dir)
    return bool(_filter_handoff_files(files, prefixes))


def _count_source_files(files: Iterable[str]) -> int:
    """Count files whose extension is in the source whitelist."""
    count = 0
    for f in files:
        _, ext = os.path.splitext(f)
        if ext.lower() in _SOURCE_EXTS:
            count += 1
    return count


def on_stop(hook_data: dict) -> Optional[str]:
    """Stop hook entry point.

    Returns:
        - "HANDOFF_REQUIRED_BLOCK:<reason>" if session has substantive work
          but no handoff file was touched.
        - None otherwise.
    """
    session_id = hook_data.get("session_id", "")
    if not session_id:
        return None

    # Competition mode: short-circuit. Benchmark / bounty sessions
    # explicitly waive the "handoff required at session end" rule
    # so scoreboard iteration is not interrupted. See
    # handoff_engine.HANDOFF_MODES for the full policy.
    try:
        from cc_cortex.handoff_engine import is_competition_mode
        if is_competition_mode():
            return None
    except Exception:
        pass

    # Feature toggle
    min_files = 3
    try:
        from cc_cortex.core.config import get_config
        cfg = get_config()
        enabled = cfg.feature("handoff_required_guard", "enabled")
        if enabled is False:
            return None
        mf = cfg.feature("handoff_required_guard", "min_files")
        if isinstance(mf, (int, float)) and mf > 0:
            min_files = int(mf)
    except Exception:
        pass

    if _already_blocked(session_id):
        return None

    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", ".")
    changed = _git_session_changed_files(project_dir)
    source_count = _count_source_files(changed)
    if source_count < min_files:
        return None  # Not enough work to require handoff

    if _session_has_handoff_changes(project_dir):
        return None  # Handoff was updated — all good

    _record_block(session_id)
    reason = (
        f"Session has {source_count} modified source files but no handoff "
        f"file was updated. Write to a `交接_*.md` / `handoff_*.md` file "
        f"under your handoff directory before stopping. "
        f"Escape: set CC_CORTEX_FORCE_STOP=1 to bypass."
    )
    return f"HANDOFF_REQUIRED_BLOCK:{reason}"
