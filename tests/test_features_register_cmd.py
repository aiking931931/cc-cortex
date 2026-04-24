"""Tests for ``concinno features register/unregister/list-user`` CLI (2.30.1)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture()
def isolated_home(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    (fake_home / ".concinno").mkdir(parents=True)
    monkeypatch.setattr("pathlib.Path.home", lambda: fake_home)
    return fake_home


def _run(*argv: str) -> int:
    import argparse

    from concinno.cli.features_register_cmd import register_features_subcommands
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    features_parser = sub.add_parser("features")
    features_sub = features_parser.add_subparsers(dest="features_command")
    register_features_subcommands(features_sub)
    args = parser.parse_args(argv)
    try:
        rc = args.func(args)
    except SystemExit as se:
        return int(se.code) if se.code is not None else 0
    return int(rc) if rc is not None else 0


def test_register_via_params_json(
    isolated_home: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    from concinno.user_features import load_user_features, user_features_path

    entry = {
        "category": "user_gate",
        "description": "Test",
        "enabled": True,
        "ziq_autotunable": False,
        "cosmetic": False,
    }
    code = _run(
        "features", "register", "my_feature",
        "--params-json", json.dumps(entry),
        "--no-interactive",
    )
    assert code == 0
    assert user_features_path().is_file()
    loaded = load_user_features()
    assert loaded["my_feature"]["category"] == "user_gate"


def test_register_no_interactive_flag_form(isolated_home: Path) -> None:
    from concinno.user_features import load_user_features

    code = _run(
        "features", "register", "flag_feature",
        "--category", "user_info",
        "--description", "Via flags",
        "--no-interactive",
    )
    assert code == 0
    loaded = load_user_features()
    assert loaded["flag_feature"]["description"] == "Via flags"
    # enabled defaults to True
    assert loaded["flag_feature"]["enabled"] is True


def test_register_missing_required_no_interactive(isolated_home: Path) -> None:
    code = _run(
        "features", "register", "missing_meta",
        "--no-interactive",
    )
    assert code == 2


def test_register_dry_run_writes_nothing(
    isolated_home: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    from concinno.user_features import load_user_features

    code = _run(
        "features", "register", "ghost",
        "--category", "user_gate",
        "--description", "Never saved",
        "--no-interactive",
        "--dry-run",
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "[dry-run]" in out
    assert load_user_features() == {}


def test_register_collision_requires_force(isolated_home: Path) -> None:
    """Registering a name that collides with FEATURE_META must fail
    without --force in --no-interactive mode."""
    from concinno.feature_config import FEATURE_META

    shipped = next(iter(FEATURE_META))
    code = _run(
        "features", "register", shipped,
        "--category", "user_gate",
        "--description", "Collides",
        "--no-interactive",
    )
    assert code == 3  # collision rejected


def test_register_collision_force_succeeds(isolated_home: Path) -> None:
    from concinno.feature_config import FEATURE_META
    from concinno.user_features import load_user_features

    shipped = next(iter(FEATURE_META))
    code = _run(
        "features", "register", shipped,
        "--category", "user_gate",
        "--description", "Forced",
        "--force",
        "--no-interactive",
    )
    assert code == 0
    # Entry is in user file (even though shipped wins in merge)
    loaded = load_user_features()
    assert shipped in loaded


def test_register_invalid_name_rejected(isolated_home: Path) -> None:
    code = _run(
        "features", "register", "has space",
        "--category", "user_gate",
        "--description", "invalid",
        "--no-interactive",
    )
    assert code == 2


def test_unregister_removes_entry(isolated_home: Path) -> None:
    from concinno.user_features import load_user_features

    _run(
        "features", "register", "shortlived",
        "--category", "user_info",
        "--description", "brief",
        "--no-interactive",
    )
    assert "shortlived" in load_user_features()
    code = _run("features", "unregister", "shortlived")
    assert code == 0
    assert "shortlived" not in load_user_features()


def test_list_user_empty(
    isolated_home: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    code = _run("features", "list-user")
    assert code == 0
    out = capsys.readouterr().out
    assert "(no user features" in out


def test_list_user_shows_entries(
    isolated_home: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    _run(
        "features", "register", "a_feat",
        "--category", "user_info",
        "--description", "Alpha",
        "--no-interactive",
    )
    _run(
        "features", "register", "b_feat",
        "--category", "user_info",
        "--description", "Bravo",
        "--no-interactive",
    )
    code = _run("features", "list-user")
    assert code == 0
    out = capsys.readouterr().out
    assert "a_feat" in out
    assert "b_feat" in out
