"""Packaged convention presets for concinno.

Each ``<name>.json`` file here is a full convention config that can be
loaded via :func:`concinno.convention_engine.load_preset` or seeded into
a workspace via ``concinno convention init --preset=<name>``.

Presets shipped:

``aiking``
    The conventions used by the AI King personal workspace. Example
    reference: 06_Handoffs / 07_Patents / memory / feedback naming.

``minimal``
    Just the naming hygiene rules (patents, handoffs, feedback) with
    no directory placement opinions. Safe default for someone who
    wants the engine's naming checks without committing to a specific
    workspace layout.
"""

from __future__ import annotations

import os
from typing import Iterator

PRESETS_DIR = os.path.dirname(os.path.abspath(__file__))


def list_presets() -> list[str]:
    """Return the available preset names (without extension)."""
    names: list[str] = []
    if not os.path.isdir(PRESETS_DIR):
        return names
    for entry in os.listdir(PRESETS_DIR):
        if entry.endswith(".json") and not entry.startswith("__"):
            names.append(entry[:-5])
    return sorted(names)


def preset_path(name: str) -> str:
    """Return the absolute path to ``<name>.json`` or '' if missing."""
    candidate = os.path.join(PRESETS_DIR, f"{name}.json")
    return candidate if os.path.isfile(candidate) else ""


def iter_preset_paths() -> Iterator[tuple[str, str]]:
    """Yield ``(name, absolute_path)`` for every shipped preset."""
    for name in list_presets():
        yield name, preset_path(name)
