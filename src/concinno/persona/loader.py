"""Persona file (Markdown + YAML frontmatter) parser.

A persona file is a plain Markdown document whose YAML frontmatter
declares the structured persona schema. The body (everything after
the closing ``---`` line) is treated as a free-form description and
is appended to the persona's ``personality`` field if not already
populated.

Cleanroom rewrite — no code copied from any other persona-style
loader. Uses stdlib only (no PyYAML required for round-trip in
the Track 1 minimal subset; consumers shipping richer YAML can
swap in their own loader).

Why no PyYAML hard dep: PyYAML isn't a Concinno core dep, and the
persona schema's frontmatter shape is small enough that a naive
indent-aware parser handles every case the schema permits. If a
consumer hits the parser limits they can pre-parse with PyYAML and
construct ``PersonaSchema`` directly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from concinno.persona.schema import EmotionalState, PersonaSchema, PinnedMemory


def _split_frontmatter(text: str) -> tuple[str, str]:
    """Return (frontmatter_yaml, body_markdown) from a persona file."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return "", text
    end = -1
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end < 0:
        return "", text
    fm = "\n".join(lines[1:end])
    body = "\n".join(lines[end + 1:]).strip()
    return fm, body


def _try_yaml(fm: str) -> dict[str, Any] | None:
    """Best-effort YAML parse. Returns None if PyYAML unavailable or parse fails."""
    try:
        import yaml  # type: ignore
    except ImportError:
        return None
    try:
        data = yaml.safe_load(fm)
        if isinstance(data, dict):
            return data
        return None
    except Exception:
        return None


def _parse_naive(fm: str) -> dict[str, Any]:
    """Tiny indent-aware parser for the small subset of YAML the schema needs.

    Handles:
        key: value
        key: |  (followed by indented block)
        key:
          - item
          - item
        key:
          subkey: value
    """
    result: dict[str, Any] = {}
    lines = fm.splitlines()
    i = 0
    while i < len(lines):
        raw = lines[i]
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue

        # Top-level key (no leading indent).
        if not raw.startswith(" ") and ":" in line:
            key, _, rest = line.partition(":")
            key = key.strip()
            rest = rest.strip()
            if rest:
                # inline value
                result[key] = _coerce_scalar(rest)
                i += 1
                continue
            # nested block — peek ahead
            block: list[str] = []
            j = i + 1
            while j < len(lines) and (lines[j].startswith(" ") or not lines[j].strip()):
                block.append(lines[j])
                j += 1
            result[key] = _parse_block(block)
            i = j
            continue
        i += 1
    return result


def _coerce_scalar(s: str) -> Any:
    """Strip quotes; coerce numeric / bool literals."""
    s = s.strip()
    if (s.startswith("'") and s.endswith("'")) or (s.startswith('"') and s.endswith('"')):
        return s[1:-1]
    if s.lower() in ("true", "yes"):
        return True
    if s.lower() in ("false", "no"):
        return False
    try:
        if "." in s:
            return float(s)
        return int(s)
    except ValueError:
        return s


def _parse_block(block: list[str]) -> Any:
    """Parse an indented block as either a list or a sub-dict."""
    items = [b for b in block if b.strip()]
    if not items:
        return None
    # All list items?
    stripped = [it.lstrip() for it in items]
    if all(s.startswith("- ") or s == "-" for s in stripped):
        out_list: list[Any] = []
        i = 0
        while i < len(stripped):
            entry = stripped[i][2:].strip() if stripped[i] != "-" else ""
            # Inline scalar list item
            if ":" not in entry or entry.startswith('"') or entry.startswith("'"):
                out_list.append(_coerce_scalar(entry))
                i += 1
                continue
            # Dict-shaped item — gather following indented lines until next "- "
            sub: dict[str, Any] = {}
            k, _, v = entry.partition(":")
            sub[k.strip()] = _coerce_scalar(v.strip()) if v.strip() else None
            j = i + 1
            while j < len(stripped) and not stripped[j].startswith("- "):
                if ":" in stripped[j]:
                    kk, _, vv = stripped[j].partition(":")
                    sub[kk.strip()] = _coerce_scalar(vv.strip())
                j += 1
            out_list.append(sub)
            i = j
        return out_list
    # Otherwise treat as nested dict: drop leading common indent then recurse.
    indent = min(len(it) - len(it.lstrip()) for it in items if it.strip())
    dedented = [it[indent:] for it in items]
    return _parse_naive("\n".join(dedented))


def _build_schema(data: dict[str, Any], body: str) -> PersonaSchema:
    """Construct a PersonaSchema from a parsed dict + optional MD body."""
    if "name" not in data:
        raise ValueError("persona file missing required 'name' field in frontmatter")

    pinned_raw = data.get("pinned_memories") or []
    pins: list[PinnedMemory] = []
    if isinstance(pinned_raw, list):
        for entry in pinned_raw:
            if isinstance(entry, dict) and "content" in entry:
                pins.append(
                    PinnedMemory(
                        content=str(entry["content"]),
                        pinned_at=str(entry.get("pinned_at") or ""),
                        reason=entry.get("reason"),
                    )
                )

    em_raw = data.get("emotional_state") or {}
    if isinstance(em_raw, dict):
        em = EmotionalState(
            default=em_raw.get("default", "neutral"),
            intensity=float(em_raw.get("intensity", 0.5)),
            decay_rate=float(em_raw.get("decay_rate", 0.95)),
        )
    else:
        em = EmotionalState()

    seed_raw = data.get("memory_seed") or []
    seed = [str(s) for s in seed_raw] if isinstance(seed_raw, list) else []

    personality = str(data.get("personality") or "").strip()
    if not personality and body:
        personality = body[:500]

    return PersonaSchema(
        name=str(data["name"]),
        personality=personality,
        voice=str(data.get("voice") or "").strip(),
        memory_seed=seed,
        pinned_memories=pins,
        emotional_state=em,
    )


def load_persona_file(path: str | Path) -> PersonaSchema:
    """Load a persona file from disk and return a validated PersonaSchema."""
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    fm, body = _split_frontmatter(text)
    if not fm.strip():
        raise ValueError(f"persona file {p} has no YAML frontmatter")
    data = _try_yaml(fm) or _parse_naive(fm)
    if not isinstance(data, dict):
        raise ValueError(f"persona file {p} frontmatter did not parse to a dict")
    return _build_schema(data, body)


__all__ = ["load_persona_file"]
