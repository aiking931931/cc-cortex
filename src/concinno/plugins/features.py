"""concinno.plugins.features -- Discover third-party feature plugins.

Entry-points group ``concinno.features`` -- each value resolves to a
``dict[str, dict]`` mapping feature name to a FEATURE_META-shaped meta
dict. See :mod:`concinno.plugins` module docstring for the high-level
contract and :file:`docs/how-to-ship-a-skills-package.md` for an
end-to-end example.

Validation semantics (2.31.0 spec v2 amendment A3):

* Accept-unknown ``schema_version``: missing -> warn+v1, ``>1``
  -> warn+ignore-unknown-fields, ``<0`` or non-int -> reject.
* Reject when core fields missing (``category`` / ``description`` /
  ``enabled``) -- cannot render without them.
* Every ``ep.load()`` wrapped in try/except; failures produce
  :class:`FeaturePluginMeta` with populated ``errors`` list rather
  than aborting discovery (amendment A2).
"""
from __future__ import annotations

import logging
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Iterator

logger = logging.getLogger("concinno.plugins.features")

ENTRYPOINT_GROUP_FEATURES = "concinno.features"

# Core meta fields that must be present for a feature row to be
# renderable in the GUI or usable by ``iter_all_features_with_origin``.
_REQUIRED_FIELDS = frozenset({"category", "description", "enabled"})

# Current schema version -- bumped on breaking changes to the meta
# shape. Unknown-forward ``schema_version`` values are accepted with
# a warning so future plugin authors can opt into new fields without
# waiting for every Concinno consumer to upgrade.
CURRENT_SCHEMA_VERSION = 1


@dataclass
class FeaturePluginMeta:
    """Discovery record for one ``concinno.features`` entry-point.

    Attributes:
        entry_point_name: the ``name = "..."`` key from pyproject.toml
            entry-points table. Does *not* correspond to any single
            feature name -- one entry-point may yield multiple features.
        package: the distribution name that owns this entry-point
            (``ep.dist.name`` when available, else best-effort extracted
            from ``ep.value``).
        meta_dict: the resolved ``dict[str, dict]`` or empty dict on
            load failure.
        load_time_ms: how long ``ep.load()`` took in milliseconds.
        errors: human-readable errors encountered during load or
            validation. Empty = valid.
    """

    entry_point_name: str
    package: str
    meta_dict: dict[str, dict[str, Any]] = field(default_factory=dict)
    load_time_ms: float = 0.0
    errors: list[str] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.errors


def _get_entrypoints():
    """Return the ``concinno.features`` entry-points (version-safe).

    Mirrors :func:`concinno.plugin_loader._get_entrypoints` so the two
    branches behave the same across Python 3.10 -> 3.12.
    """
    from importlib.metadata import entry_points

    if sys.version_info >= (3, 12):
        return entry_points(group=ENTRYPOINT_GROUP_FEATURES)

    all_eps = entry_points()
    if isinstance(all_eps, dict):
        return all_eps.get(ENTRYPOINT_GROUP_FEATURES, [])
    return all_eps.select(group=ENTRYPOINT_GROUP_FEATURES)  # type: ignore[union-attr]


def _extract_package_name(ep: Any) -> str:
    """Best-effort extraction of the owning distribution name."""
    dist = getattr(ep, "dist", None)
    if dist is not None:
        name = getattr(dist, "name", None)
        if name:
            return str(name)
    # Fallback: parse from ``value`` of form "pkg.mod:attr".
    value = str(getattr(ep, "value", ""))
    if ":" in value:
        mod_path = value.split(":", 1)[0]
        if "." in mod_path:
            return mod_path.split(".", 1)[0]
        return mod_path
    return "<unknown>"


def _validate_feature_meta(name: str, meta: Any) -> list[str]:
    """Validate a single feature meta dict per amendment A3 semantics.

    Returns a list of error strings. Empty list = valid.
    """
    errors: list[str] = []

    if not isinstance(meta, dict):
        errors.append(
            f"feature {name!r}: meta must be a dict, got {type(meta).__name__}"
        )
        return errors

    # schema_version validation (accept-unknown-forward).
    schema_version = meta.get("schema_version", None)
    if schema_version is None:
        logger.warning(
            "feature %r: schema_version missing; assuming %d (forward-compat)",
            name,
            CURRENT_SCHEMA_VERSION,
        )
    elif isinstance(schema_version, bool) or not isinstance(schema_version, int):
        errors.append(
            f"feature {name!r}: schema_version must be an int, got "
            f"{type(schema_version).__name__}"
        )
        return errors
    elif schema_version < 0:
        errors.append(
            f"feature {name!r}: schema_version={schema_version} is negative"
        )
        return errors
    elif schema_version > CURRENT_SCHEMA_VERSION:
        logger.warning(
            "feature %r: schema_version=%d > current %d; unknown fields "
            "will be accepted with best-effort rendering",
            name,
            schema_version,
            CURRENT_SCHEMA_VERSION,
        )

    # Required core fields.
    missing = _REQUIRED_FIELDS - set(meta.keys())
    if missing:
        errors.append(
            f"feature {name!r}: missing required fields: "
            f"{sorted(missing)}"
        )

    return errors


def _load_single_entrypoint(ep: Any) -> FeaturePluginMeta:
    """Load and validate one ``concinno.features`` entry-point.

    Wraps every exception so a misbehaving plugin package does not
    abort discovery of the other plugins.
    """
    ep_name = str(getattr(ep, "name", "<anon>"))
    pkg = _extract_package_name(ep)
    t0 = time.monotonic()

    try:
        raw = ep.load()
    except Exception as exc:  # noqa: BLE001 -- intentional broad catch
        logger.warning(
            "plugin %r in package %r failed to load: %s", ep_name, pkg, exc
        )
        return FeaturePluginMeta(
            entry_point_name=ep_name,
            package=pkg,
            load_time_ms=(time.monotonic() - t0) * 1000,
            errors=[f"ep.load() failed: {exc}"],
        )

    load_ms = (time.monotonic() - t0) * 1000

    # Accept callable that returns the meta dict (lets plugins compute
    # metadata at discovery time if they want).
    if callable(raw) and not isinstance(raw, dict):
        try:
            raw = raw()
        except Exception as exc:  # noqa: BLE001
            return FeaturePluginMeta(
                entry_point_name=ep_name,
                package=pkg,
                load_time_ms=load_ms,
                errors=[f"callable meta provider raised: {exc}"],
            )

    if not isinstance(raw, dict):
        return FeaturePluginMeta(
            entry_point_name=ep_name,
            package=pkg,
            load_time_ms=load_ms,
            errors=[
                f"expected dict[str, dict] from entry-point, got "
                f"{type(raw).__name__}"
            ],
        )

    result = FeaturePluginMeta(
        entry_point_name=ep_name,
        package=pkg,
        meta_dict={},
        load_time_ms=load_ms,
    )

    for feat_name, meta in raw.items():
        if not isinstance(feat_name, str) or not feat_name:
            result.errors.append(
                f"non-string feature key: {feat_name!r}"
            )
            continue
        errors = _validate_feature_meta(feat_name, meta)
        if errors:
            result.errors.extend(errors)
            continue
        result.meta_dict[feat_name] = meta

    return result


def discover_feature_entrypoints(
    *,
    entry_points_override: Any = None,
) -> list[FeaturePluginMeta]:
    """Scan all ``concinno.features`` entry-points and load each.

    Respects :func:`concinno.plugins.is_plugins_enabled` and the
    allowlist env var: when plugins are disabled the function returns
    ``[]``; when an allowlist is set, packages not in it are skipped
    silently (they get no entry at all).

    Args:
        entry_points_override: test hook. Iterable of objects with
            ``name`` / ``value`` / ``dist`` / ``load()`` attributes.
            Production callers pass nothing.
    """
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
                "concinno.features entry-points discovery failed: %s", exc
            )
            return []

    allow = plugin_allowlist()
    results: list[FeaturePluginMeta] = []
    for ep in eps:
        pkg = _extract_package_name(ep)
        if not _is_pkg_allowed(pkg, allow):
            continue
        results.append(_load_single_entrypoint(ep))
    return results


def iter_valid_feature_plugins() -> Iterator[tuple[str, dict[str, Any], str]]:
    """Yield ``(feature_name, meta_dict, origin_package)`` for every
    valid plugin feature.

    Invalid plugins (validation errors or load failures) are silently
    skipped here -- they still appear in
    :func:`discover_feature_entrypoints` for GUI surfacing.
    """
    for plugin in discover_feature_entrypoints():
        if not plugin.valid:
            continue
        for name, meta in plugin.meta_dict.items():
            yield name, meta, plugin.package
