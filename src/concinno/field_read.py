"""concinno.field_read — Selective field extraction for token-efficient injection.

@module field_read
@responsibility Parse structured markdown (handoffs, memory, learnings) and
    extract only task-relevant sections, compressed to a token budget.
@dependencies None (stdlib only)
@exports read_handoff_fields, read_memory_fields, build_field_context

Problem: DynamicSlots.handoff_summary exists but is never filled. Full handoff
files are 150-300 lines — injecting them raw wastes token budget and hits the
"Lost in the Middle" attention cliff.

Solution: FieldRead selectively extracts high-value sections (⬜/⏸/next_step/
未解決) and keyword-matches remaining sections, compressing to budget.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Optional

# ── Constants ─────────────────────────────────────────────

# ZIQ compression breakeven: below this token count, compression is
# net negative (quality loss > token savings). Empirically validated
# across Qwen2.5-7B + Llama-3-8B: ≥2500t → Pareto improvement
# (both compression ratio AND output quality improve).
# Below 2500t → pass through uncompressed.
COMPRESS_BREAKEVEN_TOKENS = 2500

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

# Frontmatter delimiter
_FRONTMATTER_RE = re.compile(r"^---\s*$")

# Section header (## level)
_SECTION_RE = re.compile(r"^##\s+(.+)$")

# Memory index entry: - [Title](file.md) — description
_MEMORY_ENTRY_RE = re.compile(
    r"^-\s+\[([^\]]+)\]\(([^)]+)\)\s*[—–-]\s*(.+)$",
)

# CJK-aware token estimation (matches prompt_engine.py)
_CJK_RANGE = 0x2E80


def _estimate_tokens(text: str) -> int:
    if not text:
        return 0
    cjk = sum(1 for c in text if ord(c) > _CJK_RANGE)
    ascii_chars = len(text) - cjk
    return int(cjk * 1.5 + ascii_chars / 4)


# ── Section Parser ────────────────────────────────────────


@dataclass
class Section:
    """A parsed markdown section."""

    title: str
    content: str
    priority: int = 0  # higher = more important
    tokens: int = 0

    def __post_init__(self) -> None:
        self.tokens = _estimate_tokens(self.content)


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

    return sections


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


# ── Handoff Field Reader ─────────────────────────────────


def read_handoff_fields(
    handoff_path: str,
    task_keywords: Optional[list[str]] = None,
    max_tokens: int = 200,
) -> str:
    """Extract high-value fields from a handoff file.

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
    """
    if not handoff_path or not os.path.isfile(handoff_path):
        return ""

    try:
        with open(handoff_path, encoding="utf-8") as f:
            text = f.read(20000)  # Cap read to prevent huge files
    except OSError:
        return ""

    # ZIQ breakeven gate: compression only pays off ≥2500t.
    # Below that, return full text (trimmed to max_tokens).
    raw_tokens = _estimate_tokens(text)
    if raw_tokens < COMPRESS_BREAKEVEN_TOKENS:
        if raw_tokens <= max_tokens:
            return text.strip()
        # Still over budget but under breakeven — trim naively
        chars = int(max_tokens * 3.5)
        return text[:chars].strip() + "\n[…truncated]"

    sections = _parse_sections(text)
    if not sections:
        return ""

    keywords = task_keywords or []

    # Score and sort sections
    scored: list[tuple[int, Section]] = []
    for sec in sections:
        score = _score_section(sec, keywords)
        if score > 0:
            sec.priority = score
            scored.append((score, sec))

    scored.sort(key=lambda x: x[0], reverse=True)

    # Pack within budget
    parts: list[str] = []
    used = 0
    for _score, sec in scored:
        if used + sec.tokens <= max_tokens:
            parts.append(f"**{sec.title}**: {sec.content}")
            used += sec.tokens
        else:
            # Try truncating to fit remaining budget
            remaining = max_tokens - used
            if remaining > 30:
                chars = int(remaining * 3.5)
                truncated = sec.content[:chars].rsplit("\n", 1)[0]
                parts.append(f"**{sec.title}**: {truncated}…")
            break

    if not parts:
        return ""

    return "\n".join(parts)


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


def read_memory_fields(
    memory_dir: str,
    task_keywords: Optional[list[str]] = None,
    max_tokens: int = 150,
    max_entries: int = 5,
) -> str:
    """Extract task-relevant memory entries.

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
    if not memory_dir or not os.path.isdir(memory_dir):
        return ""

    index_path = os.path.join(memory_dir, "MEMORY.md")
    entries = _parse_memory_index(index_path)
    if not entries:
        return ""

    keywords = task_keywords or []
    if not keywords:
        return ""

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
        return ""

    # Build compressed output
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
        else:
            # Truncate to fit
            remaining = max_tokens - used
            if remaining > 20:
                chars = int(remaining * 3.5)
                parts.append(line[:chars] + "…")
            break

    if not parts:
        return ""

    return "\n".join(parts)


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


def build_field_context(
    workspace: str,
    task_prompt: str = "",
    config: Optional[FieldReadConfig] = None,
) -> str:
    """Build compressed field context from handoff + memory sources.

    This is the main entry point. It:
    1. Finds handoff files in the workspace
    2. Finds the memory directory
    3. Extracts task-relevant fields from both
    4. Returns a combined compressed string within budget

    Args:
        workspace: Project workspace root directory.
        task_prompt: Current task description for keyword matching.
        config: Optional configuration override.

    Returns:
        Combined field context string, or empty string if nothing found.
    """
    if not workspace:
        return ""

    cfg = config or FieldReadConfig()
    keywords = _extract_keywords(task_prompt) if task_prompt else []

    parts: list[str] = []

    # 1. Handoff fields
    handoff_files = _find_handoff_files(workspace)
    if handoff_files:
        # Use the most recently modified handoff
        handoff_files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        # Read top N handoffs (usually just the most relevant one)
        handoff_budget_each = cfg.handoff_budget // min(
            len(handoff_files), 2,
        )
        for hf in handoff_files[:2]:
            result = read_handoff_fields(
                hf, keywords, max_tokens=handoff_budget_each,
            )
            if result:
                basename = os.path.basename(hf)
                parts.append(f"📋 {basename}:\n{result}")

    # 2. Memory fields
    memory_dir = _find_memory_dir(workspace)
    if memory_dir and keywords:
        mem_result = read_memory_fields(
            memory_dir, keywords,
            max_tokens=cfg.memory_budget,
            max_entries=cfg.max_memory_entries,
        )
        if mem_result:
            parts.append(f"🧠 Memory:\n{mem_result}")

    if not parts:
        return ""

    return "\n\n".join(parts)
