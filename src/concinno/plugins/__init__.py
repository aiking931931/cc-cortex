"""concinno.plugins -- Entry-points plugin layer for features + skills.

Added in 2.31.0. Two new entry-points groups let third-party
``concinno-skills-*`` packages declare switchable **features** and
bundle **skill manifests** without asking the user to run CLI
registration commands post-install.

Groups registered by this module:

* ``concinno.features`` -- value resolves to a ``dict[str, dict]``
  keyed by feature name, each value shaped like a ``FEATURE_META``
  entry. Schema validated per 2.31.0 spec v2 amendment A3
  (accept-unknown, reject only on malformed core fields).

* ``concinno.skills`` -- value resolves to a directory path (``str``
  or ``Path``) containing ``SKILL.md`` files. Each SKILL.md is
  discovered via the existing :mod:`concinno.skill_parser` pipeline
  and merged into the catalogue exposed by :func:`_discover_skills`.

Master kill-switch: ``plugins_enabled`` FEATURE_META entry + env var
``CONCINNO_PLUGINS_ENABLED=0``. Optional allowlist env var
``CONCINNO_PLUGINS_ALLOWLIST=pkg-a,pkg-b`` restricts which installed
packages get ``ep.load()`` called (default: allow all, mirroring
pytest/flask/mkdocs ecosystem convention).
"""
from __future__ import annotations

import os
from typing import Iterable

from concinno.plugins.features import (
    FeaturePluginMeta,
    discover_feature_entrypoints,
    iter_valid_feature_plugins,
)
from concinno.plugins.skills import (
    SkillPluginMeta,
    discover_skill_entrypoints,
    iter_plugin_skill_roots,
)

__all__ = [
    "FeaturePluginMeta",
    "SkillPluginMeta",
    "discover_feature_entrypoints",
    "discover_skill_entrypoints",
    "is_plugins_enabled",
    "iter_plugin_skill_roots",
    "iter_valid_feature_plugins",
    "plugin_allowlist",
]


def is_plugins_enabled() -> bool:
    """Return True when the plugin discovery layer is active.

    Env var ``CONCINNO_PLUGINS_ENABLED=0`` forces off.
    FEATURE_META ``plugins_enabled.enabled=False`` forces off.
    Default: on.

    Fail-open: any error reading config returns True (safe default is
    to respect the ecosystem convention). A user explicitly setting
    the env var or config gets a silent-failure-free disable.
    """
    env = os.environ.get("CONCINNO_PLUGINS_ENABLED", "").strip().lower()
    if env in ("0", "false", "no", "off"):
        return False
    # Check FEATURE_META override — lazy import to avoid circular dep
    # when feature_config imports plugins (it does not today, but
    # future-proof the order).
    try:
        from concinno.core.config import get_config

        cfg = get_config()
        feat = cfg.feature_all("plugins_enabled") or {}
        if feat.get("enabled") is False:
            return False
    except Exception:
        pass
    return True


def plugin_allowlist() -> frozenset[str] | None:
    """Return the set of allowed plugin package names, or None to
    allow all.

    Sourced from ``CONCINNO_PLUGINS_ALLOWLIST`` env var
    (comma-separated). Unset or empty -> allow all.
    """
    raw = os.environ.get("CONCINNO_PLUGINS_ALLOWLIST", "").strip()
    if not raw:
        return None
    items = {item.strip() for item in raw.split(",") if item.strip()}
    if not items:
        return None
    return frozenset(items)


def _is_pkg_allowed(pkg_name: str, allowlist: Iterable[str] | None) -> bool:
    """Check whether ``pkg_name`` is in the allowlist.

    Supports the package name in both underscore and dash forms
    (``concinno_skills_google`` vs ``concinno-skills-google``) so the
    allowlist is robust to PyPI-normalised names.
    """
    if allowlist is None:
        return True
    variants = {pkg_name, pkg_name.replace("_", "-"), pkg_name.replace("-", "_")}
    return bool(set(allowlist) & variants)
