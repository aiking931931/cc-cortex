"""Tests for back-compat legacy feature alias resolution.

Origin: 2026-04-26 leakage-cleanup rename of two GAIA-context feature
flags. Old names hinted at GAIA test-set answer paths; renamed to
describe actual behavior (LANCZOS image upscale gate). Old config
keys remain accepted for one minor version via the alias map in
``concinno.feature_config.LEGACY_ALIASES`` and the resolution layer
in ``concinno.core.config.Config.feature`` /
:meth:`Config.feature_all` / :meth:`Config.set_feature`.

Drop schedule: aliases scheduled to drop 2026-07.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from concinno.core.config import (
    _WARNED_LEGACY_ALIASES,
    Config,
    _resolve_feature_alias,
)
from concinno.feature_config import LEGACY_ALIASES, resolve_alias


@pytest.fixture(autouse=True)
def _clear_warning_cache():
    """Reset the per-process warning dedup cache between tests so
    each test sees a fresh stderr emission."""
    _WARNED_LEGACY_ALIASES.clear()
    yield
    _WARNED_LEGACY_ALIASES.clear()


@pytest.fixture
def cfg_with_legacy(tmp_path: Path) -> Config:
    """Config instance whose cc_config.json sets ONLY the legacy
    name (simulating an unmigrated user config)."""
    cfg_path = tmp_path / "cc_config.json"
    cfg_path.write_text(
        json.dumps({
            "features": {
                "bassclef_wordreverse": {"enabled": False},
                "polygon_counting_hint": {"enabled": True},
            }
        }),
        encoding="utf-8",
    )
    return Config(config_path=str(cfg_path))


@pytest.fixture
def cfg_with_canonical(tmp_path: Path) -> Config:
    """Config instance whose cc_config.json sets the new canonical
    name (post-migration)."""
    cfg_path = tmp_path / "cc_config.json"
    cfg_path.write_text(
        json.dumps({
            "features": {
                "gaia_music_image_upscale": {"enabled": False},
                "gaia_polygon_image_upscale": {"enabled": True},
            }
        }),
        encoding="utf-8",
    )
    return Config(config_path=str(cfg_path))


class TestAliasMap:
    def test_legacy_aliases_dict_present(self) -> None:
        assert "bassclef_wordreverse" in LEGACY_ALIASES
        assert "polygon_counting_hint" in LEGACY_ALIASES
        assert (
            LEGACY_ALIASES["bassclef_wordreverse"]
            == "gaia_music_image_upscale"
        )
        assert (
            LEGACY_ALIASES["polygon_counting_hint"]
            == "gaia_polygon_image_upscale"
        )

    def test_resolve_alias_pure_function(self) -> None:
        assert (
            resolve_alias("bassclef_wordreverse")
            == "gaia_music_image_upscale"
        )
        assert (
            resolve_alias("polygon_counting_hint")
            == "gaia_polygon_image_upscale"
        )
        # Non-aliased name passes through unchanged.
        assert resolve_alias("ocr_fallback") == "ocr_fallback"

    def test_config_resolve_alias_internal(self) -> None:
        assert (
            _resolve_feature_alias("bassclef_wordreverse")
            == "gaia_music_image_upscale"
        )
        assert _resolve_feature_alias("ocr_fallback") == "ocr_fallback"


class TestCanonicalReadsLegacyEntry:
    """Caller asks for new name; only old name set in cc_config.json."""

    def test_feature_returns_legacy_value(
        self, cfg_with_legacy: Config,
    ) -> None:
        # Caller uses canonical name; alias resolution returns the
        # value sitting under the legacy key.
        assert cfg_with_legacy.feature(
            "gaia_music_image_upscale", "enabled"
        ) is False
        assert cfg_with_legacy.feature(
            "gaia_polygon_image_upscale", "enabled"
        ) is True

    def test_feature_all_returns_legacy_dict(
        self, cfg_with_legacy: Config,
    ) -> None:
        assert cfg_with_legacy.feature_all(
            "gaia_music_image_upscale"
        ) == {"enabled": False}
        assert cfg_with_legacy.feature_all(
            "gaia_polygon_image_upscale"
        ) == {"enabled": True}

    def test_canonical_lookup_emits_deprecation_warning(
        self,
        cfg_with_legacy: Config,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        cfg_with_legacy.feature(
            "gaia_music_image_upscale", "enabled"
        )
        captured = capsys.readouterr()
        assert "bassclef_wordreverse" in captured.err
        assert "gaia_music_image_upscale" in captured.err
        assert "drops 2026-07" in captured.err


class TestLegacyCallerStillWorks:
    """Existing call-sites that still pass the old name keep working
    AND emit a deprecation warning."""

    def test_legacy_caller_returns_legacy_value(
        self, cfg_with_legacy: Config,
    ) -> None:
        # Old call-site: pass legacy name, get legacy value.
        assert cfg_with_legacy.feature(
            "bassclef_wordreverse", "enabled"
        ) is False

    def test_legacy_caller_emits_warning(
        self,
        cfg_with_legacy: Config,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        cfg_with_legacy.feature("bassclef_wordreverse", "enabled")
        captured = capsys.readouterr()
        assert "bassclef_wordreverse" in captured.err
        assert "gaia_music_image_upscale" in captured.err

    def test_legacy_caller_with_canonical_config(
        self,
        cfg_with_canonical: Config,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # User has migrated config to canonical; old caller still
        # gets the right value via alias resolution.
        assert cfg_with_canonical.feature(
            "bassclef_wordreverse", "enabled"
        ) is False
        captured = capsys.readouterr()
        assert "drops 2026-07" in captured.err


class TestWarningDedup:
    def test_warning_emitted_once_per_process(
        self,
        cfg_with_legacy: Config,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        cfg_with_legacy.feature("bassclef_wordreverse", "enabled")
        first = capsys.readouterr().err
        cfg_with_legacy.feature("bassclef_wordreverse", "enabled")
        second = capsys.readouterr().err
        assert "bassclef_wordreverse" in first
        # Second call: already warned; stderr stays empty for that
        # alias (other concinno noise is allowed but the rename
        # message must not repeat).
        assert "renamed to 'gaia_music_image_upscale'" not in second


class TestSetFeatureWritesCanonical:
    def test_set_feature_with_legacy_writes_canonical_key(
        self, tmp_path: Path,
    ) -> None:
        cfg_path = tmp_path / "cc_config.json"
        cfg_path.write_text("{}", encoding="utf-8")
        cfg = Config(config_path=str(cfg_path))
        cfg.set_feature("bassclef_wordreverse", "enabled", True)

        # On-disk cc_config.json has the canonical key, not the legacy.
        on_disk: dict[str, Any] = json.loads(
            cfg_path.read_text(encoding="utf-8")
        )
        features = on_disk.get("features", {})
        assert "gaia_music_image_upscale" in features
        assert "bassclef_wordreverse" not in features
        assert features["gaia_music_image_upscale"]["enabled"] is True


class TestSymmetric:
    def test_canonical_set_legacy_get(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "cc_config.json"
        cfg_path.write_text("{}", encoding="utf-8")
        cfg = Config(config_path=str(cfg_path))
        cfg.set_feature("gaia_polygon_image_upscale", "enabled", True)
        # Cache invalidation is internal to set_feature; re-read.
        assert cfg.feature(
            "polygon_counting_hint", "enabled"
        ) is True
        assert cfg.feature(
            "gaia_polygon_image_upscale", "enabled"
        ) is True
