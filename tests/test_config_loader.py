"""Tests for :mod:`concinno.config` (user-facing layered config loader).

Distinct from the older :mod:`concinno.core.config` singleton — that module
is tested by ``tests/test_config.py`` and manages ``cc_config.json`` for
hooks/paths/thresholds. This loader is the 2.6.0-new 4-layer user settings
(``mode``, ``locale``, ``auto_compact``, ``memory_file_enabled``).

Invariants locked by MEMORY #59:
    * ``_DEFAULT_CONFIG["mode"] == "general"``
    * ``_DEFAULT_CONFIG["locale"] == "en"``
    * These are ship defaults for every PyPI downloader. AI King's personal
      ``zh-TW + handoff`` preference belongs in ``~/.concinno/config.json``
      (layer 3), never in the source default.

Test strategy:
    * Redirect ``HOME`` via ``monkeypatch`` so ``user_config_path()`` lands
      in a tmp dir — no risk of clobbering a real user's config.
    * Use ``tmp_path`` for project-layer overrides so each test is isolated.
    * Wipe ``CONCINNO_*`` env vars before each test so env layer is blank.
"""

from __future__ import annotations

import argparse
import json
import os

import pytest

from concinno import config as cfg


# ── Fixtures ─────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch, tmp_path):
    """Redirect HOME + wipe env so every test sees a blank slate."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))  # Windows
    for env_key in list(os.environ.keys()):
        if env_key.startswith("CONCINNO_"):
            monkeypatch.delenv(env_key, raising=False)


# ── Invariant tests (MEMORY #59 — ship defaults locked) ─────────


class TestShipDefaultInvariants:
    """Ship defaults are locked. Anonymous PyPI downloads must see general/en.

    If a future change flips either of these, downstream behavior changes
    silently for every user — hence the invariant pin.
    """

    def test_default_mode_is_general(self):
        assert cfg._DEFAULT_CONFIG["mode"] == "general", (
            "Ship default mode must be 'general' per MEMORY #59 — "
            "AI King's 'handoff' preference belongs in user config only."
        )

    def test_default_locale_is_english(self):
        assert cfg._DEFAULT_CONFIG["locale"] == "en", (
            "Ship default locale must be 'en' per MEMORY #59 — "
            "AI King's 'zh-TW' preference belongs in user config only."
        )


# ── load() merge behaviour ──────────────────────────────────────


class TestLoadLayers:
    def test_empty_environment_returns_defaults(self, tmp_path):
        loaded = cfg.load(cwd=tmp_path)
        assert loaded == cfg._DEFAULT_CONFIG
        # Must be a deep copy — mutating ``loaded`` shouldn't poison defaults.
        loaded["mode"] = "handoff"
        assert cfg._DEFAULT_CONFIG["mode"] == "general"

    def test_env_override_all_keys(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CONCINNO_MODE", "handoff")
        monkeypatch.setenv("CONCINNO_LOCALE", "zh-TW")
        monkeypatch.setenv("CONCINNO_AUTO_COMPACT", "false")
        monkeypatch.setenv("CONCINNO_MEMORY_FILE_ENABLED", "0")
        loaded = cfg.load(cwd=tmp_path)
        assert loaded["mode"] == "handoff"
        assert loaded["locale"] == "zh-TW"
        assert loaded["auto_compact"] is False
        assert loaded["memory_file_enabled"] is False

    def test_user_config_override(self, tmp_path):
        user_path = cfg.user_config_path()
        user_path.parent.mkdir(parents=True, exist_ok=True)
        user_path.write_text(
            json.dumps({"mode": "handoff", "locale": "ja"}),
            encoding="utf-8",
        )
        loaded = cfg.load(cwd=tmp_path)
        assert loaded["mode"] == "handoff"
        assert loaded["locale"] == "ja"
        # Other keys fall back to defaults.
        assert loaded["auto_compact"] is True

    def test_project_overrides_user(self, tmp_path):
        user_path = cfg.user_config_path()
        user_path.parent.mkdir(parents=True, exist_ok=True)
        user_path.write_text(json.dumps({"locale": "ja"}), encoding="utf-8")
        project_path = cfg.project_config_path(cwd=tmp_path)
        project_path.parent.mkdir(parents=True, exist_ok=True)
        project_path.write_text(json.dumps({"locale": "ko"}), encoding="utf-8")
        assert cfg.load(cwd=tmp_path)["locale"] == "ko"

    def test_priority_env_beats_project_beats_user_beats_default(
        self, monkeypatch, tmp_path,
    ):
        # All four layers set locale — env must win, default must lose.
        user_path = cfg.user_config_path()
        user_path.parent.mkdir(parents=True, exist_ok=True)
        user_path.write_text(json.dumps({"locale": "ja"}), encoding="utf-8")

        project_path = cfg.project_config_path(cwd=tmp_path)
        project_path.parent.mkdir(parents=True, exist_ok=True)
        project_path.write_text(json.dumps({"locale": "ko"}), encoding="utf-8")

        monkeypatch.setenv("CONCINNO_LOCALE", "fr")

        assert cfg.load(cwd=tmp_path)["locale"] == "fr"

        # Remove env layer — project should take over.
        monkeypatch.delenv("CONCINNO_LOCALE")
        assert cfg.load(cwd=tmp_path)["locale"] == "ko"

        # Remove project layer — user takes over.
        project_path.unlink()
        assert cfg.load(cwd=tmp_path)["locale"] == "ja"

        # Remove user layer — default.
        user_path.unlink()
        assert cfg.load(cwd=tmp_path)["locale"] == "en"


# ── set_user / set_project ──────────────────────────────────────


class TestWriteAPIs:
    def test_set_user_writes_valid_value(self, tmp_path):
        cfg.set_user("mode", "handoff")
        data = json.loads(cfg.user_config_path().read_text(encoding="utf-8"))
        assert data["mode"] == "handoff"
        assert data["$schema_version"] == 1
        assert cfg.get("mode", cwd=tmp_path) == "handoff"

    def test_set_user_invalid_mode_raises(self):
        with pytest.raises(ValueError, match="Invalid mode"):
            cfg.set_user("mode", "invalid")

    def test_set_user_locale_zh_tw(self, tmp_path):
        cfg.set_user("locale", "zh-TW")
        assert cfg.get("locale", cwd=tmp_path) == "zh-TW"

    def test_set_user_unknown_key_raises(self):
        with pytest.raises(ValueError, match="Unknown config key"):
            cfg.set_user("nonexistent", "x")

    def test_set_project_writes_to_cwd(self, tmp_path):
        cfg.set_project("mode", "handoff", cwd=tmp_path)
        assert (tmp_path / ".concinno" / "config.json").is_file()
        assert cfg.get("mode", cwd=tmp_path) == "handoff"


# ── Security guard: malformed + type mismatch ───────────────────


class TestMalformedAndBadKeys:
    def test_malformed_json_falls_back_to_default(self, tmp_path, capsys):
        user_path = cfg.user_config_path()
        user_path.parent.mkdir(parents=True, exist_ok=True)
        user_path.write_text("{ not json", encoding="utf-8")
        loaded = cfg.load(cwd=tmp_path)
        assert loaded["mode"] == "general"  # fell back
        err = capsys.readouterr().err
        assert "malformed" in err

    def test_unknown_key_in_config_warns(self, tmp_path, capsys):
        user_path = cfg.user_config_path()
        user_path.parent.mkdir(parents=True, exist_ok=True)
        user_path.write_text(
            json.dumps({"mystery_key": "x", "locale": "ja"}),
            encoding="utf-8",
        )
        loaded = cfg.load(cwd=tmp_path)
        assert loaded["locale"] == "ja"  # good key still applied
        assert "mystery_key" not in loaded
        assert "unknown key" in capsys.readouterr().err

    def test_type_mismatch_warns_and_skips(self, tmp_path, capsys):
        user_path = cfg.user_config_path()
        user_path.parent.mkdir(parents=True, exist_ok=True)
        user_path.write_text(
            json.dumps({"mode": 42, "locale": "en"}),
            encoding="utf-8",
        )
        loaded = cfg.load(cwd=tmp_path)
        assert loaded["mode"] == "general"  # skipped, used default
        assert "invalid value" in capsys.readouterr().err

    def test_nonexistent_user_config_returns_default_silently(
        self, tmp_path, capsys,
    ):
        loaded = cfg.load(cwd=tmp_path)
        assert loaded == cfg._DEFAULT_CONFIG
        # Missing file should not warn — absence is the normal case.
        assert "malformed" not in capsys.readouterr().err


# ── sources() reporting ─────────────────────────────────────────


class TestSources:
    def test_all_defaults_when_no_overrides(self, tmp_path):
        src = cfg.sources(cwd=tmp_path)
        assert all(v == "default" for v in src.values())

    def test_env_attribution(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CONCINNO_LOCALE", "ja")
        src = cfg.sources(cwd=tmp_path)
        assert src["locale"] == "env"
        assert src["mode"] == "default"


# ── CLI smoke tests ─────────────────────────────────────────────


class TestCLISmoke:
    def test_set_locale_via_cli(self, tmp_path, capsys):
        from concinno.cli.config_cmd import cmd_config_set

        args = argparse.Namespace(key="locale", value="zh-TW", project=False)
        cmd_config_set(args)
        assert cfg.get("locale", cwd=tmp_path) == "zh-TW"
        out = capsys.readouterr().out
        assert "zh-TW" in out

    def test_get_mode_via_cli(self, tmp_path, capsys):
        from concinno.cli.config_cmd import cmd_config_get

        # Fresh — should read the ship default.
        cmd_config_get(argparse.Namespace(key="mode"))
        assert capsys.readouterr().out.strip() == "general"

        # After set_user, should reflect the user layer.
        cfg.set_user("mode", "handoff")
        cmd_config_get(argparse.Namespace(key="mode"))
        assert capsys.readouterr().out.strip() == "handoff"

    def test_show_reports_merged_config(self, tmp_path, capsys):
        from concinno.cli.config_cmd import cmd_config_show

        cmd_config_show(argparse.Namespace())
        out = capsys.readouterr().out
        # Every key must appear with its source annotation.
        for key in cfg._DEFAULT_CONFIG:
            assert key in out
        assert "(default)" in out

    def test_unset_restores_default(self, tmp_path):
        cfg.set_user("mode", "handoff")
        assert cfg.get("mode", cwd=tmp_path) == "handoff"
        removed = cfg.unset_user("mode")
        assert removed is True
        assert cfg.get("mode", cwd=tmp_path) == "general"

    def test_invalid_bool_env_is_ignored_with_warning(
        self, monkeypatch, tmp_path, capsys,
    ):
        monkeypatch.setenv("CONCINNO_AUTO_COMPACT", "maybe")
        loaded = cfg.load(cwd=tmp_path)
        assert loaded["auto_compact"] is True  # default preserved
        assert "invalid bool" in capsys.readouterr().err
