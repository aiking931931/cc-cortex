"""concinno.l2_index — L2 SKILL.md frontmatter walker + reverse trigger index.

@module l2_index
@responsibility Walk all installed Skill directories (user-global +
    project-local), parse the YAML frontmatter at the top of each
    ``SKILL.md`` into a typed :class:`L2Frontmatter`, build a reverse
    index ``{trigger_keyword: [skill_name, ...]}``, and persist it to
    ``_AI_BRAIN/_triggers.json`` for fast O(1) trigger-to-skill lookup
    by other tools (e.g. ``inject_scheduled_logs`` hook).
@dependencies stdlib only
@exports L2Frontmatter, ReverseIndex, walk_skills, build_reverse_index,
    write_triggers_json, query_trigger, REVERSE_INDEX_VERSION

Schema (4.4.0)
--------------
Frontmatter fields the walker recognises (all optional except ``name``):

    name        : str          — Skill identifier (matches dir name).
    triggers    : list[str]    — Trigger keywords / phrases.
    category    : str          — Coarse grouping (cognition / dev / etc).
    last_used   : str (ISO)    — Timestamp of last invocation.
    description : str          — One-line summary.

Unknown frontmatter keys are preserved in :class:`L2Frontmatter.extra`
so future schema additions do not require walker changes.

Persistence schema (``_triggers.json``):

    {
      "version": "1.0",
      "build_time": "<ISO-8601>",
      "skills_scanned": <int>,
      "trigger_to_skills": {"<trigger>": ["<skill_name>", ...]}
    }

Sub-agent K wave-2 wire (4.4.0). Plan v1 line 64.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REVERSE_INDEX_VERSION = "1.0"


__all__ = [
    "REVERSE_INDEX_VERSION",
    "L2Frontmatter",
    "build_reverse_index",
    "default_skill_roots",
    "default_triggers_json_path",
    "main",
    "parse_frontmatter",
    "query_trigger",
    "read_triggers_json",
    "walk_skills",
    "write_triggers_json",
]


# ── Dataclass ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class L2Frontmatter:
    """Parsed L2 SKILL.md frontmatter.

    All fields default-empty so that a Skill with sparse / missing
    frontmatter still yields a valid object — the walker is tolerant.
    Use :attr:`is_valid` to check whether the entry has the minimum
    fields needed for the reverse index (``name`` + at least one
    trigger).
    """

    name: str = ""
    triggers: tuple[str, ...] = ()
    category: str = ""
    last_used: str = ""
    description: str = ""
    source_path: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def is_valid(self) -> bool:
        """True when the entry can contribute to the reverse index."""
        return bool(self.name) and bool(self.triggers)


# ── Frontmatter parser ────────────────────────────────────────────


def parse_frontmatter(raw_text: str, *, source_path: str = "") -> L2Frontmatter:
    """Parse the YAML-ish frontmatter at the top of a SKILL.md string.

    We deliberately do **not** import PyYAML to keep ``concinno`` core
    zero-dep. The parser is a small hand-rolled subset that handles
    the actual frontmatter shape used across all installed skills:

        ---
        name: skill-name
        description: ...one liner...
        triggers:
          - keyword-a
          - keyword-b
        category: dev
        ---

    Multi-line strings, anchors, and complex YAML are not supported —
    if a SKILL.md uses those, install PyYAML and pre-process it.
    Unknown scalar keys are stashed in ``extra``.

    Args:
        raw_text: Full file content of a SKILL.md (frontmatter +
            body). Anything past the closing ``---`` is ignored.
        source_path: Optional file path for audit / debug.

    Returns:
        :class:`L2Frontmatter`. Empty object (``L2Frontmatter()``)
        when the file has no frontmatter or it is malformed.
    """
    if not raw_text or not raw_text.lstrip().startswith("---"):
        return L2Frontmatter(source_path=source_path)

    # Locate the bounds of the frontmatter block.
    text = raw_text.lstrip()
    after_open = text[3:]  # skip leading "---"
    # Find closing fence at the start of a line.
    close_idx = -1
    for needle in ("\n---\n", "\n---\r\n", "\n---"):
        idx = after_open.find(needle)
        if idx != -1:
            close_idx = idx
            break
    if close_idx == -1:
        return L2Frontmatter(source_path=source_path)
    block = after_open[:close_idx]

    name = ""
    description = ""
    category = ""
    last_used = ""
    triggers: list[str] = []
    extra: dict[str, Any] = {}

    current_list_key: str | None = None
    for raw_line in block.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            current_list_key = None
            continue
        if line.startswith("  - "):
            # List item. Belongs to the most recent list-key.
            value = line[4:].strip().strip('"').strip("'")
            if current_list_key == "triggers":
                triggers.append(value)
            elif current_list_key:
                # Stash other lists into extra as plain lists.
                bucket = extra.setdefault(current_list_key, [])
                if isinstance(bucket, list):
                    bucket.append(value)
            continue
        if ":" not in line:
            current_list_key = None
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip()
        if not val:
            # Key with empty value → expect a list to follow.
            current_list_key = key
            continue
        current_list_key = None
        # Strip wrapping quotes.
        if (val.startswith('"') and val.endswith('"')) or (
            val.startswith("'") and val.endswith("'")
        ):
            val = val[1:-1]
        if key == "name":
            name = val
        elif key == "description":
            description = val
        elif key == "category":
            category = val
        elif key == "last_used":
            last_used = val
        elif key == "triggers":
            # Inline list form: ``triggers: [a, b, c]`` — rare but possible.
            if val.startswith("[") and val.endswith("]"):
                inner = val[1:-1]
                triggers = [
                    t.strip().strip('"').strip("'")
                    for t in inner.split(",")
                    if t.strip()
                ]
            else:
                # Single-string trigger.
                triggers = [val]
        else:
            extra[key] = val

    return L2Frontmatter(
        name=name,
        triggers=tuple(triggers),
        category=category,
        last_used=last_used,
        description=description,
        source_path=source_path,
        extra=extra,
    )


# ── Walker ────────────────────────────────────────────────────────


def default_skill_roots() -> list[Path]:
    """Resolve the canonical skill directories on this machine.

    Returns:
        List of ``Path`` candidates. Paths that do not exist are
        included so that callers can decide whether to skip or warn.
        Order: user-global skills → project-local ``.claude/skills``.
    """
    roots: list[Path] = []
    env_override = os.environ.get("CONCINNO_SKILL_ROOTS")
    if env_override:
        for p in env_override.split(os.pathsep):
            if p.strip():
                roots.append(Path(p.strip()).expanduser())
        return roots
    home = Path.home()
    roots.append(home / ".claude" / "skills")
    cwd = Path.cwd()
    roots.append(cwd / ".claude" / "skills")
    return roots


def walk_skills(
    roots: Iterable[Path] | None = None,
    *,
    skip_quarantine: bool = True,
) -> list[L2Frontmatter]:
    """Walk every ``SKILL.md`` under ``roots`` and parse its frontmatter.

    Args:
        roots: Iterable of directories to scan. Defaults to
            :func:`default_skill_roots`.
        skip_quarantine: When True (default), skip any path containing
            a ``_quarantine`` segment — those are dormant Composio /
            third-party imports that shouldn't pollute the index.

    Returns:
        List of :class:`L2Frontmatter` for every readable ``SKILL.md``
        found, including invalid entries (caller may filter on
        :attr:`L2Frontmatter.is_valid`).
    """
    if roots is None:
        roots = default_skill_roots()
    out: list[L2Frontmatter] = []
    for root in roots:
        if not root or not root.exists() or not root.is_dir():
            continue
        for skill_md in root.rglob("SKILL.md"):
            if skip_quarantine and "_quarantine" in skill_md.parts:
                continue
            try:
                raw = skill_md.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            entry = parse_frontmatter(raw, source_path=str(skill_md))
            # Fall back to directory name when frontmatter omits it.
            if not entry.name:
                entry = L2Frontmatter(
                    name=skill_md.parent.name,
                    triggers=entry.triggers,
                    category=entry.category,
                    last_used=entry.last_used,
                    description=entry.description,
                    source_path=entry.source_path,
                    extra=entry.extra,
                )
            out.append(entry)
    return out


# ── Reverse index ─────────────────────────────────────────────────


def build_reverse_index(
    entries: Iterable[L2Frontmatter],
) -> dict[str, list[str]]:
    """Invert ``[(name, [triggers...])]`` into ``{trigger: [name...]}``.

    Trigger strings are normalised (stripped + lowercased) before
    being keyed so that ``"Sediment"`` and ``"sediment "`` map to the
    same bucket. Skill names within each bucket are deduped while
    preserving first-seen order.

    Args:
        entries: Output of :func:`walk_skills` (or any iterable of
            :class:`L2Frontmatter`).

    Returns:
        ``{trigger_key: [skill_name, ...]}`` — buckets are sorted
        skill-name ascending for deterministic output.
    """
    raw: dict[str, list[str]] = {}
    for entry in entries:
        if not entry.is_valid:
            continue
        for trigger in entry.triggers:
            key = (trigger or "").strip().lower()
            if not key:
                continue
            bucket = raw.setdefault(key, [])
            if entry.name not in bucket:
                bucket.append(entry.name)
    # Sort each bucket for deterministic JSON.
    for key, bucket in raw.items():
        bucket.sort()
    return raw


# ── Persistence ───────────────────────────────────────────────────


def default_triggers_json_path() -> Path:
    """Default location for the persisted reverse index.

    Resolution order:
      1. ``CONCINNO_TRIGGERS_JSON_PATH`` env override.
      2. ``<cwd>/_AI_BRAIN/_triggers.json`` if ``_AI_BRAIN`` dir exists.
      3. ``~/.concinno/_triggers.json`` fallback.
    """
    override = os.environ.get("CONCINNO_TRIGGERS_JSON_PATH")
    if override:
        return Path(override).expanduser()
    cwd_brain = Path.cwd() / "_AI_BRAIN"
    if cwd_brain.is_dir():
        return cwd_brain / "_triggers.json"
    return Path.home() / ".concinno" / "_triggers.json"


def write_triggers_json(
    reverse_index: dict[str, list[str]],
    *,
    path: Path | None = None,
    skills_scanned: int | None = None,
) -> Path:
    """Persist a reverse index to disk in versioned JSON.

    Args:
        reverse_index: Output of :func:`build_reverse_index`.
        path: Override the default destination.
        skills_scanned: Optional total count of SKILL.md files walked
            (for audit). When ``None``, recorded as ``-1``.

    Returns:
        The path actually written.
    """
    target = path or default_triggers_json_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": REVERSE_INDEX_VERSION,
        "build_time": datetime.now(timezone.utc).isoformat(),
        "skills_scanned": -1 if skills_scanned is None else int(skills_scanned),
        "trigger_to_skills": reverse_index,
    }
    target.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False),
        encoding="utf-8",
    )
    return target


def read_triggers_json(path: Path | None = None) -> dict[str, Any]:
    """Load and return the persisted reverse index payload.

    Returns ``{}`` when the file does not exist or is malformed.
    """
    target = path or default_triggers_json_path()
    if not target.exists():
        return {}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def query_trigger(
    keyword: str,
    *,
    path: Path | None = None,
) -> list[str]:
    """Return skills associated with ``keyword`` from the persisted index.

    The lookup is normalised the same way as :func:`build_reverse_index`
    (strip + lowercase) so callers can pass user-typed keywords as-is.

    Returns ``[]`` when the index file is missing, the keyword has
    no entry, or the file is corrupt.
    """
    payload = read_triggers_json(path=path)
    table = payload.get("trigger_to_skills", {})
    if not isinstance(table, dict):
        return []
    return list(table.get(keyword.strip().lower(), []) or [])


# ── CLI ───────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    """``concinno l2-index <build|query>`` entry point.

    Exit codes:
        0 — success.
        1 — query missed (keyword not in index).
        2 — invalid arguments / build failure.
    """
    parser = argparse.ArgumentParser(
        prog="concinno-l2-index",
        description="L2 SKILL.md frontmatter walker + reverse trigger index.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    build_p = sub.add_parser(
        "build",
        help="Walk skills, build reverse index, write _triggers.json.",
    )
    build_p.add_argument(
        "--root",
        action="append",
        default=None,
        help="Override skill root (repeatable). Default = user + project.",
    )
    build_p.add_argument(
        "--out",
        default=None,
        help="Override output path (default = _AI_BRAIN/_triggers.json).",
    )

    query_p = sub.add_parser(
        "query",
        help="Look up a trigger keyword in the persisted index.",
    )
    query_p.add_argument("keyword", help="Trigger keyword to look up.")
    query_p.add_argument(
        "--in",
        dest="in_path",
        default=None,
        help="Override input path.",
    )

    args = parser.parse_args(argv)

    if args.cmd == "build":
        roots = (
            [Path(p) for p in args.root]
            if args.root
            else default_skill_roots()
        )
        entries = walk_skills(roots)
        rev = build_reverse_index(entries)
        out_path = Path(args.out) if args.out else None
        target = write_triggers_json(
            rev, path=out_path, skills_scanned=len(entries)
        )
        print(
            f"l2-index: scanned={len(entries)} "
            f"triggers={len(rev)} → {target}",
            file=sys.stderr,
        )
        return 0

    if args.cmd == "query":
        in_path = Path(args.in_path) if args.in_path else None
        hits = query_trigger(args.keyword, path=in_path)
        if not hits:
            print(f"l2-index: no skills for {args.keyword!r}", file=sys.stderr)
            return 1
        for name in hits:
            print(name)
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
