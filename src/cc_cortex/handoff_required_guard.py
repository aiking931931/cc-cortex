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
- feature_config: handoff_required_guard.structural_gate_enabled
  (default True) — disables the second-layer structural check
- feature_config: handoff_required_guard.min_added_lines (default 10)
- feature_config: handoff_required_guard.min_signal_hits (default 2)
- CC_CORTEX_HANDOFF_MINIMAL=1 env — explicit acknowledgment that the
  handoff update is intentionally minimal (frontmatter only, pointer
  bump, etc.); skips the second-layer structural gate
- handoff_mode = "competition" (benchmark/bounty mode short-circuits
  this guard so scoreboard iteration is not interrupted by handoff
  requirements; see handoff_engine.HANDOFF_MODES docstring).

Second-layer structural gate:
A handoff file appearing in git diff passes the first layer, but the
diff must also show real structural content — enough added lines AND
enough distinct signals (status markers ✅/⬜/⏸/★, next_step field,
new H2 sections, session records, commit hashes, or doc links).
A one-line ``last_updated:`` bump no longer bypasses the guard.

Returns:
- "HANDOFF_REQUIRED_BLOCK:<reason>" if handoff missing or structure thin
- None otherwise
"""

from __future__ import annotations

import json
import os
import re
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


# ── Structural gate ──────────────────────────────────────────
#
# Second-layer gate: a handoff file touched in git diff is NOT enough to
# pass. The diff must also show *structural* updates — status markers,
# next_step field, new section headers, session records, commit hashes,
# or new doc references. A one-line `last_updated:` frontmatter bump
# passes the first layer but fails this second one. Root cause of the
# "several sessions without real handoff" bug (feedback_handoff_guard_
# too_lenient.md).

_STRUCTURAL_SIGNAL_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Status markers: ✅ ⬜ ⏸ ★ (U+2705, U+2B1C, U+23F8, U+2605)
    re.compile(r"^\+.*[\u2705\u2B1C\u23F8\u2605]"),
    # next_step field
    re.compile(r"^\+.*next_step", re.IGNORECASE),
    # New H2 section header
    re.compile(r"^\+## "),
    # New session record (H3 "Session ...")
    re.compile(r"^\+### Session ", re.IGNORECASE),
    # Commit hash record (7-12 hex chars, optionally wrapped in backticks)
    re.compile(r"^\+.*`?[0-9a-f]{7,12}`?"),
    # New Markdown link to an .md file (doc cross-reference)
    re.compile(r"^\+.*\[.+\]\(.+\.md\)"),
)

_STRUCTURAL_MIN_ADDED_LINES = 10
_STRUCTURAL_MIN_SIGNAL_HITS = 2


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


def _session_handoff_files(project_dir: str) -> list[str]:
    """Handoff files touched in session-window git changes (for gate)."""
    prefixes = _handoff_prefixes()
    files = _git_session_changed_files(project_dir)
    return _filter_handoff_files(files, prefixes)


def _git_added_lines(project_dir: str, path: str) -> list[str]:
    """Return added lines (leading '+') from `git diff --unified=0 HEAD`.

    Uses a belt-and-braces wrapper: any subprocess / OSError failure
    returns an empty list so the gate degrades to the first layer
    rather than crashing the stop hook.
    """
    quotepath = ["-c", "core.quotepath=false"]
    out = _run_git(
        ["git", *quotepath, "diff", "--unified=0", "HEAD", "--", path],
        project_dir,
    )
    added: list[str] = []
    for line in out.splitlines():
        # Skip diff header lines (+++ b/path) — only real added content.
        if line.startswith("+++"):
            continue
        if line.startswith("+"):
            added.append(line)
    return added


def _has_structural_update(
    project_dir: str,
    handoff_paths: Iterable[str],
    min_added_lines: int = _STRUCTURAL_MIN_ADDED_LINES,
    min_signal_hits: int = _STRUCTURAL_MIN_SIGNAL_HITS,
) -> tuple[bool, list[str]]:
    """Check whether handoff diffs carry real structural content.

    Returns (ok, reasons). `ok=True` means the diff has both enough
    added lines AND enough distinct structural signals. `reasons` is a
    list of short human hints explaining what was missing — suitable
    for surfacing in the BLOCK message.
    """
    all_added: list[str] = []
    for path in handoff_paths:
        all_added.extend(_git_added_lines(project_dir, path))

    total_lines = len(all_added)
    # Count DISTINCT patterns that matched — not total hits. Two ✅ lines
    # count as 1 signal; one ✅ + one next_step count as 2.
    distinct_hits = 0
    for pattern in _STRUCTURAL_SIGNAL_PATTERNS:
        if any(pattern.search(line) for line in all_added):
            distinct_hits += 1

    reasons: list[str] = []
    if total_lines < min_added_lines:
        reasons.append(
            f"only {total_lines} added line(s), need >={min_added_lines}"
        )
    if distinct_hits < min_signal_hits:
        reasons.append(
            f"only {distinct_hits} distinct structural signal(s), "
            f"need >={min_signal_hits} of "
            f"(\u2705/\u2B1C/next_step/## section/commit hash/doc link)"
        )

    ok = (total_lines >= min_added_lines) and (distinct_hits >= min_signal_hits)
    return ok, reasons


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
    structural_gate_enabled = True
    min_added_lines = _STRUCTURAL_MIN_ADDED_LINES
    min_signal_hits = _STRUCTURAL_MIN_SIGNAL_HITS
    try:
        from cc_cortex.core.config import get_config
        cfg = get_config()
        enabled = cfg.feature("handoff_required_guard", "enabled")
        if enabled is False:
            return None
        mf = cfg.feature("handoff_required_guard", "min_files")
        if isinstance(mf, (int, float)) and mf > 0:
            min_files = int(mf)
        sg = cfg.feature("handoff_required_guard", "structural_gate_enabled")
        if sg is False:
            structural_gate_enabled = False
        mal = cfg.feature("handoff_required_guard", "min_added_lines")
        if isinstance(mal, (int, float)) and mal > 0:
            min_added_lines = int(mal)
        msh = cfg.feature("handoff_required_guard", "min_signal_hits")
        if isinstance(msh, (int, float)) and msh > 0:
            min_signal_hits = int(msh)
    except Exception:
        pass

    if _already_blocked(session_id):
        return None

    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", ".")
    changed = _git_session_changed_files(project_dir)
    source_count = _count_source_files(changed)
    if source_count < min_files:
        return None  # Not enough work to require handoff

    handoff_files = _session_handoff_files(project_dir)
    if not handoff_files:
        _record_block(session_id)
        reason = (
            f"Session has {source_count} modified source files but no handoff "
            f"file was updated. Write to a `\u4ea4\u63a5_*.md` / "
            f"`handoff_*.md` file under your handoff directory before "
            f"stopping. Escape: set CC_CORTEX_FORCE_STOP=1 to bypass."
        )
        return f"HANDOFF_REQUIRED_BLOCK:{reason}"

    # Second-layer structural gate.
    # Skipped when disabled via feature config or when the user explicitly
    # acknowledges a minimal update via CC_CORTEX_HANDOFF_MINIMAL=1.
    if not structural_gate_enabled:
        return None
    if os.environ.get("CC_CORTEX_HANDOFF_MINIMAL", "").strip() == "1":
        return None

    ok, reasons = _has_structural_update(
        project_dir, handoff_files,
        min_added_lines=min_added_lines,
        min_signal_hits=min_signal_hits,
    )
    if ok:
        return None

    _record_block(session_id)
    detail = "; ".join(reasons) if reasons else "no structural content detected"
    reason = (
        f"handoff touched but structure incomplete — {detail}. "
        f"Need >={min_added_lines} added lines + >={min_signal_hits} of "
        f"(\u2705/\u2B1C/next_step/## section/commit hash/doc link). "
        f"Escape: CC_CORTEX_HANDOFF_MINIMAL=1 (intentional minimal update) "
        f"or CC_CORTEX_FORCE_STOP=1 (bypass all)."
    )
    return f"HANDOFF_REQUIRED_BLOCK:{reason}"
