"""Regression tests for ``concinno.cli._first_run`` — onboarding banner.

Each test redirects ``HOME`` to a tmp_path so the developer's real
``~/.concinno/.4_0_0_seen`` is never touched.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Redirect HOME so marker writes land in tmp_path."""
    monkeypatch.setenv("HOME", str(tmp_path))
    # Windows resolution falls back through USERPROFILE when HOME is
    # absent; in CI on Windows we need to override it too.
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    yield


def test_first_run_prints_banner(capsys: pytest.CaptureFixture[str]) -> None:
    from concinno.cli._first_run import maybe_print_first_run_banner

    printed = maybe_print_first_run_banner()
    captured = capsys.readouterr()

    assert printed is True
    assert "Welcome to concinno" in captured.out
    assert "set-profile strict" in captured.out


def test_subsequent_run_silent(capsys: pytest.CaptureFixture[str]) -> None:
    from concinno.cli._first_run import maybe_print_first_run_banner

    first = maybe_print_first_run_banner()
    capsys.readouterr()  # discard first-call output
    second = maybe_print_first_run_banner()
    captured = capsys.readouterr()

    assert first is True
    assert second is False
    assert captured.out == ""


def test_marker_creation_failure_still_prints(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """If the marker cannot be created, banner still prints (best-effort)."""
    from concinno.cli import _first_run

    # Force the marker parent into a path that mkdir / touch will fail on
    # — point HOME at a non-directory file.
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("blocker", encoding="utf-8")
    monkeypatch.setenv("HOME", str(blocker))
    monkeypatch.setenv("USERPROFILE", str(blocker))

    printed = _first_run.maybe_print_first_run_banner()
    captured = capsys.readouterr()

    assert printed is True
    assert "Welcome to concinno" in captured.out


def test_banner_includes_version(capsys: pytest.CaptureFixture[str]) -> None:
    from concinno import __version__
    from concinno.cli._first_run import maybe_print_first_run_banner

    maybe_print_first_run_banner()
    captured = capsys.readouterr()

    assert __version__ in captured.out


def test_marker_path_resolves_under_home(tmp_path: Path) -> None:
    from concinno.cli._first_run import _marker_path

    # HOME was set to tmp_path by the autouse fixture.
    expected = tmp_path / ".concinno" / ".4_0_0_seen"
    assert _marker_path() == expected
    # Sanity: relative to HOME.
    assert str(tmp_path) in str(_marker_path())
    # The path must be hidden (starts with dot).
    assert _marker_path().name.startswith(".")
    # And the env-driven HOME must have been honoured.
    assert os.environ["HOME"] == str(tmp_path)
