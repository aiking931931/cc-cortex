"""concinno.user_profile — Bounded ``USER.md`` profile + frozen snapshots.

@module user_profile
@responsibility Read / write a small, bounded user-facing profile
    document (``~/.concinno/USER.md``) that captures persistent
    operator preferences (role, language, tooling, repeat directives)
    so :mod:`concinno.field_read` can inject a few lines of relevant
    user context into every system prompt without re-discovering it
    each session.

@dependencies (stdlib only — json, os, dataclasses, pathlib, datetime)
@exports
    - :class:`UserProfile` (parsed in-memory representation)
    - :func:`profile_path`, :func:`history_path` (path helpers)
    - :func:`read_user_profile`, :func:`update_user_profile`
    - :func:`restore_snapshot`
    - :func:`render_profile_for_field_read` (one-line formatter for
      FieldRead injection)
    - :data:`DEFAULT_CHAR_BUDGET`, :data:`MIN_CHAR_BUDGET`,
      :data:`MAX_CHAR_BUDGET` (ZIQ autotuner bounds)

Why bounded:

    USER.md is meant to live inside the system prompt of every session
    via FieldRead. Even one rogue session that 100×s the file would
    poison every subsequent prompt. We hard-cap to ``DEFAULT_CHAR_BUDGET
    = 1375``. This default is **chosen empirically** by Concinno to
    balance "enough room for role + language + 5-7 directives" against
    "small enough that prompt-cache hit rate stays high"; it is **not**
    derived from any published external specification. The value is
    ZIQ-autotunable in ``[1000, 2000]`` so the FTRL learner can tighten
    or loosen it based on observed prompt-cache hit rate. The cap is
    enforced on write — :func:`update_user_profile` truncates and
    surfaces a one-line warning rather than silently dropping content.

    See ``feedback_user_transcribed_sota_numbers_unverified.md`` for
    the sediment record on why we no longer claim a third-party "spec
    aligned" provenance for this constant.

Why frozen snapshots:

    USER.md is a high-leverage file (one bad edit affects every future
    prompt). On every successful write we append a JSON-line record to
    ``~/.concinno/USER.history.jsonl`` carrying the previous content +
    timestamp + reason; the history is bounded to ``HISTORY_MAX``
    entries so disk usage stays trivial. :func:`restore_snapshot`
    rolls back to any of the last N versions atomically.

Concinno-specific extensions vs the bare Hermes spec:

    * Schema is a tiny set of named sections (``role``, ``language``,
      ``domains``, ``tools``, ``directives``, ``free``) so callers can
      target updates without parsing free-form markdown. Unknown
      headings are preserved verbatim under ``free`` (forward-compat).
    * ``language`` defaults to ``"zh-TW"`` per the Concinno operator's
      session-UI-繁體中文 rule (see ``rules/00-L0.md``); this is a
      Concinno-only default and downstream OSS adopters can override
      via ``update_user_profile``.

Backward-compat contract:

    1. Adding a new optional :class:`UserProfile` section field is a
       minor bump (existing keys keep loading).
    2. Removing or renaming a section field is a major bump.
    3. ``HISTORY_MAX`` shrinking is a minor bump (older entries are
       only ever truncated from the tail, never silently rewritten).
    4. The on-disk markdown shape (``# USER\n## section\n…``) is the
       wire format — third-party tools that grep ``USER.md`` rely on
       it being plain markdown.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

__all__ = [
    "DEFAULT_CHAR_BUDGET",
    "MIN_CHAR_BUDGET",
    "MAX_CHAR_BUDGET",
    "HISTORY_MAX",
    "UserProfile",
    "profile_path",
    "history_path",
    "read_user_profile",
    "update_user_profile",
    "restore_snapshot",
    "render_profile_for_field_read",
    "current_char_budget",
]


# ── Constants ───────────────────────────────────────────────────────

DEFAULT_CHAR_BUDGET: int = 1375
MIN_CHAR_BUDGET: int = 1000
MAX_CHAR_BUDGET: int = 2000
HISTORY_MAX: int = 3
DEFAULT_LANGUAGE: str = "zh-TW"

_BUDGET_ENV = "CONCINNO_USER_PROFILE_CHAR_BUDGET"
_BUDGET_CONFIG = "char_budget"

_SECTION_ORDER: tuple[str, ...] = (
    "role",
    "language",
    "domains",
    "tools",
    "directives",
    "free",
)


# ── Section schema ──────────────────────────────────────────────────


@dataclass(frozen=True)
class UserProfile:
    """In-memory representation of ``USER.md``.

    Every section is optional; absence is represented by an empty
    string / list. Construction is via :func:`read_user_profile` —
    direct construction is permitted for tests and update flows.

    Attributes:
        role: One-line operator role / persona identifier
            (e.g. ``"AI King — full-stack engineer"``).
        language: BCP-47-ish language tag for prompt UI
            (e.g. ``"zh-TW"`` / ``"en"``). Defaults to
            :data:`DEFAULT_LANGUAGE`.
        domains: Active subject-matter domains
            (e.g. ``["benchmarks", "RL agents", "harness design"]``).
        tools: Preferred tool stack
            (e.g. ``["Concinno", "RunPod", "ZIQ"]``).
        directives: Repeat directives the operator has corrected
            ≥1 time (e.g. ``["never ask publish authorization"]``).
            FieldRead surfaces these as the highest-signal bullets.
        free: Free-form preserved markdown for any unrecognized
            headings — keeps round-tripping lossless.
        truncated: True when the most recent
            :func:`update_user_profile` had to truncate to fit
            the char budget.
    """

    role: str = ""
    language: str = DEFAULT_LANGUAGE
    domains: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    directives: tuple[str, ...] = ()
    free: str = ""
    truncated: bool = False


# ── Path resolution ─────────────────────────────────────────────────


def _root_dir() -> Path:
    """Return the user-scope concinno directory.

    Centralised so tests can monkey-patch
    :func:`pathlib.Path.home` without leaking config to other
    modules.
    """
    root = Path.home() / ".concinno"
    return root


def profile_path() -> Path:
    """Path to ``~/.concinno/USER.md``."""
    return _root_dir() / "USER.md"


def history_path() -> Path:
    """Path to ``~/.concinno/USER.history.jsonl``."""
    return _root_dir() / "USER.history.jsonl"


def _ensure_root() -> Path:
    """Create the root dir on demand, return it."""
    root = _root_dir()
    root.mkdir(parents=True, exist_ok=True)
    return root


# ── Char budget (ZIQ autotunable) ───────────────────────────────────


def current_char_budget(config_override: Optional[int] = None) -> int:
    """Resolve the active char budget for USER.md.

    Lookup order (later overrides earlier — mirrors switches.md sources):

      1. Default :data:`DEFAULT_CHAR_BUDGET`.
      2. Concinno feature_config (``user_profile.char_budget`` —
         registered separately by the main agent so the dataclass /
         module stays import-safe with no FEATURE_META coupling).
      3. Env var :data:`_BUDGET_ENV` (``CONCINNO_USER_PROFILE_CHAR_BUDGET``).
      4. Explicit ``config_override`` argument (test / programmatic).

    All values are clamped to ``[MIN_CHAR_BUDGET, MAX_CHAR_BUDGET]`` so
    a misconfiguration cannot collapse the file to zero or balloon it.
    """
    budget = DEFAULT_CHAR_BUDGET

    # Source 2: feature_config — soft import so the module remains
    # usable in environments where FEATURE_META is being mutated by
    # another sub-agent (race-safe per the wave-1 task brief).
    try:  # pragma: no cover - feature_config is best-effort
        from concinno.core.config import get_config

        cfg = get_config()
        feat_val = cfg.feature("user_profile", _BUDGET_CONFIG)
        if isinstance(feat_val, int):
            budget = feat_val
    except Exception:
        pass

    # Source 3: env var.
    raw = os.environ.get(_BUDGET_ENV, "").strip()
    if raw:
        try:
            budget = int(raw)
        except ValueError:
            # Bad env value — keep prior budget; surface nothing
            # (env vars are operator-supplied; we don't crash here).
            pass

    # Source 4: explicit override.
    if config_override is not None:
        budget = config_override

    return max(MIN_CHAR_BUDGET, min(MAX_CHAR_BUDGET, budget))


# ── Markdown round-tripping ─────────────────────────────────────────

_HEADING_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


def _split_sections(text: str) -> dict[str, str]:
    """Split ``text`` into ``{heading_lower: body}`` dict.

    The first ``# USER`` heading is stripped; everything before the
    first ``##`` heading is dropped (it's preamble we don't model).
    Headings are lower-cased so lookup is forgiving.
    """
    sections: dict[str, str] = {}
    matches = list(_HEADING_RE.finditer(text))
    if not matches:
        return sections
    for idx, m in enumerate(matches):
        name = m.group(1).strip().lower()
        start = m.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        body = text[start:end].strip("\n")
        sections[name] = body
    return sections


def _parse_list_section(body: str) -> tuple[str, ...]:
    """Parse a markdown bullet-list section into a tuple of items.

    Tolerates ``- item`` / ``* item`` / numbered ``1. item``.
    Lines that don't look like bullets are appended verbatim.
    """
    items: list[str] = []
    for line in body.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith(("- ", "* ")):
            items.append(s[2:].strip())
        elif re.match(r"^\d+\.\s+", s):
            items.append(re.sub(r"^\d+\.\s+", "", s).strip())
        else:
            items.append(s)
    return tuple(items)


def _parse_text_section(body: str) -> str:
    """Single-line / paragraph section — strip trailing whitespace."""
    return body.strip()


def _parse_markdown(text: str) -> UserProfile:
    """Parse a USER.md-shaped markdown string into a :class:`UserProfile`."""
    sections = _split_sections(text)
    role = _parse_text_section(sections.get("role", ""))
    language = _parse_text_section(sections.get("language", DEFAULT_LANGUAGE)) or DEFAULT_LANGUAGE
    domains = _parse_list_section(sections.get("domains", ""))
    tools = _parse_list_section(sections.get("tools", ""))
    directives = _parse_list_section(sections.get("directives", ""))
    # Anything else (i.e. unrecognized ## headings) is preserved as a
    # free-form trailing block so authoring choices don't disappear on
    # round-trip.
    known = {"role", "language", "domains", "tools", "directives"}
    free_blocks: list[str] = []
    for name, body in sections.items():
        if name in known:
            continue
        free_blocks.append(f"## {name}\n{body}".rstrip())
    free = "\n\n".join(free_blocks).rstrip()
    return UserProfile(
        role=role,
        language=language,
        domains=domains,
        tools=tools,
        directives=directives,
        free=free,
    )


def _render_markdown(profile: UserProfile) -> str:
    """Render a :class:`UserProfile` to its USER.md markdown form.

    Stable section order (per :data:`_SECTION_ORDER`) so two writers
    produce byte-identical output for the same inputs.
    """
    out: list[str] = ["# USER", ""]

    if profile.role:
        out.append("## role")
        out.append(profile.role)
        out.append("")
    out.append("## language")
    out.append(profile.language or DEFAULT_LANGUAGE)
    out.append("")
    if profile.domains:
        out.append("## domains")
        out.extend(f"- {d}" for d in profile.domains)
        out.append("")
    if profile.tools:
        out.append("## tools")
        out.extend(f"- {t}" for t in profile.tools)
        out.append("")
    if profile.directives:
        out.append("## directives")
        out.extend(f"- {d}" for d in profile.directives)
        out.append("")
    if profile.free:
        out.append(profile.free.strip())
        out.append("")
    # Collapse the trailing blank-line set to one.
    rendered = "\n".join(out).rstrip() + "\n"
    return rendered


# ── Read / update ───────────────────────────────────────────────────


def read_user_profile() -> UserProfile:
    """Read ``USER.md`` and return a parsed :class:`UserProfile`.

    Returns the default empty profile when the file is absent. Never
    raises — IO errors degrade to "no profile available" which is
    safer for FieldRead injection than crashing the prompt builder.
    """
    p = profile_path()
    if not p.is_file():
        return UserProfile()
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return UserProfile()
    return _parse_markdown(text)


def _truncate_to_budget(text: str, budget: int) -> tuple[str, bool]:
    """Trim ``text`` to ``budget`` chars, return ``(text, truncated)``.

    Truncation slices from the end with a marker so the operator sees
    "yes, content was clipped, scroll up to find what was lost".
    """
    if len(text) <= budget:
        return text, False
    marker = "\n<!-- truncated to char budget -->\n"
    keep = max(0, budget - len(marker))
    return text[:keep] + marker, True


def _now_iso() -> str:
    """Return UTC now as an ISO-8601 string with second precision."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _push_history(prev_text: str, reason: str) -> None:
    """Append a snapshot record; keep only the last :data:`HISTORY_MAX`.

    Implementation notes:
        * Atomic from the operator's perspective: read existing history,
          drop overflow, append new entry, rewrite. JSONL is small
          (≤ HISTORY_MAX records of ≤ MAX_CHAR_BUDGET each ⇒ < 10 KB),
          so a full rewrite is cheap and sidesteps tail-truncation
          corner cases.
        * Failure to write the history is non-fatal: the profile update
          itself remains valid, and the operator can recover via git
          if they checked the file in.
    """
    h = history_path()
    entries: list[dict[str, Any]] = []
    if h.is_file():
        try:
            for line in h.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        except OSError:
            entries = []
    entries.append({
        "ts": _now_iso(),
        "reason": reason,
        "previous": prev_text,
    })
    # Keep the most recent HISTORY_MAX entries.
    entries = entries[-HISTORY_MAX:]
    try:
        _ensure_root()
        with open(h, "w", encoding="utf-8") as fh:
            for e in entries:
                fh.write(json.dumps(e, ensure_ascii=False))
                fh.write("\n")
    except OSError:
        # History is best-effort — never block the profile update.
        return


def _coerce_section(value: Any) -> tuple[str, ...]:
    """Coerce a list-shaped update value into ``tuple[str, ...]``.

    Tolerates strings (single-line input), lists, tuples, and ``None``.
    Strips empty entries so callers can pass ``[a, "", b]`` without
    leaving a gap.
    """
    if value is None:
        return ()
    if isinstance(value, str):
        items = [v.strip() for v in value.splitlines()]
    elif isinstance(value, (list, tuple)):
        items = [str(v).strip() for v in value]
    else:
        return ()
    return tuple(i for i in items if i)


def update_user_profile(
    updates: dict[str, Any],
    *,
    reason: str = "",
    char_budget: Optional[int] = None,
) -> UserProfile:
    """Patch the on-disk USER.md and append a frozen snapshot.

    Args:
        updates: Mapping from section name (``role`` / ``language`` /
            ``domains`` / ``tools`` / ``directives`` / ``free``) to
            new value. List-shaped sections accept either a list or a
            newline-separated string. Unknown keys are silently
            dropped so a typo does not corrupt the file.
        reason: Optional one-line explanation written to the history
            JSONL alongside the snapshot. Helpful when scrubbing the
            log later.
        char_budget: Override the active char budget for this write
            (mostly useful in tests). Defaults to
            :func:`current_char_budget`.

    Returns:
        Post-write :class:`UserProfile`. ``truncated=True`` indicates
        the rendered file exceeded the budget and was clipped.
    """
    pre = read_user_profile()
    pre_path = profile_path()
    pre_text = ""
    if pre_path.is_file():
        try:
            pre_text = pre_path.read_text(encoding="utf-8")
        except OSError:
            pre_text = ""

    role = pre.role
    language = pre.language
    domains = pre.domains
    tools = pre.tools
    directives = pre.directives
    free = pre.free

    if "role" in updates:
        role = str(updates["role"]).strip()
    if "language" in updates:
        language = (str(updates["language"]).strip() or DEFAULT_LANGUAGE)
    if "domains" in updates:
        domains = _coerce_section(updates["domains"])
    if "tools" in updates:
        tools = _coerce_section(updates["tools"])
    if "directives" in updates:
        directives = _coerce_section(updates["directives"])
    if "free" in updates:
        free = str(updates["free"]).strip()

    next_profile = UserProfile(
        role=role,
        language=language,
        domains=domains,
        tools=tools,
        directives=directives,
        free=free,
    )
    rendered = _render_markdown(next_profile)
    budget = current_char_budget(char_budget)
    rendered, truncated = _truncate_to_budget(rendered, budget)

    _ensure_root()
    try:
        pre_path.write_text(rendered, encoding="utf-8")
    except OSError:
        # Re-raising here is the right call: the caller asked for a
        # write and it failed. Silently swallowing would create a
        # phantom-success that drifts the in-memory truth from disk.
        raise

    _push_history(pre_text, reason or "update")

    return replace(next_profile, truncated=truncated)


# ── Snapshot restore ────────────────────────────────────────────────


def restore_snapshot(idx: int) -> UserProfile:
    """Roll back USER.md to a frozen snapshot.

    Args:
        idx: 0 = most recent snapshot (the file before the last
            update), 1 = one step further back, etc. Out-of-range
            indices raise ``IndexError`` so the caller can branch.

    Returns:
        Post-restore :class:`UserProfile`.

    Raises:
        IndexError: When ``idx`` is negative or larger than the
            number of available snapshots.
        FileNotFoundError: When no history exists at all.
    """
    if idx < 0:
        raise IndexError(f"snapshot index {idx} must be ≥ 0")
    h = history_path()
    if not h.is_file():
        raise FileNotFoundError(
            "no USER.history.jsonl — nothing to restore"
        )
    try:
        lines = [
            line for line in h.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except OSError as exc:
        raise FileNotFoundError(
            f"USER.history.jsonl unreadable: {exc!r}"
        ) from exc
    # Newest entry is the last line; idx=0 means take the last.
    if idx >= len(lines):
        raise IndexError(
            f"snapshot index {idx} out of range "
            f"(only {len(lines)} snapshot(s) available)"
        )
    chosen = lines[-(idx + 1)]
    try:
        record = json.loads(chosen)
    except json.JSONDecodeError as exc:
        raise FileNotFoundError(
            f"snapshot record at idx {idx} is malformed JSON: {exc!r}"
        ) from exc
    previous = record.get("previous", "")
    if not isinstance(previous, str):
        raise FileNotFoundError(
            f"snapshot record at idx {idx} has non-string ``previous``"
        )
    p = profile_path()
    _ensure_root()
    p.write_text(previous, encoding="utf-8")
    return read_user_profile()


# ── FieldRead bridge ────────────────────────────────────────────────

_FIELD_READ_SECTION_HEADER = "## USER profile"


def render_profile_for_field_read(
    profile: Optional[UserProfile] = None,
    *,
    max_chars: Optional[int] = None,
) -> str:
    """Render a compact bullet-form summary suitable for FieldRead.

    The output is single-block markdown beginning with
    ``## USER profile`` (so :mod:`concinno.field_read`'s section
    machinery treats it as a first-class block). When the active
    profile is empty, returns an empty string — FieldRead can then
    skip injection entirely.

    Args:
        profile: Pre-loaded profile; defaults to :func:`read_user_profile`.
        max_chars: Hard cap on the returned string. Defaults to
            :func:`current_char_budget` so the FieldRead inject never
            exceeds the configured operator preference.

    Returns:
        Markdown string (empty when nothing meaningful to render).
    """
    p = profile if profile is not None else read_user_profile()
    if not (
        p.role or p.domains or p.tools or p.directives
        or (p.language and p.language != DEFAULT_LANGUAGE)
    ):
        return ""
    lines: list[str] = [_FIELD_READ_SECTION_HEADER]
    if p.role:
        lines.append(f"- role: {p.role}")
    lines.append(f"- language: {p.language or DEFAULT_LANGUAGE}")
    if p.domains:
        lines.append("- domains: " + ", ".join(p.domains))
    if p.tools:
        lines.append("- tools: " + ", ".join(p.tools))
    if p.directives:
        lines.append("- directives:")
        for d in p.directives:
            lines.append(f"  - {d}")
    out = "\n".join(lines)
    cap = max_chars if max_chars is not None else current_char_budget()
    if len(out) > cap:
        out = out[: max(0, cap - 1)] + "…"
    return out


# Re-export ``field`` from dataclasses indirectly to keep the import
# graph stable for downstream callers that ``from concinno.user_profile
# import field`` (none today, but makes the API tidy).
_ = field  # noqa: F841 - signal that ``field`` import is intentional
