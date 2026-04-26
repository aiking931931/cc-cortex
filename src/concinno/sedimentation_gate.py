"""concinno.sedimentation_gate — Block stop when corrections unsedimented.

@module sedimentation_gate
@responsibility CBUA Law #5 (自適應進化) + A4 強制沉澱: detect if user
    corrections were found in the session but not yet sedimented into
    feedback/KB files. Block the session stop until sedimentation is done.
@dependencies concinno.core.state_store
@exports on_stop

On-stop pipeline module (not a BaseGuard). Returns "SEDIMENTATION_BLOCK:..."
string to block session stop, or None to allow.

Deadlock protection: after 2 consecutive blocks, downgrades to warning.
"""

from __future__ import annotations

import os
import time
from typing import Optional


def _find_session_corrections(session_id: str) -> int:
    """Count corrections logged for this session in corrections_queue.jsonl."""
    import json

    home = os.path.expanduser("~")
    queue_path = os.path.join(home, ".claude", "cognitive", "corrections_queue.jsonl")

    if not os.path.isfile(queue_path):
        return 0

    prefix = session_id[:8]
    count = 0
    try:
        with open(queue_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if entry.get("session_id", "")[:8] == prefix:
                        count += 1
                except (json.JSONDecodeError, TypeError):
                    continue
    except Exception:
        pass

    return count


def _get_session_start_time(session_id: str, workspace: str) -> float:
    """Get session start time from StateStore, or fallback to now - 2h."""
    cache_dir = os.path.join(workspace, ".concinno_cache") if workspace else ""
    if cache_dir:
        try:
            from concinno.core.state_store import StateStore

            store = StateStore(cache_dir)
            state = store.read("session_profile", session_id, default={})
            ts = state.get("start_ts", 0)
            if ts > 0:
                return ts
        except Exception:
            pass
    return time.time() - 7200  # fallback: 2 hours ago


def _check_sedimentation_evidence(session_id: str, workspace: str) -> bool:
    """Check if feedback/KB files were written during THIS session.

    Uses session start time as cutoff (not fixed 30min window) to avoid
    cross-session false negatives. Also checks minimum content length (≥100)
    to prevent empty-file bypass.
    """
    memory_dirs = [
        os.path.join(os.path.expanduser("~"), ".claude", "projects"),
        os.path.join(workspace, ".claude", "skills") if workspace else "",
    ]
    cutoff = _get_session_start_time(session_id, workspace)
    return any(_scan_dir_for_feedback(d, cutoff) for d in memory_dirs if d)


def _scan_dir_for_feedback(base_dir: str, cutoff: float) -> bool:
    """Scan a directory for recently modified feedback_*.md files."""
    if not os.path.isdir(base_dir):
        return False
    try:
        for root, _dirs, files in os.walk(base_dir):
            if root.count(os.sep) - base_dir.count(os.sep) > 3:
                _dirs.clear()  # stop recursion instead of break (scan all shallow dirs)
                continue
            for fname in files:
                if not (fname.startswith("feedback_") and fname.endswith(".md")):
                    continue
                fpath = os.path.join(root, fname)
                if _is_valid_feedback(fpath, cutoff):
                    return True
    except Exception:
        pass
    return False


def _is_valid_feedback(path: str, cutoff: float) -> bool:
    """Check if feedback file was modified after cutoff AND has real content."""
    try:
        if os.path.getmtime(path) <= cutoff:
            return False
        # Minimum content length to prevent empty-file bypass
        return os.path.getsize(path) >= 100
    except OSError:
        return False


def _is_disabled() -> bool:
    """Resolve opt-out: env var or ``cfg.feature(...)``. 2026-04-26 wiring fix."""
    raw = os.environ.get("CONCINNO_SEDIMENTATION_GATE_DISABLED", "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    try:
        from concinno.core.config import get_config
        if get_config().feature("sedimentation_gate", "enabled") is False:
            return True
    except Exception:
        pass
    return False


def on_stop(hook_data: dict) -> Optional[str]:
    """On-stop entry point. Returns block string or None.

    Called by concinno.hooks.on_stop pipeline via _build_sedimentation_gate.
    """
    if _is_disabled():
        return None
    session_id = os.environ.get("CC_SESSION_ID", "")
    if not session_id:
        return None

    # Check for corrections in this session
    correction_count = _find_session_corrections(session_id)
    if correction_count == 0:
        return None

    # Check if sedimentation was done
    workspace = os.environ.get("CLAUDE_PROJECT_DIR", "")
    if _check_sedimentation_evidence(session_id, workspace):
        return None

    # Deadlock protection: track consecutive blocks
    cache_dir = os.path.join(workspace, ".concinno_cache") if workspace else ""
    if cache_dir:
        from concinno.core.state_store import StateStore

        store = StateStore(cache_dir)
        state = store.read("sedimentation_gate", session_id, default={})
        block_count = state.get("block_count", 0) + 1
        state["block_count"] = block_count
        store.write("sedimentation_gate", session_id, state)

        if block_count > 2:
            # Downgrade to warning after 2 blocks (deadlock breaker)
            return None

    return (
        f"SEDIMENTATION_BLOCK:本 session 偵測到 {correction_count} 個用戶糾正"
        "但尚未沉澱到 feedback/KB。"
        "請先完成沉澱（寫 feedback_*.md 或更新 KB Skill）再結束。"
    )
