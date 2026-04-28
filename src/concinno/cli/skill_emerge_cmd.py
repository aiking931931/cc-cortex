"""``concinno skill-emerge {list, show, accept, reject, prune}`` CLI.

Out-of-band acceptance / rejection workflow for drafts proposed by
:class:`concinno.skills.SkillEmergenceGuard`. The guard never installs
into ``~/.claude/skills/`` automatically — only this CLI does, on
explicit user invocation.

Subcommands:

    list                — print all drafts + resolution status (one per line).
    show <slug>         — print draft markdown to stdout.
    accept <slug>       — move draft into the live Skill directory and
                          mark accepted (emits ZIQ reward=1.0).
    reject <slug>       — delete draft markdown and mark rejected
                          (emits ZIQ reward=0.0).
    prune               — remove drafts older than the retention window
                          (mirrors the in-process retention sweep).

Path overrides (tests / sandboxes):

    ``CONCINNO_SKILL_DRAFT_DIR``   — staging dir (default ``~/.concinno/skill_drafts``).
    ``CONCINNO_LIVE_SKILL_ROOT``   — install root (default ``~/.claude/skills``).

Acceptance never overwrites an existing live Skill — the user is
instructed to handle the conflict manually so that hand-tuned Skills
are not silently clobbered by a draft auto-proposed from the same slug.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from concinno.skills.skill_emergence_guard import (
    SkillEmergenceGuard,
    _load_state,
    _save_state,
    draft_root,
    live_skill_root,
)

__all__ = ["register"]


# ── Helpers ───────────────────────────────────────────────


def _resolve_draft_path(slug: str) -> Path:
    return draft_root() / f"{slug}.md"


def _resolve_live_path(slug: str) -> Path:
    return live_skill_root() / slug / "SKILL.md"


def _print_err(msg: str) -> None:
    print(msg, file=sys.stderr)


# ── Subcommand handlers ───────────────────────────────────


def _cmd_list(_args: argparse.Namespace) -> int:
    state = _load_state()
    if not state.drafts_index:
        print("(no drafts staged)")
        return 0
    rows: list[tuple[str, str, str]] = []
    for slug, entry in sorted(state.drafts_index.items()):
        resolution = entry.get("resolution") or "pending"
        kind = entry.get("trigger_kind") or "unknown"
        rows.append((slug, resolution, kind))
    width = max(len(s) for s, _, _ in rows)
    for slug, resolution, kind in rows:
        print(f"{slug:<{width}}  {resolution:<8}  {kind}")
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    md = _resolve_draft_path(args.slug)
    if not md.exists():
        _print_err(f"no draft found for slug {args.slug!r} at {md}")
        return 1
    sys.stdout.write(md.read_text(encoding="utf-8"))
    return 0


def _cmd_accept(args: argparse.Namespace) -> int:
    slug = args.slug
    md = _resolve_draft_path(slug)
    if not md.exists():
        _print_err(f"no draft found for slug {slug!r} at {md}")
        return 1

    target = _resolve_live_path(slug)
    if target.exists() and not args.force:
        _print_err(
            f"refusing to overwrite existing Skill at {target}. "
            f"Inspect the live file, then re-run with --force to "
            f"replace it, or move it aside manually."
        )
        return 2

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(md.read_text(encoding="utf-8"), encoding="utf-8")

    guard = SkillEmergenceGuard()
    recorded = guard.record_accept(slug)
    if not recorded:
        # Index drift (file present but index missing entry) — still
        # surface as success since the install completed; warn so the
        # operator can clean up out-of-band.
        _print_err(
            f"warning: draft {slug!r} not found in state index; "
            f"installed at {target} without ZIQ outcome emission."
        )

    if not args.keep_draft:
        try:
            md.unlink()
        except OSError as exc:
            _print_err(f"warning: could not remove draft file {md}: {exc}")

    print(f"accepted {slug} -> {target}")
    return 0


def _cmd_reject(args: argparse.Namespace) -> int:
    slug = args.slug
    md = _resolve_draft_path(slug)
    guard = SkillEmergenceGuard()
    recorded = guard.record_reject(slug)
    if not recorded and not md.exists():
        _print_err(f"no draft found for slug {slug!r}")
        return 1

    if md.exists():
        try:
            md.unlink()
        except OSError as exc:
            _print_err(f"warning: could not remove draft file {md}: {exc}")
            return 3

    print(f"rejected {slug}")
    return 0


def _cmd_prune(_args: argparse.Namespace) -> int:
    """Remove resolved drafts (accepted/rejected) from the index.

    The in-process guard prunes by *age*; this CLI variant prunes by
    *resolution* so operators can cleanly snapshot the index after a
    review session without waiting for the retention window.
    """
    state = _load_state()
    pruned: list[str] = []
    for slug, entry in list(state.drafts_index.items()):
        if entry.get("resolution") in ("accepted", "rejected"):
            pruned.append(slug)
            state.drafts_index.pop(slug, None)
            md = _resolve_draft_path(slug)
            try:
                if md.exists():
                    md.unlink()
            except OSError:
                pass
    if pruned:
        _save_state(state)
    if not pruned:
        print("(nothing to prune)")
        return 0
    for slug in pruned:
        print(f"pruned {slug}")
    return 0


# ── Dispatcher + registration ─────────────────────────────


def _dispatch(args: argparse.Namespace) -> None:
    action = args.skill_emerge_action
    handlers = {
        "list": _cmd_list,
        "show": _cmd_show,
        "accept": _cmd_accept,
        "reject": _cmd_reject,
        "prune": _cmd_prune,
    }
    handler = handlers.get(action)
    if handler is None:
        _print_err(
            "usage: concinno skill-emerge {list|show|accept|reject|prune}"
        )
        sys.exit(2)
    sys.exit(handler(args))


def register(
    subparsers: "argparse._SubParsersAction[argparse.ArgumentParser]",
) -> None:
    """Register the ``concinno skill-emerge ...`` namespace."""
    p = subparsers.add_parser(
        "skill-emerge",
        help=(
            "Manage SkillEmergenceGuard drafts staged at "
            "~/.concinno/skill_drafts/. Drafts are never auto-installed; "
            "this CLI is the explicit user-action layer."
        ),
    )
    sub = p.add_subparsers(dest="skill_emerge_action")

    p_list = sub.add_parser("list", help="List staged drafts and status")
    p_list.set_defaults(func=_dispatch)

    p_show = sub.add_parser("show", help="Print draft markdown to stdout")
    p_show.add_argument("slug", help="Draft slug (filename without .md)")
    p_show.set_defaults(func=_dispatch)

    p_accept = sub.add_parser(
        "accept",
        help="Install draft to ~/.claude/skills/<slug>/SKILL.md (ZIQ reward=1.0)",
    )
    p_accept.add_argument("slug", help="Draft slug to accept")
    p_accept.add_argument(
        "--force", action="store_true",
        help="Overwrite an existing live Skill at the install path",
    )
    p_accept.add_argument(
        "--keep-draft", action="store_true",
        help="Keep the draft file in the staging dir after install (default: remove)",
    )
    p_accept.set_defaults(func=_dispatch)

    p_reject = sub.add_parser(
        "reject",
        help="Delete draft and mark rejected (ZIQ reward=0.0)",
    )
    p_reject.add_argument("slug", help="Draft slug to reject")
    p_reject.set_defaults(func=_dispatch)

    p_prune = sub.add_parser(
        "prune",
        help="Remove all resolved (accepted/rejected) drafts from index + disk",
    )
    p_prune.set_defaults(func=_dispatch)
