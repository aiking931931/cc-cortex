"""Tests for 2.6.1 hotfix F1 — `get_handoff_mode()` honors concinno.config.

Before 2.6.1, ``concinno config set mode <X>`` was a decorative no-op: the
handoff engine only ever read legacy ``cc_config.json::handoff_mode`` and
ignored the new 4-layer loader. This suite pins the fix:

  1. Legacy ``cc_config.json`` still wins when present (back-compat).
  2. concinno.config ``mode=general`` → ``"phase"`` handoff mode.
  3. concinno.config ``mode=handoff`` → ``"save-token"`` handoff mode.
  4. env override (``CONCINNO_MODE``) propagates through.
  5. No opinion anywhere → ``"phase"`` fallback (ship default).
"""

from __future__ import annotations

import json
import os

import pytest

from concinno import config as cfg
from concinno import handoff_engine


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch, tmp_path):
    """Redirect HOME and wipe CONCINNO_* env so every test is isolated."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    for env_key in list(os.environ.keys()):
        if env_key.startswith("CONCINNO_"):
            monkeypatch.delenv(env_key, raising=False)
    # Unset CLAUDE_PROJECT_DIR so legacy cc_config.json doesn't leak
    # from the developer's real workspace.
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)


class TestLegacyCcConfigStillWins:
    """When a legacy cc_config.json exists, it keeps full authority —
    we must not break existing installs that already tuned via the old
    path. The new concinno.config layer only fills in the gap.
    """

    def _seed_legacy(self, tmp_path, mode: str) -> str:
        """Create a fake $CLAUDE_PROJECT_DIR/.claude/hooks/cc_config.json."""
        project_dir = tmp_path / "project"
        hooks_dir = project_dir / ".claude" / "hooks"
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "cc_config.json").write_text(
            json.dumps({"handoff_mode": mode}),
            encoding="utf-8",
        )
        return str(project_dir)

    def test_legacy_save_token_honored(self, tmp_path, monkeypatch):
        project_dir = self._seed_legacy(tmp_path, "save-token")
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", project_dir)
        assert handoff_engine.get_handoff_mode() == "save-token"

    def test_legacy_full_honored(self, tmp_path, monkeypatch):
        project_dir = self._seed_legacy(tmp_path, "full")
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", project_dir)
        assert handoff_engine.get_handoff_mode() == "full"
        assert handoff_engine.is_full_autonomous() is True

    def test_legacy_competition_honored(self, tmp_path, monkeypatch):
        project_dir = self._seed_legacy(tmp_path, "competition")
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", project_dir)
        assert handoff_engine.get_handoff_mode() == "competition"
        assert handoff_engine.is_competition_mode() is True


class TestConcinnoConfigDrivesMode:
    """When there's no legacy cc_config.json, concinno.config takes over.
    This is the consumer that makes ``concinno config set mode`` real.
    """

    def test_no_config_anywhere_falls_back_to_phase(self, tmp_path, monkeypatch):
        # No legacy file, no user config, no env override.
        # Ship default concinno.config is mode=general → phase.
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
        assert handoff_engine.get_handoff_mode() == "phase"

    def test_user_config_handoff_maps_to_save_token(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
        cfg.set_user("mode", "handoff")
        assert handoff_engine.get_handoff_mode() == "save-token"

    def test_user_config_general_maps_to_phase(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
        cfg.set_user("mode", "general")
        assert handoff_engine.get_handoff_mode() == "phase"

    def test_env_override_handoff_maps_to_save_token(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
        monkeypatch.setenv("CONCINNO_MODE", "handoff")
        assert handoff_engine.get_handoff_mode() == "save-token"

    def test_env_override_general_maps_to_phase(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
        monkeypatch.setenv("CONCINNO_MODE", "general")
        assert handoff_engine.get_handoff_mode() == "phase"


class TestLegacyWinsOverConcinnoConfig:
    """If both exist, legacy wins (back-compat for existing installs)."""

    def test_legacy_save_token_beats_user_general(self, tmp_path, monkeypatch):
        project_dir = tmp_path / "project"
        hooks_dir = project_dir / ".claude" / "hooks"
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "cc_config.json").write_text(
            json.dumps({"handoff_mode": "save-token"}),
            encoding="utf-8",
        )
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project_dir))
        cfg.set_user("mode", "general")  # concinno.config says phase
        # Legacy save-token still wins.
        assert handoff_engine.get_handoff_mode() == "save-token"


class TestInvalidValuesFallThrough:
    """Malformed legacy value doesn't poison the whole decision — we
    fall through to concinno.config, then to the ``phase`` default.
    """

    def test_bad_legacy_mode_falls_through_to_config(self, tmp_path, monkeypatch):
        project_dir = tmp_path / "project"
        hooks_dir = project_dir / ".claude" / "hooks"
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "cc_config.json").write_text(
            json.dumps({"handoff_mode": "nonsense"}),
            encoding="utf-8",
        )
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project_dir))
        cfg.set_user("mode", "handoff")
        # Bad legacy value → skip legacy, pick up concinno.config.
        assert handoff_engine.get_handoff_mode() == "save-token"

    def test_malformed_legacy_json_falls_through(self, tmp_path, monkeypatch):
        project_dir = tmp_path / "project"
        hooks_dir = project_dir / ".claude" / "hooks"
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "cc_config.json").write_text("}}not json{{", encoding="utf-8")
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project_dir))
        cfg.set_user("mode", "handoff")
        assert handoff_engine.get_handoff_mode() == "save-token"
