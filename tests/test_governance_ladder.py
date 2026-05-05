"""Tests for aiking.governance.ladder — 4-tier governance opt-in ladder.

Unit tests: tier values, LadderConfig, select_tier, apply_tier.
Integration: C0 radius → tier chain, record_outcome FTRL write.
CLI: set-tier persist + read-back.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ── Imports under test ────────────────────────────────────────────────────
from aiking.governance.ladder import (
    TIER_GUARDS,
    GovernanceTier,
    LadderConfig,
    _load_persisted_tier,
    apply_tier,
    clear_tier_override,
    get_ladder_config,
    record_outcome,
    save_tier_override,
    select_tier,
)

# ── Unit: tier enum values ─────────────────────────────────────────────────

class TestGovernanceTierEnum:
    def test_four_values(self) -> None:
        assert set(GovernanceTier) == {
            GovernanceTier.OFF,
            GovernanceTier.LITE,
            GovernanceTier.FULL,
            GovernanceTier.MAX,
        }

    def test_string_values(self) -> None:
        assert GovernanceTier.OFF.value == "off"
        assert GovernanceTier.LITE.value == "lite"
        assert GovernanceTier.FULL.value == "full"
        assert GovernanceTier.MAX.value == "max"

    def test_construction_from_string(self) -> None:
        assert GovernanceTier("lite") is GovernanceTier.LITE

    def test_invalid_raises(self) -> None:
        with pytest.raises(ValueError):
            GovernanceTier("unknown")


# ── Unit: LadderConfig dataclass ──────────────────────────────────────────

class TestLadderConfig:
    def test_fields_present(self) -> None:
        cfg = LadderConfig(
            tier=GovernanceTier.LITE,
            guards=TIER_GUARDS[GovernanceTier.LITE],
            ziq_arm="lite",
        )
        assert cfg.tier is GovernanceTier.LITE
        assert cfg.ziq_arm == "lite"
        assert isinstance(cfg.guards, frozenset)

    def test_to_dict_keys(self) -> None:
        cfg = LadderConfig(
            tier=GovernanceTier.FULL,
            guards=TIER_GUARDS[GovernanceTier.FULL],
            ziq_arm="full",
            radius="complex",
        )
        d = cfg.to_dict()
        assert d["tier"] == "full"
        assert d["radius"] == "complex"
        assert isinstance(d["guards"], list)

    def test_destruction_guard_always_in_full(self) -> None:
        # destruction_guard is added by apply_tier, not in TIER_GUARDS directly
        result = apply_tier(GovernanceTier.OFF)
        assert result.get("destruction_guard") is True


# ── Unit: select_tier mapping per radius ─────────────────────────────────

class TestSelectTier:
    # v6.1 Plan B Cross-cutting #1 (2026-05-03): default OFF for all radii.
    # User opts in to LITE/FULL/MAX explicitly via override. Only
    # destruction_guard R0-R4 stays hardcoded always-on.
    def test_simple_defaults_off(self) -> None:
        assert select_tier("simple") is GovernanceTier.OFF

    def test_complicated_defaults_off(self) -> None:
        assert select_tier("complicated") is GovernanceTier.OFF

    def test_complex_defaults_off(self) -> None:
        assert select_tier("complex") is GovernanceTier.OFF

    def test_chaotic_defaults_off(self) -> None:
        assert select_tier("chaotic") is GovernanceTier.OFF

    def test_user_override_wins_for_any_radius(self) -> None:
        # Override applies regardless of radius (including chaotic which used
        # to auto-select MAX prior to v6.1).
        assert select_tier("simple",   user_override=GovernanceTier.MAX) is GovernanceTier.MAX
        assert select_tier("chaotic",  user_override=GovernanceTier.LITE) is GovernanceTier.LITE

    def test_persisted_override_wins(self, tmp_path: Path) -> None:
        override_file = tmp_path / "governance_tier.json"
        override_file.write_text(json.dumps({"tier": "full"}), encoding="utf-8")
        tier = _load_persisted_tier(override_file)
        assert tier is GovernanceTier.FULL

    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        assert _load_persisted_tier(tmp_path / "missing.json") is None

    def test_corrupt_file_returns_none(self, tmp_path: Path) -> None:
        p = tmp_path / "governance_tier.json"
        p.write_text("not json", encoding="utf-8")
        assert _load_persisted_tier(p) is None

    def test_invalid_tier_in_file_returns_none(self, tmp_path: Path) -> None:
        p = tmp_path / "governance_tier.json"
        p.write_text(json.dumps({"tier": "unknown"}), encoding="utf-8")
        assert _load_persisted_tier(p) is None


# ── Unit: apply_tier guard whitelist ─────────────────────────────────────

class TestApplyTier:
    def test_off_only_destruction_guard(self) -> None:
        result = apply_tier(GovernanceTier.OFF)
        # Only destruction_guard should be True; no other guards
        assert result == {"destruction_guard": True}

    def test_lite_includes_cbua_guards(self) -> None:
        result = apply_tier(GovernanceTier.LITE)
        assert result["cbua_pipeline_guard"] is True
        assert result["butterfly_guard"] is True
        assert result["premise_gate"] is True
        assert result["destruction_guard"] is True

    def test_full_includes_sentinel_and_consecutive(self) -> None:
        result = apply_tier(GovernanceTier.FULL)
        assert result["sentinel_gate"] is True
        assert result["consecutive_fail_gate"] is True
        assert result["redblue_green_dispatch_guard"] is True
        # Lite guards also present (cumulative)
        assert result["cbua_pipeline_guard"] is True

    def test_max_includes_opus_dispatch(self) -> None:
        result = apply_tier(GovernanceTier.MAX)
        assert result["redteam_opus_dispatch"] is True
        # All lower tiers present
        assert result["sentinel_gate"] is True
        assert result["cbua_pipeline_guard"] is True

    def test_destruction_guard_always_true_for_all_tiers(self) -> None:
        for tier in GovernanceTier:
            assert apply_tier(tier)["destruction_guard"] is True


# ── Unit: record_outcome FTRL write ──────────────────────────────────────

class TestRecordOutcome:
    def test_ftrl_write_called(self) -> None:
        """record_outcome must call record_ftrl_update with correct args."""
        mock_load = MagicMock(return_value={"lite": 1.2})
        mock_record = MagicMock()
        with (
            patch("aiking.governance.ladder.load_ftrl_state", mock_load),
            patch("aiking.governance.ladder.record_ftrl_update", mock_record),
        ):
            record_outcome(GovernanceTier.LITE, reward=1.0)

        mock_load.assert_called_once_with("governance_ladder")
        mock_record.assert_called_once()
        call_kwargs = mock_record.call_args
        assert call_kwargs.args[0] == "governance_ladder"
        assert call_kwargs.args[1] == "lite"
        assert call_kwargs.kwargs["signal"] == pytest.approx(1.0)

    def test_ftrl_unavailable_does_not_raise(self) -> None:
        """record_outcome must not raise if ZIQ persist is missing."""
        with patch.dict(sys.modules, {
            "concinno.ziq.persist": None,  # type: ignore[dict-item]
        }):
            # Should not raise
            record_outcome(GovernanceTier.LITE, reward=-2.0)

    def test_weight_increases_on_positive_reward(self) -> None:
        captured: dict = {}

        def _fake_record(feature, key, *, weight_before, weight_after, signal=None):
            captured["weight_before"] = weight_before
            captured["weight_after"] = weight_after

        mock_load = MagicMock(return_value={"full": 1.0})
        with (
            patch("aiking.governance.ladder.load_ftrl_state", mock_load),
            patch("aiking.governance.ladder.record_ftrl_update", _fake_record),
        ):
            record_outcome(GovernanceTier.FULL, reward=1.0)

        assert captured["weight_after"] > captured["weight_before"]


# ── Integration: C0 radius → tier selection real chain ───────────────────

class TestC0Integration:
    def test_radius_to_tier_mapping_complete(self) -> None:
        """All four radii must map to a valid GovernanceTier."""
        for radius in ("simple", "complicated", "complex", "chaotic"):
            tier = select_tier(radius)  # type: ignore[arg-type]
            assert isinstance(tier, GovernanceTier)

    def test_get_ladder_config_default_off(self) -> None:
        cfg = get_ladder_config("complex")
        assert cfg.tier is GovernanceTier.OFF
        assert cfg.radius == "complex"
        # OFF tier has no guards in TIER_GUARDS (destruction_guard added by apply_tier).
        assert cfg.guards == frozenset()

    def test_get_ladder_config_with_override(self) -> None:
        cfg = get_ladder_config("complex", user_override=GovernanceTier.FULL)
        assert cfg.tier is GovernanceTier.FULL
        assert "sentinel_gate" in cfg.guards
        assert cfg.user_override is True

    def test_c0_result_has_governance_tier(self) -> None:
        """C0Router.classify() must populate governance_tier on C0Result."""
        from aiking.governance.c0_router import C0Router
        router = C0Router()
        result = router.classify("write a quick hello world script")
        assert hasattr(result, "governance_tier")
        assert result.governance_tier in ("off", "lite", "full", "max")

    def test_governance_tier_in_signals(self) -> None:
        """governance_tier must appear in signals dict for auditability."""
        from aiking.governance.c0_router import C0Router
        router = C0Router()
        result = router.classify("refactor the entire authentication module")
        assert "governance_tier" in result.signals


# ── CLI: set-tier persist + read-back ────────────────────────────────────

class TestCLISetTier:
    def test_save_and_load_roundtrip(self, tmp_path: Path) -> None:
        p = tmp_path / "governance_tier.json"
        save_tier_override(GovernanceTier.MAX, path=p)
        loaded = _load_persisted_tier(path=p)
        assert loaded is GovernanceTier.MAX

    def test_clear_removes_file(self, tmp_path: Path) -> None:
        p = tmp_path / "governance_tier.json"
        save_tier_override(GovernanceTier.FULL, path=p)
        assert p.exists()
        removed = clear_tier_override(path=p)
        assert removed is True
        assert not p.exists()

    def test_clear_nonexistent_returns_false(self, tmp_path: Path) -> None:
        p = tmp_path / "missing.json"
        assert clear_tier_override(path=p) is False

    def test_saved_file_is_valid_json(self, tmp_path: Path) -> None:
        p = tmp_path / "governance_tier.json"
        save_tier_override(GovernanceTier.LITE, path=p)
        data = json.loads(p.read_text(encoding="utf-8"))
        assert data["tier"] == "lite"
