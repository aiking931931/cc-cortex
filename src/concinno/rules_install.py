"""concinno.rules_install — install the bundled official rule set into
the user's ``~/.claude/rules/`` tree.

Ships alongside Concinno so every ``pip install concinno`` user gets
the same L0 + L1 + switches.md baseline without curl / git clone.
Idempotent: identical content is not rewritten; orphaned files
carrying the Concinno anchor are cleaned up.

User directive 2026-04-24: "通用規則也要在 PyPI 包裡面. 規則分公開
和私人, 公開的改叫官方". Hence this module ships the rules under
``concinno/rules/official/`` and installs them into
``~/.claude/rules/official/`` so the user's hand-authored
``~/.claude/rules/private/`` (and the canonical
``~/.claude/rules/00-L0.md`` + ``L1/*.md``) stay untouched.

@exports install_rules, list_official_rules, official_root, main
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

__all__ = [
    "install_rules",
    "list_official_rules",
    "official_root",
    "user_official_root",
    "user_private_root",
    "main",
]

ANCHOR_COMMENT = "<!-- concinno-official-rule: do-not-edit -->"


def official_root() -> Path:
    """Return the path to the bundled ``rules/official/`` inside the
    installed package (the SSOT shipped to PyPI).

    Only files under ``rules/official/`` are installed by
    :func:`install_rules`. Files under ``rules/reference/`` are
    shipped alongside but **not auto-installed** — they mix portable
    methodology with author-specific workflow and need a red/blue CBUA
    split (scoped for 2.29.0) before they can honestly be called
    official.
    """
    return Path(__file__).parent / "rules" / "official"


def reference_root() -> Path:
    """Return the bundled ``rules/reference/`` path.

    Reference files are shipped but skipped by :func:`install_rules`;
    see ``rules/reference/README.md`` for per-file audit and adoption
    guidance.
    """
    return Path(__file__).parent / "rules" / "reference"


def user_official_root() -> Path:
    return Path.home() / ".claude" / "rules" / "official"


def user_private_root() -> Path:
    return Path.home() / ".claude" / "rules" / "private"


def list_official_rules() -> list[dict]:
    """List every .md file under the bundled official tree."""
    root = official_root()
    if not root.is_dir():
        return []
    rows: list[dict] = []
    for p in sorted(root.rglob("*.md")):
        rel = p.relative_to(root).as_posix()
        rows.append({
            "path": str(p),
            "relative": rel,
            "size": p.stat().st_size,
        })
    return rows


def install_rules(*, target: str | Path | None = None,
                  dry_run: bool = False) -> dict:
    """Copy the bundled official rules into the user's rules tree.

    Safety:
      - Only writes under ``~/.claude/rules/official/`` (or ``target``).
      - The user's ``private/`` tree and hand-edited files at the
        canonical root (``~/.claude/rules/00-L0.md``, ``L1/*.md``) are
        NEVER touched.
      - ``dry_run=True`` returns the planned changes without writing.
    """
    source = official_root()
    if not source.is_dir():
        raise FileNotFoundError(
            f"bundled official rules not found at {source} — is the "
            f"package installed correctly?"
        )
    dest = Path(target) if target else user_official_root()
    report: dict = {
        "source": str(source),
        "dest": str(dest),
        "dry_run": dry_run,
        "written": [],
        "unchanged": [],
        "removed_orphans": [],
    }
    if not dry_run:
        dest.mkdir(parents=True, exist_ok=True)

    bundled: set[str] = set()
    for src in sorted(source.rglob("*.md")):
        rel = src.relative_to(source)
        bundled.add(rel.as_posix())
        dst = dest / rel
        body = src.read_text(encoding="utf-8")
        if dst.is_file() and dst.read_text(encoding="utf-8") == body:
            report["unchanged"].append(rel.as_posix())
            continue
        if dry_run:
            report["written"].append(rel.as_posix() + " (would write)")
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(body, encoding="utf-8")
        report["written"].append(rel.as_posix())

    # Clean orphans under dest that Concinno owns (by anchor comment)
    if dest.is_dir():
        for existing in dest.rglob("*.md"):
            rel = existing.relative_to(dest).as_posix()
            if rel in bundled:
                continue
            try:
                text = existing.read_text(encoding="utf-8")
            except Exception:
                continue
            if ANCHOR_COMMENT in text:
                if dry_run:
                    report["removed_orphans"].append(rel + " (would remove)")
                else:
                    existing.unlink()
                    report["removed_orphans"].append(rel)

    return report


def uninstall_rules(*, target: str | Path | None = None) -> dict:
    """Remove the Concinno-managed ``rules/official/`` tree entirely.

    Leaves ``rules/private/`` and the canonical ``rules/00-L0.md`` /
    ``L1/*.md`` untouched.
    """
    dest = Path(target) if target else user_official_root()
    if not dest.is_dir():
        return {"dest": str(dest), "removed": False}
    shutil.rmtree(dest)
    return {"dest": str(dest), "removed": True}


def main(argv: list[str] | None = None) -> int:
    """CLI: ``concinno rules {install,list,uninstall}``."""
    args = argv if argv is not None else sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print("Usage: concinno rules {install|list|uninstall} [path]")
        return 0
    cmd = args[0]
    path = args[1] if len(args) > 1 else None
    if cmd == "install":
        rep = install_rules(target=path)
        print(json.dumps(rep, indent=2))
        return 0
    if cmd == "list":
        for r in list_official_rules():
            print(f"  {r['relative']:40} {r['size']:>8} B")
        return 0
    if cmd == "uninstall":
        rep = uninstall_rules(target=path)
        print(json.dumps(rep, indent=2))
        return 0
    if cmd == "dry-run":
        rep = install_rules(target=path, dry_run=True)
        print(json.dumps(rep, indent=2))
        return 0
    print(f"unknown subcommand: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
