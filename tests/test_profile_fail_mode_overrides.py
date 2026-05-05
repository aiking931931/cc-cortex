"""Tests for the 4.3.0 profile ``fail_mode_overrides`` schema and the
``get_fail_mode()`` public API.

Plan B Step 1 ship — every profile carries:

  * ``fail_mode_overrides: dict[str, FailMode]`` — per-feature explicit
    settings (e.g. lite forces ``destruction_guard = "hard_deny"``)
  * ``fail_mode_default: FailMode`` — catch-all for any feature absent
    from the override dict (e.g. paranoid catch-all = ``"hard_deny"``)

The ``get_fail_mode()`` resolver chain (later wins):

  1. profile per-feature override
  2. profile catch-all
  3. user override on disk (``cfg.feature(feat, "fail_mode")``)
  4. env var (handled by Config.feature's 6-source chain)

The ``permissive`` profile is now an alias for ``lite``; ``get_fail_mode
("...", "permissive")`` and ``get_fail_mode("...", "lite")`` must return
identical values for every feature.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from concinno.core.config import Config
from concinno.feature_config import (
    FEATURE_TOGGLE_PROFILES,
    VALID_FAIL_MODES,
    _resolve_profile_alias,
    apply_feature_toggle_profile,
    get_fail_mode,
)


@pytest.fixture()
def tmp_cfg(tmp_path: Path) -> Config:
    """Config rooted at a tmp cc_config.json — no real ~ pollution."""
    cfg_path = tmp_path / "cc_config.json"
    cfg_path.write_text("{}", encoding="utf-8")
    return Config(config_path=str(cfg_path))


# ── Schema invariants ────────────────────────────────────────────────


def test_four_new_profiles_registered() -> None:
    """The 4.3.0 four-profile set is fully present."""
    for name in ("lite", "mainstream", "strict", "paranoid"):
        assert name in FEATURE_TOGGLE_PROFILES, (
            f"Profile {name!r} missing from FEATURE_TOGGLE_PROFILES"
        )


def test_every_profile_has_fail_mode_overrides_field() -> None:
    """Schema invariant — every entry must declare both fields."""
    for name, prof in FEATURE_TOGGLE_PROFILES.items():
        assert "fail_mode_overrides" in prof, (
            f"Profile {name!r} missing fail_mode_overrides"
        )
        assert "fail_mode_default" in prof, (
            f"Profile {name!r} missing fail_mode_default"
        )
        assert isinstance(prof["fail_mode_overrides"], dict)
        assert prof["fail_mode_default"] in VALID_FAIL_MODES


def test_every_override_value_is_valid_fail_mode() -> None:
    """No typos — every override value must be one of the four
    canonical literals."""
    for name, prof in FEATURE_TOGGLE_PROFILES.items():
        for feat, mode in prof["fail_mode_overrides"].items():
            assert mode in VALID_FAIL_MODES, (
                f"Profile {name!r} override {feat!r} = {mode!r} "
                f"is not a valid FailMode"
            )


# ── Profile-default behaviour ────────────────────────────────────────


def test_lite_default_is_silent_for_unknown_feature() -> None:
    """A feature with no explicit override falls through to lite's
    catch-all (silent)."""
    assert get_fail_mode("some_random_feature_xyz", "lite") == "silent"


def test_mainstream_default_is_warn_for_unknown_feature() -> None:
    assert get_fail_mode("some_random_feature_xyz", "mainstream") == "warn"


def test_strict_default_is_warn_plus_log_for_unknown_feature() -> None:
    assert (
        get_fail_mode("some_random_feature_xyz", "strict") == "warn+log"
    )


def test_paranoid_default_is_hard_deny_for_unknown_feature() -> None:
    """Paranoid catch-all hard-denies everything not explicitly
    softened."""
    assert (
        get_fail_mode("some_random_feature_xyz", "paranoid") == "hard_deny"
    )


# ── Per-profile per-feature overrides ────────────────────────────────


def test_destruction_guard_is_hard_deny_in_every_profile() -> None:
    """Data-loss protection invariant — destruction_guard NEVER
    drops below hard_deny regardless of profile (per L0 鐵律 #5
    + the publish-authorization opt-out which keeps destruction
    on)."""
    for name in ("lite", "mainstream", "strict", "paranoid"):
        assert get_fail_mode("destruction_guard", name) == "hard_deny", (
            f"destruction_guard must hard_deny under {name!r}"
        )


def test_strict_pii_and_deserialize_hard_deny() -> None:
    """Strict raises pii / deserialize to hard_deny per spec."""
    assert get_fail_mode("pii_guard", "strict") == "hard_deny"
    assert get_fail_mode("deserialize_guard", "strict") == "hard_deny"


def test_lite_butterfly_warn_not_hard_deny() -> None:
    """Lite softens butterfly_guard from hard_deny to warn — the
    flagship "minimal blocking" profile semantic."""
    assert get_fail_mode("butterfly_guard", "lite") == "warn"


# ── permissive → lite alias resolution ───────────────────────────────


def test_permissive_alias_resolves_to_lite() -> None:
    assert _resolve_profile_alias("permissive") == "lite"


def test_permissive_get_fail_mode_matches_lite() -> None:
    """Every feature looked up under ``permissive`` must return the
    same value as under ``lite`` — the alias is purely cosmetic for
    backward compat with 4.2.x callers."""
    sample_features = [
        "destruction_guard",
        "butterfly_guard",
        "pii_guard",
        "some_random_unknown_feature",
    ]
    for feat in sample_features:
        assert get_fail_mode(feat, "permissive") == get_fail_mode(
            feat, "lite"
        ), f"permissive/lite divergence on feature {feat!r}"


# ── Validator behaviour ──────────────────────────────────────────────


def test_invalid_profile_raises() -> None:
    with pytest.raises(ValueError, match="Unknown profile"):
        get_fail_mode("destruction_guard", "this_profile_does_not_exist")


def test_invalid_fail_mode_in_profile_raises_at_import_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The module-level ``_validate_profile_fail_modes()`` must reject
    any profile whose override dict contains a non-canonical literal.

    We invoke the validator directly with a tampered registry rather
    than re-import the module, since import-time runs at process start
    and a re-import would race the fixture-scoped patches.
    """
    from concinno import feature_config as fc

    bad_profile = {
        "lite": {
            "description": "x",
            "enable": frozenset(),
            "disable": frozenset(),
            "fail_mode_overrides": {"foo": "definitely_not_valid"},
            "fail_mode_default": "silent",
        }
    }
    monkeypatch.setattr(fc, "FEATURE_TOGGLE_PROFILES", bad_profile)
    with pytest.raises(ValueError, match="invalid"):
        fc._validate_profile_fail_modes()


def test_invalid_fail_mode_default_raises() -> None:
    from concinno import feature_config as fc

    saved = fc.FEATURE_TOGGLE_PROFILES
    try:
        fc.FEATURE_TOGGLE_PROFILES = {  # type: ignore[misc]
            "lite": {
                "description": "x",
                "enable": frozenset(),
                "disable": frozenset(),
                "fail_mode_overrides": {},
                "fail_mode_default": "garbage_value",
            }
        }
        with pytest.raises(ValueError, match="fail_mode_default"):
            fc._validate_profile_fail_modes()
    finally:
        fc.FEATURE_TOGGLE_PROFILES = saved  # type: ignore[misc]


# ── User override (cfg.feature) wins over profile default ────────────


def test_user_override_beats_profile_default(tmp_cfg: Config) -> None:
    """If the user pins ``cfg.feature(feat, "fail_mode") = "hard_deny"``
    on disk, ``get_fail_mode()`` honours it even under the lite
    profile (whose default for that feature would be ``silent``)."""
    tmp_cfg.set_feature("some_random_feature", "fail_mode", "hard_deny")
    # Without cfg → profile default
    assert get_fail_mode("some_random_feature", "lite") == "silent"
    # With cfg → user override wins
    assert (
        get_fail_mode("some_random_feature", "lite", cfg=tmp_cfg)
        == "hard_deny"
    )


def test_user_override_invalid_value_falls_through_to_profile(
    tmp_cfg: Config,
) -> None:
    """Garbage in cc_config.json must NOT crash — it falls through
    to the profile default (defence in depth)."""
    tmp_cfg.set_feature("some_random_feature", "fail_mode", "junk")
    assert (
        get_fail_mode("some_random_feature", "lite", cfg=tmp_cfg)
        == "silent"
    )


# ── Backward compat — apply_feature_toggle_profile still works ───────


def test_existing_apply_feature_toggle_profile_still_works(
    tmp_cfg: Config,
) -> None:
    """The 4.3.0 schema additions must not break the existing
    ``apply_feature_toggle_profile`` API contract."""
    result = apply_feature_toggle_profile("strict", cfg=tmp_cfg)
    assert "error" not in result
    assert result["profile"] == "strict"


def test_apply_permissive_routes_through_lite_definition(
    tmp_cfg: Config,
) -> None:
    """Applying ``permissive`` writes the same enabled/disabled deltas
    as applying ``lite`` — the alias points at lite's enable/disable
    spec under the hood."""
    perm = apply_feature_toggle_profile("permissive", cfg=tmp_cfg)
    # Reset cfg so the second call sees a fresh slate.
    cfg_path = Path(str(tmp_cfg._config_path))
    cfg_path.write_text("{}", encoding="utf-8")
    fresh_cfg = Config(config_path=str(cfg_path))
    lite = apply_feature_toggle_profile("lite", cfg=fresh_cfg)
    # Both produce the same enable/disable deltas (sets, since order
    # doesn't matter for equivalence).
    assert set(perm["enabled"]) == set(lite["enabled"])
    assert set(perm["disabled"]) == set(lite["disabled"])
    # The user-facing profile name is preserved (no surprise rename
    # in the result dict).
    assert perm["profile"] == "permissive"
    assert lite["profile"] == "lite"
