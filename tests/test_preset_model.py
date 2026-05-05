"""Tests for concinno.preset_model — Pydantic schema validation.

Covers the narrower-scope v4 ship-time drift catches: schema
violations raise ``ValidationError`` instead of silently corrupting
the cascade at runtime.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from concinno.preset_model import PresetModel, PresetsFile


class TestPresetModel:
    """Single preset schema validation."""

    def test_minimal_preset_valid(self) -> None:
        p = PresetModel(name="benchmark", summary={}, index={})
        assert p.name == "benchmark"
        assert p.summary == {}
        assert p.full_ref is None
        assert p.index == {}

    def test_full_preset_valid(self) -> None:
        p = PresetModel(
            name="general",
            summary={"release_auth.disabled": False, "token_gate.agent_threshold": 140000},
            full_ref="docs/preset-full.md",
            index={
                "release_auth.disabled": "publish gate",
                "token_gate.agent_threshold": "ctx budget",
            },
        )
        assert p.summary["token_gate.agent_threshold"] == 140000
        assert p.full_ref == "docs/preset-full.md"

    def test_invalid_name_spaces(self) -> None:
        with pytest.raises(ValidationError):
            PresetModel(name="bad name", summary={}, index={})

    def test_invalid_name_unicode(self) -> None:
        with pytest.raises(ValidationError):
            PresetModel(name="保密", summary={}, index={})

    def test_invalid_name_empty(self) -> None:
        with pytest.raises(ValidationError):
            PresetModel(name="", summary={}, index={})

    def test_invalid_name_too_long(self) -> None:
        with pytest.raises(ValidationError):
            PresetModel(name="a" * 65, summary={}, index={})

    def test_summary_key_invalid_char(self) -> None:
        with pytest.raises(ValidationError):
            PresetModel(name="x", summary={"bad key": True}, index={})

    def test_summary_key_empty(self) -> None:
        with pytest.raises(ValidationError):
            PresetModel(name="x", summary={"": True}, index={})

    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            PresetModel(name="x", summary={}, index={}, bogus_field=42)


class TestPresetsFile:
    """Top-level schema validation + key/name consistency."""

    def test_empty_file_valid(self) -> None:
        pf = PresetsFile()
        assert pf.version == 1
        assert pf.presets == {}

    def test_two_presets_valid(self) -> None:
        pf = PresetsFile(
            version=1,
            presets={
                "a": PresetModel(name="a", summary={}, index={}),
                "b": PresetModel(name="b", summary={}, index={}),
            },
        )
        assert set(pf.presets.keys()) == {"a", "b"}

    def test_key_name_mismatch_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PresetsFile(
                presets={"benchmark": PresetModel(name="general", summary={}, index={})},
            )

    def test_version_lower_bound(self) -> None:
        with pytest.raises(ValidationError):
            PresetsFile(version=0, presets={})

    def test_version_upper_bound(self) -> None:
        with pytest.raises(ValidationError):
            PresetsFile(version=100_000, presets={})

    def test_round_trip_dump_load(self) -> None:
        original = PresetsFile(
            version=1,
            presets={
                "benchmark": PresetModel(
                    name="benchmark",
                    summary={"k.v": 1},
                    index={"k.v": "d"},
                ),
            },
        )
        dumped = original.model_dump()
        reloaded = PresetsFile.model_validate(dumped)
        assert reloaded == original
