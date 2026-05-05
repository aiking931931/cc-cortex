"""concinno.preset_cascade — v4 preset cascade + effective-value router.

@module preset_cascade
@responsibility Implement the four-layer cascade triggered by
    :func:`set_preset`: (1) Concinno library feature_config, (2) Sancio
    (and any other) consumer via ``concinno.preset_consumers``
    entry_points, (3) skill-level flags read lazily, (4) hook
    configurations. Also provide :func:`get_effective_value` — the
    ``user_pinned > ZIQ_runtime > preset > FEATURE_META default``
    resolution chain from narrower-scope v4.
@dependencies
    concinno.preset_model, concinno.feature_config,
    concinno.coordination._os_lock (atomic write), stdlib only otherwise
@exports
    load_presets_file, get_active_preset, set_preset, get_effective_value,
    CascadeReport, list_presets

Design notes (narrower-scope v4 §2):

* **Discovery priority** (later overrides earlier):

  1. Packaged default: ``concinno/data/preset_default.json``
  2. User global:      ``~/.concinno/preset.json``
  3. Project-local:    ``<cwd or git root>/.claude/concinno-preset.json``

* **Active preset persistence**: ``~/.concinno/active_preset.txt``
  — a single line containing the preset name. Default ``"general"``.

* **Atomic writes**: every mutation (``set_preset_value``,
  ``set_preset``) uses :class:`concinno.coordination._os_lock.OSFileLock`
  around a tmp-file + ``os.replace`` atomic rename so concurrent CLI
  invocations do not corrupt state.

* **Fail-soft consumer dispatch**: a broken ``apply_preset(name)`` hook
  raises → isolated in try/except → logged in
  ``CascadeReport.errors`` → cascade continues for remaining consumers.

* **effective_value router**: ``(value, origin_tuple)`` — origin is
  the audit breadcrumb used by ``concinno preset show`` so users see
  *why* a key has its current value. Chain order mirrors the v4 doc:
  user_pinned > ziq > preset > FEATURE_META default.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

from .coordination._os_lock import LockAcquireTimeout, OSFileLock
from .preset_model import PresetModel, PresetsFile

_LOG = logging.getLogger("concinno.preset_cascade")

# ── Paths ───────────────────────────────────────────────────


def _user_concinno_dir() -> Path:
    """Return ``~/.concinno`` (never touches the workspace)."""
    return Path.home() / ".concinno"


def _user_preset_path() -> Path:
    return _user_concinno_dir() / "preset.json"


def _active_preset_path() -> Path:
    return _user_concinno_dir() / "active_preset.txt"


def _lock_path(target: Path) -> Path:
    """Sidecar lockfile path for atomic writes."""
    return target.with_suffix(target.suffix + ".lock")


def _project_preset_path() -> Path | None:
    """Project-local override at ``<cwd-or-git-root>/.claude/concinno-preset.json``.

    Falls back to cwd if git metadata cannot be found.
    """
    cwd = Path.cwd()
    # Walk up looking for a .git dir/file; stop at filesystem root.
    cur = cwd.resolve()
    while True:
        if (cur / ".git").exists():
            return cur / ".claude" / "concinno-preset.json"
        if cur.parent == cur:
            break
        cur = cur.parent
    return cwd / ".claude" / "concinno-preset.json"


def _packaged_default_path() -> Path:
    """Return path to the bundled ``preset_default.json``.

    Uses ``importlib.resources`` so the file is found both from a wheel
    install and an editable checkout.
    """
    try:
        with resources.as_file(
            resources.files("concinno.data") / "preset_default.json"
        ) as p:
            return Path(p)
    except (ModuleNotFoundError, FileNotFoundError):
        # Fallback for direct-source-layout execution (tests etc.).
        return Path(__file__).parent / "data" / "preset_default.json"


# ── Loading / discovery ────────────────────────────────────


def _safe_parse(path: Path) -> PresetsFile | None:
    """Parse a preset.json file. Returns ``None`` on any I/O or schema error."""
    if not path.is_file():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        _LOG.warning("preset file %s has invalid JSON: %s", path, exc)
        return None
    try:
        return PresetsFile.model_validate(data)
    except Exception as exc:  # pydantic ValidationError + defensive catch-all
        _LOG.warning("preset file %s failed schema validation: %s", path, exc)
        return None


def load_presets_file() -> PresetsFile:
    """Discover + merge preset files in documented priority order.

    Later layers override earlier. Missing / invalid files are skipped
    silently — the packaged default is always available as a fallback.
    """
    layers: list[PresetsFile] = []
    default = _safe_parse(_packaged_default_path())
    if default is not None:
        layers.append(default)

    user = _safe_parse(_user_preset_path())
    if user is not None:
        layers.append(user)

    project_path = _project_preset_path()
    if project_path is not None:
        project = _safe_parse(project_path)
        if project is not None:
            layers.append(project)

    if not layers:
        # Empty fallback — allows library to keep running in exotic envs
        # where even the packaged data wasn't shipped (e.g. partial sdist).
        return PresetsFile(version=1, presets={})

    merged: dict[str, PresetModel] = {}
    for layer in layers:
        merged.update(layer.presets)
    return PresetsFile(version=layers[-1].version, presets=merged)


def list_presets() -> list[str]:
    """Return sorted names of all discovered presets."""
    return sorted(load_presets_file().presets.keys())


def get_active_preset() -> str:
    """Read the active preset name from ``~/.concinno/active_preset.txt``.

    Defaults to ``"general"`` when the file is missing, empty, or names a
    preset that doesn't exist in the current discovery set (so a stale
    entry does not crash SessionStart).
    """
    default = "general"
    path = _active_preset_path()
    try:
        if path.is_file():
            raw = path.read_text(encoding="utf-8").strip()
            if raw:
                available = list_presets()
                if raw in available:
                    return raw
                if available:
                    return default if default in available else available[0]
                return default
    except OSError:
        pass
    return default


# ── Atomic write helper ────────────────────────────────────


def _atomic_write_json(target: Path, data: dict[str, Any]) -> None:
    """Lock-protected atomic write of ``data`` as JSON to ``target``.

    Uses :class:`OSFileLock` to serialize concurrent CLI invocations,
    writes to a tmp file in the same directory, then ``os.replace()``
    to the final path so partially written files never appear.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    lock = OSFileLock(str(_lock_path(target)), timeout=5.0)
    with lock:
        fd, tmp_path = tempfile.mkstemp(
            prefix=target.name + ".", suffix=".tmp", dir=str(target.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, ensure_ascii=False)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, target)
        except Exception:
            # Clean up tmp on failure.
            try:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
            except OSError:
                pass
            raise


def _atomic_write_text(target: Path, text: str) -> None:
    """Lock-protected atomic text write (for active_preset.txt)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    lock = OSFileLock(str(_lock_path(target)), timeout=5.0)
    with lock:
        fd, tmp_path = tempfile.mkstemp(
            prefix=target.name + ".", suffix=".tmp", dir=str(target.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, target)
        except Exception:
            try:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
            except OSError:
                pass
            raise


# ── Cascade ────────────────────────────────────────────────

_CONSUMER_GROUP = "concinno.preset_consumers"


@dataclass
class CascadeReport:
    """Structured outcome of a :func:`set_preset` call."""

    preset_name: str
    fields_changed: list[tuple[str, Any, Any]] = field(default_factory=list)
    consumers_called: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "preset_name": self.preset_name,
            "fields_changed": [
                {"key": k, "old": o, "new": n} for k, o, n in self.fields_changed
            ],
            "consumers_called": list(self.consumers_called),
            "errors": list(self.errors),
        }


def _split_dotted(key: str) -> tuple[str, str]:
    """Split a ``feature.param`` dotted key into ``(feature, param)``.

    Keys without a dot map to ``(feature, 'enabled')``.
    """
    if "." not in key:
        return key, "enabled"
    feature, _, param = key.partition(".")
    return feature, param


def _apply_to_feature_config(
    preset: PresetModel, report: CascadeReport
) -> None:
    """Push every dotted ``feature.param`` key into
    :mod:`concinno.core.config` with origin tuple ``('preset', name)``.

    Keys that do not correspond to a registered FEATURE_META entry are
    silently skipped — callers may use preset keys for consumer-only
    signals (e.g. ``sancio.require_auth``) that don't live in Concinno's
    FEATURE_META.
    """
    try:
        from concinno.core.config import get_config
        from concinno.feature_config import FEATURE_META
    except Exception as exc:  # pragma: no cover — import defense
        report.errors.append(f"feature_config import failed: {exc}")
        return

    cfg = get_config()
    for dotted_key, new_value in preset.summary.items():
        feature, param = _split_dotted(dotted_key)
        if feature not in FEATURE_META:
            # Not a library-owned feature; consumers handle these.
            continue
        try:
            old = cfg.feature(feature, param)
        except Exception:
            old = None
        if old == new_value:
            continue
        try:
            cfg.set_feature(feature, param, new_value)
            # Record origin sidecar (see set_feature_with_origin).
            _record_origin(feature, param, ("preset", preset.name))
            report.fields_changed.append((dotted_key, old, new_value))
        except Exception as exc:
            report.errors.append(
                f"feature_config write failed for {dotted_key}: {exc}"
            )


def _dispatch_consumers(preset_name: str, report: CascadeReport) -> None:
    """Call every registered ``concinno.preset_consumers`` entry_point.

    Broken consumers are logged as errors but never abort the cascade.
    """
    try:
        from importlib.metadata import entry_points

        try:
            eps = entry_points(group=_CONSUMER_GROUP)
        except TypeError:
            # Python 3.9 compat — group= kwarg unsupported in some builds.
            eps = entry_points().get(_CONSUMER_GROUP, [])  # type: ignore[assignment]
    except Exception as exc:
        report.errors.append(f"entry_points discovery failed: {exc}")
        return

    for ep in eps:
        name = getattr(ep, "name", "<anon>")
        try:
            fn = ep.load()
        except Exception as exc:
            report.errors.append(f"consumer {name!r} failed to load: {exc}")
            continue
        try:
            fn(preset_name)
            report.consumers_called.append(name)
        except Exception as exc:
            report.errors.append(f"consumer {name!r} raised: {exc}")


def set_preset(name: str, *, cascade: bool = True) -> CascadeReport:
    """Switch the active preset and cascade its summary to every consumer.

    Args:
        name: Preset name. Must exist in the merged discovery set.
        cascade: When True (default), walk the four layers. False = only
            persist the active preset name; useful for tests.

    Returns:
        :class:`CascadeReport` — fields changed, consumers called, errors.
    """
    presets = load_presets_file()
    if name not in presets.presets:
        avail = ", ".join(sorted(presets.presets.keys()))
        raise KeyError(
            f"unknown preset {name!r}. Available: {avail or '(none)'}"
        )

    report = CascadeReport(preset_name=name)
    preset = presets.presets[name]

    # Persist active_preset.txt FIRST — if cascade partially fails the
    # user still sees the new name via get_active_preset() and can re-run.
    try:
        _atomic_write_text(_active_preset_path(), name + "\n")
    except (OSError, LockAcquireTimeout) as exc:
        report.errors.append(f"active_preset.txt write failed: {exc}")

    if cascade:
        _apply_to_feature_config(preset, report)
        _dispatch_consumers(name, report)

    return report


# ── Effective-value router ─────────────────────────────────


def _origin_sidecar_path() -> Path:
    """Path to the origin-tracking sidecar file.

    Stored separately from the main config so feature_config's JSON
    layout stays unchanged for backwards compatibility.
    """
    return _user_concinno_dir() / "preset_origins.json"


def _load_origins() -> dict[str, list[str]]:
    path = _origin_sidecar_path()
    if not path.is_file():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        if isinstance(data, dict):
            return {
                k: list(v) if isinstance(v, list) else [str(v)]
                for k, v in data.items()
            }
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def _record_origin(feature: str, param: str, origin: tuple[str, ...]) -> None:
    """Record ``origin`` for ``feature.param`` in the sidecar file."""
    try:
        origins = _load_origins()
        origins[f"{feature}.{param}"] = list(origin)
        _atomic_write_json(_origin_sidecar_path(), origins)
    except (OSError, LockAcquireTimeout) as exc:
        _LOG.warning("preset origin sidecar write failed: %s", exc)


def _default_from_meta(feature: str, param: str) -> Any:
    """Pull the FEATURE_META default for ``feature.param``."""
    try:
        from concinno.feature_config import FEATURE_META
    except Exception:
        return None
    meta = FEATURE_META.get(feature, {})
    if param == "enabled":
        return meta.get("enabled", True)
    params = meta.get("params", {})
    p = params.get(param, {})
    return p.get("default")


def _user_pinned(feature: str, param: str) -> tuple[bool, Any]:
    """Return ``(is_pinned, value)`` from ``~/.concinno/user_pinned.json``.

    Structure: ``{"feature.param": {"value": X}}``. Missing file = not pinned.
    """
    path = _user_concinno_dir() / "user_pinned.json"
    if not path.is_file():
        return False, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        entry = data.get(f"{feature}.{param}")
        if isinstance(entry, dict) and "value" in entry:
            return True, entry["value"]
    except (OSError, json.JSONDecodeError):
        pass
    return False, None


def _ziq_suggestion(
    feature: str, param: str
) -> tuple[bool, Any]:
    """Ask ZIQAutoTuner for a current suggestion for ``feature.param``.

    Returns ``(has_suggestion, value)``. The tuner's ``current_regime()``
    returning ``"preset"`` = still in cold-start; we treat that as no ZIQ
    suggestion yet (let the preset layer handle the value).
    """
    try:
        from concinno.ziq_autotune_registry import (
            TUNABLE_REGISTRY,
            get_tuner,
        )
    except Exception:
        return False, None
    dotted = f"{feature}.{param}"
    if dotted not in TUNABLE_REGISTRY:
        return False, None
    try:
        tuner = get_tuner(dotted)
    except Exception:
        return False, None
    if tuner.current_regime() == "preset":
        return False, None
    try:
        return True, tuner.suggest()
    except Exception:
        return False, None


def get_effective_value(key: str) -> tuple[Any, tuple[str, ...]]:
    """Return ``(value, origin)`` for a dotted ``feature.param`` key.

    Resolution chain (first match wins):

    1. ``user_pinned > ziq_runtime > preset > FEATURE_META default``

    Origin tuple examples:
        ``("user_pinned",)``
        ``("ziq", "autotune", "full")``
        ``("preset", "benchmark")``
        ``("feature_meta_default",)``
    """
    feature, param = _split_dotted(key)

    # 1. User pinned
    pinned, val = _user_pinned(feature, param)
    if pinned:
        return val, ("user_pinned",)

    # 2. ZIQ runtime
    has_sugg, val = _ziq_suggestion(feature, param)
    if has_sugg:
        try:
            from concinno.ziq_autotune_registry import get_tuner

            regime = get_tuner(key).current_regime()
        except Exception:
            regime = "unknown"
        return val, ("ziq", "autotune", regime)

    # 3. Active preset summary
    try:
        presets = load_presets_file()
        active = get_active_preset()
        if active in presets.presets:
            summary = presets.presets[active].summary
            if key in summary:
                return summary[key], ("preset", active)
    except Exception:
        pass

    # 4. FEATURE_META default
    return _default_from_meta(feature, param), ("feature_meta_default",)


# ── Helpers for ziq_autotune_loop write-back ──────────────


def set_preset_value(
    preset_name: str,
    key: str,
    new_value: Any,
    *,
    origin: tuple[str, ...] = ("manual",),
) -> bool:
    """Write ``key -> new_value`` into the named preset's summary.

    Merges on top of the user-level preset.json (creating it from the
    merged discovery view if absent). Leaves project-local overlays
    untouched so ZIQ-learned defaults do not invade repo-local preset
    overrides.

    Returns True on successful write, False if the preset is unknown.
    """
    presets_all = load_presets_file()
    if preset_name not in presets_all.presets:
        return False

    # Read existing user file (or synthesize empty).
    user_path = _user_preset_path()
    user_file = _safe_parse(user_path)
    if user_file is None:
        user_file = PresetsFile(version=1, presets={})

    base = user_file.presets.get(preset_name)
    if base is None:
        # Seed from merged view so untouched keys survive rewriting.
        merged = presets_all.presets[preset_name]
        base = PresetModel(
            name=merged.name,
            summary=dict(merged.summary),
            full_ref=merged.full_ref,
            index=dict(merged.index),
        )

    new_summary = dict(base.summary)
    new_summary[key] = new_value
    new_preset = PresetModel(
        name=base.name,
        summary=new_summary,
        full_ref=base.full_ref,
        index=dict(base.index),
    )

    new_user = PresetsFile(
        version=user_file.version,
        presets={**user_file.presets, preset_name: new_preset},
    )

    try:
        _atomic_write_json(
            user_path,
            json.loads(new_user.model_dump_json()),
        )
    except (OSError, LockAcquireTimeout) as exc:
        _LOG.warning("set_preset_value failed to write %s: %s", user_path, exc)
        return False

    # Record origin for this specific key.
    feature, param = _split_dotted(key)
    _record_origin(feature, param, origin)
    return True


__all__ = [
    "CascadeReport",
    "get_active_preset",
    "get_effective_value",
    "list_presets",
    "load_presets_file",
    "set_preset",
    "set_preset_value",
]


# Quiet unused-import warnings in environments where sys is stripped by
# type stubs — we intentionally import it for future stderr logging.
_ = sys
