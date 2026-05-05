"""Tests for concinno.preset_cascade — cascade + effective_value router.

Isolates HOME via ``tmp_path`` so no test touches the real
``~/.concinno/`` directory, and every test starts from a clean state.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from concinno.preset_cascade import (
    CascadeReport,
    _packaged_default_path,
    _safe_parse,
    get_active_preset,
    get_effective_value,
    list_presets,
    load_presets_file,
    set_preset,
    set_preset_value,
)
from concinno.preset_model import PresetModel, PresetsFile


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect every ``Path.home()`` call in preset_cascade to ``tmp_path``.

    Also resets the concinno.core.config singleton AND forces every
    test to use a tmp hooks dir, so cascade writes never touch the
    real ``cc_config.json`` and tests do not cross-contaminate through
    persisted feature-config state.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Windows alias
    # pathlib.Path.home() reads USERPROFILE on Windows / HOME on POSIX. Both set.
    monkeypatch.chdir(tmp_path)
    import concinno.core.config as _ccfg_mod
    from concinno.core.config import get_config

    hooks_tmp = tmp_path / "hooks"
    hooks_tmp.mkdir(parents=True, exist_ok=True)
    _ccfg_mod._instance = None  # type: ignore[attr-defined]
    get_config(hooks_dir=str(hooks_tmp))
    yield tmp_path
    _ccfg_mod._instance = None  # type: ignore[attr-defined]


class TestLoadPresetsFile:
    """Multi-layer discovery priority."""

    def test_packaged_default_exists(self) -> None:
        assert _packaged_default_path().is_file()

    def test_packaged_default_parses(self) -> None:
        f = _safe_parse(_packaged_default_path())
        assert f is not None
        assert "benchmark" in f.presets
        assert "general" in f.presets

    def test_default_when_no_user_or_project(self) -> None:
        pf = load_presets_file()
        assert "benchmark" in pf.presets
        assert "general" in pf.presets
        assert "prod" in pf.presets

    def test_user_override_takes_priority(
        self, tmp_path: Path
    ) -> None:
        user_cfg_dir = tmp_path / ".concinno"
        user_cfg_dir.mkdir(parents=True, exist_ok=True)
        user_preset = PresetsFile(
            version=1,
            presets={
                "benchmark": PresetModel(
                    name="benchmark",
                    summary={"custom.override": True},
                    index={"custom.override": "user override"},
                ),
            },
        )
        (user_cfg_dir / "preset.json").write_text(
            user_preset.model_dump_json(), encoding="utf-8"
        )
        merged = load_presets_file()
        assert merged.presets["benchmark"].summary == {"custom.override": True}

    def test_malformed_user_file_ignored(
        self, tmp_path: Path
    ) -> None:
        user_cfg_dir = tmp_path / ".concinno"
        user_cfg_dir.mkdir(parents=True, exist_ok=True)
        (user_cfg_dir / "preset.json").write_text(
            "{not valid json", encoding="utf-8"
        )
        merged = load_presets_file()
        # Packaged default still present.
        assert "benchmark" in merged.presets


class TestGetActivePreset:
    def test_default_general(self) -> None:
        assert get_active_preset() == "general"

    def test_reads_active_file(self, tmp_path: Path) -> None:
        (tmp_path / ".concinno").mkdir(parents=True, exist_ok=True)
        (tmp_path / ".concinno" / "active_preset.txt").write_text(
            "benchmark\n", encoding="utf-8"
        )
        assert get_active_preset() == "benchmark"

    def test_stale_name_falls_back_to_default(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / ".concinno").mkdir(parents=True, exist_ok=True)
        (tmp_path / ".concinno" / "active_preset.txt").write_text(
            "nonexistent\n", encoding="utf-8"
        )
        assert get_active_preset() == "general"


class TestSetPreset:
    def test_switch_updates_active_file(self, tmp_path: Path) -> None:
        report = set_preset("benchmark")
        assert isinstance(report, CascadeReport)
        assert report.preset_name == "benchmark"
        assert get_active_preset() == "benchmark"

    def test_unknown_raises_keyerror(self) -> None:
        with pytest.raises(KeyError):
            set_preset("nonexistent")

    def test_cascade_false_skips_feature_config(
        self, tmp_path: Path
    ) -> None:
        report = set_preset("benchmark", cascade=False)
        assert report.fields_changed == []
        assert report.consumers_called == []

    def test_cascade_true_touches_feature_config(
        self,
        tmp_path: Path,
    ) -> None:
        # _isolate_home fixture already bound Config to tmp hooks_dir.
        report = set_preset("benchmark", cascade=True)
        # token_gate.agent_threshold is in FEATURE_META and benchmark
        # summary (160000) differs from the default (140000) — must
        # appear in fields_changed.
        changed_keys = {k for k, _, _ in report.fields_changed}
        assert "token_gate.agent_threshold" in changed_keys

    def test_cascade_report_dict_serializes(self) -> None:
        report = set_preset("benchmark", cascade=False)
        d = report.as_dict()
        assert d["preset_name"] == "benchmark"
        assert isinstance(d["fields_changed"], list)
        assert isinstance(d["consumers_called"], list)
        assert isinstance(d["errors"], list)


class TestGetEffectiveValue:
    def test_preset_wins_over_default(self, tmp_path: Path) -> None:
        set_preset("benchmark")
        value, origin = get_effective_value("token_gate.agent_threshold")
        assert value == 160000
        assert origin == ("preset", "benchmark")

    def test_unknown_key_returns_none_default(self) -> None:
        value, origin = get_effective_value("nonexistent.feature")
        assert value is None
        assert origin == ("feature_meta_default",)

    def test_user_pinned_wins(self, tmp_path: Path) -> None:
        set_preset("benchmark")
        (tmp_path / ".concinno").mkdir(parents=True, exist_ok=True)
        (tmp_path / ".concinno" / "user_pinned.json").write_text(
            json.dumps(
                {"token_gate.agent_threshold": {"value": 999_999}}
            ),
            encoding="utf-8",
        )
        value, origin = get_effective_value("token_gate.agent_threshold")
        assert value == 999_999
        assert origin == ("user_pinned",)


class TestSetPresetValue:
    def test_write_back_creates_user_file(self, tmp_path: Path) -> None:
        ok = set_preset_value("benchmark", "custom.key", 42)
        assert ok
        user_file = tmp_path / ".concinno" / "preset.json"
        assert user_file.is_file()
        data = json.loads(user_file.read_text(encoding="utf-8"))
        assert (
            data["presets"]["benchmark"]["summary"]["custom.key"] == 42
        )

    def test_write_back_unknown_preset_returns_false(self) -> None:
        ok = set_preset_value("nonexistent", "k", 1)
        assert ok is False

    def test_origin_recorded(self, tmp_path: Path) -> None:
        set_preset_value(
            "benchmark",
            "token_gate.agent_threshold",
            150_000,
            origin=("ziq", "autotune", "full"),
        )
        origins_file = tmp_path / ".concinno" / "preset_origins.json"
        assert origins_file.is_file()
        data = json.loads(origins_file.read_text(encoding="utf-8"))
        assert data["token_gate.agent_threshold"] == [
            "ziq", "autotune", "full",
        ]


class TestListPresets:
    def test_returns_sorted(self) -> None:
        names = list_presets()
        assert names == sorted(names)
        assert "benchmark" in names
        assert "general" in names
