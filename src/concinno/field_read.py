"""concinno.field_read — Selective field extraction for token-efficient injection.

@module field_read
@responsibility Parse structured markdown (handoffs, memory, learnings) and
    extract only task-relevant sections, compressed to a token budget.
@dependencies None (stdlib only)
@exports read_handoff_fields, read_memory_fields, build_field_context,
    read_handoff_fields_v2, read_memory_fields_v2, build_field_context_v2,
    expand, FieldReadResult, ElidedSection, render_elision_breadcrumbs,
    build_field_context_v2_string, compress_breakeven_for,
    COMPRESS_BREAKEVEN_BY_COMPLEXITY

Problem v1 (2.5.x):
    DynamicSlots.handoff_summary exists but is never filled. Full handoff
    files are 150-300 lines — injecting them raw wastes token budget and
    hits the "Lost in the Middle" attention cliff.

Solution v1: FieldRead selectively extracts high-value sections (⬜/⏸/
    next_step/未解決) and keyword-matches remaining sections, compressing
    to budget. → silent compressor.

Problem v2 (why 2.6.0 exists):
    Silent compression breaks LLM cognitive sync. The LLM sees only the
    kept sections — it doesn't know entire categories were elided. Users
    must trigger "之前不就..." (MEMORY #15) for the model to re-read.

Solution v2: metadata-first output. Kept sections are rendered as before
    BUT accompanied by breadcrumb records of elided sections (id, heading,
    line count, gist, confidence). The LLM gains explicit awareness of
    what was skipped AND a trigger hint: if user mentions the elided
    topic, call `expand(path, section_id)` to retrieve full content.

Backward compat: the v1 API (`read_handoff_fields`, `read_memory_fields`,
`build_field_context`) still returns `str` — internally delegates to the
v2 implementation and returns only `.content`. Existing callers unchanged.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# ── Constants ─────────────────────────────────────────────

# ZIQ compression breakeven: below this token count, compression is
# net negative (quality loss > token savings). Empirically validated
# across Qwen2.5-7B + Llama-3-8B: ≥2500t → Pareto improvement
# (both compression ratio AND output quality improve).
# Below 2500t → pass through uncompressed.
#
# This is the *default* breakeven. C0Router-aware callers should prefer
# :func:`compress_breakeven_for` so the threshold scales with complexity
# (Simple → aggressive elide / Chaotic → conservative). The constant
# stays as a safe fallback for callers without a complexity signal.
COMPRESS_BREAKEVEN_TOKENS = 2500

# Per-complexity breakeven overrides (4.4.0 — Plan C ZIQ tunable target).
#
# Mapping: ComplexityDomain.value → breakeven token threshold.
# Plumbed through :func:`compress_breakeven_for` so callers stay decoupled
# from the cognitive.router enum import (avoids circular dependency at
# module import time).
#
# Rationale:
# - Simple tasks (1500): aggressive elide, the LLM rarely needs the
#   long history; saving tokens wins.
# - Complicated (2500): default — matches the legacy global constant.
# - Complex (3500): conservative — exploration may need surrounding
#   context to avoid premature pruning.
# - Chaotic (4000): very conservative — emergencies require maximum
#   recall; only elide when really necessary.
#
# Bounds (1500 ≤ value ≤ 4000) are mirrored in the FEATURE_META
# `field_read.compress_breakeven_tokens` entry so ZIQ FTRL outcome
# learning operates inside the same envelope.
COMPRESS_BREAKEVEN_BY_COMPLEXITY: dict[str, int] = {
    "simple": 1500,
    "complicated": 2500,
    "complex": 3500,
    "chaotic": 4000,
}


def compress_breakeven_for(complexity: Optional[str] = None) -> int:
    """Return the compression breakeven threshold for a complexity class.

    Args:
        complexity: ``"simple" | "complicated" | "complex" | "chaotic"``.
            Case-insensitive. ``None`` / unknown values fall back to the
            default :data:`COMPRESS_BREAKEVEN_TOKENS` so the function is
            safe to call without a routed task.

    Returns:
        Token count below which the file is passed through uncompressed.
    """
    if not complexity:
        return COMPRESS_BREAKEVEN_TOKENS
    return COMPRESS_BREAKEVEN_BY_COMPLEXITY.get(
        complexity.lower(), COMPRESS_BREAKEVEN_TOKENS,
    )

# Sections always extracted from handoff (highest value for continuation)
_ALWAYS_SECTIONS = frozenset({
    "next_step",
    # English defaults + Chinese locale variants
    "unresolved", "未解決",
    "status", "狀態",
    "constraints", "鐵律",
})

# Status markers that indicate actionable items
_ACTIONABLE_RE = re.compile(r"[⬜⏸]")
_PENDING_RE = re.compile(r"[⬜⏸]")

# Frontmatter delimiter
_FRONTMATTER_RE = re.compile(r"^---\s*$")

# Section header (## level)
_SECTION_RE = re.compile(r"^##\s+(.+)$")

# Memory index entry: - [Title](file.md) — description
_MEMORY_ENTRY_RE = re.compile(
    r"^-\s+\[([^\]]+)\]\(([^)]+)\)\s*[—–-]\s*(.+)$",
)

# Slug char whitelist (ascii letters/digits/dash, CJK kept)
_SLUG_STRIP_RE = re.compile(r"[^\w\u4e00-\u9fff-]+")
_SLUG_DASHES_RE = re.compile(r"-{2,}")

# YAML top-level key
_YAML_KEY_RE = re.compile(r"^([A-Za-z_][\w-]*)\s*:")

# Bullet prefix
_BULLET_RE = re.compile(r"^\s*[-*+]\s+(.+)$")

# CJK-aware token estimation (matches prompt_engine.py)
_CJK_RANGE = 0x2E80


def _estimate_tokens(text: str) -> int:
    if not text:
        return 0
    cjk = sum(1 for c in text if ord(c) > _CJK_RANGE)
    ascii_chars = len(text) - cjk
    return int(cjk * 1.5 + ascii_chars / 4)


# ── v2 dataclasses ───────────────────────────────────────


@dataclass
class ElidedSection:
    """Breadcrumb for a section that was skipped during compression.

    Emitted alongside kept content so the LLM is *aware* the skip
    happened and can call `expand(path, id)` if the user later
    references the topic.
    """

    id: str              # stable id, used for expand() lookup
    heading: str         # original heading text
    lines: int           # line count that was elided
    gist: str            # 1-sentence summary
    confidence: float    # 0-1; how sure we are we can skip this


@dataclass
class FieldReadResult:
    """Structured return of v2 field reads.

    Attributes:
        content: Concatenated kept-section text (drop-in for v1 string).
        sections_kept: Stable ids of sections that were included.
        sections_elided: Breadcrumbs for skipped sections.
        confidence: Overall confidence — mean of elided confidences,
            or 1.0 if nothing was elided (no risk).
        expand_hint: Human-readable trigger line for the LLM.
    """

    content: str
    sections_kept: list[str]
    sections_elided: list[ElidedSection]
    confidence: float
    expand_hint: str


# Empty result singleton-ish factory (confidence 1.0 — nothing to miss)
def _empty_result() -> FieldReadResult:
    return FieldReadResult(
        content="",
        sections_kept=[],
        sections_elided=[],
        confidence=1.0,
        expand_hint="",
    )


# ── Section Parser ────────────────────────────────────────


@dataclass
class Section:
    """A parsed markdown section."""

    title: str
    content: str
    priority: int = 0  # higher = more important
    tokens: int = 0
    # v2: assigned post-parse, stable within a file
    section_id: str = ""
    line_count: int = 0

    def __post_init__(self) -> None:
        self.tokens = _estimate_tokens(self.content)
        if not self.line_count:
            self.line_count = len(self.content.splitlines()) if self.content else 0


def _parse_sections(text: str) -> list[Section]:
    """Parse markdown into sections by ## headers.

    Strips YAML frontmatter if present. Content before the first ##
    header becomes a section titled "_preamble".
    """
    lines = text.splitlines()

    # Strip frontmatter
    if lines and _FRONTMATTER_RE.match(lines[0]):
        for i, line in enumerate(lines[1:], 1):
            if _FRONTMATTER_RE.match(line):
                lines = lines[i + 1:]
                break

    sections: list[Section] = []
    current_title = "_preamble"
    current_lines: list[str] = []

    for line in lines:
        m = _SECTION_RE.match(line)
        if m:
            # Flush previous section
            body = "\n".join(current_lines).strip()
            if body:
                sections.append(Section(title=current_title, content=body))
            current_title = m.group(1).strip()
            current_lines = []
        else:
            current_lines.append(line)

    # Flush last section
    body = "\n".join(current_lines).strip()
    if body:
        sections.append(Section(title=current_title, content=body))

    # v2 post-processing: assign stable ids, dedup within file
    _assign_section_ids(sections)
    return sections


def _heading_slug(heading: str) -> str:
    """Derive a stable short slug from a heading.

    - lowercases ascii
    - strips punctuation (keeps CJK)
    - collapses runs of whitespace/punctuation to single dash
    - caps at 30 chars
    """
    h = heading.strip().lower()
    # Replace whitespace with dash first so _SLUG_STRIP_RE keeps structure
    h = re.sub(r"\s+", "-", h)
    h = _SLUG_STRIP_RE.sub("-", h)
    h = _SLUG_DASHES_RE.sub("-", h).strip("-")
    if not h:
        return ""
    return h[:30]


def _assign_section_ids(sections: list[Section]) -> None:
    """Assign stable `section_id` values (mutates in place).

    Rules:
    - Slug from heading; dedup within file with -2 / -3 suffix.
    - Unnamed fallback: `sec-{N}` (1-indexed by position).
    - Stable across calls on the same text (no timestamp / no hash
      of body — body can change, id stays pinned to heading).
    """
    seen: dict[str, int] = {}
    for idx, sec in enumerate(sections, 1):
        slug = _heading_slug(sec.title) if sec.title != "_preamble" else ""
        if not slug:
            base = f"sec-{idx}"
        else:
            base = slug
        count = seen.get(base, 0)
        seen[base] = count + 1
        sec.section_id = base if count == 0 else f"{base}-{count + 1}"


def _score_section(section: Section, keywords: list[str]) -> int:
    """Score a section for relevance. Higher = more relevant."""
    title_lower = section.title.lower()
    score = 0

    # Always-extract sections get highest priority
    for always in _ALWAYS_SECTIONS:
        if always in title_lower:
            score += 100
            break

    # Actionable items (⬜/⏸) boost priority
    if _ACTIONABLE_RE.search(section.content):
        score += 50

    # Keyword matches
    content_lower = section.content.lower()
    for kw in keywords:
        kw_lower = kw.lower()
        if kw_lower in title_lower:
            score += 30
        if kw_lower in content_lower:
            score += 10

    return score


def _extract_keywords(task_prompt: str) -> list[str]:
    """Extract meaningful keywords from a task prompt."""
    # Remove common stop words and short tokens
    stops = {
        "the", "a", "an", "is", "are", "was", "were", "be", "been",
        "do", "does", "did", "have", "has", "had", "will", "would",
        "can", "could", "should", "may", "might", "shall",
        "to", "of", "in", "for", "on", "at", "by", "with", "from",
        "and", "or", "but", "not", "no", "if", "then", "else",
        "this", "that", "these", "those", "it", "its",
        "i", "you", "we", "they", "he", "she", "me", "us",
        "的", "是", "在", "了", "不", "也", "就", "都", "而",
        "及", "與", "或", "但", "把", "被", "讓", "給", "從",
    }
    words = re.findall(r"[\w\u4e00-\u9fff]+", task_prompt.lower())
    return [w for w in words if len(w) >= 2 and w not in stops]


# ── v2 Helpers: gist + confidence ────────────────────────


def _derive_gist(section_body: str, max_tokens: int = 20) -> str:
    """Derive a one-sentence gist from section body.

    Heuristics (no LLM call):
    1. If body has ⬜/⏸/✅ markers: count them → "N pending / M blocked"
    2. If body looks like yaml frontmatter-ish top-level keys: list keys
    3. If first non-empty line is a bullet: return first bullet trimmed
    4. Fallback: first ~N chars of first non-empty line.
    """
    if not section_body:
        return ""

    # Normalize: find first non-empty line
    lines = [ln for ln in section_body.splitlines() if ln.strip()]
    if not lines:
        return ""

    # 1. Pending markers
    pending = len(re.findall(r"⬜", section_body))
    blocked = len(re.findall(r"⏸", section_body))
    done = len(re.findall(r"✅", section_body))
    if pending or blocked:
        parts: list[str] = []
        if pending:
            parts.append(f"{pending} pending")
        if blocked:
            parts.append(f"{blocked} blocked")
        if done:
            parts.append(f"{done} done")
        return ", ".join(parts)

    # 2. yaml-ish block: consecutive top-level key: lines at start
    yaml_keys: list[str] = []
    for ln in lines[:10]:
        m = _YAML_KEY_RE.match(ln)
        if m:
            yaml_keys.append(m.group(1))
        else:
            break
    if len(yaml_keys) >= 2:
        keys_str = ", ".join(yaml_keys[:5])
        suffix = "…" if len(yaml_keys) > 5 else ""
        return f"keys: {keys_str}{suffix}"

    # 3. First line bullet
    first = lines[0]
    bm = _BULLET_RE.match(first)
    if bm:
        text = bm.group(1).strip()
        # Cap to roughly max_tokens worth (CJK-aware crude cap)
        char_cap = max_tokens * 3
        if len(text) > char_cap:
            text = text[:char_cap].rstrip() + "…"
        return text

    # 4. Fallback: first line trimmed
    text = first.strip()
    char_cap = max_tokens * 3
    if len(text) > char_cap:
        text = text[:char_cap].rstrip() + "…"
    return text


def _score_elision_confidence(
    section: Section,
    keywords: list[str],
) -> float:
    """Return confidence that eliding this section is safe.

    0.0 = likely critical, skip at your peril
    1.0 = totally safe to elide

    Factors (additive adjustments, clamped to [0, 1]):
    - Keyword relevance: each keyword hit in title/body drops conf.
    - Pending markers (⬜/⏸): strong signal to keep → drop conf a lot.
    - Always-sections (next_step / 狀態 / 未解決 / 鐵律): drop conf hard.
    - Size: very large sections are risky to blindly skip.
    - Default baseline for a generic section: 0.7.
    """
    title_lower = section.title.lower()

    # Baseline
    conf = 0.75

    # Always-sections: critical
    for always in _ALWAYS_SECTIONS:
        if always in title_lower:
            conf -= 0.5
            break

    # Pending/blocked items
    if _PENDING_RE.search(section.content):
        conf -= 0.35

    # Keyword overlap
    if keywords:
        content_lower = section.content.lower()
        hits = 0
        for kw in keywords:
            kw_lower = kw.lower()
            if kw_lower in title_lower:
                hits += 2
            elif kw_lower in content_lower:
                hits += 1
        if hits:
            conf -= min(0.4, 0.1 * hits)

    # Large sections: risky to skip silently → lower confidence slightly
    if section.tokens > 500:
        conf -= 0.1
    if section.tokens > 1500:
        conf -= 0.1

    # Clamp
    if conf < 0.0:
        conf = 0.0
    elif conf > 1.0:
        conf = 1.0
    return round(conf, 3)


def _build_expand_hint(elided: list[ElidedSection]) -> str:
    """Generate a human-readable trigger line for the LLM."""
    if not elided:
        return ""
    n = len(elided)
    # Show up to 3 ids inline, then count overflow
    ids = [e.id for e in elided[:3]]
    ids_str = ", ".join(ids)
    if n > 3:
        ids_str += f", +{n - 3} more"
    return (
        f"Elided {n} section(s) ({ids_str}). "
        f"Call expand(<id>) if user references any of them."
    )


def _overall_confidence(elided: list[ElidedSection]) -> float:
    if not elided:
        return 1.0
    return round(sum(e.confidence for e in elided) / len(elided), 3)


# ── Handoff Field Reader (v2 + v1 shim) ──────────────────


def _read_handoff_text(handoff_path: str) -> str:
    """Return raw text of a handoff file (empty on error / missing)."""
    if not handoff_path or not os.path.isfile(handoff_path):
        return ""
    try:
        with open(handoff_path, encoding="utf-8") as f:
            return f.read(20000)  # Cap read to prevent huge files
    except OSError:
        return ""


def read_handoff_fields_v2(
    handoff_path: str,
    task_keywords: Optional[list[str]] = None,
    max_tokens: int = 200,
    *,
    compress_breakeven: Optional[int] = None,
) -> FieldReadResult:
    """Extract high-value fields from a handoff file (v2 structured output).

    Metadata-first: returns kept content + breadcrumbs of elided sections
    so the LLM can detect when to call `expand(path, section_id)`.

    Args:
        handoff_path: Path to handoff markdown file.
        task_keywords: Keywords to match against sections.
        max_tokens: Maximum token budget for output.
        compress_breakeven: Override the compression breakeven threshold
            (typically supplied by C0Router via
            :func:`compress_breakeven_for`). ``None`` uses the default
            global :data:`COMPRESS_BREAKEVEN_TOKENS`.

    See `read_handoff_fields` for the v1 str-only compatibility shim.
    """
    text = _read_handoff_text(handoff_path)
    if not text:
        return _empty_result()

    # ZIQ breakeven gate: compression only pays off above this threshold.
    # Below that, return full text (trimmed to max_tokens).
    breakeven = (
        compress_breakeven
        if compress_breakeven is not None
        else COMPRESS_BREAKEVEN_TOKENS
    )
    raw_tokens = _estimate_tokens(text)
    if raw_tokens < breakeven:
        if raw_tokens <= max_tokens:
            return FieldReadResult(
                content=text.strip(),
                sections_kept=["_full"],
                sections_elided=[],
                confidence=1.0,
                expand_hint="",
            )
        # Still over budget but under breakeven — trim naively
        chars = int(max_tokens * 3.5)
        return FieldReadResult(
            content=text[:chars].strip() + "\n[…truncated]",
            sections_kept=["_full-truncated"],
            sections_elided=[],
            confidence=0.8,  # some info lost but we didn't section-select
            expand_hint="",
        )

    sections = _parse_sections(text)
    if not sections:
        return _empty_result()

    keywords = task_keywords or []

    # Score all sections
    scored: list[tuple[int, Section]] = []
    for sec in sections:
        score = _score_section(sec, keywords)
        sec.priority = score
        if score > 0:
            scored.append((score, sec))
    scored.sort(key=lambda x: x[0], reverse=True)

    kept_ids: set[str] = set()
    parts: list[str] = []
    used = 0
    for _score, sec in scored:
        if used + sec.tokens <= max_tokens:
            parts.append(f"**{sec.title}**: {sec.content}")
            used += sec.tokens
            kept_ids.add(sec.section_id)
        else:
            # Try truncating to fit remaining budget (half-kept)
            remaining = max_tokens - used
            if remaining > 30:
                chars = int(remaining * 3.5)
                truncated = sec.content[:chars].rsplit("\n", 1)[0]
                parts.append(f"**{sec.title}**: {truncated}…")
                kept_ids.add(sec.section_id)
            break

    # Everything that wasn't kept is elided (including score==0 sections)
    elided: list[ElidedSection] = []
    for sec in sections:
        if sec.section_id in kept_ids:
            continue
        elided.append(ElidedSection(
            id=sec.section_id,
            heading=sec.title,
            lines=sec.line_count,
            gist=_derive_gist(sec.content),
            confidence=_score_elision_confidence(sec, keywords),
        ))

    content = "\n".join(parts)
    return FieldReadResult(
        content=content,
        sections_kept=sorted(kept_ids),
        sections_elided=elided,
        confidence=_overall_confidence(elided),
        expand_hint=_build_expand_hint(elided),
    )


def read_handoff_fields(
    handoff_path: str,
    task_keywords: Optional[list[str]] = None,
    max_tokens: int = 200,
) -> str:
    """Extract high-value fields from a handoff file (v1 string API).

    Always extracts: 狀態/next_step/未解決/鐵律 sections.
    Keyword-matches remaining sections within token budget.

    Args:
        handoff_path: Path to handoff markdown file.
        task_keywords: Keywords to match against sections. If None,
            only always-extract sections are included.
        max_tokens: Maximum token budget for output.

    Returns:
        Compressed handoff summary string, or empty string if file
        not found or no relevant content.

    Note: this is the v1-compatible shim; see `read_handoff_fields_v2`
    for structured output with elided-section breadcrumbs.
    """
    result = read_handoff_fields_v2(handoff_path, task_keywords, max_tokens)
    return result.content


# ── Memory Field Reader ──────────────────────────────────


@dataclass
class MemoryEntry:
    """A parsed memory index entry."""

    title: str
    filename: str
    description: str
    relevance: float = 0.0


def _read_file_body(path: str, max_chars: int = 1500) -> str:
    """Read a file, strip YAML frontmatter, return body text."""
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read(max_chars)
    except OSError:
        return ""
    lines = raw.splitlines()
    if lines and _FRONTMATTER_RE.match(lines[0]):
        for i, line in enumerate(lines[1:], 1):
            if _FRONTMATTER_RE.match(line):
                lines = lines[i + 1:]
                break
    return "\n".join(lines).strip()


def _parse_memory_index(index_path: str) -> list[MemoryEntry]:
    """Parse MEMORY.md index into structured entries."""
    if not os.path.isfile(index_path):
        return []

    try:
        with open(index_path, encoding="utf-8") as f:
            content = f.read(10000)
    except OSError:
        return []

    entries: list[MemoryEntry] = []
    for line in content.splitlines():
        m = _MEMORY_ENTRY_RE.match(line.strip())
        if m:
            entries.append(MemoryEntry(
                title=m.group(1),
                filename=m.group(2),
                description=m.group(3).strip(),
            ))
    return entries


def _score_memory_entry(entry: MemoryEntry, keywords: list[str]) -> float:
    """Score a memory entry against keywords. 0.0 = no match."""
    if not keywords:
        return 0.0
    searchable = (entry.title + " " + entry.description).lower()
    hits = sum(1 for kw in keywords if kw.lower() in searchable)
    return hits / len(keywords) if keywords else 0.0


def read_memory_fields_v2(
    memory_dir: str,
    task_keywords: Optional[list[str]] = None,
    max_tokens: int = 150,
    max_entries: int = 5,
) -> FieldReadResult:
    """Extract task-relevant memory entries (v2 structured output).

    Same selection logic as v1 but returns a FieldReadResult so the
    caller sees which entries were elided (e.g. matched-but-over-budget
    or entire index when no keywords given).
    """
    if not memory_dir or not os.path.isdir(memory_dir):
        return _empty_result()

    index_path = os.path.join(memory_dir, "MEMORY.md")
    entries = _parse_memory_index(index_path)
    if not entries:
        return _empty_result()

    keywords = task_keywords or []
    if not keywords:
        # Zero-keyword: everything elided. Emit breadcrumbs for the top
        # max_entries so the LLM knows they exist.
        elided = [
            ElidedSection(
                id=_heading_slug(e.title) or f"mem-{i + 1}",
                heading=e.title,
                lines=1,
                gist=e.description[:80],
                confidence=0.85,
            )
            for i, e in enumerate(entries[:max_entries])
        ]
        return FieldReadResult(
            content="",
            sections_kept=[],
            sections_elided=elided,
            confidence=_overall_confidence(elided),
            expand_hint=_build_expand_hint(elided),
        )

    # Score entries
    scored: list[tuple[float, MemoryEntry]] = []
    for entry in entries:
        score = _score_memory_entry(entry, keywords)
        if score > 0:
            entry.relevance = score
            scored.append((score, entry))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:max_entries]

    if not top:
        return _empty_result()

    # Build compressed output
    kept_entries: list[MemoryEntry] = []
    parts: list[str] = []
    used = 0
    for _score, entry in top:
        file_path = os.path.join(memory_dir, entry.filename)
        body = _read_file_body(file_path)
        snippet = body[:400] if body else ""

        line = f"- **{entry.title}**: {entry.description}"
        if snippet:
            line += f"\n  {snippet}"

        t = _estimate_tokens(line)
        if used + t <= max_tokens:
            parts.append(line)
            used += t
            kept_entries.append(entry)
        else:
            remaining = max_tokens - used
            if remaining > 20:
                chars = int(remaining * 3.5)
                parts.append(line[:chars] + "…")
                kept_entries.append(entry)
            break

    kept_ids = [
        _heading_slug(e.title) or f"mem-{i + 1}"
        for i, e in enumerate(kept_entries)
    ]

    # Entries that matched but didn't fit
    matched_not_kept = [e for _s, e in top if e not in kept_entries]
    elided: list[ElidedSection] = []
    for i, e in enumerate(matched_not_kept):
        elided.append(ElidedSection(
            id=_heading_slug(e.title) or f"mem-over-{i + 1}",
            heading=e.title,
            lines=1,
            gist=e.description[:80],
            confidence=0.5,  # they matched — worth flagging
        ))

    content = "\n".join(parts) if parts else ""
    return FieldReadResult(
        content=content,
        sections_kept=kept_ids,
        sections_elided=elided,
        confidence=_overall_confidence(elided) if elided else 1.0,
        expand_hint=_build_expand_hint(elided),
    )


def read_memory_fields(
    memory_dir: str,
    task_keywords: Optional[list[str]] = None,
    max_tokens: int = 150,
    max_entries: int = 5,
) -> str:
    """Extract task-relevant memory entries (v1 string API).

    Reads MEMORY.md index, scores entries against keywords, and
    reads matched memory file content (first 500 chars per file).

    Args:
        memory_dir: Directory containing MEMORY.md and memory files.
        task_keywords: Keywords to match against memory descriptions.
        max_tokens: Maximum token budget for output.
        max_entries: Maximum number of memory entries to include.

    Returns:
        Compressed memory context string, or empty string if no matches.
    """
    return read_memory_fields_v2(
        memory_dir, task_keywords, max_tokens, max_entries,
    ).content


# ── expand() — detail-on-demand ──────────────────────────


def expand(
    source_path: str,
    section_id: str,
    *,
    workspace_root: "str | os.PathLike[str] | None" = None,
) -> str:
    """Return the full content of a previously elided section.

    Re-parses the source file and looks up the section by its stable id.
    Returns empty string if source is missing or id not found.

    Args:
        source_path: Path to the original markdown file.
        section_id: Stable id from an ElidedSection (heading slug or
            ``sec-N`` fallback).
        workspace_root: Optional directory that ``source_path`` must
            live under (after symlink resolution). When set, protects
            against ``expand("../../../etc/passwd", ...)`` style disk
            reads through untrusted section ids — the path is resolved
            and rejected with :class:`ValueError` if it escapes the
            root. Defaults to ``None`` (no check) so legacy callers
            that pass absolute handoff paths outside any particular
            workspace keep working. Callers receiving untrusted input
            SHOULD pass this explicitly (e.g. ``workspace_root=Path.cwd()``
            or the project root) — the whitepaper-section plumbing in
            hook pipelines already does.

    Returns:
        The full ``## Heading\\n\\nBody`` text of the matching section,
        or empty string on miss.

    Raises:
        ValueError: When ``workspace_root`` is set and the resolved
            ``source_path`` lies outside it, or when either path cannot
            be resolved (broken symlink / cycle — fail-closed).
    """
    if not source_path or not section_id:
        return ""

    # Path traversal defense. Off by default for back-compat; callers
    # pass ``workspace_root`` explicitly to opt in. Symlinks are
    # resolved before the comparison so a symlink planted inside the
    # workspace cannot proxy reads to ``/etc/passwd`` etc.
    if workspace_root is not None:
        try:
            root_resolved = Path(workspace_root).resolve()
            src_resolved = Path(source_path).resolve()
        except (OSError, RuntimeError) as exc:
            raise ValueError(
                f"expand() could not resolve paths: {exc}",
            ) from exc
        try:
            inside = src_resolved.is_relative_to(root_resolved)
        except AttributeError:
            inside = str(src_resolved).startswith(str(root_resolved))
        if not inside:
            raise ValueError(
                f"expand() path {source_path!r} resolves outside workspace "
                f"{str(root_resolved)!r}",
            )

    if not os.path.isfile(source_path):
        return ""
    try:
        with open(source_path, encoding="utf-8") as f:
            text = f.read(50000)  # larger than read_handoff for full recall
    except OSError:
        return ""
    if not text:
        return ""

    sections = _parse_sections(text)
    for sec in sections:
        if sec.section_id == section_id:
            if sec.title == "_preamble":
                return sec.content
            return f"## {sec.title}\n\n{sec.content}"
    return ""


# ── Orchestrator ──────────────────────────────────────────


@dataclass
class FieldReadConfig:
    """Configuration for field read context building."""

    handoff_budget: int = 200
    memory_budget: int = 150
    max_memory_entries: int = 5
    handoff_patterns: list[str] = field(default_factory=lambda: [
        "交接_*.md",
    ])


def _is_handoff_index(fname: str) -> bool:
    """Check if a filename is a handoff index file (not archive/summary)."""
    return (
        fname.startswith("交接_")
        and fname.endswith(".md")
        and "_archive" not in fname
        and "_summary" not in fname
    )


def _find_handoff_files(workspace: str) -> list[str]:
    """Find handoff files in standard locations."""
    candidates: list[str] = []

    for subdir in (
        os.path.join("_AI_BRAIN", "06_Handoffs"),
        "handoffs",
        ".",
    ):
        d = os.path.join(workspace, subdir)
        if not os.path.isdir(d):
            continue
        try:
            for root, _dirs, files in os.walk(d):
                for fname in files:
                    if _is_handoff_index(fname):
                        candidates.append(os.path.join(root, fname))
                if root.count(os.sep) - d.count(os.sep) >= 3:
                    break
        except OSError:
            continue

    return candidates


def _find_memory_dir(workspace: str) -> str:
    """Find the memory directory."""
    # Standard location for Claude Code projects
    for candidate in (
        os.path.join(
            os.path.expanduser("~"),
            ".claude", "projects",
            workspace.replace("\\", "-").replace("/", "-").replace(":", ""),
            "memory",
        ),
        os.path.join(workspace, ".claude", "memory"),
        os.path.join(workspace, "memory"),
    ):
        if os.path.isdir(candidate) and os.path.isfile(
            os.path.join(candidate, "MEMORY.md"),
        ):
            return candidate
    return ""


def build_field_context_v2(
    workspace: str,
    task_prompt: str = "",
    config: Optional[FieldReadConfig] = None,
    *,
    complexity: Optional[str] = None,
) -> FieldReadResult:
    """Build compressed field context from handoff + memory (v2).

    Combines handoff and memory breadcrumbs into a single
    FieldReadResult. Confidence is the weighted mean of component
    confidences. Section ids include source prefix
    (`handoff:<basename>:<id>` / `memory:<id>`) so expand() targets
    are disambiguated.

    Args:
        workspace: Project workspace root directory.
        task_prompt: Current task description for keyword matching.
        config: Optional configuration override.
        complexity: Optional C0 complexity classification
            (``"simple" | "complicated" | "complex" | "chaotic"``); when
            supplied it routes the per-call breakeven through
            :func:`compress_breakeven_for` so Simple tasks elide
            aggressively while Chaotic tasks retain more context.
    """
    if not workspace:
        return _empty_result()

    cfg = config or FieldReadConfig()
    keywords = _extract_keywords(task_prompt) if task_prompt else []
    breakeven = compress_breakeven_for(complexity)

    content_parts: list[str] = []
    all_kept: list[str] = []
    all_elided: list[ElidedSection] = []

    # 0. USER profile (4.4.0 — HP3 wave-1 Hermes USER.md port).
    # Bounded by ``user_profile.current_char_budget()`` so it cannot
    # crowd out handoff / memory; soft-imported so a misconfigured
    # ~/.concinno/USER.md cannot break the whole context build.
    try:  # pragma: no cover - soft path
        from concinno.user_profile import (
            current_char_budget,
            render_profile_for_field_read,
        )
        user_block = render_profile_for_field_read(
            max_chars=current_char_budget(),
        )
        if user_block:
            content_parts.append(user_block)
            all_kept.append("user_profile:overview")
    except Exception:
        # Never fail the prompt build because of USER.md weirdness —
        # the operator can run ``concinno user-profile show`` to debug.
        pass

    # 1. Handoff
    handoff_files = _find_handoff_files(workspace)
    if handoff_files:
        handoff_files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        handoff_budget_each = cfg.handoff_budget // min(
            len(handoff_files), 2,
        )
        for hf in handoff_files[:2]:
            sub = read_handoff_fields_v2(
                hf, keywords,
                max_tokens=handoff_budget_each,
                compress_breakeven=breakeven,
            )
            if sub.content:
                basename = os.path.basename(hf)
                content_parts.append(f"📋 {basename}:\n{sub.content}")
                prefix = f"handoff:{basename}:"
                all_kept.extend(prefix + k for k in sub.sections_kept)
                for e in sub.sections_elided:
                    all_elided.append(ElidedSection(
                        id=prefix + e.id,
                        heading=e.heading,
                        lines=e.lines,
                        gist=e.gist,
                        confidence=e.confidence,
                    ))

    # 2. Memory
    memory_dir = _find_memory_dir(workspace)
    if memory_dir and keywords:
        mem = read_memory_fields_v2(
            memory_dir, keywords,
            max_tokens=cfg.memory_budget,
            max_entries=cfg.max_memory_entries,
        )
        if mem.content:
            content_parts.append(f"🧠 Memory:\n{mem.content}")
            prefix = "memory:"
            all_kept.extend(prefix + k for k in mem.sections_kept)
            for e in mem.sections_elided:
                all_elided.append(ElidedSection(
                    id=prefix + e.id,
                    heading=e.heading,
                    lines=e.lines,
                    gist=e.gist,
                    confidence=e.confidence,
                ))

    if not content_parts and not all_elided:
        return _empty_result()

    content = "\n\n".join(content_parts)
    return FieldReadResult(
        content=content,
        sections_kept=all_kept,
        sections_elided=all_elided,
        confidence=_overall_confidence(all_elided),
        expand_hint=_build_expand_hint(all_elided),
    )


def build_field_context(
    workspace: str,
    task_prompt: str = "",
    config: Optional[FieldReadConfig] = None,
) -> str:
    """Build compressed field context from handoff + memory sources.

    This is the main v1-compatible entry point. Returns a string — the
    same format as 2.5.x — by extracting `.content` from the v2 result.

    Args:
        workspace: Project workspace root directory.
        task_prompt: Current task description for keyword matching.
        config: Optional configuration override.

    Returns:
        Combined field context string, or empty string if nothing found.

    See `build_field_context_v2` for structured output with elided-
    section breadcrumbs.
    """
    return build_field_context_v2(workspace, task_prompt, config).content


# ── v2 PromptEngine integration helpers (4.4.0) ───────────


def render_elision_breadcrumbs(
    elided: list[ElidedSection],
    *,
    max_inline_ids: int = 3,
    expand_callback: Optional[str] = None,
) -> str:
    """Render an ``<system-context-elided/>`` breadcrumb tag.

    The tag is the visible counterpart to silent compression: the LLM
    reads it inside the system prompt and learns *what was hidden* +
    *how to ask for it back*. Without this signal, FieldRead v2's
    elision is invisible — the model would hallucinate "the handoff
    didn't mention X" when in fact X was simply elided to save tokens.

    Format::

        <system-context-elided count=3 reason="token-budget"
            ids="status,history,old"
            expand="expand(path, id)"/>

    Args:
        elided: List of :class:`ElidedSection` breadcrumbs from a
            :class:`FieldReadResult`. Empty list yields ``""`` so this
            helper is safe to inline-call without a guard.
        max_inline_ids: Cap on how many ids are listed inline; remaining
            ids are summarised as ``+N more``. Keeps overhead at
            roughly ~50 tokens regardless of elision count (Plan target
            line 61).
        expand_callback: Optional override for the ``expand=...``
            attribute. Defaults to the canonical signature; callers
            wanting a localised hint (e.g. zh-TW) may pass their own
            string.

    Returns:
        XML-style self-closing tag string, or ``""`` when nothing was
        elided.
    """
    if not elided:
        return ""
    n = len(elided)
    ids = [e.id for e in elided[:max_inline_ids]]
    ids_str = ",".join(ids)
    if n > max_inline_ids:
        ids_str += f",+{n - max_inline_ids}-more"
    cb = expand_callback or (
        "concinno.field_read.expand(path, section_id)"
    )
    # Aggregate gist surface: sample first 2 gists for color, capped.
    gist_sample = " | ".join(
        e.gist for e in elided[:2] if e.gist
    )
    if gist_sample and len(gist_sample) > 80:
        gist_sample = gist_sample[:77] + "..."
    gist_attr = f' gist="{gist_sample}"' if gist_sample else ""
    reason = "token-budget"
    return (
        f'<system-context-elided count={n} reason="{reason}" '
        f'ids="{ids_str}"{gist_attr} expand="{cb}"/>'
    )


def build_field_context_v2_string(
    workspace: str,
    task_prompt: str = "",
    config: Optional[FieldReadConfig] = None,
    *,
    complexity: Optional[str] = None,
    include_breadcrumbs: bool = True,
) -> str:
    """Build a string-shaped v2 field context with elision breadcrumbs.

    This is the **PromptEngine integration point** (4.4.0): drop-in
    replacement for :func:`build_field_context` that *also* appends a
    ``<system-context-elided/>`` tag whenever sections were skipped.
    The tag costs roughly ~50 tokens but gives the LLM explicit
    awareness of what was hidden, replacing the "silent compressor"
    failure mode that motivated v2 (see module docstring "Problem v2").

    Args:
        workspace: Project workspace root directory.
        task_prompt: Current task description for keyword matching.
        config: Optional configuration override.
        complexity: Optional C0 complexity classification routed through
            :func:`compress_breakeven_for`.
        include_breadcrumbs: Toggle for the trailing breadcrumb tag.
            Set ``False`` to recover exact byte-for-byte parity with
            :func:`build_field_context` (used by callers who already
            consume the structured FieldReadResult elsewhere).

    Returns:
        Concatenated field context. When elision happened *and*
        ``include_breadcrumbs`` is true, a ``<system-context-elided/>``
        tag is appended on its own line.
    """
    result = build_field_context_v2(
        workspace, task_prompt, config, complexity=complexity,
    )
    if not include_breadcrumbs or not result.sections_elided:
        return result.content
    crumb = render_elision_breadcrumbs(result.sections_elided)
    if not crumb:
        return result.content
    if not result.content:
        return crumb
    return f"{result.content}\n{crumb}"
