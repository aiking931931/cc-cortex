"""W1B audit hotfix regression tests (Concinno 5.5.1).

Sources:
  - Audit report: ``_AI_BRAIN/05_Planning/switches_audit_report_2026-05-03.md``
  - Plan: ``C:/Users/zerox/.claude/plans/logical-dancing-crayon.md``

Three P0/P1 fixes covered here:

* **F1 (P0)** — ``concinno.guards.wiredo_subagent_verify_guard._feature_enabled``
  / ``_feature_param`` previously read ``FEATURE_META`` hardcoded default only.
  The documented opt-in
  ``cfg.feature('wiredo_subagent_verify','enabled')=True`` silently had no
  effect at runtime.
* **F2 (P0)** — ``concinno.feature_config.list_features`` /
  ``get_feature`` used ``cfg.feature_all(name)`` which returns the raw
  cc_config dict and silently swallows env var overrides
  (``CONCINNO_<FEATURE>_<PARAM>``). CLI echo and runtime behaviour could
  disagree.
* **F5 (P1)** — Source #4 of the documented 6-source chain
  (``~/.concinno/cc_config.json`` and ``~/.concinno/<feature>.json``) was
  marked "future" since v3 and never implemented. This is the root-cause
  class behind user's repeated "明明關閉還是擋" reports.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from concinno.core.config import Config
from concinno.feature_config import get_feature, list_features
from concinno.guards import wiredo_subagent_verify_guard as wsv

# ── Helpers ────────────────────────────────────────────────────────


def _redirect_home(monkeypatch: pytest.MonkeyPatch, fake_home: Path) -> None:
    """Redirect ``Path.home()`` (and ``$HOME`` / ``$USERPROFILE``) to *fake_home*.

    Both env vars are needed because Python on Windows resolves
    ``Path.home()`` via ``USERPROFILE`` while POSIX uses ``HOME`` —
    monkeypatching only one side of the fence breaks one OS.
    """
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))


def _force_fresh_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset the module-level ``get_config`` singleton between tests.

    ``concinno.core.config.get_config()`` caches the Config instance in a
    module-global; without resetting, env / overlay changes from one test
    bleed into the next.
    """
    import concinno.core.config as cfg_mod

    monkeypatch.setattr(cfg_mod, "_config_instance", None, raising=False)
    # Some versions name the cache differently — best-effort reset.
    for attr in ("_CONFIG", "_singleton", "_cached_config"):
        if hasattr(cfg_mod, attr):
            monkeypatch.setattr(cfg_mod, attr, None, raising=False)


# ── F1: wiredo_subagent_verify guard reads through 6-source chain ──


class TestWiredoSubagentVerifyFeatureRead:
    def test_user_override_in_cc_config_now_takes_effect(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Documented opt-in ``cfg.feature('wiredo_subagent_verify','enabled')=True``
        must flip ``_feature_enabled()`` to True (was always False pre-5.5.1)."""
        cfg_path = tmp_path / "cc_config.json"
        cfg_path.write_text(
            json.dumps({"features": {"wiredo_subagent_verify": {"enabled": True}}}),
            encoding="utf-8",
        )

        # Patch get_config to return our scoped Config.
        scoped = Config(config_path=str(cfg_path))
        import concinno.core.config as cfg_mod

        monkeypatch.setattr(cfg_mod, "get_config", lambda *a, **kw: scoped)

        assert wsv._feature_enabled() is True

    def test_user_override_disables(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """User can opt-out via ``cfg.feature(...,'enabled')=False`` even when
        FEATURE_META ships ``enabled=True`` default. Pre-5.5.1 the guard ignored
        cc_config entirely and always returned the FEATURE_META default."""
        cfg_path = tmp_path / "cc_config.json"
        cfg_path.write_text(
            json.dumps({"features": {"wiredo_subagent_verify": {"enabled": False}}}),
            encoding="utf-8",
        )

        scoped = Config(config_path=str(cfg_path))
        import concinno.core.config as cfg_mod

        monkeypatch.setattr(cfg_mod, "get_config", lambda *a, **kw: scoped)
        # Ensure no env override leaks in from the host.
        monkeypatch.delenv("CONCINNO_WIREDO_SUBAGENT_VERIFY_ENABLED", raising=False)

        assert wsv._feature_enabled() is False

    def test_env_override_now_takes_effect(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Env var ``CONCINNO_WIREDO_SUBAGENT_VERIFY_ENABLED=1`` flips the flag."""
        cfg_path = tmp_path / "cc_config.json"
        cfg_path.write_text("{}", encoding="utf-8")

        monkeypatch.setenv("CONCINNO_WIREDO_SUBAGENT_VERIFY_ENABLED", "true")
        scoped = Config(config_path=str(cfg_path))
        import concinno.core.config as cfg_mod

        monkeypatch.setattr(cfg_mod, "get_config", lambda *a, **kw: scoped)

        assert wsv._feature_enabled() is True

    def test_feature_param_reads_through_chain(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """``_feature_param('retry_cap', default)`` honours user override."""
        cfg_path = tmp_path / "cc_config.json"
        cfg_path.write_text(
            json.dumps(
                {"features": {"wiredo_subagent_verify": {"retry_cap": 5}}}
            ),
            encoding="utf-8",
        )

        scoped = Config(config_path=str(cfg_path))
        import concinno.core.config as cfg_mod

        monkeypatch.setattr(cfg_mod, "get_config", lambda *a, **kw: scoped)

        assert wsv._feature_param("retry_cap", 3) == 5
        # Unset key falls back to caller default.
        assert wsv._feature_param("not_a_real_param", "fallback") == "fallback"


# ── F2: list_features / get_feature reflect env overrides ──────────


class TestFeatureConfigCliReflectsEnv:
    def test_list_features_reflects_env_disable(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """When env disables a feature, ``list_features`` echoes disabled state."""
        cfg_path = tmp_path / "cc_config.json"
        cfg_path.write_text("{}", encoding="utf-8")

        # agent_cap defaults enabled=True per FEATURE_META; force env off.
        monkeypatch.setenv("CONCINNO_AGENT_CAP_ENABLED", "false")

        scoped = Config(config_path=str(cfg_path))
        import concinno.core.config as cfg_mod

        monkeypatch.setattr(cfg_mod, "get_config", lambda *a, **kw: scoped)

        rows = list_features()
        agent_cap_row = next((r for r in rows if r["name"] == "agent_cap"), None)
        assert agent_cap_row is not None
        assert agent_cap_row["enabled"] is False, (
            "CLI echo must mirror env var override (was True pre-5.5.1)"
        )

    def test_get_feature_reflects_env_override(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """``get_feature`` (single-feature query) honours env overrides too."""
        cfg_path = tmp_path / "cc_config.json"
        cfg_path.write_text("{}", encoding="utf-8")

        monkeypatch.setenv("CONCINNO_AGENT_CAP_ENABLED", "false")

        scoped = Config(config_path=str(cfg_path))
        import concinno.core.config as cfg_mod

        monkeypatch.setattr(cfg_mod, "get_config", lambda *a, **kw: scoped)

        info = get_feature("agent_cap")
        assert info is not None
        assert info["enabled"] is False


# ── F5: Source #4 — ~/.concinno user-level overlay ─────────────────


class TestUserLevelConfigOverlay:
    def test_main_user_config_overrides_project(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """``~/.concinno/cc_config.json`` overlays project cc_config.json."""
        fake_home = tmp_path / "fake_home"
        (fake_home / ".concinno").mkdir(parents=True)
        _redirect_home(monkeypatch, fake_home)

        # Project cfg says agent_cap.enabled=True (or unset).
        proj_cfg = tmp_path / "cc_config.json"
        proj_cfg.write_text(
            json.dumps({"features": {"agent_cap": {"enabled": True}}}),
            encoding="utf-8",
        )

        # User-level overlay disables it — Source #4 must win over Source #3.
        user_cfg = fake_home / ".concinno" / "cc_config.json"
        user_cfg.write_text(
            json.dumps({"features": {"agent_cap": {"enabled": False}}}),
            encoding="utf-8",
        )

        cfg = Config(config_path=str(proj_cfg))
        assert cfg.feature("agent_cap", "enabled") is False

    def test_per_feature_overlay_file_picked_up(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """``~/.concinno/<feature>.json`` schema ``{"features":{<name>:{...}}}``
        merges into features dict — the documented switches.md opt-out path."""
        fake_home = tmp_path / "fake_home"
        (fake_home / ".concinno").mkdir(parents=True)
        _redirect_home(monkeypatch, fake_home)

        proj_cfg = tmp_path / "cc_config.json"
        proj_cfg.write_text("{}", encoding="utf-8")

        # Per-feature overlay — file name doesn't have to match feature name,
        # only the inner "features" dict matters. Use the documented
        # ``~/.concinno/wiredo.json`` shape.
        per_feat = fake_home / ".concinno" / "wiredo.json"
        per_feat.write_text(
            json.dumps(
                {
                    "features": {
                        "wiredo_subagent_verify": {
                            "enabled": True,
                            "retry_cap": 4,
                        }
                    }
                }
            ),
            encoding="utf-8",
        )

        cfg = Config(config_path=str(proj_cfg))
        assert cfg.feature("wiredo_subagent_verify", "enabled") is True
        assert cfg.feature("wiredo_subagent_verify", "retry_cap") == 4

    def test_special_case_files_not_re_interpreted(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """``release_auth.json``/``locale.json``/etc must not be loaded by
        the overlay loop — they have their own dedicated loaders elsewhere."""
        fake_home = tmp_path / "fake_home"
        (fake_home / ".concinno").mkdir(parents=True)
        _redirect_home(monkeypatch, fake_home)

        # Drop a release_auth.json with a *misshapen* "features" dict that
        # would corrupt agent_cap if the overlay loop touched it.
        (fake_home / ".concinno" / "release_auth.json").write_text(
            json.dumps(
                {"features": {"agent_cap": {"enabled": False}}, "disabled": True}
            ),
            encoding="utf-8",
        )

        proj_cfg = tmp_path / "cc_config.json"
        proj_cfg.write_text(
            json.dumps({"features": {"agent_cap": {"enabled": True}}}),
            encoding="utf-8",
        )

        cfg = Config(config_path=str(proj_cfg))
        # If special-case skip is broken, this would flip to False.
        assert cfg.feature("agent_cap", "enabled") is True

    def test_missing_user_dir_is_silent(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """No ``~/.concinno/`` directory — must not error, must not modify state."""
        fake_home = tmp_path / "fake_home_no_concinno"
        fake_home.mkdir()
        # Note: deliberately no .concinno subdir.
        _redirect_home(monkeypatch, fake_home)

        proj_cfg = tmp_path / "cc_config.json"
        proj_cfg.write_text(
            json.dumps({"features": {"agent_cap": {"enabled": True}}}),
            encoding="utf-8",
        )

        cfg = Config(config_path=str(proj_cfg))
        # Must not raise.
        assert cfg.feature("agent_cap", "enabled") is True

    def test_malformed_overlay_json_is_silent(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Corrupt user overlay must fall through to project/defaults silently."""
        fake_home = tmp_path / "fake_home"
        (fake_home / ".concinno").mkdir(parents=True)
        _redirect_home(monkeypatch, fake_home)

        # Garbage overlay file — must not abort _load.
        (fake_home / ".concinno" / "cc_config.json").write_text(
            "this is not { valid json", encoding="utf-8"
        )

        proj_cfg = tmp_path / "cc_config.json"
        proj_cfg.write_text(
            json.dumps({"features": {"agent_cap": {"enabled": True}}}),
            encoding="utf-8",
        )

        cfg = Config(config_path=str(proj_cfg))
        # Falls back to project value — no exception.
        assert cfg.feature("agent_cap", "enabled") is True
