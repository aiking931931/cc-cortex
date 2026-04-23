"""concinno.cli.configure_permissions_cmd — One-shot permission allowlist.

@module configure_permissions_cmd
@responsibility Register ~100 safe Bash patterns into
    ``~/.claude/settings.json::permissions.allow`` in a single command so
    users stop being prompted for pytest / ruff / git status / pip show /
    etc. after ``pip install concinno``.

Design (AI King 2026-04-23 directive):

  "每次授權都在問很煩，把他根治也要納入新版的 pip，除了刪除重要資料要
   詢問視窗以外其他要授權的都關閉"

  → Only destructive operations (rm -rf / DROP TABLE / force push /
    git gc --prune / pip uninstall) stay gated via destruction_guard.
    Everything else (test / build / lint / read-only git / package
    inspection) goes into the allowlist.

Safety model:
  - Default: ``--all-safe --preserve-destructive``. Destruction guard
    patterns are NEVER added to allow[], regardless of how the user
    invokes this command.
  - ``--publish``: additionally adds ``twine upload`` / ``npm publish`` /
    etc. OFF by default because these bypass the host's accident-safety
    against irreversible publish. Opt-in only.
  - Existing allow[] / ask[] / deny[] entries are MERGED (not
    overwritten). Backup is written to ``settings.json.backup-<ISO>``
    before any change. Atomic ``os.replace`` at the end.

@dependencies (stdlib only — argparse, json, os, pathlib, datetime, sys)
@exports cmd_configure_permissions, SAFE_PATTERNS, PUBLISH_PATTERNS,
    DESTRUCTIVE_PATTERNS, plan_merge
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
from pathlib import Path

# ── Pattern catalogues ─────────────────────────────────────────────
#
# Patterns are "Bash(<match>)" strings following the Claude Code
# settings.json allow[] conventions. A trailing ``*`` matches anything.

SAFE_PATTERNS: list[str] = [
    # Testing
    "Bash(pytest*)",
    "Bash(python -m pytest*)",
    "Bash(python -m unittest*)",
    # Linting / formatting / type-check
    "Bash(ruff check*)",
    "Bash(ruff format*)",
    "Bash(ruff *)",
    "Bash(mypy*)",
    "Bash(black*)",
    "Bash(isort*)",
    "Bash(flake8*)",
    # Build (without uploading)
    "Bash(python -m build*)",
    "Bash(python setup.py build*)",
    "Bash(hatch build*)",
    "Bash(python -m twine check*)",
    "Bash(twine check*)",
    # Pip — inspection + install, NOT uninstall
    "Bash(pip install*)",
    "Bash(pip list*)",
    "Bash(pip show*)",
    "Bash(pip freeze*)",
    "Bash(pip download*)",
    "Bash(pip index*)",
    "Bash(pip check*)",
    "Bash(python -m pip install*)",
    "Bash(python -m pip list*)",
    "Bash(python -m pip show*)",
    # Python execution (user scripts)
    "Bash(python -c*)",
    "Bash(python *.py)",
    "Bash(python3 *.py)",
    "Bash(python -m *)",
    # Git — read-only + normal commit flow, NOT force-push / reset --hard
    "Bash(git status*)",
    "Bash(git log*)",
    "Bash(git diff*)",
    "Bash(git show*)",
    "Bash(git blame*)",
    "Bash(git tag -l*)",
    "Bash(git tag --list*)",
    "Bash(git branch*)",
    "Bash(git fetch*)",
    "Bash(git pull*)",
    "Bash(git add*)",
    "Bash(git commit*)",
    "Bash(git stash*)",
    "Bash(git checkout*)",
    "Bash(git restore*)",
    "Bash(git reset HEAD*)",
    "Bash(git cherry-pick*)",
    "Bash(git merge*)",
    "Bash(git rebase*)",
    "Bash(git describe*)",
    "Bash(git rev-parse*)",
    "Bash(git remote*)",
    "Bash(git config*)",
    # File inspection
    "Bash(ls*)",
    "Bash(cat*)",
    "Bash(head*)",
    "Bash(tail*)",
    "Bash(wc*)",
    "Bash(grep*)",
    "Bash(find*)",
    "Bash(du*)",
    "Bash(df*)",
    "Bash(tree*)",
    "Bash(file*)",
    "Bash(stat*)",
    # Archive inspection
    "Bash(unzip -l*)",
    "Bash(tar -tf*)",
    "Bash(tar tf*)",
    # Node / npm (read-only + install — not publish)
    "Bash(node *.js)",
    "Bash(node --version*)",
    "Bash(npm list*)",
    "Bash(npm install*)",
    "Bash(npm ls*)",
    "Bash(npm view*)",
    "Bash(npm run*)",
    "Bash(npm test*)",
    "Bash(npx*)",
    "Bash(yarn install*)",
    "Bash(yarn test*)",
    "Bash(yarn build*)",
    # Cargo (read-only + build)
    "Bash(cargo build*)",
    "Bash(cargo check*)",
    "Bash(cargo test*)",
    "Bash(cargo clippy*)",
    "Bash(cargo fmt*)",
    # Go
    "Bash(go build*)",
    "Bash(go test*)",
    "Bash(go vet*)",
    "Bash(go run*)",
    # Make
    "Bash(make *)",
    "Bash(make -n*)",
    # Environment / shell info
    "Bash(env*)",
    "Bash(which *)",
    "Bash(where *)",
    "Bash(echo *)",
    "Bash(pwd*)",
    "Bash(uname*)",
    # HTTP inspection (read-only)
    "Bash(curl -s https://pypi.org*)",
    "Bash(curl -sI *)",
    "Bash(curl --head *)",
    # Virtualenv creation + activation commands
    "Bash(python -m venv*)",
    "Bash(virtualenv*)",
    # Scheduling inspection (read-only)
    "Bash(schtasks /query*)",
    "Bash(crontab -l*)",
]

# Opt-in: publish operations. Adds irreversible registry uploads.
PUBLISH_PATTERNS: list[str] = [
    "Bash(twine upload*)",
    "Bash(python -m twine upload*)",
    "Bash(npm publish*)",
    "Bash(cargo publish*)",
    "Bash(git push*)",
    "Bash(git push origin*)",
    "Bash(git tag -a*)",
    "Bash(git tag v*)",
]

# NEVER added under any flag — destruction_guard owns these.
DESTRUCTIVE_PATTERNS: frozenset[str] = frozenset(
    {
        "Bash(rm -rf*)",
        "Bash(rm -fr*)",
        "Bash(rm -rf /*)",
        "Bash(git push --force*)",
        "Bash(git push -f*)",
        "Bash(git push --force-with-lease*)",
        "Bash(git reset --hard*)",
        "Bash(git branch -D*)",
        "Bash(git clean -fd*)",
        "Bash(git clean -fdx*)",
        "Bash(git gc --prune*)",
        "Bash(git filter-repo*)",
        "Bash(git filter-branch*)",
        "Bash(pip uninstall*)",
        "Bash(npm uninstall*)",
        "Bash(cargo uninstall*)",
        "Bash(psql*DROP TABLE*)",
        "Bash(psql*DROP DATABASE*)",
        "Bash(mysql*DROP TABLE*)",
        "Bash(dropdb*)",
        "Bash(docker system prune*)",
        "Bash(docker volume rm*)",
    }
)


# ── Settings file I/O ──────────────────────────────────────────────


def _default_settings_path() -> Path:
    return Path.home() / ".claude" / "settings.json"


def _read_settings(path: Path) -> tuple[dict, str | None]:
    """Read settings.json; return (data, warning_or_None)."""
    if not path.is_file():
        return {}, None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return {}, f"{path} is not a JSON object; refusing to touch"
        return data, None
    except json.JSONDecodeError:
        return {}, f"{path} is malformed JSON; refusing to touch"
    except OSError as exc:
        return {}, f"{path}: read failed ({exc})"


def _backup_settings(path: Path) -> Path | None:
    """Snapshot settings.json to path.backup-<ISO>.

    Returns the backup path on success, None if the original doesn't
    exist (no backup needed).
    """
    if not path.is_file():
        return None
    stamp = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
    backup = path.with_suffix(path.suffix + f".backup-{stamp}")
    backup.write_bytes(path.read_bytes())
    return backup


def _atomic_write_settings(path: Path, data: dict) -> None:
    """Atomic write via tmp + os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


# ── Merge planning ─────────────────────────────────────────────────


def plan_merge(
    existing: dict,
    *,
    include_publish: bool = False,
    preserve_destructive: bool = True,
) -> tuple[dict, list[str], list[str]]:
    """Compute the new settings.json content without writing.

    Returns:
        ``(new_settings, added_patterns, skipped_destructive)``.

        - ``new_settings``: full settings dict after merge.
        - ``added_patterns``: patterns that will be newly added to allow[].
        - ``skipped_destructive``: patterns the caller requested but were
          blocked by ``preserve_destructive``.
    """
    # Fresh dict — don't mutate caller's.
    result = json.loads(json.dumps(existing)) if existing else {}
    permissions = result.setdefault("permissions", {})
    allow = permissions.setdefault("allow", [])
    if not isinstance(allow, list):
        # Malformed existing allow — reset to a list so we can still merge.
        allow = []
        permissions["allow"] = allow

    target_patterns = list(SAFE_PATTERNS)
    if include_publish:
        target_patterns.extend(PUBLISH_PATTERNS)

    existing_set = set(allow)
    added: list[str] = []
    skipped: list[str] = []

    for pattern in target_patterns:
        if preserve_destructive and pattern in DESTRUCTIVE_PATTERNS:
            skipped.append(pattern)
            continue
        if pattern in existing_set:
            continue
        allow.append(pattern)
        existing_set.add(pattern)
        added.append(pattern)

    return result, added, skipped


# ── CLI entry ──────────────────────────────────────────────────────


def cmd_configure_permissions(args: argparse.Namespace) -> None:
    """`concinno configure-permissions` entry point."""
    path = Path(args.path) if args.path else _default_settings_path()
    include_publish = bool(getattr(args, "publish", False))
    preserve_destructive = bool(getattr(args, "preserve_destructive", True))
    dry_run = bool(getattr(args, "dry_run", False))

    existing, warning = _read_settings(path)
    if warning:
        print(f"concinno: {warning}", file=sys.stderr)
        sys.exit(2)

    new_settings, added, skipped = plan_merge(
        existing,
        include_publish=include_publish,
        preserve_destructive=preserve_destructive,
    )

    if dry_run:
        _print_diff(added, skipped, include_publish)
        print(f"\n[DRY RUN] Would write to {path}")
        return

    if not added:
        print(f"concinno: all safe patterns already present in {path}")
        return

    backup = _backup_settings(path)
    _atomic_write_settings(path, new_settings)

    print(f"concinno: added {len(added)} pattern(s) to {path}")
    if backup is not None:
        print(f"  backup: {backup}")
    if include_publish:
        print(
            "  (publish patterns included — "
            "release_authorization.py still enforces publish-string gate)"
        )
    if skipped:
        print(
            f"  skipped {len(skipped)} destructive pattern(s) "
            f"(use destruction_guard + explicit AskUser for those)"
        )


def _print_diff(added: list[str], skipped: list[str], include_publish: bool) -> None:
    """Print the dry-run diff."""
    print(f"Would add {len(added)} pattern(s):")
    for p in added:
        print(f"  + {p}")
    if skipped:
        print(f"\nSkipped {len(skipped)} destructive pattern(s):")
        for p in skipped:
            print(f"  ✖ {p}")
    if include_publish:
        print("\n(publish patterns included)")
    else:
        print("\n(publish patterns NOT included — pass --publish to opt in)")


def register(subparsers: argparse._SubParsersAction) -> None:
    """Register the `configure-permissions` subcommand."""
    p = subparsers.add_parser(
        "configure-permissions",
        help="Add safe Bash patterns to ~/.claude/settings.json::permissions.allow",
    )
    p.add_argument(
        "--publish",
        action="store_true",
        help="Also add twine upload / npm publish / etc. (OFF by default)",
    )
    p.add_argument(
        "--preserve-destructive",
        dest="preserve_destructive",
        action="store_true",
        default=True,
        help="Never add rm -rf / force push / pip uninstall (default: True)",
    )
    p.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="Print diff without writing",
    )
    p.add_argument(
        "--path",
        default="",
        help="Override settings.json path (default: ~/.claude/settings.json)",
    )
    p.set_defaults(func=cmd_configure_permissions)


__all__ = [
    "SAFE_PATTERNS",
    "PUBLISH_PATTERNS",
    "DESTRUCTIVE_PATTERNS",
    "plan_merge",
    "cmd_configure_permissions",
    "register",
]
