#!/usr/bin/env python3
"""concinno.git_assist — Auto-commit on session stop + detect uncommitted changes.

@module git_assist
@responsibility Auto-commit changed files on session stop (excluding secrets).
               Falls back to report-only if commit fails. i18n-aware.
@dependencies (none — stdlib only)
@exports auto_commit, generate_report
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
import sys
from typing import Optional

from concinno.destruction_guard import destruction_gate
from concinno.i18n import msg as i18n_msg

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


def _git_raw(args: list[str], cwd: str, timeout: int = 10) -> Optional[str]:
    """Like ``_git`` but does NOT strip stdout — use for ``git status -z``
    (NUL-terminated, no leading/trailing whitespace meaningful).

    2.10.4 治本 — `_git`'s `.strip()` ate the leading space of `" M path"`
    so column-3 path slicing went off-by-one. Cleanup.py already worked
    around this with an inline subprocess; this helper is the shared fix.
    """
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
            return result.stdout
        return None
    except Exception:
        return None


def _status_records_z(cwd: str, timeout: int = 10) -> Optional[list[str]]:
    """Return git status records as raw strings, NUL-separated.

    2.10.4 治本 (FATAL F1) — ``git status --short`` quotes paths with
    non-ASCII characters or spaces (``"交接_X.md"`` / ``"path with
    space.txt"``), and ``_parse_status``'s ``line[3:].strip()`` then
    handed the still-quoted path to ``_is_large_unignored`` /
    ``_is_secret``, where ``os.stat()`` raised ``FileNotFoundError``
    on the literal ``"<quote>foo<quote>"`` filename and the file was
    silently passed through. Real ai-king CJK paths reproduced this.

    ``status -z`` outputs the unquoted byte stream of paths separated
    by NUL — no shell escaping, no leading-space ambiguity.
    """
    raw = _git_raw(
        ["-c", "core.quotepath=false", "status", "-z"], cwd, timeout=timeout
    )
    if raw is None:
        return None
    # Split by NUL; rename records emit `R XX\0old\0new` so a naive split
    # leaves a stray "old" record; we filter it out below in the parser.
    return [r for r in raw.split("\x00") if r]


def _parse_status_z(records: list[str]) -> tuple[list[str], list[str], list[str]]:
    """Parse ``git status -z`` records into (staged, unstaged, untracked).

    Each record is ``XY path`` with X/Y guaranteed (-z preserves the
    leading space; no quoting). For rename records ``R  new`` the next
    record is the old path — skipped here because the cleanup callers
    only need the destination path.
    """
    staged, unstaged, untracked = [], [], []
    skip_next = False
    for rec in records:
        if skip_next:
            skip_next = False
            continue
        if len(rec) < 3:
            continue
        x, y = rec[0], rec[1]
        # Path is everything from col 3 onwards. -z preserves leading
        # space (no shell-escaping); column offsets are stable.
        fname = rec[3:]
        if x == "?":
            untracked.append(fname)
        elif x != " ":
            staged.append(fname)
            # Rename / copy emits the old path as the next NUL record.
            if x in ("R", "C"):
                skip_next = True
        if y != " " and y != "?":
            unstaged.append(fname)
    return staged, unstaged, untracked


def _parse_status(raw: str) -> tuple[list[str], list[str], list[str]]:
    """Parse git status --short output into (staged, unstaged, untracked).

    ⚠ Legacy parser — uses ``line[3:].strip()`` which fails on quoted
    paths (CJK / spaces). Kept for ``generate_report`` (display-only,
    quotes are tolerable). New code (auto_commit, anything that touches
    the filesystem with the path) MUST use ``_status_records_z`` +
    ``_parse_status_z`` instead.
    """
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
    "/.concinno_cache/",
    "/cognition_shared/markers/",
    "/cognition_shared/instance_lock.json",
    "/transcript_path.txt",
    "/streak_ux.json",
    "/audit/guard_denies.jsonl",
    "/audit/config_changes.jsonl",
    "/mcp_cleanup_state.json",
    "/confidence_record.json",
)


def _large_file_threshold() -> int:
    """Bytes above which a tracked-or-newly-added file is considered large.

    Configurable via ``CONCINNO_LARGE_FILE_THRESHOLD`` (bytes). Defaults to
    10 MiB — PyPI's per-file limit is 100 MiB and GitHub warns at 50 MiB,
    but most accidental bloat (model checkpoints, datasets, backup zips)
    sits at 10 MiB+ so that's where the pre-commit filter trips.
    """
    try:
        raw = os.environ.get("CONCINNO_LARGE_FILE_THRESHOLD", "10485760")
        val = int(raw)
        return val if val > 0 else 10_485_760
    except (ValueError, TypeError):
        return 10_485_760


def _is_large_unignored(path: str, cwd: str, threshold: Optional[int] = None) -> bool:
    """Return True when ``path`` in ``cwd`` is ≥threshold bytes AND the
    file is currently staged or tracked (not already gitignored).

    2.10.3 治本 — ``auto_commit`` calls this after ``git add -A`` to
    unstage accidentally-bulked files (model checkpoints, datasets,
    backup archives) before they enter outer .git history. MEMORY #77
    noted the 7.6 GB bloat traced to LoRA / safetensors / BEIR corpus
    blobs the .gitignore did not catch. The squash-runaway fix (2.10.2)
    makes keep=3 work, but squashing historical blobs is expensive — it
    is cheaper to never stage them in the first place.

    Failure modes (all treated as "not large"): path missing, broken
    symlink, stat errors, git command failure. Large-file detection is a
    hygiene signal, not a security gate — on doubt, let the commit
    through and let the operator notice.
    """
    if threshold is None:
        threshold = _large_file_threshold()

    try:
        full = os.path.join(cwd, path)
        # follow_symlinks=False: symlinks themselves are tiny, and the
        # thing they point at may live outside the repo; don't blame the
        # link for its target's size.
        st = os.stat(full, follow_symlinks=False)
    except OSError:
        return False

    if not stat.S_ISREG(st.st_mode):
        return False

    return st.st_size >= threshold


def _is_trivial_path(path: str) -> bool:
    """True if *path* is pure hook-internal state, not user work.

    Uses a forward-slash normalised form so Windows backslashes match
    the same fragments.
    """
    if not path:
        return False
    norm = "/" + path.replace("\\", "/").lstrip("/")
    return any(frag in norm for frag in _TRIVIAL_PATH_FRAGMENTS)


# Default staleness threshold. A real `git commit` or `git add` holds
# ``.git/index.lock`` for milliseconds; any lock older than 60s with no
# live progress is orphaned (process killed, parent session died, etc.)
# and must be cleared before subsequent commits will ever succeed.
# Override via ``CONCINNO_LOCK_STALE_SEC`` for tests / unusual setups.
_DEFAULT_LOCK_STALE_SEC = 60


def _stale_lock_threshold() -> int:
    """Read the staleness threshold (seconds) from env, falling back to default."""
    try:
        return max(1, int(os.environ.get("CONCINNO_LOCK_STALE_SEC", "")))
    except (TypeError, ValueError):
        return _DEFAULT_LOCK_STALE_SEC


def _resolve_index_lock_path(cwd: str) -> str:
    """Return the absolute path of ``.git/index.lock`` for *cwd*.

    Handles three layouts:
        1. Normal repo: ``cwd/.git/`` is a directory → lock at ``cwd/.git/index.lock``.
        2. Worktree / submodule: ``cwd/.git`` is a file containing ``gitdir: <path>``
           → lock at ``<path>/index.lock``.
        3. Unresolvable → fall back to layout #1 (caller's stat will miss and bail).
    """
    default = os.path.join(cwd, ".git", "index.lock")
    dot_git = os.path.join(cwd, ".git")
    if os.path.isdir(dot_git):
        return default
    if not os.path.isfile(dot_git):
        return default
    try:
        with open(dot_git, encoding="utf-8") as fh:
            line = fh.readline().strip()
    except OSError:
        return default
    if not line.startswith("gitdir: "):
        return default
    real = line[len("gitdir: "):]
    if not os.path.isabs(real):
        real = os.path.normpath(os.path.join(cwd, real))
    return os.path.join(real, "index.lock")


def _clear_stale_index_lock(cwd: str, max_age: Optional[int] = None) -> bool:
    """Remove ``{cwd}/.git/index.lock`` if it is an orphan.

    Returns True when it is now safe to proceed (no lock, or we removed a
    stale one). Returns False when a *live* lock exists (someone is mid-
    commit): caller must bail rather than racing.

    Why this exists: when a prior ``git add -A`` / ``git commit`` is
    SIGKILLed (CC session crash, Ctrl-C during startup, Windows reboot,
    sub-agent timeout), it leaves behind a zero-byte lock file that
    blocks every subsequent commit with
    ``fatal: Unable to create '.git/index.lock': File exists``.
    Sub-agents mis-diagnose this as a pre-commit hook recreating the
    lock and reach for ``--no-verify``, which bypasses nothing because
    no hook is involved — the root cause is the orphan.

    Safety model:
        - Only remove locks older than ``max_age`` (default 60s). A
          real op completes in milliseconds, so 60s = comfortably past
          any legitimate git call.
        - If removal fails (another process actually owns it or the
          filesystem denies delete), return False. Caller must skip
          this commit cycle rather than spinning.
        - Zero-age lock or mid-op writes are left alone.

    Tunable via env ``CONCINNO_LOCK_STALE_SEC``.
    """
    import time

    if max_age is None:
        max_age = _stale_lock_threshold()

    lock_path = _resolve_index_lock_path(cwd)
    try:
        st = os.stat(lock_path)
    except (FileNotFoundError, OSError):
        return True  # no lock, proceed

    age = time.time() - st.st_mtime
    if age < max_age:
        # Lock is fresh → a real op is in progress elsewhere. Do not
        # race; caller bails.
        return False

    # Orphan: remove and report stderr breadcrumb so operators can
    # correlate with the crashed session.
    try:
        os.remove(lock_path)
    except OSError as exc:
        _safe_stderr(
            f"[git_assist] stale lock present but unremovable "
            f"({lock_path}, age={int(age)}s): {exc}\n",
        )
        return False
    _safe_stderr(
        f"[git_assist] cleared stale .git/index.lock "
        f"(age={int(age)}s) — prior git op was killed mid-write\n",
    )
    return True


def _safe_stderr(msg: str) -> None:
    """Write to stderr, never raising (sys.stderr can be None under Windows GUI hosts)."""
    try:
        if sys.stderr is not None:
            sys.stderr.write(msg)
    except Exception:
        pass


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
        _git(["config", "user.email", "concinno@local"], cwd, timeout=timeout)

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
                    ".concinno_cache/\n"
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


@destruction_gate(risk="R3", op_name="rollback")
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
        1. ``CONCINNO_NO_AUTOCOMMIT=1`` env override — for polling /
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
    if os.environ.get("CONCINNO_NO_AUTOCOMMIT") == "1":
        return None

    if cwd is None:
        cwd = os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())

    # Bump the per-call timeout floor: a large working tree can take
    # >15s for `add -A` on Windows even though the operation itself is
    # batch-fast. Callers that need a tighter bound can still pass one.
    op_timeout = max(60, timeout)

    if _git(["rev-parse", "--is-inside-work-tree"], cwd, timeout=timeout) != "true":
        return None

    # Clear stale .git/index.lock orphans before any write op. A fresh
    # lock (age < threshold) means a sibling is mid-commit: bail rather
    # than race. See ``_clear_stale_index_lock`` for the full rationale.
    if not _clear_stale_index_lock(cwd):
        return None

    # 2.10.4 治本 (FATAL F1): use -z + core.quotepath=false so CJK / spaced
    # paths arrive unquoted. _parse_status's "line[3:].strip()" handed
    # quoted paths like "交接_X.md" to os.stat() which then 404'd, so the
    # large-file/secret filters silently passed CJK files through. Real
    # ai-king CJK paths reproduced this on 2.10.3.
    #
    # Fallback to legacy `--short` parsing only if `-z` returns None — that
    # path is a hygiene safety net for environments where `_git_raw` cannot
    # spawn (constrained sandboxes, mocked subprocess in tests). Real git
    # repos always succeed via the -z path.
    records = _status_records_z(cwd, timeout=timeout)
    if records is not None:
        staged, unstaged, untracked = _parse_status_z(records)
    else:
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

    # 2.13.1 治本 — skip nested repo subdirs from `git add -A` to break
    # the outer-inner race of MEMORY #67. When an outer repo intentionally
    # tracks paths inside a nested working tree (e.g. ai-king's
    # `!projects/concinno/` carve-out), a plain `git add -A` stages the
    # inner-repo's untracked WIP into the outer index. Any subsequent
    # outer rebase/checkout that replays an older outer tree state can
    # then **delete those files from the inner working tree** (they are
    # now outer-tracked paths, and the old tree does not contain them).
    # The 2.10.2 snapshot/restore handles the rebase phase but does not
    # prevent the stage — the file gets drawn into the outer index long
    # before squash ever runs. Excluding the nested subdir from `add -A`
    # keeps outer blind to inner WIP; the inner repo owns its own commits.
    # Set ``CONCINNO_SKIP_NESTED_ADD=0`` to restore pre-2.13.1 behavior.
    nested_excludes: list[str] = []
    if os.environ.get("CONCINNO_SKIP_NESTED_ADD", "1") != "0":
        try:
            from concinno.cleanup import _detect_embedded_nested_repos
            nested_excludes = _detect_embedded_nested_repos(cwd)
        except Exception:
            nested_excludes = []

    # Batch stage everything (L0: never per-file).
    if nested_excludes:
        add_cmd = ["add", "-A", "--", "."] + [
            f":(exclude){rel}" for rel in nested_excludes
        ]
        _safe_stderr(
            f"concinno: skipping `git add -A` for {len(nested_excludes)} "
            f"nested repo subdir(s) to avoid outer-inner race: "
            f"{', '.join(nested_excludes[:3])}"
            + (" …" if len(nested_excludes) > 3 else "")
            + " (escape: CONCINNO_SKIP_NESTED_ADD=0)"
        )
    else:
        add_cmd = ["add", "-A"]
    if _git(add_cmd, cwd, timeout=op_timeout) is None:
        return None

    # Defensive unstage: remove any newly-detected secret-like files
    # from the index before committing. Already-tracked secrets that
    # predate this guard are out of scope — those need a manual
    # `git rm --cached`. This only protects against the new stage.
    secret_files = [f for f in all_files if _is_secret(f)]
    if secret_files:
        _git(["reset", "HEAD", "--", *secret_files], cwd, timeout=op_timeout)

    # 2.10.3 治本 — unstage large unignored blobs so they never enter
    # outer .git history. The squash fix (2.10.2) reclaims historical
    # bloat, but preventing the stage is cheaper than squashing it
    # later. MEMORY #77's 7.6 GB came from LoRA/safetensors/BEIR
    # corpus files the .gitignore missed — this is the belt to that
    # .gitignore suspenders.
    large_files = [
        f for f in safe_files if _is_large_unignored(f, cwd)
    ]
    if large_files:
        _git(["reset", "HEAD", "--", *large_files], cwd, timeout=op_timeout)
        _safe_stderr(
            f"concinno: unstaged {len(large_files)} large file(s) "
            f"(≥{_large_file_threshold() // 1_048_576} MiB each) to prevent "
            "repo bloat. Add matching patterns to .gitignore:\n  " +
            "\n  ".join(large_files[:5]) +
            ("\n  …" if len(large_files) > 5 else "") +
            "\n(escape: CONCINNO_LARGE_FILE_THRESHOLD=<bytes>)"
        )
        # Recompute safe_files: if ALL safe_files were large, there is
        # nothing left to commit. Otherwise commit the remainder.
        safe_files = [f for f in safe_files if f not in large_files]
        if not safe_files:
            return None

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
    keep defaults to CONCINNO_KEEP_COMMITS env var, or 3.
    """
    keep = int(os.environ.get("CONCINNO_KEEP_COMMITS", "3"))
    threshold = keep * 2

    count_str = _git(["rev-list", "--count", "HEAD"], cwd, timeout=timeout)
    if not count_str:
        return
    total = int(count_str)
    if total <= threshold:
        return

    # Import here to avoid circular dependency
    try:
        from concinno.cleanup import squash_auto_commits
        # destruction_gate escape: this is the trusted inline path that
        # auto-commit invokes from the stop hook pipeline. Set the
        # per-op flag so the decorator passes through without demanding
        # a reason kwarg.
        prev_flag = os.environ.get("CONCINNO_INLINE_SQUASH")
        os.environ["CONCINNO_INLINE_SQUASH"] = "1"
        try:
            result = squash_auto_commits(cwd, keep=keep)
        finally:
            if prev_flag is None:
                os.environ.pop("CONCINNO_INLINE_SQUASH", None)
            else:
                os.environ["CONCINNO_INLINE_SQUASH"] = prev_flag
        # Surface squash errors to stderr — prior `pass` silently masked
        # dirty-tree aborts so the "keep N commits" rule appeared to work
        # while .git bloated unbounded.
        if result.error:
            sys.stderr.write(f"concinno: inline squash skipped — {result.error}\n")
    except Exception as e:
        # Keep hook resilient: any unexpected failure logs, does not raise.
        sys.stderr.write(f"concinno: inline squash failed — {type(e).__name__}: {e}\n")


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
