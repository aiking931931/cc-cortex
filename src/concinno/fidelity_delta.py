"""concinno.fidelity_delta — Subagent fork in/out fidelity measurement.

@module fidelity_delta
@responsibility Measure structured-field information loss between a
    subagent's input prompt and its returned ``final_text``. Reuses
    :mod:`concinno.field_read`'s elided-log machinery so "lost fields"
    are reported as :class:`ElidedSection` records with gist + confidence,
    not as an opaque scalar.
@dependencies concinno.field_read
@exports FidelityDeltaRecord, compute_fidelity_delta

Rationale (Tran & Kiela 2025 DPI attack; GAIA Sancio plan Part 5):
    Subagent fork collapses a multi-round child loop (potentially rich
    tool-use, intermediate reasoning, citations) into a single
    ``final_text`` string handed back to the parent. That collapse is
    the one fundamental MAS/SAS limit with *no* architectural cover —
    no routing trick or N-aware fan-out recovers information the
    child chose not to say.

    This module quantifies that loss per spawn so:

    - operators can spot high-delta forks (child dropped critical input)
    - the meta-router can penalise arms / task shapes that systematically
      lose information
    - ZIQ can learn "when not to fork at all" via outcome feedback

Field extraction strategy
-------------------------
Inputs are parsed with the same section/bullet heuristics as FieldRead.
Each bullet item, numbered item, or single-field section becomes one
"field". The output is keyword-indexed (``_extract_keywords``); a field
is considered *preserved* when at least ``PRESERVATION_THRESHOLD`` of
its keywords appear in the output. Fields below the threshold are
reported as :class:`ElidedSection` records with ``confidence = 1 - recall``
(higher confidence = we're more sure the field was lost).

Unstructured inputs (no sections, no bullets) are treated as a single
field. A degenerate call with either side empty yields ``delta = 0.0``
and a diagnostic summary — the caller decides whether "no input" or
"no output" is itself a failure signal.

See also
--------
- :mod:`concinno.field_read` — shared ``ElidedSection`` dataclass.
- :mod:`concinno.gaia_meta_router` — N-aware routing; fidelity delta
  is the second half of the routing-vs-fidelity trade-off.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Optional

from concinno.field_read import (
    _BULLET_RE,
    ElidedSection,
    _estimate_tokens,
    _extract_keywords,
    _heading_slug,
    _parse_sections,
)

# Preservation threshold: ≥50% of a field's keywords appearing in the
# output qualifies as "preserved". Below is "lost" and earns an
# ElidedSection record. 50% matches FieldRead's own elision gate and
# empirically separates paraphrased-preserved from dropped on the
# Tran&Kiela subagent sample without needing a learned threshold.
PRESERVATION_THRESHOLD = 0.5

# Soft cap on number of fields extracted. Extremely long prompts can
# produce hundreds of bullets; we keep the top-N by token weight so
# delta stays stable and cheap to compute.
_DEFAULT_MAX_FIELDS = 40

# Numbered list line: "1. item" / "1) item" — treated like bullets.
_NUMBERED_RE = re.compile(r"^\s*\d+[.)]\s+(.+)$")


# ── Structured record ─────────────────────────────────────


@dataclass
class _Field:
    """Internal field record (pre-scoring)."""

    id: str
    title: str
    content: str
    line_count: int


@dataclass
class FidelityDeltaRecord:
    """Fidelity-delta measurement for one subagent fork.

    Attributes:
        delta: Structured-field information loss ∈ [0, 1]. 0 = every
            input field has keyword coverage in the output (full
            fidelity); 1 = nothing in the input was echoed or
            acknowledged in the output.
        recall: ``1 - delta`` — fraction of input fields preserved.
        fields_in: Number of fields extracted from the input.
        fields_preserved: Count with recall ≥ ``PRESERVATION_THRESHOLD``.
        lost_fields: :class:`ElidedSection` records for each dropped
            field (reuses FieldRead's schema; ``confidence`` here reads
            as "how sure we are this field was lost", 0 = ambiguous,
            1 = zero keyword overlap).
        confidence: Meta-confidence in the delta number itself. Low
            when the input had very few keywords (signal too sparse
            to judge), high when we had ≥10 keywords and a clear
            kept/lost split.
        summary: One-line human-readable tag, e.g.
            ``"3/7 fields preserved (Δ=0.57)"``.
    """

    delta: float
    recall: float
    fields_in: int
    fields_preserved: int
    lost_fields: list[ElidedSection] = dc_field(default_factory=list)
    confidence: float = 1.0
    summary: str = ""


# ── Degenerate cases ─────────────────────────────────────


def _degenerate(reason: str) -> FidelityDeltaRecord:
    """Build a zero-delta record for empty-input / empty-output cases.

    Returning ``delta=0`` on "no input" is the safe choice: we have no
    evidence of loss. The caller sees ``fields_in=0`` and can treat
    that as a diagnostic signal instead of a fidelity alarm.
    """
    return FidelityDeltaRecord(
        delta=0.0,
        recall=1.0,
        fields_in=0,
        fields_preserved=0,
        lost_fields=[],
        confidence=0.0,  # no signal to be confident about
        summary=reason,
    )


# ── Field extraction ─────────────────────────────────────


def _split_bullets(section_content: str) -> list[str]:
    """Return the bullet/numbered-item lines from a section body."""
    items: list[str] = []
    for line in section_content.splitlines():
        bm = _BULLET_RE.match(line)
        if bm:
            items.append(bm.group(1).strip())
            continue
        nm = _NUMBERED_RE.match(line)
        if nm:
            items.append(nm.group(1).strip())
    return items


def _extract_fields(text: str, *, max_fields: int) -> list[_Field]:
    """Parse ``text`` into atomic fields for fidelity scoring.

    Strategy:
    1. Parse sections via FieldRead's ``_parse_sections``.
    2. For each section:
       - If it has bullets / numbered items → each item is one field.
       - Else → the whole section body is one field.
    3. If no sections at all (single paragraph prompts) → bullets
       extracted from raw text, else the whole text as one field.
    4. Cap at ``max_fields`` by token weight (largest first).
    """
    fields: list[_Field] = []

    sections = _parse_sections(text)
    if sections:
        for sec in sections:
            items = _split_bullets(sec.content)
            if items:
                for i, item in enumerate(items, 1):
                    base_slug = _heading_slug(sec.title) or sec.section_id
                    fid = f"{base_slug}#{i}" if base_slug else f"item-{i}"
                    fields.append(_Field(
                        id=fid,
                        title=f"{sec.title} · item {i}",
                        content=item,
                        line_count=1,
                    ))
            else:
                fields.append(_Field(
                    id=sec.section_id or _heading_slug(sec.title),
                    title=sec.title,
                    content=sec.content,
                    line_count=sec.line_count or 1,
                ))

    if not fields:
        # Unstructured fallback: try bullets on the raw text.
        bullets = _split_bullets(text)
        if bullets:
            for i, item in enumerate(bullets, 1):
                fields.append(_Field(
                    id=f"item-{i}",
                    title=f"item {i}",
                    content=item,
                    line_count=1,
                ))
        else:
            # Single whole-prompt field.
            fields.append(_Field(
                id="body",
                title="(whole prompt)",
                content=text.strip(),
                line_count=len(text.splitlines()) or 1,
            ))

    if len(fields) <= max_fields:
        return fields

    # Keep the heaviest fields — they carry the most signal.
    fields.sort(key=lambda f: _estimate_tokens(f.content), reverse=True)
    return fields[:max_fields]


# ── Scoring ──────────────────────────────────────────────


def _field_recall(field_kws: set[str], out_kws: set[str]) -> float:
    """Recall = |overlap| / |field_kws|, 0 if field has no keywords."""
    if not field_kws:
        return 0.0
    overlap = field_kws & out_kws
    return len(overlap) / len(field_kws)


def _meta_confidence(total_input_kws: int, total_fields: int) -> float:
    """How much we trust the computed delta.

    - 0 keywords → no signal → 0.
    - 1-4 keywords → sparse → 0.3-0.5.
    - 5-9 keywords → okay → 0.7.
    - ≥10 keywords AND ≥2 fields → strong → 0.9.
    """
    if total_input_kws == 0 or total_fields == 0:
        return 0.0
    if total_input_kws < 5:
        return round(0.3 + 0.05 * total_input_kws, 2)
    if total_input_kws < 10:
        return 0.7
    if total_fields >= 2:
        return 0.9
    return 0.75


# ── Public entry point ───────────────────────────────────


def compute_fidelity_delta(
    in_message: str,
    out_message: str,
    *,
    max_fields: int = _DEFAULT_MAX_FIELDS,
    preservation_threshold: Optional[float] = None,
) -> FidelityDeltaRecord:
    """Compute structured-field fidelity delta for one subagent fork.

    Args:
        in_message: Prompt / user text sent into the child subagent.
        out_message: Child's ``final_text`` returned to the parent.
        max_fields: Cap on extracted input fields (default 40). Very
            long prompts are trimmed to the heaviest-N fields by token
            count so delta computation stays O(n) in field count.
        preservation_threshold: Optional override for the per-field
            recall cutoff. Default :data:`PRESERVATION_THRESHOLD`
            (0.5). Lower values treat partial paraphrase as preserved;
            higher values demand verbatim echo.

    Returns:
        :class:`FidelityDeltaRecord` with delta, recall, per-field
        breadcrumbs for lost fields, and a meta-confidence score
        on the delta itself.

    Notes:
        - Empty inputs / outputs return ``delta=0`` with
          ``confidence=0`` — no evidence is not evidence of loss.
        - Keyword matching is case-insensitive and ignores short /
          stop-word tokens (shared with
          :func:`concinno.field_read._extract_keywords`).
    """
    threshold = (
        PRESERVATION_THRESHOLD
        if preservation_threshold is None
        else max(0.0, min(1.0, preservation_threshold))
    )

    if not in_message or not in_message.strip():
        return _degenerate("empty input")
    if not out_message or not out_message.strip():
        # Empty output from a non-empty input is a strong fidelity hit.
        # Surface it as delta=1 so callers treat the fork as failed.
        fields = _extract_fields(in_message, max_fields=max_fields)
        lost = [
            ElidedSection(
                id=f.id,
                heading=f.title,
                lines=f.line_count,
                gist=f.content[:80].replace("\n", " "),
                confidence=1.0,
            )
            for f in fields
        ]
        total = len(fields)
        return FidelityDeltaRecord(
            delta=1.0 if total else 0.0,
            recall=0.0 if total else 1.0,
            fields_in=total,
            fields_preserved=0,
            lost_fields=lost,
            confidence=_meta_confidence(
                len(_extract_keywords(in_message)), total,
            ),
            summary=(
                f"0/{total} fields preserved (empty output, Δ=1.00)"
                if total else "empty input"
            ),
        )

    fields = _extract_fields(in_message, max_fields=max_fields)
    out_keywords = set(_extract_keywords(out_message))
    input_keywords = set(_extract_keywords(in_message))

    preserved = 0
    lost: list[ElidedSection] = []
    for f in fields:
        field_kws = set(_extract_keywords(f.content))
        if not field_kws:
            # Field is pure punctuation / stop words — nothing to match.
            # Skip it rather than count as lost (would skew delta).
            continue
        recall = _field_recall(field_kws, out_keywords)
        if recall >= threshold:
            preserved += 1
        else:
            lost.append(ElidedSection(
                id=f.id,
                heading=f.title,
                lines=f.line_count,
                gist=f.content[:80].replace("\n", " "),
                confidence=round(1.0 - recall, 3),
            ))

    total = preserved + len(lost)
    if total == 0:
        return _degenerate("no scorable fields in input")

    recall_ratio = preserved / total
    delta = 1.0 - recall_ratio
    confidence = _meta_confidence(len(input_keywords), total)

    return FidelityDeltaRecord(
        delta=round(delta, 3),
        recall=round(recall_ratio, 3),
        fields_in=total,
        fields_preserved=preserved,
        lost_fields=lost,
        confidence=confidence,
        summary=f"{preserved}/{total} fields preserved (Δ={delta:.2f})",
    )


__all__ = [
    "PRESERVATION_THRESHOLD",
    "FidelityDeltaRecord",
    "compute_fidelity_delta",
]
