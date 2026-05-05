"""Tests for ``concinno skills ...`` CLI (2.30.1)."""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture()
def isolated_home(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    (fake_home / ".claude" / "skills").mkdir(parents=True)
    (fake_home / ".concinno").mkdir(parents=True)
    monkeypatch.setattr("pathlib.Path.home", lambda: fake_home)
    monkeypatch.chdir(tmp_path)
    return fake_home


def _run(*argv: str) -> int:
    import argparse

    from concinno.cli.skills_cmd import register
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    register(sub)
    # `skills` is the parent subparser; argv begins with "skills"
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except SystemExit as se:
        return int(se.code)
    return 0


def test_new_no_interactive_creates_file(
    isolated_home: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    code = _run(
        "skills", "new", "hydrate_reminder",
        "--description", "Nudges me to drink water every 45 minutes",
        "--triggers", "hydrate,water,break",
        "--user-invocable", "true",
        "--scope", "user",
        "--body-template", "minimal",
        "--no-interactive",
    )
    assert code == 0
    target = isolated_home / ".claude" / "skills" / "user" / "hydrate_reminder" / "SKILL.md"
    assert target.is_file()
    body = target.read_text(encoding="utf-8")
    assert "name: hydrate_reminder" in body
    assert "Nudges me to drink water" in body
    assert "hydrate" in body
    assert "user-invocable: true" in body


def test_new_refuses_missing_description(
    isolated_home: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    code = _run(
        "skills", "new", "needs_desc",
        "--no-interactive",
    )
    assert code == 2


def test_new_refuses_clobber_without_force(isolated_home: Path) -> None:
    args_common = [
        "skills", "new", "twice",
        "--description", "x",
        "--no-interactive",
    ]
    assert _run(*args_common) == 0
    # Second run without --force must refuse
    assert _run(*args_common) == 3


def test_new_force_overwrites(isolated_home: Path) -> None:
    args_common = [
        "skills", "new", "dup",
        "--description", "first",
        "--no-interactive",
    ]
    assert _run(*args_common) == 0
    assert _run(*args_common, "--force", "--description", "second") == 0
    body = (
        isolated_home / ".claude" / "skills" / "user" / "dup" / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert "second" in body


def test_new_dry_run_does_not_write(
    isolated_home: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    code = _run(
        "skills", "new", "ghost",
        "--description", "x",
        "--no-interactive",
        "--dry-run",
    )
    assert code == 0
    target = isolated_home / ".claude" / "skills" / "user" / "ghost" / "SKILL.md"
    assert not target.exists()
    out = capsys.readouterr().out
    assert "[dry-run]" in out


def test_list_shows_created_skill(
    isolated_home: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    _run("skills", "new", "listed_one",
         "--description", "x", "--no-interactive")
    code = _run("skills", "list")
    assert code == 0
    out = capsys.readouterr().out
    assert "listed_one" in out


def test_enable_disable_updates_state(isolated_home: Path) -> None:
    import json
    _run("skills", "new", "toggleable",
         "--description", "x", "--no-interactive")
    _run("skills", "disable", "toggleable")
    state_path = isolated_home / ".concinno" / "skills.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["toggleable"]["enabled"] is False
    _run("skills", "enable", "toggleable")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["toggleable"]["enabled"] is True


def test_delete_removes_dir(isolated_home: Path) -> None:
    _run("skills", "new", "erase_me",
         "--description", "x", "--no-interactive")
    target = isolated_home / ".claude" / "skills" / "user" / "erase_me"
    assert target.is_dir()
    _run("skills", "delete", "erase_me")
    assert not target.exists()


def test_new_rejects_invalid_name(isolated_home: Path) -> None:
    code = _run(
        "skills", "new", "bad name!",
        "--description", "x",
        "--no-interactive",
    )
    assert code == 2
