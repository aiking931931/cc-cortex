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
    # Banner now goes to stderr so stdout pipelines (jq, grep …) stay clean.
    assert "Welcome to concinno" in captured.err
    assert "set-profile strict" in captured.err
    assert captured.out == ""


def test_subsequent_run_silent(capsys: pytest.CaptureFixture[str]) -> None:
    from concinno.cli._first_run import maybe_print_first_run_banner

    first = maybe_print_first_run_banner()
    capsys.readouterr()  # discard first-call output
    second = maybe_print_first_run_banner()
    captured = capsys.readouterr()

    assert first is True
    assert second is False
    assert captured.out == ""
    assert captured.err == ""


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
    assert "Welcome to concinno" in captured.err


def test_banner_includes_version(capsys: pytest.CaptureFixture[str]) -> None:
    from concinno import __version__
    from concinno.cli._first_run import maybe_print_first_run_banner

    maybe_print_first_run_banner()
    captured = capsys.readouterr()

    assert __version__ in captured.err


# ── Env-var killswitch (CONCINNO_FIRST_RUN_BANNER=0) ─────────────────


@pytest.mark.parametrize("falsy", ["0", "false", "FALSE", "no", "off"])
def test_env_killswitch_suppresses_banner(
    falsy: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``CONCINNO_FIRST_RUN_BANNER`` falsy values silence the banner.

    Critically: the marker is NOT touched, so a real first run on the
    same machine after unsetting the env var will still show it.
    """
    from concinno.cli._first_run import (
        marker_exists,
        maybe_print_first_run_banner,
    )

    monkeypatch.setenv("CONCINNO_FIRST_RUN_BANNER", falsy)
    printed = maybe_print_first_run_banner()
    captured = capsys.readouterr()

    assert printed is False
    assert captured.err == ""
    assert captured.out == ""
    # Marker untouched so a later real run can still see the banner.
    assert marker_exists() is False


def test_env_truthy_does_not_suppress(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Any non-falsy value keeps the banner active (no surprise gating)."""
    from concinno.cli._first_run import maybe_print_first_run_banner

    monkeypatch.setenv("CONCINNO_FIRST_RUN_BANNER", "1")
    printed = maybe_print_first_run_banner()
    captured = capsys.readouterr()

    assert printed is True
    assert "Welcome to concinno" in captured.err


# ── Chicken-and-egg: set-profile invocation must not trigger banner ──


def test_set_profile_argv_skips_banner(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Invoking ``concinno features set-profile <x>`` must NOT show the
    banner (chicken-and-egg). The set-profile command itself touches
    the marker so subsequent invocations (any subcommand) are silent.
    """
    from concinno.cli._first_run import (
        marker_exists,
        maybe_print_first_run_banner,
        should_skip_banner_for_argv,
    )

    monkeypatch.setattr(
        "sys.argv",
        ["concinno", "features", "set-profile", "permissive"],
    )
    assert should_skip_banner_for_argv() is True

    printed = maybe_print_first_run_banner()
    captured = capsys.readouterr()
    assert printed is False
    assert captured.err == ""
    # Crucially, the banner-side-effect did NOT touch the marker —
    # the set-profile cmd_features_set_profile body is responsible
    # for that, exercised in test_features_cli.py.
    assert marker_exists() is False


def test_other_subcommands_do_not_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    """``concinno features list`` (or other subcommands) should still
    show the banner — only ``set-profile`` is in the chicken-and-egg
    skip list."""
    from concinno.cli._first_run import should_skip_banner_for_argv

    monkeypatch.setattr("sys.argv", ["concinno", "features", "list"])
    assert should_skip_banner_for_argv() is False

    monkeypatch.setattr("sys.argv", ["concinno", "doctor"])
    assert should_skip_banner_for_argv() is False


def test_mark_seen_writes_iso_timestamp() -> None:
    """The marker file body should be a parseable ISO-8601 timestamp."""
    from datetime import datetime

    from concinno.cli._first_run import mark_seen

    marker = mark_seen()
    body = marker.read_text(encoding="utf-8").strip()
    # Round-trip parse — raises if the format drifted.
    parsed = datetime.fromisoformat(body)
    assert parsed.tzinfo is not None  # we wrote UTC explicitly


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
