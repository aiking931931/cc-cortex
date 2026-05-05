"""Regression test — user values in ~/.concinno/*.json survive upgrades.

Covers concinno.config_preservation: preserve_user_values +
safe_write_config + assert_preservation_invariant. The guarantee:
``pip install --upgrade concinno`` NEVER resets a user-set value.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from concinno.config_preservation import (
    ConfigPreservationError,
    assert_preservation_invariant,
    merge_and_write,
    preserve_user_values,
    read_user_config,
    safe_write_config,
)

# ── preserve_user_values ───────────────────────────────────────────


class TestPreserveUserValues:
    def test_user_scalar_wins_over_new_default(self) -> None:
        existing = {"disabled": True}
        new_defaults = {"disabled": False}
        out = preserve_user_values(existing, new_defaults)
        assert out["disabled"] is True, (
            "user opt-out must survive a default flip in the new version"
        )

    def test_new_key_added_without_touching_existing(self) -> None:
        existing = {"disabled": True, "mode": "askuser_answer"}
        new_defaults = {"disabled": False, "mode": "string_match", "new_knob": 42}
        out = preserve_user_values(existing, new_defaults)
        assert out["disabled"] is True
        assert out["mode"] == "askuser_answer"
        assert out["new_knob"] == 42

    def test_nested_dict_recurses(self) -> None:
        existing = {"feature": {"enabled": False}}
        new_defaults = {"feature": {"enabled": True, "threshold": 100}}
        out = preserve_user_values(existing, new_defaults)
        assert out["feature"]["enabled"] is False  # user opt-out survives
        assert out["feature"]["threshold"] == 100  # new default added

    def test_user_key_not_in_defaults_is_kept(self) -> None:
        """Forward-compat: user may have keys a newer version doesn't know."""
        existing = {"custom_key": "user-set"}
        new_defaults = {"new_key": "default"}
        out = preserve_user_values(existing, new_defaults)
        assert out["custom_key"] == "user-set"
        assert out["new_key"] == "default"

    def test_list_treated_as_scalar(self) -> None:
        """User owns the list wholesale — no merge."""
        existing = {"allow": ["pattern1"]}
        new_defaults = {"allow": ["pattern2", "pattern3"]}
        out = preserve_user_values(existing, new_defaults)
        assert out["allow"] == ["pattern1"]

    def test_input_dicts_not_mutated(self) -> None:
        existing = {"disabled": True}
        new_defaults = {"disabled": False, "new": 1}
        preserve_user_values(existing, new_defaults)
        assert existing == {"disabled": True}
        assert new_defaults == {"disabled": False, "new": 1}

    def test_empty_existing_returns_defaults(self) -> None:
        out = preserve_user_values({}, {"a": 1, "b": 2})
        assert out == {"a": 1, "b": 2}

    def test_non_dict_inputs_handled(self) -> None:
        assert preserve_user_values("not a dict", {"a": 1}) == {"a": 1}  # type: ignore[arg-type]
        assert preserve_user_values({"a": 1}, "not a dict") == {"a": 1}  # type: ignore[arg-type]


# ── safe_write_config + read_user_config ───────────────────────────


class TestSafeWriteConfig:
    def test_atomic_write_creates_file(self, tmp_path: Path) -> None:
        target = tmp_path / "c.json"
        safe_write_config(target, {"disabled": True})
        assert target.is_file()
        data, warn = read_user_config(target)
        assert warn is None
        assert data == {"disabled": True}

    def test_creates_parent_dir(self, tmp_path: Path) -> None:
        target = tmp_path / "deep" / "nested" / "c.json"
        safe_write_config(target, {"x": 1})
        assert target.is_file()

    def test_rotates_backups(self, tmp_path: Path) -> None:
        target = tmp_path / "c.json"
        safe_write_config(target, {"v": 1})
        safe_write_config(target, {"v": 2})
        bak1 = target.with_suffix(target.suffix + ".bak.1")
        assert bak1.is_file()
        assert json.loads(bak1.read_text(encoding="utf-8")) == {"v": 1}


class TestReadUserConfig:
    def test_missing_file_is_empty_dict_no_warning(self, tmp_path: Path) -> None:
        data, warn = read_user_config(tmp_path / "nonexistent.json")
        assert data == {}
        assert warn is None

    def test_malformed_json_returns_empty_with_warning(self, tmp_path: Path) -> None:
        target = tmp_path / "bad.json"
        target.write_text("{ not valid json", encoding="utf-8")
        data, warn = read_user_config(target)
        assert data == {}
        assert warn is not None
        assert "malformed" in warn.lower()
        # User file MUST stay untouched (fail-safe policy).
        assert target.read_text(encoding="utf-8") == "{ not valid json"

    def test_non_object_json_returns_empty_with_warning(self, tmp_path: Path) -> None:
        target = tmp_path / "list.json"
        target.write_text("[1, 2, 3]", encoding="utf-8")
        data, warn = read_user_config(target)
        assert data == {}
        assert warn is not None


# ── assert_preservation_invariant ─────────────────────────────────


class TestPreservationInvariant:
    def test_identical_passes(self) -> None:
        assert_preservation_invariant({"a": 1}, {"a": 1})

    def test_drift_on_untouched_key_raises(self) -> None:
        with pytest.raises(ConfigPreservationError) as exc:
            assert_preservation_invariant({"disabled": True}, {"disabled": False})
        assert "disabled" in str(exc.value)

    def test_touched_key_allowed_to_drift(self) -> None:
        # version bump is allowed to touch the schema_version key
        assert_preservation_invariant(
            {"schema_version": 1, "disabled": True},
            {"schema_version": 2, "disabled": True},
            touched_keys=["schema_version"],
        )

    def test_nested_drift_detected(self) -> None:
        with pytest.raises(ConfigPreservationError) as exc:
            assert_preservation_invariant(
                {"feature": {"enabled": True}},
                {"feature": {"enabled": False}},
            )
        assert "feature.enabled" in str(exc.value)

    def test_key_removal_detected(self) -> None:
        with pytest.raises(ConfigPreservationError):
            assert_preservation_invariant({"old_key": "val"}, {})


# ── Upgrade simulation (the canonical invariant) ───────────────────


class TestUpgradeSimulation:
    def test_release_auth_disabled_true_survives_upgrade(self, tmp_path: Path) -> None:
        """Simulate 1.0 → 2.16.0 upgrade for release_auth.json."""
        cfg = tmp_path / "release_auth.json"
        safe_write_config(cfg, {"disabled": True, "mode": "askuser_answer"})

        # 2.16.0 ships new defaults.
        new_defaults = {
            "disabled": False,
            "mode": "string_match",
            "new_feature_2_16": "default_value",
        }
        merged, warn = merge_and_write(cfg, new_defaults)
        assert warn is None
        assert merged["disabled"] is True, "USER OPT-OUT SURVIVES"
        assert merged["mode"] == "askuser_answer"
        assert merged["new_feature_2_16"] == "default_value"

        # Reload from disk to confirm persistence.
        reloaded, _ = read_user_config(cfg)
        assert reloaded == merged

    def test_destruction_guard_extra_pattern_preserved(
        self, tmp_path: Path,
    ) -> None:
        cfg = tmp_path / "destruction_guard.json"
        safe_write_config(
            cfg,
            {"enabled": True, "extra_pattern": "rm -rf /tmp/*"},
        )
        new_defaults = {"enabled": True, "risk_level": "max"}
        merged, _ = merge_and_write(cfg, new_defaults)
        assert merged["extra_pattern"] == "rm -rf /tmp/*"
        assert merged["risk_level"] == "max"

    def test_new_key_fresh_on_first_load(self, tmp_path: Path) -> None:
        """session_switches.json doesn't exist pre-2.16 — must be created clean."""
        cfg = tmp_path / "session_switches.json"
        assert not cfg.exists()
        new_defaults = {"enabled": True, "top_n": 10}
        merged, warn = merge_and_write(cfg, new_defaults)
        assert warn is None
        assert merged == new_defaults
        assert cfg.is_file()

    def test_corrupted_file_kept_untouched(self, tmp_path: Path) -> None:
        cfg = tmp_path / "bad.json"
        cfg.write_text("totally broken}", encoding="utf-8")
        new_defaults = {"enabled": True}
        merged, warn = merge_and_write(cfg, new_defaults)
        # Defaults returned in memory:
        assert merged == new_defaults
        # Warning emitted:
        assert warn is not None and "malformed" in warn.lower()
        # File NOT touched (fail-safe):
        assert cfg.read_text(encoding="utf-8") == "totally broken}"

    def test_empty_dir_lazy_creates_files(self, tmp_path: Path) -> None:
        """No files exist — merge_and_write creates them on first load."""
        cfg = tmp_path / "release_auth.json"
        assert not cfg.exists()
        new_defaults = {"disabled": False, "mode": "string_match"}
        merged, _ = merge_and_write(cfg, new_defaults)
        assert cfg.is_file()
        assert merged == new_defaults

    def test_all_existing_keys_held_constant_across_upgrade(
        self, tmp_path: Path,
    ) -> None:
        """The invariant: every user-set key must match before/after."""
        cfg = tmp_path / "release_auth.json"
        before = {"disabled": True, "mode": "askuser_answer", "extra": "user"}
        safe_write_config(cfg, before)

        new_defaults = {
            "disabled": False,
            "mode": "string_match",
            "new_2_16_only": "default",
        }
        after, _ = merge_and_write(cfg, new_defaults)

        # touched_keys = keys the upgrade is allowed to have ADDED
        # (not modify existing ones).
        assert_preservation_invariant(before, after, touched_keys=["new_2_16_only"])
