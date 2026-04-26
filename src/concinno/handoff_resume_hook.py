"""concinno.handoff_resume_hook — On-start §0 + §5 inject for resumed handoffs.

@module handoff_resume_hook
@responsibility UserPromptSubmit-time helper: detect "resume project X"
    intent in the user message → load existing handoff Index for project
    X → if §0 fields are stale (>24hr old), re-run FieldRead → inject §0,
    §1, §5 (next_step) compactly into the prompt as system context.
@dependencies concinno.handoff_section0, concinno.handoff_composer,
    concinno.handoff_templates (lazy)
@exports detect_resume_intent, build_resume_inject, STALENESS_SECONDS

Why a separate module (not inside ``hooks/on_prompt_submit.py``):
  The CC-side hook handler is the *integration* point — this module
  is the *logic*. Keeping logic out of the hook handler means we can:
    1. unit-test the resume detector and the inject builder without
       spinning up a CC harness;
    2. let third-party hook hosts (Sancio runtime, plain Python
       agents, REPL workflows) reuse the same logic by importing
       from a stable module path.

Trigger detection (per spec):
  Match user message against the documented triggers in
  ``rules/L1/multilingual_triggers.md`` for the ``handoff`` row plus
  the explicit ``/handoff resume X`` slash form. Keep the matcher
  syntactic (regex on a small set of canonical phrasings + path-like
  tokens) per the multilingual rule's hook-author guidance — semantic
  matching is the model's job at higher layers; here we just need a
  cheap detector that doesn't fire false-positively on every prompt.

§0 staleness:
  Default 24 hours (``STALENESS_SECONDS = 86400``). When the existing
  handoff's §0 ``populated_at`` timestamp is older than the cutoff (or
  unparseable, or absent), the helper repopulates §0 before injection.
  Repopulation is incremental: only the §0 block is rewritten, the
  rest of the handoff is left untouched.

What does NOT get injected:
  §2 / §3 / §4 (decision rationale / pitfalls / pointers) are
  read-on-demand. Injecting them on every resume defeats the point
  of the new template system — those sections are precisely the kind
  of detail that a token-aware resume should cite by reference, not
  re-paste.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

STALENESS_SECONDS = 24 * 60 * 60
"""Cutoff age (seconds) above which §0 is considered stale and re-populated."""

# ── Trigger detection ──────────────────────────────────────

# Slash-style trigger: ``/handoff resume <project>``.
_SLASH_TRIGGER = re.compile(
    r"^/handoff\s+resume\s+(?P<project>[\w\-./]+)\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# Free-form trigger: a leading word semantically equivalent to "handoff"
# in the documented multilingual table, followed by a project token.
# We keep this list short on purpose — semantic paraphrasing is handled
# by the LLM at higher layers; the hook needs only enough to catch the
# canonical forms users actually type.
_HANDOFF_WORDS = (
    "handoff",
    "hand off",
    "hand-off",
    "交接",          # zh-TW / zh-CN
    "交棒",          # zh-TW
    "引き継ぎ",      # ja
    "인수인계",       # ko
    "pass the baton",
)

_FREEFORM_TRIGGER = re.compile(
    r"^\s*(?P<word>"
    + "|".join(re.escape(w) for w in _HANDOFF_WORDS)
    + r")\s+(?P<project>[\w\-./]+)\b",
    re.IGNORECASE,
)


def detect_resume_intent(user_message: str) -> Optional[str]:
    """Return the project name the user wants to resume, or None.

    Recognised forms:
      - ``/handoff resume <project>``
      - ``handoff <project>`` / ``交接 <project>`` / ``交棒 <project>`` /
        ``hand off <project>`` / etc. (multilingual list documented in
        ``rules/L1/multilingual_triggers.md``)

    Args:
        user_message: The raw user prompt text.

    Returns:
        Project name as a string (non-empty), or None if no resume
        intent is detected.

    Notes:
        - Empty / whitespace-only inputs return None.
        - The detector is anchored at line/string start to avoid false
          positives on words that contain "handoff" mid-sentence.
        - Project names allow alphanumerics, dash, underscore, dot,
          and forward-slash so paths like ``arb-bot/strategy-3`` work.
    """
    if not user_message or not user_message.strip():
        return None
    m = _SLASH_TRIGGER.search(user_message)
    if m:
        return m.group("project").strip()
    m = _FREEFORM_TRIGGER.match(user_message)
    if m:
        return m.group("project").strip()
    return None


# ── Staleness check ────────────────────────────────────────


def _parse_iso8601(value: str) -> Optional[datetime]:
    """Lenient ISO-8601 parser. Returns None on any failure."""
    if not isinstance(value, str) or not value:
        return None
    # Accept trailing 'Z' as +00:00 for stdlib fromisoformat in 3.10.
    cleaned = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def is_section0_stale(populated_at_value: str | None,
                      *, now: datetime | None = None,
                      max_age_seconds: int = STALENESS_SECONDS) -> bool:
    """Return True iff the §0 timestamp is older than ``max_age_seconds``.

    A missing / unparseable timestamp is treated as stale (safer to
    repopulate than to ship outdated SSH commands).
    """
    if populated_at_value is None:
        return True
    parsed = _parse_iso8601(populated_at_value)
    if parsed is None:
        return True
    if now is None:
        now = datetime.now(timezone.utc)
    return (now - parsed) > timedelta(seconds=max_age_seconds)


# ── Existing handoff parser (lightweight) ──────────────────

_FIELD_TABLE_ROW = re.compile(
    r"^\|\s*(?P<key>[\w_]+)\s*\|\s*(?P<value>.+?)\s*\|\s*$",
    re.MULTILINE,
)


def _extract_field_table(handoff_md: str) -> dict[str, str]:
    """Pull key/value rows from the §0 markdown table (best-effort)."""
    out: dict[str, str] = {}
    for m in _FIELD_TABLE_ROW.finditer(handoff_md):
        key = m.group("key").strip()
        value = m.group("value").strip()
        # Skip the header separator row (``|---|---|``).
        if set(value) <= {"-"}:
            continue
        if key in {"Item", "key"}:
            continue
        out[key] = value
    return out


def _extract_next_step(handoff_md: str) -> str | None:
    """Return the §5 next_step body (single line), best-effort."""
    # Look for "## §5 next_step" then take the first non-empty,
    # non-comment line that follows.
    m = re.search(
        r"##\s*§5[^\n]*\n(?P<body>.*?)(?:\n##\s|\Z)",
        handoff_md,
        re.DOTALL,
    )
    if not m:
        return None
    for line in m.group("body").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("<!--") or line.startswith("```"):
            continue
        return line
    return None


def _extract_status_block(handoff_md: str) -> str | None:
    """Return the §1 status header block (raw markdown), best-effort."""
    m = re.search(
        r"##\s*§1[^\n]*\n(?P<body>.*?)(?:\n##\s|\Z)",
        handoff_md,
        re.DOTALL,
    )
    if not m:
        return None
    body = m.group("body").strip()
    return body or None


# ── Public composer ────────────────────────────────────────


def build_resume_inject(
    project_name: str,
    handoff_path: Path | str,
    *,
    now: datetime | None = None,
    max_age_seconds: int = STALENESS_SECONDS,
    project_root: Path | str | None = None,
) -> str:
    """Build the compact resume context to inject into the prompt.

    Args:
        project_name: Project the user wants to resume.
        handoff_path: Path to the existing handoff Index file (one of
            ``_AI_BRAIN/06_Handoffs/<project>/交接_*.md``). The file
            must exist and be readable; otherwise an error advisory is
            returned (never raised).
        now: Optional clock override for tests.
        max_age_seconds: Override staleness threshold for tests.
        project_root: Override for §0 git root if repopulation runs.

    Returns:
        A short markdown-formatted string suitable for hook
        ``additionalContext``. ≤50-token target — only §0 / §1 / §5.
        Always non-empty: when the handoff is missing or unreadable
        the function returns a one-line advisory pointing the user
        at the expected path.
    """
    handoff_path = Path(handoff_path)
    if not handoff_path.is_file():
        return (
            f"<concinno:resume>"
            f"\nproject: {project_name}"
            f"\nstatus: handoff index not found at {handoff_path}"
            f"\nnext_step: none — start a fresh handoff"
            f"\n</concinno:resume>"
        )

    try:
        text = handoff_path.read_text(encoding="utf-8")
    except Exception as exc:
        return (
            f"<concinno:resume>"
            f"\nproject: {project_name}"
            f"\nstatus: handoff read failed ({exc!r})"
            f"\n</concinno:resume>"
        )

    # Decide if §0 needs repopulation.
    fields = _extract_field_table(text)
    populated_at = fields.get("populated_at")
    stale = is_section0_stale(
        populated_at, now=now, max_age_seconds=max_age_seconds,
    )

    if stale:
        try:
            from concinno.handoff_section0 import populate_section_0
            fresh = populate_section_0(
                project_name, project_root=project_root,
            )
            # Merge fresh into fields so the inject reflects the
            # latest values even when we don't rewrite the file.
            fields.update(fresh)
        except Exception as exc:  # pragma: no cover (defensive)
            fields.setdefault(
                "populate_error",
                f"<unknown — populate_section_0 raised: {exc!r}>",
            )

    status_block = _extract_status_block(text) or "(status not found)"
    next_step = _extract_next_step(text) or "(next_step not found)"

    # Compact key:value form. Stay terse — the budget target is ≤50
    # tokens, but English+CJK content varies, so we just keep one
    # line per field rather than a markdown table.
    interesting = (
        "pod_id", "ssh_cmd", "vault_aliases", "last_commit",
        "last_session_id", "populated_at",
    )
    s0_lines = [
        f"  {k}: {fields[k]}"
        for k in interesting
        if fields.get(k)
    ]

    lines = [
        f"<concinno:resume project={project_name}>",
        "§0 (auto-revival):",
        *s0_lines,
        "§1 (status):",
        f"  {status_block.splitlines()[0] if status_block else '?'}",
        "§5 (next_step):",
        f"  {next_step}",
        "</concinno:resume>",
    ]
    return "\n".join(lines)


__all__ = [
    "STALENESS_SECONDS",
    "build_resume_inject",
    "detect_resume_intent",
    "is_section0_stale",
]
