"""Tests for concinno.feature_config set_feature origin kwarg + list_autotunable."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from concinno.feature_config import (
    FEATURE_META,
    list_autotunable,
    set_feature,
)


@pytest.fixture(autouse=True)
def _isolate_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    import concinno.core.config as _ccfg_mod
    from concinno.core.config import get_config

    hooks_tmp = tmp_path / "hooks"
    hooks_tmp.mkdir(parents=True, exist_ok=True)
    _ccfg_mod._instance = None  # type: ignore[attr-defined]
    get_config(hooks_dir=str(hooks_tmp))
    yield tmp_path
    _ccfg_mod._instance = None  # type: ignore[attr-defined]


class TestSetFeatureOrigin:
    def test_default_origin_manual(self, tmp_path: Path) -> None:
        warnings = set_feature(
            "token_gate", "agent_threshold", 140000
        )
        assert warnings == []
        origins_file = tmp_path / ".concinno" / "preset_origins.json"
        assert origins_file.is_file()
        data = json.loads(origins_file.read_text(encoding="utf-8"))
        assert data["token_gate.agent_threshold"] == ["manual"]

    def test_preset_origin_recorded(self, tmp_path: Path) -> None:
        set_feature(
            "token_gate",
            "agent_threshold",
            140000,
            origin=("preset", "benchmark"),
        )
        origins_file = tmp_path / ".concinno" / "preset_origins.json"
        data = json.loads(origins_file.read_text(encoding="utf-8"))
        assert data["token_gate.agent_threshold"] == [
            "preset", "benchmark",
        ]

    def test_ziq_origin_recorded(self, tmp_path: Path) -> None:
        set_feature(
            "delivery_gate",
            "max_iterations",
            5,
            origin=("ziq", "autotune", "full"),
        )
        origins_file = tmp_path / ".concinno" / "preset_origins.json"
        data = json.loads(origins_file.read_text(encoding="utf-8"))
        assert data["delivery_gate.max_iterations"] == [
            "ziq", "autotune", "full",
        ]

    def test_warning_blocks_write(self, tmp_path: Path) -> None:
        # Value well below min — should warn and not apply (no origin write).
        warnings = set_feature(
            "token_gate", "agent_threshold", 10
        )
        assert warnings
        origins_file = tmp_path / ".concinno" / "preset_origins.json"
        assert not origins_file.is_file()


class TestListAutotunable:
    def test_returns_sorted_list(self) -> None:
        names = list_autotunable()
        assert names == sorted(names)

    def test_every_entry_is_autotunable_and_noncosmetic(self) -> None:
        for name in list_autotunable():
            meta = FEATURE_META[name]
            assert meta.get("ziq_autotunable") is True
            assert meta.get("cosmetic", False) is False

    def test_cosmetic_never_appears(self) -> None:
        names = list_autotunable()
        # session_summary is cosmetic.
        assert "session_summary" not in names
        # handoff_required_guard is safety (non-tunable).
        assert "handoff_required_guard" not in names

    def test_known_tunable_present(self) -> None:
        names = list_autotunable()
        assert "token_gate" in names
        assert "delivery_gate" in names
        assert "consecutive_fail_gate" in names


class TestFeatureMetaFlagsAudit:
    def test_every_feature_has_flags(self) -> None:
        """Every FEATURE_META entry must declare both flags —
        non-declaration is treated as ``None`` which silently disables
        both the loop and the safety contract."""
        for name, meta in FEATURE_META.items():
            assert "ziq_autotunable" in meta, (
                f"feature {name!r} missing ziq_autotunable flag"
            )
            assert "cosmetic" in meta, (
                f"feature {name!r} missing cosmetic flag"
            )

    def test_safety_features_not_autotunable(self) -> None:
        safety = {
            "handoff_required_guard",
            "boundary_guard",
            "publish_scan",
            "identity_guard",
            "ui_verify",
            "read_first_gate",
            "bash_background_gate",
            "python_c_gate",
        }
        for name in safety & set(FEATURE_META.keys()):
            assert (
                FEATURE_META[name].get("ziq_autotunable") is False
            ), f"safety feature {name!r} must not be autotunable"
