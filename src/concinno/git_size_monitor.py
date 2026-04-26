"""concinno.git_size_monitor — Lightweight .git size warning for stop hook.

@module git_size_monitor
@responsibility Detect bloated .git directories so the user hears about
    ballooning pack/object files before they tip the repo over the edge.
    Fast path: sum ``.git/objects/pack/*.pack`` sizes only. Avoids full
    recursive ``du`` inside the stop hook.
@dependencies (none — stdlib only)
@exports check_git_size, git_size_monitor_hook, DEFAULT_WARN_GB
"""

from __future__ import annotations

import os
from pathlib import Path

# 5 GB default threshold — below this, warnings are noise; above, repo ops
# (fetch / gc / clone) start getting painful.
DEFAULT_WARN_GB = 5.0

_ENV_THRESHOLD = "CONCINNO_GIT_SIZE_WARN_GB"
_ENV_DISABLED = "CONCINNO_GIT_SIZE_MONITOR_DISABLED"
# Legacy alias from ``rules/L1/switches.md`` row #24, where the doc
# referred to a ``CC_GIT_HEALTH_DISABLED`` env var that the code never
# actually read. Honoured 2026-04-26+ for backward compat with anyone
# who set it on faith of the docs.
_ENV_DISABLED_LEGACY = "CC_GIT_HEALTH_DISABLED"


def _is_disabled() -> bool:
    """Resolve opt-out: env vars (modern + legacy alias) or ``cfg.feature(...)``."""
    for name in (_ENV_DISABLED, _ENV_DISABLED_LEGACY):
        raw = os.environ.get(name, "").strip().lower()
        if raw in {"1", "true", "yes", "on"}:
            return True
    try:
        from concinno.core.config import get_config
        if get_config().feature("git_size_monitor", "enabled") is False:
            return True
    except Exception:
        pass
    return False


def _resolve_threshold_gb(override: float | None) -> float:
    """Resolve threshold in GB from explicit override or env var."""
    if override is not None:
        return float(override)
    raw = os.environ.get(_ENV_THRESHOLD)
    if not raw:
        return DEFAULT_WARN_GB
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_WARN_GB


def _iter_pack_files(git_dir: Path):
    """Yield ``.pack`` files inside ``.git/objects/pack/`` if present."""
    pack_dir = git_dir / "objects" / "pack"
    if not pack_dir.is_dir():
        return
    try:
        for entry in pack_dir.iterdir():
            if entry.suffix == ".pack" and entry.is_file():
                yield entry
    except OSError:
        return


def _pack_bytes(git_dir: Path) -> int:
    """Sum pack file sizes. Fast path — skips loose objects."""
    total = 0
    for pack in _iter_pack_files(git_dir):
        try:
            total += pack.stat().st_size
        except OSError:
            continue
    return total


def check_git_size(
    project_dir: str,
    warn_gb: float | None = None,
) -> str | None:
    """Return a warning string if ``.git`` exceeds threshold, else ``None``.

    Fast path: only sums ``.git/objects/pack/*.pack``. Loose objects are
    excluded — they are bounded by ``gc.auto`` and so unlikely to dominate
    footprint for repos where this check matters. Keeps stop-hook latency
    at single-digit ms even on 10 GB repos.

    Args:
        project_dir: Project root (expected to contain ``.git/``). Accepts
            absolute or relative path; resolved before size probing.
        warn_gb: Threshold in GB. If None, read env
            ``CONCINNO_GIT_SIZE_WARN_GB`` (fallback to
            :data:`DEFAULT_WARN_GB`).

    Returns:
        Warning string in the form
        ``"git_size_monitor: .git pack size 6.3 GB exceeds 5.0 GB threshold"``
        or ``None`` if below threshold / no ``.git`` present.
    """
    if _is_disabled():
        return None
    if not project_dir:
        return None
    root = Path(project_dir)
    if not root.is_absolute():
        root = root.resolve()
    git_dir = root / ".git"
    # Support worktrees (file containing "gitdir: <path>")
    if git_dir.is_file():
        try:
            line = git_dir.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return None
        if line.startswith("gitdir:"):
            ref = line.split(":", 1)[1].strip()
            ref_path = Path(ref)
            if not ref_path.is_absolute():
                ref_path = (root / ref_path).resolve()
            git_dir = ref_path
    if not git_dir.is_dir():
        return None

    threshold_gb = _resolve_threshold_gb(warn_gb)
    if threshold_gb <= 0:
        return None
    threshold_bytes = int(threshold_gb * 1024**3)

    size_bytes = _pack_bytes(git_dir)
    if size_bytes < threshold_bytes:
        return None

    size_gb = size_bytes / 1024**3
    return (
        f"\033[93m\u26a0 [git_size_monitor] .git pack size "
        f"{size_gb:.1f} GB exceeds {threshold_gb:.1f} GB threshold. "
        f"Consider `git gc --auto` (safe) or plan a reason-gated "
        f"`git gc --prune=<N>.weeks.ago`.\033[0m"
    )


def git_size_monitor_hook() -> str | None:
    """Entry point used by :mod:`concinno.hooks.on_stop` as a _StopModule.

    Reads ``CLAUDE_PROJECT_DIR`` (as every other stop module does) and
    returns a stderr-ready warning or None. Never raises — fail-open.
    """
    try:
        project_dir = os.environ.get("CLAUDE_PROJECT_DIR", "")
        return check_git_size(project_dir)
    except Exception:
        return None
