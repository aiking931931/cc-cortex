"""concinno.plugin_loader -- Discover, load, and validate guard plugins.

@module plugin_loader
@responsibility Dynamic guard discovery from entrypoints and file paths;
    validation against BaseGuard contract; plugin metadata
@dependencies concinno.guards.base (BaseGuard)
@exports discover_plugins, load_guard_file, validate_guard, PluginError,
    PluginMeta

Guard plugins are third-party or user-defined guards that extend the
CC Cortex pipeline without modifying registry.py. Two discovery methods:

1. **Entrypoints** (pip-installed packages):
   ``[project.entry-points."concinno.guards"]``
   ``my_guard = "my_package:MyGuard"``

2. **File paths** (local .py files):
   ``concinno plugins load ./my_guard.py``
   or ``plugins.paths`` in cc_config.json

Loaded guards are validated against BaseGuard before registration.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

from concinno.guards.base import BaseGuard


class PluginError(Exception):
    """Raised when a plugin fails to load or validate."""


@dataclass
class PluginMeta:
    """Metadata about a loaded plugin guard."""

    guard: BaseGuard
    source: str  # "entrypoint", "file", "inline"
    path: str = ""  # file path or entrypoint spec
    load_time_ms: float = 0.0
    errors: list[str] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return len(self.errors) == 0


# ── Validation ──────────────────────────────────────────


def validate_guard(
    guard: object,
    existing_names: Optional[set[str]] = None,
) -> list[str]:
    """Validate a guard instance against the BaseGuard contract.

    Returns a list of error strings. Empty list = valid.
    """
    errors: list[str] = []

    if not isinstance(guard, BaseGuard):
        errors.append(
            f"Not a BaseGuard subclass: {type(guard).__name__}. "
            f"All guards must inherit from concinno.guards.BaseGuard."
        )
        return errors  # Can't check further

    if not guard.name:
        errors.append(
            f"{type(guard).__name__}.name is empty. "
            f"Every guard must have a unique name."
        )

    if existing_names and guard.name in existing_names:
        errors.append(
            f"Duplicate guard name: {guard.name!r}. "
            f"Guard names must be unique across all registered guards."
        )

    # check() must be implemented (not abstract)
    if getattr(guard.check, "__isabstractmethod__", False):
        errors.append(
            f"{type(guard).__name__}.check() is still abstract. "
            f"Guards must implement check()."
        )

    return errors


# ── File Loading ────────────────────────────────────────


def _import_module_from_path(path: str) -> tuple:
    """Import a .py file as a module. Returns (module, load_time_ms)."""
    module_name = f"_concinno_plugin_{os.path.basename(path)[:-3]}"

    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise PluginError(f"Cannot create module spec for: {path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module

    t0 = time.monotonic()
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        del sys.modules[module_name]
        raise PluginError(f"Failed to load {path}: {exc}") from exc

    return module, (time.monotonic() - t0) * 1000


def _extract_guards_from_module(
    module: object, source: str, path: str, load_ms: float,
) -> list[PluginMeta]:
    """Scan a module for concrete BaseGuard subclasses and instantiate them."""
    results: list[PluginMeta] = []
    for attr_name in dir(module):
        obj = getattr(module, attr_name)
        if not (
            isinstance(obj, type)
            and issubclass(obj, BaseGuard)
            and obj is not BaseGuard
            and not getattr(obj.check, "__isabstractmethod__", False)
        ):
            continue

        try:
            instance = obj()
        except Exception as exc:
            results.append(PluginMeta(
                guard=None,  # type: ignore[arg-type]
                source=source, path=path, load_time_ms=load_ms,
                errors=[f"Failed to instantiate {obj.__name__}: {exc}"],
            ))
            continue

        results.append(PluginMeta(
            guard=instance, source=source, path=path,
            load_time_ms=load_ms, errors=validate_guard(instance),
        ))
    return results


def load_guard_file(path: str) -> list[PluginMeta]:
    """Load guard classes from a Python file.

    Scans the module for all concrete BaseGuard subclasses.
    Returns a list of PluginMeta (one per guard found).
    """
    path = os.path.abspath(path)
    if not os.path.isfile(path):
        raise PluginError(f"Plugin file not found: {path}")
    if not path.endswith(".py"):
        raise PluginError(f"Plugin must be a .py file: {path}")

    module, load_ms = _import_module_from_path(path)
    results = _extract_guards_from_module(module, "file", path, load_ms)

    if not results:
        raise PluginError(
            f"No BaseGuard subclasses found in {path}. "
            f"Plugin files must contain at least one concrete BaseGuard subclass."
        )
    return results


# ── Entrypoint Discovery ────────────────────────────────


ENTRYPOINT_GROUP = "concinno.guards"


def _get_entrypoints():
    """Get entrypoints for the concinno.guards group (version-safe)."""
    from importlib.metadata import entry_points

    if sys.version_info >= (3, 12):
        return entry_points(group=ENTRYPOINT_GROUP)

    all_eps = entry_points()
    if isinstance(all_eps, dict):
        return all_eps.get(ENTRYPOINT_GROUP, [])
    return all_eps.select(group=ENTRYPOINT_GROUP)  # type: ignore[union-attr]


def _load_single_entrypoint(ep) -> PluginMeta:
    """Load and validate a single entrypoint into a PluginMeta."""
    ep_path = f"{ep.name}={ep.value}"
    t0 = time.monotonic()

    try:
        cls = ep.load()
    except Exception as exc:
        return PluginMeta(
            guard=None,  # type: ignore[arg-type]
            source="entrypoint", path=ep_path,
            load_time_ms=(time.monotonic() - t0) * 1000,
            errors=[f"Failed to load entrypoint {ep.name}: {exc}"],
        )

    load_ms = (time.monotonic() - t0) * 1000

    # Entrypoint returned an instance directly
    if isinstance(cls, BaseGuard):
        return PluginMeta(
            guard=cls, source="entrypoint", path=ep_path,
            load_time_ms=load_ms, errors=validate_guard(cls),
        )

    # Entrypoint returned a class
    if isinstance(cls, type) and issubclass(cls, BaseGuard):
        try:
            instance = cls()
        except Exception as exc:
            return PluginMeta(
                guard=None,  # type: ignore[arg-type]
                source="entrypoint", path=ep_path, load_time_ms=load_ms,
                errors=[f"Failed to instantiate {cls.__name__}: {exc}"],
            )
        return PluginMeta(
            guard=instance, source="entrypoint", path=ep_path,
            load_time_ms=load_ms, errors=validate_guard(instance),
        )

    return PluginMeta(
        guard=None,  # type: ignore[arg-type]
        source="entrypoint", path=ep_path, load_time_ms=load_ms,
        errors=[
            f"Entrypoint {ep.name} resolved to {type(cls).__name__}, "
            f"expected BaseGuard subclass."
        ],
    )


def discover_entrypoint_plugins() -> list[PluginMeta]:
    """Discover guard plugins from installed packages via entrypoints.

    Packages register guards in pyproject.toml::

        [project.entry-points."concinno.guards"]
        my_guard = "my_package.guards:MyGuard"
    """
    try:
        eps = _get_entrypoints()
    except Exception:
        return []

    return [_load_single_entrypoint(ep) for ep in eps]


# ── Unified Discovery ───────────────────────────────────


def discover_plugins(
    *,
    paths: Optional[list[str]] = None,
    use_entrypoints: bool = True,
    existing_names: Optional[set[str]] = None,
) -> list[PluginMeta]:
    """Discover all guard plugins from all sources.

    Args:
        paths: List of .py file paths to load guards from.
        use_entrypoints: Whether to scan installed package entrypoints.
        existing_names: Set of already-registered guard names (for dedup).

    Returns:
        List of PluginMeta. Check .valid before using .guard.
    """
    results: list[PluginMeta] = []
    seen_names: set[str] = set(existing_names or ())

    # 1. Entrypoints
    if use_entrypoints:
        for meta in discover_entrypoint_plugins():
            if meta.valid and meta.guard is not None:
                name_errors = []
                if meta.guard.name in seen_names:
                    name_errors.append(
                        f"Duplicate guard name: {meta.guard.name!r}"
                    )
                else:
                    seen_names.add(meta.guard.name)
                meta.errors.extend(name_errors)
            results.append(meta)

    # 2. File paths
    for path in (paths or []):
        try:
            file_metas = load_guard_file(path)
        except PluginError as exc:
            results.append(PluginMeta(
                guard=None,  # type: ignore[arg-type]
                source="file",
                path=path,
                errors=[str(exc)],
            ))
            continue

        for meta in file_metas:
            if meta.valid and meta.guard is not None:
                name_errors = []
                if meta.guard.name in seen_names:
                    name_errors.append(
                        f"Duplicate guard name: {meta.guard.name!r}"
                    )
                else:
                    seen_names.add(meta.guard.name)
                meta.errors.extend(name_errors)
            results.append(meta)

    return results
