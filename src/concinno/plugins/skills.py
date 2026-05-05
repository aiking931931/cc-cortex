"""concinno.plugins.skills -- Discover packaged SKILL.md directories.

Entry-points group ``concinno.skills`` -- each value resolves to a
directory path (``str``, ``Path``, or callable returning either)
containing one or more ``SKILL.md`` files organised as::

    <root>/
        my_skill/
            SKILL.md
            (other skill assets...)
        another_skill/
            SKILL.md

See :mod:`concinno.plugins` module docstring for the high-level
contract, and :func:`iter_plugin_skill_roots` below for the resolved
iteration order consumed by :func:`concinno.gui.server._discover_skills`.
"""
from __future__ import annotations

import logging
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger("concinno.plugins.skills")

ENTRYPOINT_GROUP_SKILLS = "concinno.skills"


@dataclass
class SkillPluginMeta:
    """Discovery record for one ``concinno.skills`` entry-point."""

    entry_point_name: str
    package: str
    resolved_path: Path | None = None
    load_time_ms: float = 0.0
    errors: list[str] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.errors and self.resolved_path is not None


def _get_entrypoints():
    """Return ``concinno.skills`` entry-points (version-safe)."""
    from importlib.metadata import entry_points

    if sys.version_info >= (3, 12):
        return entry_points(group=ENTRYPOINT_GROUP_SKILLS)

    all_eps = entry_points()
    if isinstance(all_eps, dict):
        return all_eps.get(ENTRYPOINT_GROUP_SKILLS, [])
    return all_eps.select(group=ENTRYPOINT_GROUP_SKILLS)  # type: ignore[union-attr]


def _extract_package_name(ep: Any) -> str:
    """Same extraction heuristic as ``features._extract_package_name``.

    Kept local to avoid a cross-module import for such a small helper.
    """
    dist = getattr(ep, "dist", None)
    if dist is not None:
        name = getattr(dist, "name", None)
        if name:
            return str(name)
    value = str(getattr(ep, "value", ""))
    if ":" in value:
        mod_path = value.split(":", 1)[0]
        if "." in mod_path:
            return mod_path.split(".", 1)[0]
        return mod_path
    return "<unknown>"


def _resolve_to_path(raw: Any) -> Path | None:
    """Turn an entry-point value into a :class:`Path`, or None.

    Accepts: ``Path``, ``str``, callable-returning-either. Rejects
    other shapes to avoid surprising behaviour (e.g. accepting a
    ``list`` would invite ambiguity about which element is the root).
    """
    if callable(raw) and not isinstance(raw, (str, Path)):
        try:
            raw = raw()
        except Exception:
            return None
    if isinstance(raw, Path):
        return raw
    if isinstance(raw, str):
        return Path(raw)
    return None


def _load_single_entrypoint(ep: Any) -> SkillPluginMeta:
    """Load, resolve, and sanity-check one ``concinno.skills`` EP."""
    ep_name = str(getattr(ep, "name", "<anon>"))
    pkg = _extract_package_name(ep)
    t0 = time.monotonic()

    try:
        raw = ep.load()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "skill plugin %r in package %r failed to load: %s",
            ep_name,
            pkg,
            exc,
        )
        return SkillPluginMeta(
            entry_point_name=ep_name,
            package=pkg,
            load_time_ms=(time.monotonic() - t0) * 1000,
            errors=[f"ep.load() failed: {exc}"],
        )

    load_ms = (time.monotonic() - t0) * 1000
    resolved = _resolve_to_path(raw)
    if resolved is None:
        return SkillPluginMeta(
            entry_point_name=ep_name,
            package=pkg,
            load_time_ms=load_ms,
            errors=[
                f"expected str|Path|callable, got {type(raw).__name__}"
            ],
        )

    try:
        is_dir = resolved.is_dir()
    except OSError as exc:
        return SkillPluginMeta(
            entry_point_name=ep_name,
            package=pkg,
            load_time_ms=load_ms,
            errors=[f"cannot stat {resolved}: {exc}"],
        )
    if not is_dir:
        return SkillPluginMeta(
            entry_point_name=ep_name,
            package=pkg,
            load_time_ms=load_ms,
            errors=[f"resolved path is not a directory: {resolved}"],
        )

    return SkillPluginMeta(
        entry_point_name=ep_name,
        package=pkg,
        resolved_path=resolved,
        load_time_ms=load_ms,
    )


def discover_skill_entrypoints(
    *,
    entry_points_override: Any = None,
) -> list[SkillPluginMeta]:
    """Scan all ``concinno.skills`` entry-points and resolve each."""
    from concinno.plugins import _is_pkg_allowed, is_plugins_enabled, plugin_allowlist

    if not is_plugins_enabled():
        return []

    if entry_points_override is not None:
        eps = entry_points_override
    else:
        try:
            eps = _get_entrypoints()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "concinno.skills entry-points discovery failed: %s", exc
            )
            return []

    allow = plugin_allowlist()
    results: list[SkillPluginMeta] = []
    for ep in eps:
        pkg = _extract_package_name(ep)
        if not _is_pkg_allowed(pkg, allow):
            continue
        results.append(_load_single_entrypoint(ep))
    return results


def iter_plugin_skill_roots() -> Iterator[tuple[Path, str]]:
    """Yield ``(skill_dir, package_name)`` for every valid plugin root.

    ``skill_dir`` is the directory the plugin registered. The caller
    (``gui.server._discover_skills``) is responsible for walking the
    directory's children and calling ``_ingest_skill`` on each
    subdirectory that contains a ``SKILL.md``.
    """
    for plugin in discover_skill_entrypoints():
        if plugin.valid and plugin.resolved_path is not None:
            yield plugin.resolved_path, plugin.package
