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

    2.9.0 治本: skip when outer repo embeds another repo's working tree
    (e.g. ai-king/.gitignore carve-out for projects/concinno/). See
    ``_detect_embedded_nested_repos`` for the full rationale — replaying
    outer commits overwrites inner working tree with stale snapshots.
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

    if dry_run:
        # Surface embedded-repo detection in dry-run too so operators can
        # see the risk before enabling the escape hatch.
        embedded = _detect_embedded_nested_repos(cwd)
        if embedded:
            result.details.append(
                f"[dry-run] WOULD SKIP: outer repo embeds {len(embedded)} "
                f"nested repo(s) with tracked paths: {', '.join(embedded[:3])}"
            )
            return result
        result.details.append(
            f"[dry-run] would squash {to_squash} commits, keeping newest {keep}"
        )
        return result

    # Treatment: if outer repo embeds another repo's working tree, refuse.
    # Without this check, the outer rebase replays old snapshots of
    # inner's source files and silently overwrites the inner working tree.
    embedded = _detect_embedded_nested_repos(cwd)
    if embedded:
        result.error = (
            "nested repo(s) with tracked paths detected — refusing squash "
            "to avoid overwriting inner working tree (set "
            f"CONCINNO_SKIP_NESTED_REPOS=0 to bypass): {', '.join(embedded[:3])}"
        )
        return result

    # Guard: dirty tree → abort (rebase with uncommitted changes = broken state)
    # --ignore-submodules=all: nested repo markers (e.g. skills/last30days) stay
    # perpetually "modified" at top level and would otherwise block every squash.
    status = _git(["status", "--short", "--ignore-submodules=all"], cwd)
    if status:
        result.error = "uncommitted changes — stash or commit first"
        return result

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
    result.details.append(f"squashed {to_squash} commits into 1 archive, kept {keep}")
    return result


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
