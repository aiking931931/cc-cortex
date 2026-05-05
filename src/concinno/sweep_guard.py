"""concinno.sweep_guard — Detect stale .git residual state at session end.

@module sweep_guard
@responsibility Warn when a session ends with an interrupted git operation
    (rebase-in-progress, merge-in-progress, cherry-pick-in-progress,
    bisect-in-progress). These artefacts accumulate silently when an
    autocommit / rebase fails mid-way, leaving ``git status`` in a broken
    state that the next session inherits. Surfaces one actionable line per
    residual so the user (or next session) can clean it with a known
    recipe, rather than discovering it only when something else breaks.
@dependencies (none — stdlib only)
@exports on_stop, check_git_residuals, RESIDUAL_MARKERS

This guard is **stderr WARN only**, not a hard block. Rationale:
- Some residuals are legitimate WIP (an operator is mid-rebase on purpose
  and paused). Blocking would force them to set an escape var every
  session they happen to stop in the middle.
- Git residuals are recoverable — no data loss — so the cost of missing
  one is lower than blocking a legit stop.
- Users who want hard block can flip ``feature_config.sweep_guard.block =
  true``.

Circuit breaker: once warned for a residual in a session, do not warn
again for the same residual in the same session (5-min cooldown).

Escape valves (by decreasing priority):
- ``CONCINNO_FORCE_STOP=1`` — dispatch-level bypass
- ``stop_hook_active=true`` — CC retry signal, dispatch-level bypass
- ``CONCINNO_SWEEP_SKIP=1`` — skip this guard only, leave siblings intact
- ``feature_config: sweep_guard.enabled = false`` — disable entirely
- ``feature_config: sweep_guard.block = true`` — upgrade WARN to BLOCK

Layered with sibling guards:
- ``handoff_required_guard`` covers "no handoff written"
- ``sweep_guard`` covers "unresolved in git filesystem state"
Together they enforce CLAUDE.md "任務結束順手修" ironclad rule.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional

# ── Residual markers ─────────────────────────────────────────
#
# Each entry maps an in-progress git operation to:
#   - path(s) under .git/ that indicate the operation is ongoing
#   - a short human name
#   - a one-line recovery hint that points the next session at the right
#     cleanup recipe (avoid generic "run git status" — be actionable).
#
# Note the `MERGE_HEAD` / `CHERRY_PICK_HEAD` / `REVERT_HEAD` are files,
# `rebase-merge` / `rebase-apply` / `BISECT_LOG` surface as directories or
# files. All paths are relative to the .git directory.

RESIDUAL_MARKERS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    (
        "rebase",
        ("rebase-merge", "rebase-apply"),
        "git rebase --abort  # or --continue after resolving",
    ),
    (
        "merge",
        ("MERGE_HEAD",),
        "git merge --abort  # or resolve conflicts + git commit",
    ),
    (
        "cherry-pick",
        ("CHERRY_PICK_HEAD",),
        "git cherry-pick --abort  # or --continue",
    ),
    (
        "revert",
        ("REVERT_HEAD",),
        "git revert --abort  # or --continue",
    ),
    (
        "bisect",
        ("BISECT_LOG",),
        "git bisect reset",
    ),
)


# ── Circuit breaker ──────────────────────────────────────────

_STATE_PATH = Path.home() / ".claude" / "sweep_guard_state.json"
_COOLDOWN_S = 300.0


def _already_warned(session_id: str, residual_key: str) -> bool:
    """Has this session already been warned for this specific residual?"""
    if not session_id or not _STATE_PATH.is_file():
        return False
    try:
        state = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return False
    entry = state.get(session_id)
    if not isinstance(entry, dict):
        return False
    ts = entry.get(residual_key, 0)
    try:
        return (time.time() - float(ts)) < _COOLDOWN_S
    except (TypeError, ValueError):
        return False


def _record_warning(session_id: str, residual_key: str) -> None:
    """Persist that we warned this session for this residual (with TTL)."""
    if not session_id:
        return
    try:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        state: dict = {}
        if _STATE_PATH.is_file():
            try:
                state = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
            except Exception:
                state = {}
        now = time.time()
        # Drop expired top-level session entries to keep the file bounded.
        pruned: dict = {}
        for sid, entries in state.items():
            if not isinstance(entries, dict):
                continue
            keep = {
                k: v for k, v in entries.items()
                if isinstance(v, (int, float)) and (now - v) < _COOLDOWN_S * 4
            }
            if keep:
                pruned[sid] = keep
        pruned.setdefault(session_id, {})[residual_key] = now
        _STATE_PATH.write_text(
            json.dumps(pruned, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        pass


# ── Core check ───────────────────────────────────────────────


def _resolve_git_dir(project_dir: str) -> Optional[Path]:
    """Locate the .git directory for ``project_dir``.

    Handles plain checkouts (``.git`` is a directory) and linked
    worktrees (``.git`` is a file containing ``gitdir: <path>``).
    Returns ``None`` when not a git repo.
    """
    root = Path(project_dir).resolve()
    candidate = root / ".git"
    if candidate.is_dir():
        return candidate
    if candidate.is_file():
        try:
            line = candidate.read_text(encoding="utf-8").strip()
        except Exception:
            return None
        if line.startswith("gitdir:"):
            pointed = line.split(":", 1)[1].strip()
            resolved = Path(pointed)
            if not resolved.is_absolute():
                resolved = (root / pointed).resolve()
            if resolved.exists():
                return resolved
    return None


def check_git_residuals(project_dir: str) -> list[tuple[str, str, str]]:
    """Return a list of ``(key, label, hint)`` for residuals found.

    Empty list = clean. Caller formats for stderr / block output.
    """
    git_dir = _resolve_git_dir(project_dir)
    if git_dir is None:
        return []
    out: list[tuple[str, str, str]] = []
    for key, paths, hint in RESIDUAL_MARKERS:
        hit: Optional[str] = None
        for p in paths:
            candidate = git_dir / p
            if candidate.exists():
                hit = p
                break
        if hit is not None:
            label = f"{key} in progress ({hit})"
            out.append((key, label, hint))
    return out


# ── Hook entry point ─────────────────────────────────────────


def on_stop(hook_data: dict) -> Optional[str]:
    """Stop hook — warn (or block) when git residual state is present.

    Returns:
        - ``"SWEEP_BLOCK:<reason>"`` when configured as blocking and a
          residual was found (rare; opt-in via
          ``sweep_guard.block = true``).
        - ``"<stderr warning text>"`` when a residual was found in WARN
          mode. Caller writes this to stderr through the standard
          ``_emit_stderr_outputs`` whitelist.
        - ``None`` when the guard is disabled, the session already got
          warned for every residual, no residual is present, or the
          project dir is missing.
    """
    session_id = hook_data.get("session_id", "") or ""

    # Feature config — block toggle + enable toggle.
    enabled = True
    block_mode = False
    try:
        from concinno.core.config import get_config
        cfg = get_config()
        v = cfg.feature("sweep_guard", "enabled")
        if v is False:
            enabled = False
        b = cfg.feature("sweep_guard", "block")
        if b is True:
            block_mode = True
    except Exception:
        pass
    if not enabled:
        return None
    if os.environ.get("CONCINNO_SWEEP_SKIP", "").strip() == "1":
        return None

    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", ".") or "."
    residuals = check_git_residuals(project_dir)
    if not residuals:
        return None

    # Suppress already-warned residuals in this session.
    fresh = [
        (k, label, hint) for (k, label, hint) in residuals
        if not _already_warned(session_id, k)
    ]
    if not fresh:
        return None

    for k, _label, _hint in fresh:
        _record_warning(session_id, k)

    lines = [
        f"⚠ sweep_guard: {len(fresh)} git residual state(s) — clean before "
        f"handoff (CLAUDE.md 任務結束順手修 rule):",
    ]
    for _key, label, hint in fresh:
        lines.append(f"  • {label} — suggested: {hint}")
    lines.append(
        "  Escape: CONCINNO_SWEEP_SKIP=1 (single-session) or "
        "feature_config sweep_guard.enabled=false (permanent).",
    )
    message = "\n".join(lines)

    if block_mode:
        return f"SWEEP_BLOCK:{message}"
    return message
