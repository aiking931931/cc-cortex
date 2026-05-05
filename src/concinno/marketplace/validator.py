"""Frontmatter validation for installed marketplace distributions.

Thin adapter around the existing HP1
:func:`concinno.skills.frontmatter_validator.validate_skill_md`
hardened in 4.4.0. The marketplace calls this after a successful pip
install so the GUI row can flip to ``frontmatter_status="invalid"``
when the freshly installed package ships malformed metadata.

We deliberately do not re-implement validation — the HP1 module is
the canonical implementation and the marketplace must not fork its
opinions.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Any

logger = logging.getLogger("concinno.marketplace.validator")


@dataclass(frozen=True)
class FrontmatterReport:
    """Marketplace-friendly subset of HP1's :class:`ValidationReport`.

    The full validator type carries internal helper fields we do not
    want to leak across the REST surface. This dataclass captures the
    operator-relevant signal: did frontmatter parse, what counts of
    ERROR / RECOMMENDATION / NOTE issues did the file emit, and how
    many were auto-fixable.
    """

    skill_md_path: str
    status: str  # "valid" | "invalid" | "absent"
    error_count: int
    recommendation_count: int
    note_count: int
    fixable_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill_md_path": self.skill_md_path,
            "status": self.status,
            "error_count": self.error_count,
            "recommendation_count": self.recommendation_count,
            "note_count": self.note_count,
            "fixable_count": self.fixable_count,
        }


def _classify_severity(report: Any) -> tuple[int, int, int]:
    """Bucket HP1 issues into (errors, recommendations, notes)."""
    err = rec = note = 0
    for issue in getattr(report, "issues", []):
        sev = getattr(getattr(issue, "severity", None), "name", "")
        if sev == "ERROR":
            err += 1
        elif sev == "RECOMMENDATION":
            rec += 1
        else:
            note += 1
    return err, rec, note


def _files_for_dist(dist_name: str) -> list[Path]:
    """Return absolute paths of every ``SKILL.md`` shipped by a dist.

    Returns empty list when the distribution has no SKILL.md files
    (the common case for hook-only sub-pkgs).
    """
    try:
        dist = importlib_metadata.distribution(dist_name)
    except importlib_metadata.PackageNotFoundError:
        return []
    out: list[Path] = []
    try:
        files = dist.files or []
    except Exception:  # noqa: BLE001
        return []
    for rel in files:
        try:
            rel_str = str(rel)
        except Exception:  # noqa: BLE001
            continue
        if not rel_str.endswith("SKILL.md"):
            continue
        try:
            located = rel.locate()
        except Exception:  # noqa: BLE001
            continue
        if located is None:
            continue
        out.append(Path(located))
    return out


def validate_dist_frontmatter(
    dist_name: str,
) -> list[FrontmatterReport]:
    """Validate every SKILL.md shipped by a distribution.

    Args:
        dist_name: Distribution name (already validated by caller).

    Returns:
        List of :class:`FrontmatterReport`. Empty list when the dist
        ships no SKILL.md files (caller renders ``frontmatter_status:
        "absent"`` on the row).
    """
    skill_files = _files_for_dist(dist_name)
    if not skill_files:
        return []

    # Lazy import — keeps marketplace cold-start cheap when no install
    # has happened yet.
    try:
        from concinno.skills.frontmatter_validator import validate_skill_md
    except ImportError as exc:
        logger.warning(
            "frontmatter_validator unavailable, skipping validation: %s",
            exc,
        )
        return []

    out: list[FrontmatterReport] = []
    for path in skill_files:
        try:
            raw = validate_skill_md(path)
        except Exception as exc:  # noqa: BLE001 — defensive at REST edge
            logger.warning("validate_skill_md(%s) failed: %s", path, exc)
            out.append(
                FrontmatterReport(
                    skill_md_path=str(path),
                    status="invalid",
                    error_count=1,
                    recommendation_count=0,
                    note_count=0,
                    fixable_count=0,
                )
            )
            continue
        err, rec, note = _classify_severity(raw)
        status = "valid" if err == 0 else "invalid"
        fixable = len(getattr(raw, "fixable", ()) or ())
        out.append(
            FrontmatterReport(
                skill_md_path=str(path),
                status=status,
                error_count=err,
                recommendation_count=rec,
                note_count=note,
                fixable_count=fixable,
            )
        )
    return out
