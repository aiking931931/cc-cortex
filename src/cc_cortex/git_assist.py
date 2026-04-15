#!/usr/bin/env python3
"""cc_cortex.git_assist — Auto-commit on session stop + detect uncommitted changes.

@module git_assist
@responsibility Auto-commit changed files on session stop (excluding secrets).
               Falls back to report-only if commit fails. i18n-aware.
@dependencies (none — stdlib only)
@exports auto_commit, generate_report
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from typing import Optional

from cc_cortex.i18n import msg as i18n_msg

_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def _gt(key: str, locale: str = "en") -> str:
    """Get translated git string via i18n (falls back to en)."""
    return i18n_msg(f"git_assist.{key}")


def _hidden_startupinfo():
    if sys.platform != "win32":
        return None
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0
    return si


def _git(args: list[str], cwd: str, timeout: int = 10) -> Optional[str]:
    """Run a git command and return stdout, or None on failure."""
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
        if result.returncode == 0:
            return result.stdout.strip()
        return None
    except Exception:
        return None


def _parse_status(raw: str) -> tuple[list[str], list[str], list[str]]:
    """Parse git status --short output into (staged, unstaged, untracked)."""
    staged, unstaged, untracked = [], [], []
    for line in raw.splitlines():
        if len(line) < 3:
            continue
        x, y = line[0], line[1]
        fname = line[3:].strip()
        if x == "?":
            untracked.append(fname)
        elif x != " ":
            staged.append(fname)
        if y != " " and y != "?":
            unstaged.append(fname)
    return staged, unstaged, untracked


def _format_section(
    emoji: str, label: str, items: list[str], limit: int, more: str,
) -> str:
    """Format one section (staged/modified/untracked) of the report."""
    names = ", ".join(items[:limit])
    extra = f" +{len(items) - limit} {more}" if len(items) > limit else ""
    return f"{emoji} {label} ({len(items)}): {names}{extra}"


# Word-boundary tokens that mark a FILENAME (not a directory path) as
# likely credential material. Matched against os.path.basename() only —
# directory layout is intentionally ignored because "secret" appearing
# in a directory name (e.g. src/services/secretScanner.ts) is almost
# always a scanner / test fixture, not a real secret.
#
# History: the previous substring-on-whole-path matcher false-positived
# on test_secret_scan.py, secretScanner.ts, teamMemSecretGuard.ts, and
# any source file whose module name talks ABOUT secrets. Every false
# positive was silently unstaged by auto_commit → lost work.
_SECRET_BASENAME_TOKENS = re.compile(
    r"(?:"
    # High-signal compound tokens. "secret" alone is too noisy
    # (secret_scan.py is a module name, not a credential), but
    # "secret_key" / "secret_token" are unambiguous.
    r"(?:^|[._-])credentials?(?:$|[._-])"
    r"|(?:^|[._-])api[_-]?keys?(?:$|[._-])"
    r"|(?:^|[._-])private[_-]?keys?(?:$|[._-])"
    r"|(?:^|[._-])secret[_-]?keys?(?:$|[._-])"
    r"|(?:^|[._-])secret[_-]?tokens?(?:$|[._-])"
    r"|(?:^|[._-])service[_-]?accounts?(?:$|[._-])"
    r"|(?:^|[._-])access[_-]?tokens?(?:$|[._-])"
    # .env family — .env / .env.local / .env.production
    r"|\.env(?:$|\.)"
    # Binary key / cert extensions
    r"|\.pem$|\.p12$|\.pfx$|\.gpg$|\.key$|\.keystore$"
    # SSH private key names (exact match to avoid id_rsa_utils.py)
    r"|^id_rsa(?:\.pub)?$"
    r"|^id_dsa(?:\.pub)?$"
    r"|^id_ecdsa(?:\.pub)?$"
    r"|^id_ed25519(?:\.pub)?$"
    # OAuth / auth artifacts
    r"|^token\.json$"
    r"|^\.pypirc$|^\.netrc$"
    r")",
    re.IGNORECASE,
)

# Directory components that mean "this is a scanner target / test
# fixture, not a live credential". Any path containing one of these as
# a full path segment is exempt from _is_secret.
_TEST_DIR_MARKERS = frozenset({
    "tests", "test", "__tests__", "spec", "specs", "fixtures",
    "samples", "examples", "mocks", "__mocks__",
})

# Basename prefixes that mark a file as a test target by naming
# convention (pytest / jest style) regardless of directory location.
_TEST_BASENAME_PREFIXES = ("test_", "spec_")
_TEST_BASENAME_SUFFIXES = ("_test.py", "_spec.py", ".test.ts", ".test.tsx",
                            ".test.js", ".test.jsx", ".spec.ts", ".spec.tsx",
                            ".spec.js", ".spec.jsx")


def _is_secret(path: str) -> bool:
    """Return True iff *path*'s basename looks like a real credential file.

    Matching is anchored to ``os.path.basename(path)`` with a
    word-boundary-style regex (see ``_SECRET_BASENAME_TOKENS``), then
    short-circuited by the test-fixture whitelists
    (``_TEST_DIR_MARKERS``, ``_TEST_BASENAME_PREFIXES``,
    ``_TEST_BASENAME_SUFFIXES``). This replaces the old
    substring-on-whole-path scan which false-positived on any source
    file containing "secret" / "credential" / "key" in its name — e.g.
    ``tests/test_secret_scan.py`` or
    ``services/teamMemorySync/secretScanner.ts``.

    Pure, no I/O. Call sites pass path strings verbatim from
    ``git status --short``.
    """
    if not path:
        return False

    parts = path.replace("\\", "/").split("/")
    # Whitelist: anything living under a test/fixture/sample dir is
    # explicitly not a live credential. Real secrets never live here.
    if any(p in _TEST_DIR_MARKERS for p in parts):
        return False

    base = os.path.basename(path.replace("\\", "/"))
    if not base:
        return False

    # Whitelist: pytest/jest naming convention at basename level.
    # test_credentials.py and credentials.test.ts are NOT live secrets.
    # Also catch leading underscore variants (_test_credentials.py) and
    # infix markers (conftest_secret_fixture.py).
    base_lower = base.lower()
    if base_lower.startswith(_TEST_BASENAME_PREFIXES):
        return False
    if base_lower.endswith(_TEST_BASENAME_SUFFIXES):
        return False
    if "_test_" in base_lower or "_spec_" in base_lower:
        return False
    # Leading underscore variants: _test_credentials.py, _spec_...
    if base_lower.startswith(("_test_", "_spec_")):
        return False

    return bool(_SECRET_BASENAME_TOKENS.search(base))


# Pure hook-internal state. A working tree containing only these is
# noise from the auto-commit's perspective: every Stop event would
# otherwise commit cache writes / session heartbeat markers, polluting
# the git log and racing the .git/index.lock with sibling sessions
# (the polling/benchmark scenario where one session burns 5 min on
# heartbeat ticks against another that's actually working).
_TRIVIAL_PATH_FRAGMENTS = (
    "/.cc_cortex_cache/",
    "/cognition_shared/markers/",
    "/cognition_shared/instance_lock.json",
    "/transcript_path.txt",
    "/streak_ux.json",
    "/audit/guard_denies.jsonl",
    "/audit/config_changes.jsonl",
    "/mcp_cleanup_state.json",
    "/confidence_record.json",
)


def _is_trivial_path(path: str) -> bool:
    """True if *path* is pure hook-internal state, not user work.

    Uses a forward-slash normalised form so Windows backslashes match
    the same fragments.
    """
    if not path:
        return False
    norm = "/" + path.replace("\\", "/").lstrip("/")
    return any(frag in norm for frag in _TRIVIAL_PATH_FRAGMENTS)


def ensure_git_repo(
    cwd: str | None = None,
    timeout: int = 10,
) -> dict:
    """Ensure cwd is a git repo. Init if not. Returns status dict.

    Returns:
        {"is_repo": bool, "initialized": bool, "branch": str, "error": str}
    """
    if cwd is None:
        cwd = os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())

    result = {"is_repo": False, "initialized": False, "branch": "", "error": ""}

    # Already a repo?
    if _git(["rev-parse", "--is-inside-work-tree"], cwd, timeout=timeout) == "true":
        result["is_repo"] = True
        result["branch"] = _git(
            ["branch", "--show-current"], cwd, timeout=timeout,
        ) or "unknown"
        return result

    # Not a repo — init
    init_out = _git(["init"], cwd, timeout=timeout)
    if init_out is None:
        result["error"] = "git init failed"
        return result

    result["is_repo"] = True
    result["initialized"] = True

    # Set git user if not configured (required for commit)
    if not _git(["config", "user.name"], cwd, timeout=timeout):
        _git(["config", "user.name", "CC Cortex"], cwd, timeout=timeout)
    if not _git(["config", "user.email"], cwd, timeout=timeout):
        _git(["config", "user.email", "cc-cortex@local"], cwd, timeout=timeout)

    # Create .gitignore if missing
    gitignore = os.path.join(cwd, ".gitignore")
    if not os.path.isfile(gitignore):
        try:
            with open(gitignore, "w", encoding="utf-8") as f:
                f.write(
                    "# Auto-generated by CC Cortex\n"
                    "node_modules/\n"
                    ".env\n"
                    "*.key\n"
                    "*credentials*\n"
                    "__pycache__/\n"
                    "dist/\n"
                    ".cc_cortex_cache/\n"
                )
        except Exception:
            pass

    # Initial commit
    _git(["add", "-A"], cwd, timeout=timeout)
    _git(["commit", "-m", "initial: project snapshot by CC Cortex"], cwd, timeout=timeout)

    result["branch"] = _git(
        ["branch", "--show-current"], cwd, timeout=timeout,
    ) or "main"
    return result


def rollback(
    cwd: str | None = None,
    steps: int = 1,
    timeout: int = 10,
) -> dict:
    """Rollback to a previous auto-commit.

    Only rolls back commits with 'auto:' prefix (CC Cortex auto-commits).
    Won't touch user's manual commits.

    Args:
        cwd: Working directory.
        steps: Number of auto-commits to undo (default 1).
        timeout: Per-command timeout.

    Returns:
        {"success": bool, "rolled_back": int, "current_head": str, "error": str}
    """
    if cwd is None:
        cwd = os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())

    result = {"success": False, "rolled_back": 0, "current_head": "", "error": ""}

    if _git(["rev-parse", "--is-inside-work-tree"], cwd, timeout=timeout) != "true":
        result["error"] = "not a git repo"
        return result

    # Check for uncommitted changes first
    status = _git(["status", "--short"], cwd, timeout=timeout)
    if status:
        result["error"] = f"uncommitted changes present ({len(status.splitlines())} files)"
        return result

    # Find auto-commits to undo
    log = _git(
        ["log", "--oneline", "-20", "--format=%H %s"],
        cwd, timeout=timeout,
    )
    if not log:
        result["error"] = "no commits found"
        return result

    rolled = 0
    for line in log.splitlines():
        if rolled >= steps:
            break
        parts = line.split(" ", 1)
        if len(parts) < 2:
            continue
        commit_hash, msg = parts
        if not msg.startswith("auto:"):
            break  # Stop at first non-auto commit (don't touch user work)
        rolled += 1

    if rolled == 0:
        result["error"] = "no auto-commits to rollback (latest commit is manual)"
        return result

    # Soft reset — keeps changes as unstaged (user can review)
    reset_out = _git(["reset", f"HEAD~{rolled}"], cwd, timeout=timeout)
    if reset_out is None:
        result["error"] = "git reset failed"
        return result

    head = _git(["log", "--oneline", "-1"], cwd, timeout=timeout) or ""
    result["success"] = True
    result["rolled_back"] = rolled
    result["current_head"] = head
    return result


def auto_commit(
    cwd: str | None = None,
    timeout: int = 15,
) -> Optional[str]:
    """Auto-commit all non-secret changed files.

    Staging strategy follows the L0 rule "git add -A, never per-file":
    one ``git add -A`` shot for the whole working tree, then a single
    ``git reset HEAD --`` to unstage any files matching `_is_secret`.
    The previous per-file loop hung for ~10 minutes on working trees
    with hundreds of cache deletions because each subprocess paid the
    full git startup + index lock acquisition cost. The batch path
    finishes in O(seconds) regardless of file count.

    Skip gates (return None without staging anything):
        1. ``CC_CORTEX_NO_AUTOCOMMIT=1`` env override — for polling /
           benchmark sessions that explicitly opt out.
        2. Working tree contains only hook-internal state — see
           ``_is_trivial_path``. Without this, a 5-minute polling
           session burns one commit per turn on cache/marker writes
           and races ``.git/index.lock`` with any sibling session
           that is actually working.

    Returns:
        Commit message on success, or None if nothing to commit / failed.
    """
    # Skip gate 1: explicit opt-out
    if os.environ.get("CC_CORTEX_NO_AUTOCOMMIT") == "1":
        return None

    if cwd is None:
        cwd = os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())

    # Bump the per-call timeout floor: a large working tree can take
    # >15s for `add -A` on Windows even though the operation itself is
    # batch-fast. Callers that need a tighter bound can still pass one.
    op_timeout = max(60, timeout)

    if _git(["rev-parse", "--is-inside-work-tree"], cwd, timeout=timeout) != "true":
        return None

    status = _git(["status", "--short"], cwd, timeout=timeout)
    if not status:
        return None

    staged, unstaged, untracked = _parse_status(status)
    all_files = staged + unstaged + untracked
    if not all_files:
        return None

    # Skip gate 2: pure hook-internal state. If every changed file is
    # cache / marker / heartbeat noise, this is a polling tick and
    # auto-commit would just churn git history + race the index lock.
    if all(_is_trivial_path(f) for f in all_files):
        return None

    safe_files = [f for f in all_files if not _is_secret(f)]
    if not safe_files:
        return None

    # Batch stage everything (L0: never per-file).
    if _git(["add", "-A"], cwd, timeout=op_timeout) is None:
        return None

    # Defensive unstage: remove any newly-detected secret-like files
    # from the index before committing. Already-tracked secrets that
    # predate this guard are out of scope — those need a manual
    # `git rm --cached`. This only protects against the new stage.
    secret_files = [f for f in all_files if _is_secret(f)]
    if secret_files:
        _git(["reset", "HEAD", "--", *secret_files], cwd, timeout=op_timeout)

    # Generate commit message from file types
    exts = set()
    for f in safe_files[:10]:
        parts = f.rsplit(".", 1)
        if len(parts) == 2:
            exts.add(parts[1])
    ext_hint = ", ".join(sorted(exts)[:5]) if exts else "files"
    msg = f"auto: update {len(safe_files)} files ({ext_hint})"

    result = _git(["commit", "-m", msg], cwd, timeout=op_timeout)
    if result is None:
        return None

    # 反熵優先: squash old commits when accumulated beyond threshold
    _inline_squash_if_needed(cwd, timeout=timeout)
    return msg


def _inline_squash_if_needed(
    cwd: str,
    timeout: int = 15,
) -> None:
    """Squash old commits inline after auto-commit (反熵優先 principle).

    Only runs when total commits > keep * 2 to avoid rebasing on every commit.
    keep defaults to CC_CORTEX_KEEP_COMMITS env var, or 3.
    """
    keep = int(os.environ.get("CC_CORTEX_KEEP_COMMITS", "3"))
    threshold = keep * 2

    count_str = _git(["rev-list", "--count", "HEAD"], cwd, timeout=timeout)
    if not count_str:
        return
    total = int(count_str)
    if total <= threshold:
        return

    # Import here to avoid circular dependency
    try:
        from cc_cortex.cleanup import squash_auto_commits
        squash_auto_commits(cwd, keep=keep)
    except Exception:
        pass  # Non-critical: cleanup failure shouldn't block commits


def generate_report(
    cwd: str | None = None,
    locale: str = "en",
    timeout: int = 2,
) -> Optional[str]:
    """Generate a git status report for uncommitted changes.

    Args:
        cwd: Working directory (defaults to CLAUDE_PROJECT_DIR or cwd).
        locale: Locale for i18n strings.
        timeout: Per-command timeout in seconds (default 2s for toast use).

    Returns:
        Report string, or None if repo is clean / not a git repo.
    """
    if cwd is None:
        cwd = os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())

    if _git(["rev-parse", "--is-inside-work-tree"], cwd, timeout=timeout) != "true":
        return None

    status = _git(["status", "--short"], cwd, timeout=timeout)
    if not status:
        return None

    staged, unstaged, untracked = _parse_status(status)
    if not staged and not unstaged and not untracked:
        return None

    m = _gt("more", locale)
    parts = []
    if staged:
        parts.append(_format_section("🟡", _gt("staged", locale), staged, 5, m))
    if unstaged:
        parts.append(_format_section("📝", _gt("modified", locale), unstaged, 5, m))
    if untracked:
        parts.append(_format_section("❓", _gt("untracked", locale), untracked, 3, m))

    total = len(staged) + len(unstaged) + len(untracked)
    branch = _git(["branch", "--show-current"], cwd, timeout=timeout) or "unknown"
    header = f"🔀 Git: {total} {_gt('uncommitted', locale)} ({branch})"
    return header + "\n" + "\n".join(parts)
