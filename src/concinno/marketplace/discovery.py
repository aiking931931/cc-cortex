"""Local + PyPI discovery of ``concinno-skills-*`` distributions.

Two sources fold into a single :class:`MarketplaceRow` shape:

* :func:`list_installed_concinno_skills` — walks
  :func:`importlib.metadata.distributions` for the ``concinno-skills-``
  prefix. Surfaces installed sub-packages whether or not they ship a
  ``SKILL.md`` (fixes bug 4b — hook-only packages were invisible).
* :func:`list_available_pypi` — uses :class:`PyPIClient` (1-hour cache)
  to fetch the candidate list from the PyPI JSON API. Degrades
  gracefully to a hardcoded first-party fallback list when offline.

Both return ``MarketplaceRow`` instances; callers merge by ``name``.
"""
from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Any, Iterable, cast

logger = logging.getLogger("concinno.marketplace.discovery")


# Hardcoded first-party fallback. Keeps cold-start meaningful when PyPI
# is unreachable on the very first GUI open. Maintained manually; PyPI
# is the source of truth so freshness comes from the cache layer.
HARDCODED_AVAILABLE: tuple[str, ...] = (
    "concinno-skills-memory",
    "concinno-skills-memoria",
    "concinno-skills-ziq",
)


# Sub-packages whose distribution name starts with this prefix qualify
# as a marketplace entry. Anything else is ignored — we never surface
# arbitrary distributions, only the curated ``concinno-skills-*``
# namespace.
DIST_PREFIX = "concinno-skills-"


# Distribution name validation: lowercase, digits, hyphens. Mirrors the
# subset of PEP 503 normalization we accept on install.
_DIST_NAME_RE = re.compile(rf"^{re.escape(DIST_PREFIX)}[a-z0-9-]+$")


@dataclass(frozen=True)
class MarketplaceRow:
    """One row in ``/api/skills/marketplace``.

    Field semantics match design doc §1.4. ``kind`` distinguishes
    skill-pkgs (ship SKILL.md dirs) from hook-pkgs (only register
    ``concinno.hooks.*`` entry-points). ``wired_consumers`` lists which
    main-pkg hooks call into the package, so install isn't blind.
    """

    name: str
    kind: str  # "skill-pkg" | "hook-pkg" | "unknown"
    version_installed: str | None
    version_latest: str | None
    summary: str
    homepage: str
    hook_entry_points: list[dict[str, str]] = field(default_factory=list)
    skill_entry_points: list[dict[str, str]] = field(default_factory=list)
    skill_md_dirs: list[str] = field(default_factory=list)
    frontmatter_status: str = "unchecked"
    install_state: str = "available"
    wired_consumers: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-friendly dict for the FastAPI response."""
        return asdict(self)


def is_valid_dist_name(name: str) -> bool:
    """True iff ``name`` matches the ``concinno-skills-<slug>`` shape.

    Used to (a) filter ``importlib.metadata.distributions`` results and
    (b) refuse install/uninstall calls with arbitrary package names.
    """
    return bool(_DIST_NAME_RE.match(name))


def _hook_groups_for(dist_name: str) -> list[str]:
    """Best-effort mapping from a sub-pkg name to the main-pkg hook
    groups it typically wires into. Used to populate
    ``wired_consumers`` so the install dialog isn't a black box.

    Heuristic, not authoritative — the marketplace also surfaces the
    actual ``hook_entry_points`` so the operator can verify.
    """
    suffix = dist_name[len(DIST_PREFIX):]
    table = {
        "memory": ["concinno.hooks.on_session_start", "concinno.hooks.on_stop"],
        "memoria": [],
        "ziq": ["concinno.hooks.on_post_tool"],
    }
    return list(table.get(suffix, []))


def _entry_points_for_dist(dist: importlib_metadata.Distribution) -> tuple[
    list[dict[str, str]], list[dict[str, str]]
]:
    """Return (hook_eps, skill_eps) for a single distribution.

    Walks the dist's own ``entry_points`` attribute (PEP 621) so we do
    not require the package to be importable yet.
    """
    hook_eps: list[dict[str, str]] = []
    skill_eps: list[dict[str, str]] = []
    try:
        eps = dist.entry_points
    except Exception:  # noqa: BLE001 — broken metadata, log + skip
        logger.warning("entry_points read failed for %s", dist.metadata["Name"])
        return hook_eps, skill_eps
    for ep in eps:
        group = getattr(ep, "group", "")
        record = {
            "group": str(group),
            "name": str(getattr(ep, "name", "")),
            "value": str(getattr(ep, "value", "")),
        }
        if group.startswith("concinno.hooks"):
            hook_eps.append(record)
        elif group == "concinno.skills":
            skill_eps.append(record)
    return hook_eps, skill_eps


def _summary_of(dist: importlib_metadata.Distribution) -> str:
    """Short summary string from PKG-INFO ``Summary`` field."""
    try:
        meta = cast(Any, dist.metadata)
    except Exception:  # noqa: BLE001
        return ""
    return str(meta.get("Summary") or "").strip()


def _homepage_of(dist_name: str) -> str:
    """Canonical PyPI project URL for a distribution."""
    return f"https://pypi.org/project/{dist_name}/"


def list_installed_concinno_skills(
    *,
    distributions: Iterable[importlib_metadata.Distribution] | None = None,
) -> list[MarketplaceRow]:
    """Walk installed distributions and return marketplace rows.

    Args:
        distributions: Override iterator (test injection). When omitted,
            uses :func:`importlib.metadata.distributions`.

    Returns:
        List of :class:`MarketplaceRow` with ``install_state="installed"``
        and best-effort ``kind`` classification. Order is sorted by
        distribution name so the GUI render is stable.
    """
    if distributions is None:
        try:
            distributions = list(importlib_metadata.distributions())
        except Exception as exc:  # noqa: BLE001
            logger.warning("distributions() walk failed: %s", exc)
            return []

    rows: list[MarketplaceRow] = []
    for dist in distributions:
        try:
            meta = cast(Any, dist.metadata)
            name = str(meta.get("Name") or "")
        except Exception:  # noqa: BLE001
            continue
        if not name or not is_valid_dist_name(name):
            continue
        version = str(getattr(dist, "version", "") or "")
        hook_eps, skill_eps = _entry_points_for_dist(dist)
        skill_md_dirs = _resolve_skill_md_dirs(skill_eps)
        kind = "skill-pkg" if (skill_eps or skill_md_dirs) else "hook-pkg"
        rows.append(
            MarketplaceRow(
                name=name,
                kind=kind,
                version_installed=version or None,
                version_latest=None,  # filled in by merge step
                summary=_summary_of(dist),
                homepage=_homepage_of(name),
                hook_entry_points=hook_eps,
                skill_entry_points=skill_eps,
                skill_md_dirs=skill_md_dirs,
                frontmatter_status="unchecked",
                install_state="installed",
                wired_consumers=_hook_groups_for(name),
            )
        )
    rows.sort(key=lambda r: r.name)
    return rows


def _resolve_skill_md_dirs(
    skill_eps: list[dict[str, str]],
) -> list[str]:
    """Best-effort resolution of ``concinno.skills`` entry-points to
    on-disk dir paths. Failures are silent — the caller treats an
    empty list as "no SKILL.md detected".
    """
    out: list[str] = []
    for ep_record in skill_eps:
        # ``value`` is a ``module:attr`` reference. We deliberately do
        # NOT import here (lazy by design — see __init__ docstring).
        # The GUI surfaces this as informational; the existing
        # ``iter_plugin_skill_roots`` does the real loading on demand.
        value = ep_record.get("value", "")
        if value:
            out.append(value)
    return out


def list_available_pypi(
    *,
    pypi_client: Any | None = None,
    cache_age_seconds: int | None = None,
) -> tuple[list[MarketplaceRow], bool, int]:
    """Return ``(rows, pypi_reachable, cache_age_sec)``.

    Args:
        pypi_client: Override (test injection). When omitted a fresh
            :class:`PyPIClient` is constructed.
        cache_age_seconds: Override age reported alongside rows (test
            injection — production reads it from the client).

    Returns:
        Tuple of (list of :class:`MarketplaceRow` for **available**
        candidates, ``pypi_reachable`` flag, cache age in seconds).
        On total PyPI failure we fall back to :data:`HARDCODED_AVAILABLE`
        with empty version metadata; the operator still sees a useful
        catalogue.
    """
    from concinno.marketplace.pypi_client import (
        PyPIClient,
        PyPIUnreachableError,
    )

    client = pypi_client or PyPIClient()
    rows: list[MarketplaceRow] = []
    pypi_reachable = True
    try:
        candidates = client.list_concinno_skills_packages()
    except PyPIUnreachableError as exc:
        logger.info("PyPI unreachable, using hardcoded fallback: %s", exc)
        pypi_reachable = False
        candidates = [
            {"name": n, "version": None, "summary": ""}
            for n in HARDCODED_AVAILABLE
        ]

    for candidate in candidates:
        name = candidate.get("name", "")
        if not is_valid_dist_name(name):
            continue
        rows.append(
            MarketplaceRow(
                name=name,
                kind="unknown",
                version_installed=None,
                version_latest=candidate.get("version"),
                summary=candidate.get("summary", ""),
                homepage=_homepage_of(name),
                hook_entry_points=[],
                skill_entry_points=[],
                skill_md_dirs=[],
                frontmatter_status="unchecked",
                install_state="available",
                wired_consumers=_hook_groups_for(name),
            )
        )
    rows.sort(key=lambda r: r.name)
    age = cache_age_seconds if cache_age_seconds is not None else client.cache_age_seconds()
    return rows, pypi_reachable, age


def merge_installed_and_available(
    installed: list[MarketplaceRow],
    available: list[MarketplaceRow],
) -> tuple[list[MarketplaceRow], list[MarketplaceRow]]:
    """Combine installed + PyPI lists.

    Rules:
      * ``installed`` rows keep their ``install_state="installed"`` and
        absorb ``version_latest`` from the matching available row when
        present. ``install_state`` flips to ``"outdated"`` when the
        latest version differs from the installed one (string compare —
        we never auto-upgrade so no semver math required).
      * ``available`` rows for packages already installed are dropped
        (they merge into the installed list).
      * ``available`` rows for un-installed packages stay as-is.
    """
    by_name: dict[str, MarketplaceRow] = {r.name: r for r in available}
    merged_installed: list[MarketplaceRow] = []
    for row in installed:
        latest = by_name.pop(row.name, None)
        if latest is None:
            merged_installed.append(row)
            continue
        new_state = row.install_state
        if (
            latest.version_latest
            and row.version_installed
            and latest.version_latest != row.version_installed
        ):
            new_state = "outdated"
        merged_installed.append(
            MarketplaceRow(
                name=row.name,
                kind=row.kind,
                version_installed=row.version_installed,
                version_latest=latest.version_latest,
                summary=row.summary or latest.summary,
                homepage=row.homepage,
                hook_entry_points=row.hook_entry_points,
                skill_entry_points=row.skill_entry_points,
                skill_md_dirs=row.skill_md_dirs,
                frontmatter_status=row.frontmatter_status,
                install_state=new_state,
                wired_consumers=row.wired_consumers,
            )
        )

    remaining_available = sorted(by_name.values(), key=lambda r: r.name)
    merged_installed.sort(key=lambda r: r.name)
    return merged_installed, remaining_available


def list_extra_skill_dirs() -> list[Path]:
    """Yield extra skill directories contributed by installed
    ``concinno-skills-*`` packages.

    Used by :func:`concinno.gui.server._skills_roots` (bug 4b extension)
    so the existing Skills tab also surfaces SKILL.md content shipped
    inside hook-only packages. Walks installed distributions' ``Files:``
    metadata for ``SKILL.md`` siblings under ``<dist>/skills/`` (the
    convention picked by HP1 first-party packages).

    Returns:
        Sorted list of directory paths (each containing one SKILL.md or
        a tree of them). Empty list when no installed package has such
        a directory.
    """
    out: set[Path] = set()
    try:
        dists = list(importlib_metadata.distributions())
    except Exception:  # noqa: BLE001
        return []
    for dist in dists:
        try:
            meta = cast(Any, dist.metadata)
            name = str(meta.get("Name") or "")
        except Exception:  # noqa: BLE001
            continue
        if not is_valid_dist_name(name):
            continue
        try:
            files = dist.files or []
        except Exception:  # noqa: BLE001
            continue
        for rel in files:
            # Standard convention: SKILL.md lives under ``<pkg>/skills/<slug>/SKILL.md``.
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
            located_path = Path(located)
            # Walk up to the parent dir that holds the SKILL.md so the
            # GUI can ingest it just like a filesystem skill.
            parent = located_path.parent
            if parent.is_dir():
                # Add the **grand-parent** so each individual skill dir
                # under it gets ingested by the GUI walker. Mirrors how
                # ``iter_plugin_skill_roots`` is consumed.
                grand = parent.parent
                if grand.is_dir():
                    out.add(grand)
    return sorted(out)
