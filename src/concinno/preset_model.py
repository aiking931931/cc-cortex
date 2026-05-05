"""concinno.preset_model — Pydantic ship-time schema for the v4 preset cascade.

@module preset_model
@responsibility Declare the typed shape of ``~/.concinno/preset.json`` and
    the packaged ``data/preset_default.json``. Catches schema drift at ship
    time (hypothesis property-test sampled_from(preset) × FEATURE_KEYS)
    instead of at cascade time.
@dependencies pydantic>=2
@exports PresetModel, PresetsFile, PresetName

Design notes (narrower-scope v4 §1):

* ``name`` is a plain ``str`` — **not** an enum — so the preset layer
  stays aligned with Claude Code's own string-based ``permissionMode``
  convention. Callers can add a ``"staging"`` preset without touching
  the library.
* ``summary`` is a flat ``dict[str, Any]``. Keys use dotted addresses
  (``release_auth.disabled``, ``token_gate.agent_threshold``). The
  cascade router (:mod:`concinno.preset_cascade`) maps each dotted key
  to its target layer (Concinno feature_config, Sancio consumer env
  var, skill flag, hook configuration).
* ``full_ref`` is an optional path string for lazy L3 pull via
  :func:`concinno.field_read.build_field_context`. ``None`` means the
  L2 ``summary`` is self-contained.
* ``index`` is a ``{key: one-line description}`` map consumed by
  ``concinno preset show`` so users see *why* a key exists without
  reading the full spec.

This module is **data-only** — no cascade logic, no I/O. The cascade
engine lives in :mod:`concinno.preset_cascade`.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

PresetName = str  # alias; kept as str per CC permissionMode convention

# Built-in preset names. Extensible — users may define additional names
# in ``~/.concinno/preset.json``. We only guard that names match a
# conservative identifier pattern so typos do not silently create new
# presets.
_NAME_RE_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")


class PresetModel(BaseModel):
    """One preset (e.g. ``benchmark``, ``general``, ``prod``).

    Attributes:
        name: String identifier. Must match the dict key in ``PresetsFile.presets``.
        summary: Flat ``dotted_key -> value`` map cascaded to switches.
        full_ref: Optional path (workspace-relative or absolute) for
            FieldRead lazy-load of the full spec. ``None`` = self-contained.
        index: ``dotted_key -> one_line_description``. Shown by ``preset show``.
    """

    model_config = ConfigDict(extra="forbid", frozen=False)

    name: PresetName = Field(min_length=1, max_length=64)
    summary: dict[str, Any] = Field(default_factory=dict)
    full_ref: str | None = None
    index: dict[str, str] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def _name_charset(cls, v: str) -> str:
        if any(ch not in _NAME_RE_CHARS for ch in v):
            raise ValueError(
                f"preset name {v!r} may only contain ASCII letters, digits, _ and -"
            )
        return v

    @field_validator("summary")
    @classmethod
    def _summary_keys_are_dotted_ascii(cls, v: dict[str, Any]) -> dict[str, Any]:
        for key in v:
            if not isinstance(key, str) or not key:
                raise ValueError("summary keys must be non-empty strings")
            # Allow dotted keys with alphanumerics, dot, underscore, dash.
            for ch in key:
                if ch not in _NAME_RE_CHARS and ch != ".":
                    raise ValueError(
                        f"summary key {key!r} contains invalid character {ch!r}"
                    )
        return v


class PresetsFile(BaseModel):
    """Top-level schema for ``preset.json`` + ``preset_default.json``.

    Attributes:
        version: Schema version integer. Current = 1.
        presets: ``name -> PresetModel``. The model's own ``name`` must
            match its key (validated at parse time).
    """

    model_config = ConfigDict(extra="forbid")

    version: int = Field(default=1, ge=1, le=9999)
    presets: dict[PresetName, PresetModel] = Field(default_factory=dict)

    @field_validator("presets")
    @classmethod
    def _names_match_keys(
        cls, v: dict[str, PresetModel]
    ) -> dict[str, PresetModel]:
        for key, preset in v.items():
            if preset.name != key:
                raise ValueError(
                    f"preset dict key {key!r} does not match model name {preset.name!r}"
                )
        return v


__all__ = [
    "PresetModel",
    "PresetName",
    "PresetsFile",
]
