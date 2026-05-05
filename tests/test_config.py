"""Tests for concinno.core.config — Config singleton, update_file, config_file_path."""

from __future__ import annotations

import json

import pytest

from concinno.core.config import (
    Config,
    _coerce_env_value,
    _env_feature_override,
    reset_config,
)


@pytest.fixture(autouse=True)
def _reset():
    """Ensure each test starts with a fresh singleton."""
    reset_config()
    yield
    reset_config()


class TestConfigFilePath:
    def test_returns_hooks_dir_based_path(self):
        cfg = Config(hooks_dir="/fake/hooks")
        assert cfg.config_file_path.endswith("cc_config.json")
        assert "/fake/hooks" in cfg.config_file_path.replace("\\", "/")

    def test_returns_explicit_path(self):
        cfg = Config(config_path="/custom/path.json")
        assert cfg.config_file_path == "/custom/path.json"

    def test_empty_when_no_dir(self):
        cfg = Config()
        assert cfg.config_file_path == ""


class TestUpdateFile:
    def test_creates_and_updates(self, tmp_path):
        cfg_path = str(tmp_path / "cc_config.json")
        cfg = Config(config_path=cfg_path)

        cfg.update_file("locale", "zh-TW")
        with open(cfg_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert data["locale"] == "zh-TW"

        # Verify cache invalidated — next read picks up the new value
        assert cfg.raw("locale") == "zh-TW"

    def test_merges_with_existing(self, tmp_path):
        cfg_path = str(tmp_path / "cc_config.json")
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump({"existing": True}, f)

        cfg = Config(config_path=cfg_path)
        cfg.update_file("modules", {"foo": True})

        with open(cfg_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert data["existing"] is True
        assert data["modules"] == {"foo": True}

    def test_noop_when_no_path(self):
        cfg = Config()
        cfg.update_file("key", "val")  # should not raise


class TestCoerceEnvValue:
    """Unit tests for _coerce_env_value — type coercion of env var strings."""

    def test_bool_true_variants(self):
        for raw in ("1", "true", "True", "TRUE", "yes", "YES", "on", "ON"):
            assert _coerce_env_value(raw, "enabled") is True, raw

    def test_bool_false_variants(self):
        for raw in ("0", "false", "False", "FALSE", "no", "NO", "off", "OFF"):
            assert _coerce_env_value(raw, "enabled") is False, raw

    def test_bool_malformed_returns_none(self):
        assert _coerce_env_value("maybe", "enabled") is None

    def test_int_coercion(self):
        assert _coerce_env_value("42", "max_spawns") == 42
        assert isinstance(_coerce_env_value("42", "max_spawns"), int)

    def test_float_coercion(self):
        result = _coerce_env_value("3.14", "threshold")
        assert abs(result - 3.14) < 1e-9

    def test_string_fallback(self):
        assert _coerce_env_value("step_back_first", "mode") == "step_back_first"

    def test_enabled_suffix_key_treated_as_bool(self):
        assert _coerce_env_value("1", "feature_enabled") is True
        assert _coerce_env_value("0", "feature_enabled") is False


class TestEnvFeatureOverride:
    """Tests for _env_feature_override — env var lookup + coercion."""

    def test_returns_none_when_absent(self, monkeypatch):
        monkeypatch.delenv("CONCINNO_AGENT_CAP_ENABLED", raising=False)
        assert _env_feature_override("agent_cap", "enabled") is None

    def test_bool_override(self, monkeypatch):
        monkeypatch.setenv("CONCINNO_AGENT_CAP_ENABLED", "false")
        assert _env_feature_override("agent_cap", "enabled") is False

    def test_int_override(self, monkeypatch):
        monkeypatch.setenv("CONCINNO_AGENT_CAP_MAX_SPAWNS", "8")
        assert _env_feature_override("agent_cap", "max_spawns") == 8

    def test_env_naming_upper_snake(self, monkeypatch):
        # CONCINNO_TOKEN_GATE_AGENT_THRESHOLD should resolve correctly
        monkeypatch.setenv("CONCINNO_TOKEN_GATE_AGENT_THRESHOLD", "100000")
        assert _env_feature_override("token_gate", "agent_threshold") == 100000

    def test_consecutive_fail_gate(self, monkeypatch):
        monkeypatch.setenv("CONCINNO_CONSECUTIVE_FAIL_GATE_MAX_FAILS", "2")
        assert _env_feature_override("consecutive_fail_gate", "max_fails") == 2


class TestFeatureEnvOverride:
    """Integration: Config.feature() respects env var as source-5."""

    def test_env_overrides_default_enabled(self, monkeypatch):
        # agent_cap is DEFAULT_OFF — env var should be able to enable it
        monkeypatch.setenv("CONCINNO_AGENT_CAP_ENABLED", "true")
        cfg = Config()
        assert cfg.feature("agent_cap", "enabled") is True

    def test_env_overrides_config_file(self, monkeypatch, tmp_path):
        cfg_path = str(tmp_path / "cc_config.json")
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump({"features": {"agent_cap": {"max_spawns": 3}}}, f)
        monkeypatch.setenv("CONCINNO_AGENT_CAP_MAX_SPAWNS", "12")
        cfg = Config(config_path=cfg_path)
        assert cfg.feature("agent_cap", "max_spawns") == 12

    def test_absent_env_falls_through_to_config(self, monkeypatch, tmp_path):
        monkeypatch.delenv("CONCINNO_AGENT_CAP_MAX_SPAWNS", raising=False)
        cfg_path = str(tmp_path / "cc_config.json")
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump({"features": {"agent_cap": {"max_spawns": 7}}}, f)
        cfg = Config(config_path=cfg_path)
        assert cfg.feature("agent_cap", "max_spawns") == 7
