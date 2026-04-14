"""Tests for cc_cortex.core.config — Config singleton, update_file, config_file_path."""

from __future__ import annotations

import json

import pytest

from cc_cortex.core.config import Config, reset_config


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
