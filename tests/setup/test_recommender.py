"""Tests for ``concinno.setup.recommender`` and ``concinno setup`` CLI."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import pytest

from concinno.cli.setup_cmd import cmd_setup, register
from concinno.setup.recommender import PROFILES, Profile, apply, recommend


def test_five_profiles_registered() -> None:
    """The catalogue ships exactly the five canonical user types."""
    assert set(PROFILES.keys()) == {
        "senior",
        "junior",
        "benchmark",
        "production",
        "researcher",
    }
    for profile in PROFILES.values():
        assert isinstance(profile, Profile)
        assert profile.feature_overrides, (
            f"profile {profile.name!r} must override at least one feature"
        )


def test_each_profile_has_destruction_guard_on() -> None:
    """DestructionGuard is the one rail no profile may opt out of."""
    for name, profile in PROFILES.items():
        guard = profile.feature_overrides.get("destruction_guard")
        assert guard is not None, f"{name} missing destruction_guard"
        assert guard.get("enabled") is True, f"{name} disabled destruction_guard"


def test_recommend_returns_serializable_dict() -> None:
    """``recommend()`` output round-trips through ``json.dumps``."""
    out = recommend("senior")
    assert out["profile"] == "senior"
    assert "description" in out
    assert isinstance(out["features"], dict)
    assert isinstance(out["notes"], list)
    # Round-trip through JSON to confirm serializability.
    encoded = json.dumps(out)
    assert json.loads(encoded) == out


def test_recommend_unknown_profile_raises() -> None:
    """Bad profile names raise ``ValueError`` with the catalogue listed."""
    with pytest.raises(ValueError) as excinfo:
        recommend("nonexistent")
    msg = str(excinfo.value)
    assert "nonexistent" in msg
    assert "senior" in msg  # catalogue surfaced for the user


def test_apply_dry_run_does_not_write(tmp_path: Path) -> None:
    """``dry_run=True`` must leave the target file absent."""
    target = tmp_path / "cc_config.json"
    diff = apply("benchmark", dry_run=True, config_path=target)
    assert diff["dry_run"] is True
    assert diff["profile"] == "benchmark"
    assert not target.exists(), "dry-run accidentally wrote to disk"
    assert isinstance(diff["changed"], list)
    assert "destruction_guard" in diff["changed"]


def test_apply_persists_and_round_trips(tmp_path: Path) -> None:
    """Real apply writes; re-applying same profile yields zero new changes."""
    target = tmp_path / "cc_config.json"
    first = apply("production", dry_run=False, config_path=target)
    assert target.exists()
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert "features" in payload
    assert payload["features"]["release_authorization"]["enabled"] is True
    assert first["changed"], "first apply produced no changes"

    # Re-apply: nothing further should change.
    second = apply("production", dry_run=False, config_path=target)
    assert second["changed"] == []
    assert second["before"] == second["after"]


def test_apply_preserves_unrelated_top_level_keys(tmp_path: Path) -> None:
    """Apply merges ``features`` without touching sibling top-level keys."""
    target = tmp_path / "cc_config.json"
    target.write_text(
        json.dumps(
            {
                "modules": {"core": True},
                "features": {"unrelated": {"enabled": True, "param": 7}},
            }
        ),
        encoding="utf-8",
    )
    apply("researcher", dry_run=False, config_path=target)
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["modules"] == {"core": True}, "siblings clobbered"
    assert payload["features"]["unrelated"] == {"enabled": True, "param": 7}, (
        "untouched feature mutated"
    )
    assert payload["features"]["ziq_autotune"]["enabled"] is True


def test_cli_list_prints_all_five_profiles(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``concinno setup --list`` enumerates every profile name."""
    parser = argparse.ArgumentParser()
    register(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["setup", "--list"])
    cmd_setup(args)
    out = capsys.readouterr().out
    for name in ("senior", "junior", "benchmark", "production", "researcher"):
        assert name in out, f"--list omitted profile {name!r}"


def test_cli_list_json_round_trips(capsys: pytest.CaptureFixture[str]) -> None:
    """``--list --format=json`` returns parseable JSON listing five names."""
    parser = argparse.ArgumentParser()
    register(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["setup", "--list", "--format=json"])
    cmd_setup(args)
    payload = json.loads(capsys.readouterr().out)
    names = [p["name"] for p in payload["profiles"]]
    assert sorted(names) == sorted(PROFILES.keys())


def test_cli_unknown_profile_exits_nonzero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Argparse rejects unknown ``--profile`` values via ``choices``."""
    parser = argparse.ArgumentParser()
    register(parser.add_subparsers(dest="command"))
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["setup", "--profile=does-not-exist"])
    assert excinfo.value.code != 0


def test_cli_module_invocation_lists_profiles() -> None:
    """``python -m concinno setup --list`` must exit 0 and list five."""
    proc = subprocess.run(
        [sys.executable, "-m", "concinno", "setup", "--list"],
        capture_output=True,
        text=False,
        timeout=30,
    )
    assert proc.returncode == 0, f"stderr={proc.stderr!r}"
    stdout = (proc.stdout or b"").decode("utf-8", errors="replace")
    for name in ("senior", "junior", "benchmark", "production", "researcher"):
        assert name in stdout, f"missing profile {name!r} in CLI output"
