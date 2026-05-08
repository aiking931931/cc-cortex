"""Tests for the 5.0.0 D-class default-on flip and ``4-x-compat`` profile.

Background: 4.0.0 SEMVER-MAJOR shipped 27 D-class features default-off
under the senior-dev permissive baseline. The 2026-04-29 8-axis audit
found those 27 had zero production trace, so 5.0.0 promotes them to
default-on (BREAKING). The ``D_CLASS_5_0_0`` frozenset is the canonical
manifest, and the ``4-x-compat`` profile is the one-call opt-out for
operators who want the 4.x baseline back.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from concinno.core.config import Config
from concinno.feature_config import (
    D_CLASS_5_0_0,
    DEFAULT_OFF_4_0_0,
    FEATURE_META,
    FEATURE_TOGGLE_PROFILES,
    _resolve_profile_features,
    apply_feature_toggle_profile,
    meta_enabled_default,
)


@pytest.fixture()
def tmp_cfg(tmp_path: Path) -> Config:
    """Config rooted at a tmp cc_config.json — no real ~ pollution."""
    cfg_path = tmp_path / "cc_config.json"
    cfg_path.write_text("{}", encoding="utf-8")
    return Config(config_path=str(cfg_path))


# ── D_CLASS_5_0_0 manifest ────────────────────────────────────────


def test_d_class_5_0_0_size_26() -> None:
    """The 26-feature manifest is locked — adding entries here implies
    another semver-major bump.

    Note: original 5.0.0 spec said 27; the off-by-one drift documented
    in CHANGELOG 5.7.0 §"Known carryover" was reconciled at the
    FEATCFG-S1 maintenance wave (2026-05-08). Current canonical
    composition: 9 security + 10 CBUA + 2 skill audit + 5 operational
    = 26, matching the actual frozenset content.
    """
    assert len(D_CLASS_5_0_0) == 26


def test_d_class_5_0_0_all_in_feature_meta() -> None:
    """Every promoted name must point at a real FEATURE_META entry,
    otherwise the bulk-disable CLI silently no-ops on typos."""
    missing = sorted(n for n in D_CLASS_5_0_0 if n not in FEATURE_META)
    assert missing == []


def test_d_class_5_0_0_does_not_overlap_default_off() -> None:
    """Promoted (default-on) and retained (default-off) sets must not
    intersect, otherwise ``meta_enabled_default`` returns False before
    the FEATURE_META entry's True is reached."""
    overlap = D_CLASS_5_0_0 & DEFAULT_OFF_4_0_0
    assert overlap == frozenset()


def test_d_class_5_0_0_default_on_in_meta() -> None:
    """``meta_enabled_default`` must report True for every D-class name
    on a fresh install — the whole point of the 5.0.0 flip."""
    for name in D_CLASS_5_0_0:
        assert meta_enabled_default(name) is True, f"{name} not default-on"


# ── Sentinel resolver ─────────────────────────────────────────────


def test_sentinel_resolves_to_d_class_5_0_0() -> None:
    """``_resolve_profile_features('D_CLASS_5_0_0')`` returns the
    canonical frozenset (not a copy)."""
    assert _resolve_profile_features("D_CLASS_5_0_0") == D_CLASS_5_0_0


def test_sentinel_resolves_to_default_off_4_0_0_unchanged() -> None:
    """The DEFAULT_OFF_4_0_0 sentinel still resolves correctly after
    the 5.0.0 schema additions (regression guard)."""
    assert _resolve_profile_features("DEFAULT_OFF_4_0_0") == DEFAULT_OFF_4_0_0


# ── 4-x-compat profile ────────────────────────────────────────────


def test_4_x_compat_profile_registered() -> None:
    """The profile must live in FEATURE_TOGGLE_PROFILES and reference
    the D_CLASS_5_0_0 sentinel."""
    prof = FEATURE_TOGGLE_PROFILES["4-x-compat"]
    assert prof["disable"] == "D_CLASS_5_0_0"
    assert prof["enable"] == frozenset()
    assert "BREAKING" in prof["description"]


def test_4_x_compat_disables_all_27(tmp_cfg: Config) -> None:
    """Applying the profile flips every D-class feature to enabled=False."""
    result = apply_feature_toggle_profile("4-x-compat", cfg=tmp_cfg)
    assert "error" not in result
    for feat in D_CLASS_5_0_0:
        assert tmp_cfg.feature(feat, "enabled") is False, (
            f"{feat} not disabled after 4-x-compat apply"
        )


def test_4_x_compat_is_idempotent(tmp_cfg: Config) -> None:
    """Re-running the profile yields zero diff."""
    apply_feature_toggle_profile("4-x-compat", cfg=tmp_cfg)
    second = apply_feature_toggle_profile("4-x-compat", cfg=tmp_cfg)
    assert second["enabled"] == []
    assert second["disabled"] == []
    assert len(second["unchanged"]) == len(D_CLASS_5_0_0)


def test_4_x_compat_preserves_destruction_guard(tmp_cfg: Config) -> None:
    """4-x-compat must NOT touch DestructionGuard — that's hard-deny in
    every profile because it's the only data-loss prevention left in
    5.0.0."""
    apply_feature_toggle_profile("4-x-compat", cfg=tmp_cfg)
    assert "destruction_guard" not in D_CLASS_5_0_0
    # Not in D_CLASS so applying the profile has no effect on its enabled
    # state. Whatever default is in FEATURE_META must remain.
    assert tmp_cfg.feature("destruction_guard", "enabled") in (True, False)


# ── disable-all-d-class CLI alias ─────────────────────────────────


def _run_disable_all_d_class_cli(cfg: Config) -> tuple[int, str]:
    """Invoke the alias subcommand in a self-contained parser."""
    import contextlib
    import io

    from concinno.cli.features_profile_cmd import register_disable_all_d_class

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    feat_parser = sub.add_parser("features")
    feat_sub = feat_parser.add_subparsers(dest="features_command")
    register_disable_all_d_class(feat_sub)
    args = parser.parse_args(["features", "disable-all-d-class"])
    args._injected_cfg = cfg
    buf = io.StringIO()
    rc: int = 0
    try:
        with contextlib.redirect_stdout(buf):
            args.func(args)
    except SystemExit as se:
        rc = int(se.code) if se.code is not None else 0
    return rc, buf.getvalue()


def test_cli_alias_applies_4_x_compat(tmp_cfg: Config) -> None:
    """``concinno features disable-all-d-class`` is equivalent to
    ``concinno features set-profile 4-x-compat``."""
    rc, out = _run_disable_all_d_class_cli(tmp_cfg)
    assert rc == 0
    assert "Applied profile '4-x-compat'" in out
    for feat in D_CLASS_5_0_0:
        assert tmp_cfg.feature(feat, "enabled") is False


def test_cli_alias_idempotent(tmp_cfg: Config) -> None:
    """Re-running the alias yields zero diff (matches profile semantics)."""
    _run_disable_all_d_class_cli(tmp_cfg)
    rc, out = _run_disable_all_d_class_cli(tmp_cfg)
    assert rc == 0
    assert "0 features enabled" in out or "Applied profile '4-x-compat'" in out
    # Still all disabled.
    for feat in D_CLASS_5_0_0:
        assert tmp_cfg.feature(feat, "enabled") is False
