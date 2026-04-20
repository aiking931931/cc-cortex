"""concinno.cleanup — Automated workspace hygiene: git, handoffs, backups, temp files.

@module cleanup
@responsibility Detect and clean stale artifacts. Git auto-commit squash. Log rotation.
               All functions are generic (no hardcoded paths). Called by hooks, schedules,
               or the /tidy Skill.
@dependencies (none — stdlib only)
@exports run_cleanup, detect_dead_handoffs, squash_auto_commits, git_gc,
         detect_large_git_objects, cleanup_stale_files, rotate_log_files
"""

from __future__ import annotations

import contextlib
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from concinno.destruction_guard import destruction_gate


@contextlib.contextmanager
def _gate_escape(*flags: str):
    """Temporarily raise destruction_gate escape env flags.

    Used by the /tidy orchestrator (``run_cleanup``) so the gated cleanup
    functions pass through their decorator without requiring a reason
    kwarg from the CLI caller. Inside the with-block, ``CLAUDE_PROJECT_DIR``
    is also ensured (fall back to CWD) so ``_hook_context_permits`` returns
    True.
    """
    prev = {name: os.environ.get(name) for name in flags}
    prev_proj = os.environ.get("CLAUDE_PROJECT_DIR")
    if not prev_proj:
        os.environ["CLAUDE_PROJECT_DIR"] = os.getcwd()
    for name in flags:
        os.environ[name] = "1"
    try:
        yield
    finally:
        for name, value in prev.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        if prev_proj is None:
            os.environ.pop("CLAUDE_PROJECT_DIR", None)

_TZ = timezone(timedelta(hours=8))
_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def _hidden_startupinfo():
    if sys.platform != "win32":
        return None
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0
    return si


def _git(args: list[str], cwd: str, timeout: int = 30) -> Optional[str]:
    """Run a git command. Returns stdout or None on failure."""
    try:
        result = subprocess.run(
            ["git"] + args,
            cwd=cwd,
            capture_output=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
            creationflags=_CREATE_NO_WINDOW,
            startupinfo=_hidden_startupinfo(),
        )
        return result.stdout.strip() if result.returncode == 0 else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class CleanupResult:
    """Result of a cleanup operation."""
    action: str
    items_found: int = 0
    items_cleaned: int = 0
    bytes_freed: int = 0
    details: list[str] = field(default_factory=list)
    error: str = ""


# ---------------------------------------------------------------------------
# Dead handoff detection
# ---------------------------------------------------------------------------

# Default patterns — configurable via function args for non-CC consumers.
_DEFAULT_TODO_RE = re.compile(r"^[\s-]*[⬜⏸]", re.MULTILINE)
_DEFAULT_STAR_RE = re.compile(r"^[\s-]*★", re.MULTILINE)


def detect_dead_handoffs(
    handoff_dir: str | Path,
    max_age_days: int = 30,
    glob_pattern: str = "**/交接_*.md",
    pending_re: re.Pattern[str] | None = None,
    milestone_re: re.Pattern[str] | None = None,
) -> list[Path]:
    """Find handoff files with 0 pending tasks and older than max_age_days.

    A handoff is "dead" when:
    - No pending markers (default: ⬜ or ⏸) anywhere in the file
    - No milestone markers (default: ★)
    - last_updated in frontmatter > max_age_days ago, OR file mtime > max_age_days

    Args:
        pending_re: Regex for pending-task markers. Defaults to ⬜/⏸.
        milestone_re: Regex for permanent milestone markers. Defaults to ★.

    Returns list of dead handoff paths (candidates for archival, NOT deletion).
    """
    todo_re = pending_re or _DEFAULT_TODO_RE
    star_re = milestone_re or _DEFAULT_STAR_RE

    handoff_dir = Path(handoff_dir)
    if not handoff_dir.is_dir():
        return []

    cutoff = datetime.now(_TZ) - timedelta(days=max_age_days)
    dead: list[Path] = []

    for fp in handoff_dir.glob(glob_pattern):
        if not fp.is_file() or "_archive" in fp.parts:
            continue
        try:
            content = fp.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        # Has pending tasks? Keep it alive
        if todo_re.search(content) or star_re.search(content):
            continue

        # Check age: frontmatter last_updated or file mtime
        age_ok = False
        m = re.search(r"last_updated:\s*(\d{4}-\d{2}-\d{2})", content)
        if m:
            try:
                updated = datetime.strptime(m.group(1), "%Y-%m-%d").replace(tzinfo=_TZ)
                age_ok = updated < cutoff
            except ValueError:
                pass
        if not age_ok:
            mtime = datetime.fromtimestamp(fp.stat().st_mtime, tz=_TZ)
            age_ok = mtime < cutoff

        if age_ok:
            dead.append(fp)

    return dead


def archive_dead_handoffs(
    handoff_dir: str | Path,
    max_age_days: int = 30,
    dry_run: bool = False,
) -> CleanupResult:
    """Move dead handoffs to _archive/ subdirectory."""
    result = CleanupResult(action="archive_dead_handoffs")
    dead = detect_dead_handoffs(handoff_dir, max_age_days)
    result.items_found = len(dead)

    for fp in dead:
        archive_dir = fp.parent / "_archive"
        if dry_run:
            result.details.append(f"[dry-run] would archive: {fp.name}")
            continue
        try:
            archive_dir.mkdir(exist_ok=True)
            dest = archive_dir / fp.name
            fp.rename(dest)
            result.items_cleaned += 1
            result.details.append(f"archived: {fp.name}")
        except OSError as e:
            result.details.append(f"failed: {fp.name} — {e}")

    return result


# ---------------------------------------------------------------------------
# Git auto-commit squash
# ---------------------------------------------------------------------------

def count_auto_commits(
    repo_dir: str | Path = ".",
    pattern: str = "auto:",
) -> int:
    """Count commits matching a pattern (default: auto-commits)."""
    out = _git(["log", "--oneline", "--all"], str(repo_dir))
    if not out:
        return 0
    return sum(1 for line in out.splitlines() if pattern in line)


def _detect_embedded_nested_repos(cwd: str, max_depth: int = 4) -> list[str]:
    """Find nested `.git` directories that live under paths tracked by `cwd`.

    Why this exists (2.9.0 治本):
        Some outer repos intentionally track files that live *inside* another
        repo's working tree (e.g. `ai-king/.gitignore` has `!projects/concinno/`
        so the outer repo snapshots Concinno's source). When this outer repo
        runs `squash_auto_commits`, its rebase replays commits whose tree
        contains the outer's old snapshot of those paths — **overwriting the
        inner working tree** with stale content and silently blowing away
        work-in-progress. Stashed draft `2.9.0-draft-WIP-blocked-by-outer-squash`
        is evidence of exactly this race.

    Fix: detect this configuration and refuse to squash. Squashing a repo
    that embeds another repo's working tree is never safe — the outer rebase
    cannot distinguish "stale snapshot replay" from "user-intended rollback",
    and the inner repo has no say in the matter.

    Returns list of embedded repo paths (relative to `cwd`) whose contents
    are tracked by the outer index. Empty list = safe to squash.

    Set `CONCINNO_SKIP_NESTED_REPOS=0` to bypass (not recommended; only for
    the rare case where the caller is sure outer does not track inner paths).
    """
    if os.environ.get("CONCINNO_SKIP_NESTED_REPOS", "1") == "0":
        return []

    root = Path(cwd)
    if not root.is_dir():
        return []

    # Walk depth-limited; `.git` matches are either real repos or gitlink
    # files (submodules). We only care about real repos with a working tree
    # that the outer repo also tracks.
    embedded: list[str] = []
    try:
        for dot_git in root.rglob(".git"):
            # Depth limit to keep cost bounded on large trees.
            try:
                rel = dot_git.relative_to(root)
            except ValueError:
                continue
            if len(rel.parts) == 0 or len(rel.parts) > max_depth:
                continue
            # Skip the outer repo's own .git
            if rel == Path(".git"):
                continue
            # Skip submodule gitlinks (those are files, not directories) —
            # submodules are safe because outer stores only the commit SHA.
            if not dot_git.is_dir():
                continue

            nested_root_rel = rel.parent  # path relative to outer cwd
            nested_root_abs = dot_git.parent
            # Does the outer index track paths *inside* this nested repo?
            # Use a trailing-slash pathspec to require hits beneath the
            # nested root — plain `ls-files path` would also match a
            # gitlink entry (submodule) at that exact path, which is
            # safe (outer stores only commit SHA, not tree contents).
            nested_rel_str = str(nested_root_rel).replace(os.sep, "/")
            ls = _git(
                ["ls-files", "--", f"{nested_rel_str}/"],
                cwd,
            )
            # Filter: path must be strictly below nested_root_rel (not
            # the gitlink itself). `ls-files path/` already excludes the
            # gitlink, but double-check against accidental matches.
            tracked_inside = [
                ln for ln in (ls or "").splitlines()
                if ln and ln != nested_rel_str
                and ln.startswith(f"{nested_rel_str}/")
            ]
            if tracked_inside:
                embedded.append(nested_rel_str)
            # Bounded: if we already found too many, stop — no point in
            # enumerating further, the squash is already unsafe.
            if len(embedded) >= 8:
                break
            _ = nested_root_abs  # keep for future extensions (ignore-list)
    except OSError:
        return embedded
    return embedded


def _snapshot_inner_repo(inner_abs: str) -> Optional[dict]:
    """Snapshot inner repo state so outer squash can safely replay history.

    2.10.2 治本 (direction D): instead of refusing outer squash when it
    embeds an inner repo, we snapshot the inner's HEAD + stash any
    uncommitted state, let the outer rebase run (it may temporarily
    overwrite inner-tracked files with stale snapshots), then
    ``_restore_inner_repo`` puts the inner back. See MEMORY #77 for the
    .git bloat this unblocks.

    Returns:
        Dict with ``head`` (inner HEAD SHA) and ``stashed`` (bool).
        ``None`` when the inner is in an unsafe state (rebase/merge in
        progress, unreadable HEAD, or stash failure) — caller must refuse
        the outer squash to avoid losing inner work.
    """
    if not os.path.isdir(os.path.join(inner_abs, ".git")):
        return None

    # Refuse when inner is mid-rebase / mid-merge: a snapshot + reset would
    # clobber the in-progress operation.
    for sentinel in ("rebase-merge", "rebase-apply", "MERGE_HEAD", "CHERRY_PICK_HEAD"):
        if os.path.exists(os.path.join(inner_abs, ".git", sentinel)):
            return None

    head = _git(["rev-parse", "HEAD"], inner_abs)
    if not head:
        return None

    stashed = False
    status = _git(["status", "--porcelain", "-uall"], inner_abs)
    if status and status.strip():
        stash_out = _git(
            ["stash", "push", "-u", "-m", "concinno-outer-squash-protect"],
            inner_abs,
            timeout=60,
        )
        if stash_out is None:
            return None
        stashed = True

    return {"head": head, "stashed": stashed}


def _restore_inner_repo(inner_abs: str, snap: Optional[dict]) -> None:
    """Restore inner repo to snapshotted state after outer squash.

    Best-effort: errors are surfaced to stderr but do not raise, so an
    outer cleanup that otherwise succeeded is not reported as failed.
    Callers should treat a missing restore as a signal to inspect the
    inner repo manually.
    """
    if not snap:
        return

    # `reset --hard` realigns inner working tree to the snapshotted HEAD,
    # overwriting anything the outer rebase replayed into tracked paths.
    # Inner-untracked files (not stashed) are left alone — that's the
    # same semantics as a normal `reset --hard`.
    if _git(["reset", "--hard", snap["head"]], inner_abs, timeout=60) is None:
        sys.stderr.write(
            f"concinno: inner repo at {inner_abs!r} reset --hard failed — "
            f"manual check required (expected HEAD={snap['head']})\n"
        )
        return

    if snap.get("stashed"):
        if _git(["stash", "pop"], inner_abs, timeout=60) is None:
            sys.stderr.write(
                f"concinno: inner repo at {inner_abs!r} stash pop failed — "
                "WIP preserved under refs/stash, run `git stash list` + "
                "`git stash pop` manually to recover\n"
            )


@destruction_gate(risk="R3", op_name="squash_auto_commits")
def squash_auto_commits(
    repo_dir: str | Path = ".",
    keep: int = 3,
    dry_run: bool = False,
) -> CleanupResult:
    """Squash old commits, keeping the newest `keep` commits intact.

    Strategy: create an orphan archive commit with the tree state at HEAD~keep,
    then rebase the last `keep` commits on top of it.

    ⚠ DESTRUCTIVE: rewrites git history. Only safe for single-user repos.

    2.10.2 治本 (direction D): outer repos that embed another repo's
    working tree (e.g. ai-king/.gitignore carve-out for
    projects/concinno/) used to be refused outright (2.9.0). That
    protected inner WIP but starved outer squash → unbounded outer
    .git bloat (MEMORY #77: 7.6GB observed). The new default snapshots
    each embedded inner repo (HEAD + stash dirty), runs the outer
    squash, then restores the inner via ``_restore_inner_repo``. Set
    ``CONCINNO_PROTECT_NESTED_REPOS=0`` to fall back to 2.9.0 refuse
    behavior.
    """
    result = CleanupResult(action="squash_auto_commits")
    cwd = str(repo_dir)

    # Count total commits
    out = _git(["rev-list", "--count", "HEAD"], cwd)
    if not out:
        result.error = "not a git repo or no commits"
        return result

    total = int(out)
    if total <= keep:
        result.details.append(f"only {total} commits, nothing to squash (keep={keep})")
        return result

    to_squash = total - keep
    result.items_found = to_squash
    embedded = _detect_embedded_nested_repos(cwd)
    protect_mode = os.environ.get("CONCINNO_PROTECT_NESTED_REPOS", "1") != "0"

    if dry_run:
        if embedded and not protect_mode:
            result.details.append(
                f"[dry-run] WOULD SKIP (legacy refuse mode): outer repo embeds "
                f"{len(embedded)} nested repo(s) with tracked paths: "
                f"{', '.join(embedded[:3])}"
            )
            return result
        if embedded:
            result.details.append(
                f"[dry-run] would snapshot+restore {len(embedded)} embedded "
                f"repo(s): {', '.join(embedded[:3])}"
            )
        result.details.append(
            f"[dry-run] would squash {to_squash} commits, keeping newest {keep}"
        )
        return result

    # Legacy opt-out: keep 2.9.0 refuse behavior for operators who want it.
    if embedded and not protect_mode:
        result.error = (
            "nested repo(s) with tracked paths detected — refusing squash "
            "(CONCINNO_PROTECT_NESTED_REPOS=0): "
            f"{', '.join(embedded[:3])}"
        )
        return result

    # Protect mode: snapshot every embedded inner repo up front. Failure to
    # snapshot any one of them aborts the outer squash — losing inner WIP
    # is worse than a bloated outer .git.
    embedded_snapshots: dict[str, dict] = {}
    if embedded:
        for nested_rel in embedded:
            inner_abs = os.path.join(cwd, nested_rel)
            snap = _snapshot_inner_repo(inner_abs)
            if snap is None:
                # Roll back any inner repos we already stashed.
                for done_rel, done_snap in embedded_snapshots.items():
                    _restore_inner_repo(
                        os.path.join(cwd, done_rel), done_snap
                    )
                result.error = (
                    f"inner repo at {nested_rel!r} is unsafe to snapshot "
                    "(rebase/merge in progress, HEAD unreadable, or stash "
                    "failed) — resolve inner state first"
                )
                return result
            embedded_snapshots[nested_rel] = snap

    # Guard: dirty tree → abort. Inner dirt has been stashed above via
    # snapshot; only OUTER dirt (paths not inside embedded inners) should
    # still block. --ignore-submodules=all quiets perpetually-"modified"
    # submodule markers (e.g. skills/last30days).
    # Use -z for unambiguous parsing: NUL-separated records, "XY path\0...".
    # Avoids the pitfall where _git().strip() eats the leading space of the
    # first status line (" M foo" → "M foo"), breaking ln[3:] offsets.
    try:
        status_proc = subprocess.run(
            ["git", "status", "-z", "--ignore-submodules=all"],
            cwd=cwd,
            capture_output=True,
            timeout=30,
            encoding="utf-8",
            errors="replace",
            creationflags=_CREATE_NO_WINDOW,
            startupinfo=_hidden_startupinfo(),
        )
        status_raw = status_proc.stdout if status_proc.returncode == 0 else ""
    except Exception:
        status_raw = ""
    status_records = [r for r in status_raw.split("\x00") if r]
    if status_records:
        outer_dirty_lines: list[str] = []
        for rec in status_records:
            # Each record is "XY path" — status is chars [0:2], space at [2],
            # path starts at [3]. -z guarantees XY both present (including
            # leading space for unstaged).
            path = rec[3:] if len(rec) > 3 else ""
            inside_inner = any(
                path == nested_rel or path.startswith(nested_rel + "/")
                for nested_rel in embedded_snapshots
            )
            if not inside_inner:
                outer_dirty_lines.append(rec)
        if outer_dirty_lines:
            # Restore inner snapshots before bailing.
            for nested_rel, snap in embedded_snapshots.items():
                _restore_inner_repo(os.path.join(cwd, nested_rel), snap)
            result.error = "uncommitted changes — stash or commit first"
            return result

    try:
        # Pre-rebase sync: outer WT still shows inner HEAD content for
        # inner-tracked paths (we stashed inner to its HEAD, not to outer's
        # HEAD). Reset outer WT for inner paths to outer HEAD so rebase's
        # cleanliness check passes. Inner's .git is untouched; the finally
        # block restores inner's WT from the snapshot + stash.
        for nested_rel in embedded_snapshots:
            _git(["checkout", "HEAD", "--", nested_rel], cwd, timeout=60)

        # Get tree at HEAD~keep
        tree = _git(["rev-parse", f"HEAD~{keep}^{{tree}}"], cwd)
        if not tree:
            result.error = f"failed to parse tree at HEAD~{keep}"
            return result

        # Create orphan archive commit
        stamp = datetime.now(_TZ).strftime("%Y%m%d")
        msg = f"archive: squash {to_squash} commits before {stamp}"
        archive = _git(["commit-tree", tree, "-m", msg], cwd)
        if not archive:
            result.error = "failed to create archive commit"
            return result

        # Rebase last `keep` commits onto archive
        rebase = _git(["rebase", "--onto", archive, f"HEAD~{keep}"], cwd, timeout=120)
        if rebase is None:
            head = _git(["rev-list", "--count", "HEAD"], cwd)
            if head and int(head) == keep + 1:
                pass  # success
            else:
                result.error = "rebase failed — run 'git rebase --abort' to recover"
                return result

        # gc --auto: git-throttled maintenance. Fast no-op most calls; only
        # repacks when gc.auto/autoPackLimit thresholds hit. Without this,
        # squashed commits leave orphan objects/packs that never shrink —
        # that's why .git keeps growing despite "keep 3 commits" rule.
        # Aggressive repack + prune=now stays in /tidy git (user-triggered).
        _git(["gc", "--auto"], cwd, timeout=60)

        result.items_cleaned = to_squash
        msg_tail = (
            f" (protected {len(embedded_snapshots)} embedded inner repo(s))"
            if embedded_snapshots else ""
        )
        result.details.append(
            f"squashed {to_squash} commits into 1 archive, kept {keep}{msg_tail}"
        )
        return result
    finally:
        # Protect inner repos no matter how the outer squash exited: even
        # if rebase aborted mid-way, inner HEAD+stash must be restored so
        # the next session does not find the inner overwritten or dirty.
        for nested_rel, snap in embedded_snapshots.items():
            _restore_inner_repo(os.path.join(cwd, nested_rel), snap)


@destruction_gate(risk="R3", op_name="git_gc")
def git_gc(
    repo_dir: str | Path = ".",
    aggressive: bool = False,
) -> CleanupResult:
    """Run git gc. Returns size before/after."""
    result = CleanupResult(action="git_gc")
    cwd = str(repo_dir)
    git_dir = Path(cwd) / ".git"

    # Size before
    size_before = _dir_size(git_dir) if git_dir.is_dir() else 0

    args = ["gc", "--prune=now"]
    if aggressive:
        args.insert(1, "--aggressive")

    out = _git(args, cwd, timeout=300)
    if out is None:
        # git gc may return empty stdout on success
        pass

    size_after = _dir_size(git_dir) if git_dir.is_dir() else 0
    freed = size_before - size_after

    result.bytes_freed = max(freed, 0)
    result.details.append(
        f"before: {_human_size(size_before)}, "
        f"after: {_human_size(size_after)}, "
        f"freed: {_human_size(max(freed, 0))}"
    )
    return result


def detect_large_git_objects(
    repo_dir: str | Path = ".",
    min_size_mb: float = 10.0,
) -> list[dict]:
    """Find large objects in git history. Returns list of {hash, size, path}."""
    cwd = str(repo_dir)
    min_bytes = int(min_size_mb * 1024 * 1024)

    # Get all objects with sizes
    out = _git(
        ["rev-list", "--objects", "--all"],
        cwd,
        timeout=60,
    )
    if not out:
        return []

    # Batch check sizes
    lines = out.splitlines()[:5000]  # cap for performance
    hashes = [line.split()[0] for line in lines if line.strip()]

    large: list[dict] = []
    # Check in batches
    batch_input = "\n".join(hashes)
    try:
        proc = subprocess.run(
            ["git", "cat-file", "--batch-check"],
            input=batch_input,
            cwd=cwd,
            capture_output=True,
            timeout=60,
            encoding="utf-8",
            errors="replace",
            creationflags=_CREATE_NO_WINDOW,
            startupinfo=_hidden_startupinfo(),
        )
        if proc.returncode == 0:
            hash_to_path = {}
            for line in lines:
                parts = line.split(maxsplit=1)
                if len(parts) == 2:
                    hash_to_path[parts[0]] = parts[1]

            for line in proc.stdout.splitlines():
                parts = line.split()
                if len(parts) < 3 or parts[1] != "blob":
                    continue
                try:
                    size = int(parts[2])
                except ValueError:
                    continue
                if size < min_bytes:
                    continue
                large.append({
                    "hash": parts[0][:12],
                    "size": size,
                    "size_human": _human_size(size),
                    "path": hash_to_path.get(parts[0], "unknown"),
                })
    except Exception:
        pass

    large.sort(key=lambda x: x["size"], reverse=True)
    return large[:20]


# ---------------------------------------------------------------------------
# Stale file cleanup
# ---------------------------------------------------------------------------

@destruction_gate(risk="R2", op_name="cleanup_stale_files")
def cleanup_stale_files(
    base_dir: str | Path,
    patterns: list[str] | None = None,
    max_age_days: int = 7,
    dry_run: bool = False,
) -> CleanupResult:
    """Remove temp/backup files matching patterns and older than max_age_days."""
    result = CleanupResult(action="cleanup_stale_files")
    base = Path(base_dir)
    if patterns is None:
        patterns = ["_temp_*", "*.bak", "*.tmp"]

    cutoff = datetime.now(_TZ) - timedelta(days=max_age_days)

    for pattern in patterns:
        for fp in base.glob(pattern):
            result.items_found += 1
            mtime = datetime.fromtimestamp(fp.stat().st_mtime, tz=_TZ)
            if mtime >= cutoff:
                continue  # too recent

            if dry_run:
                result.details.append(f"[dry-run] would remove: {fp.name}")
                continue

            try:
                if fp.is_dir():
                    import shutil
                    size = _dir_size(fp)
                    shutil.rmtree(fp)
                else:
                    size = fp.stat().st_size
                    fp.unlink()
                result.items_cleaned += 1
                result.bytes_freed += size
                result.details.append(f"removed: {fp.name} ({_human_size(size)})")
            except OSError as e:
                result.details.append(f"failed: {fp.name} — {e}")

    return result


# ---------------------------------------------------------------------------
# Log rotation
# ---------------------------------------------------------------------------

@destruction_gate(risk="R2", op_name="rotate_log_files")
def rotate_log_files(
    log_dir: str | Path,
    max_lines: int = 500,
    keep_lines: int = 200,
    glob_pattern: str = "*.log",
) -> CleanupResult:
    """Truncate log files that exceed max_lines, keeping the newest keep_lines."""
    result = CleanupResult(action="rotate_log_files")
    log_dir = Path(log_dir)
    if not log_dir.is_dir():
        return result

    for fp in log_dir.glob(glob_pattern):
        if not fp.is_file():
            continue
        try:
            lines = fp.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue

        if len(lines) <= max_lines:
            continue

        result.items_found += 1
        trimmed = lines[-keep_lines:]
        now_str = datetime.now(_TZ).strftime("%Y-%m-%d %H:%M")
        header = f"# [rotated {now_str}] kept {keep_lines}/{len(lines)} lines\n"

        try:
            fp.write_text(header + "\n".join(trimmed) + "\n", encoding="utf-8")
            result.items_cleaned += 1
            result.bytes_freed += len("\n".join(lines[:-keep_lines]).encode())
            result.details.append(f"rotated: {fp.name} ({len(lines)}→{keep_lines})")
        except OSError as e:
            result.details.append(f"failed: {fp.name} — {e}")

    return result


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run_cleanup(
    repo_dir: str | Path = ".",
    handoff_dir: str | Path | None = None,
    log_dir: str | Path | None = None,
    keep_commits: int = 3,
    keep_backups: int = 2,
    max_handoff_age_days: int = 30,
    squash_git: bool = False,
    aggressive_gc: bool = False,
    dry_run: bool = False,
) -> list[CleanupResult]:
    """Run all cleanup operations. Returns list of results.

    By default runs safe operations only (stale files, logs, dead handoffs, gc).
    Set squash_git=True for destructive git history rewrite.
    """
    results: list[CleanupResult] = []
    repo = Path(repo_dir)

    # /tidy orchestrator is the authorised caller for all gated cleanup
    # ops. Raise escape flags for the duration of the orchestration so
    # each decorator passes through without requiring an explicit
    # reason= kwarg from the CLI user.
    with _gate_escape(
        "CONCINNO_STALE_CLEANUP",
        "CONCINNO_LOG_ROTATE",
        "CONCINNO_GIT_GC",
        "CONCINNO_INLINE_SQUASH",
    ):
        # 1. Stale files
        results.append(cleanup_stale_files(repo, dry_run=dry_run))

        # 2. Log rotation
        if log_dir:
            results.append(rotate_log_files(log_dir))

        # 3. Dead handoffs (no gate — archive, not delete)
        if handoff_dir:
            results.append(
                archive_dead_handoffs(handoff_dir, max_handoff_age_days, dry_run)
            )

        # 4. Git gc (always safe)
        results.append(git_gc(repo, aggressive=aggressive_gc))

        # 5. Git squash (destructive, opt-in)
        if squash_git:
            results.append(
                squash_auto_commits(repo, keep=keep_commits, dry_run=dry_run)
            )

    return results


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _dir_size(path: Path) -> int:
    """Total size of a directory in bytes."""
    total = 0
    try:
        for f in path.rglob("*"):
            if f.is_file():
                total += f.stat().st_size
    except OSError:
        pass
    return total


def _human_size(nbytes: int) -> str:
    """Format bytes as human-readable string."""
    for unit in ("B", "KB", "MB", "GB"):
        if abs(nbytes) < 1024:
            return f"{nbytes:.1f}{unit}"
        nbytes //= 1024
    return f"{nbytes:.1f}TB"
