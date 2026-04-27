"""Regression tests for ``concinno.cli._first_run`` — onboarding banner.

Each test redirects ``HOME`` to a tmp_path so the developer's real
``~/.concinno/.4_0_0_seen`` is never touched.

The autouse ``_isolated_home`` fixture also force-reports stderr as a
TTY so the suite exercises the historical (pre-4.2.4) banner path; the
4.2.4 R3.1 fix added a non-TTY suppression branch that has its own
dedicated regression test ``test_no_banner_in_pipe``.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    """Redirect HOME so marker writes land in tmp_path.

    Also force ``sys.stderr.isatty`` to ``True`` so the historical
    banner-printing test path keeps firing under pytest's capsys
    (which otherwise wraps stderr in a non-TTY pipe and would trip
    the 4.2.4 R3.1 non-TTY suppression branch).
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    # Windows resolution falls back through USERPROFILE when HOME is
    # absent; in CI on Windows we need to override it too.
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    # Reset module-level latches so each test starts clean even though
    # they live across the full test session.
    from concinno.cli import _first_run
    monkeypatch.setattr(_first_run, "_session_marker_failed", False)
    monkeypatch.setattr(_first_run, "_oserror_warning_emitted", False)
    # Pytest's capsys swaps stderr for an EncodedFile whose isatty is a
    # C-level method_descriptor that cannot be monkeypatched on the
    # class. Stub the module-level gate function instead so the
    # historical banner-printing tests still exercise the print path.
    # Tests that need the non-TTY branch (test_no_banner_in_pipe)
    # override the same attribute themselves.
    _ = capsys  # ensure capsys is initialised before our setattr lands
    monkeypatch.setattr(_first_run, "is_stderr_tty", lambda: True)
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


# ── 4.2.4 R3.1 + R1.4 + R3.2 regressions ─────────────────────────────


def test_no_banner_in_pipe(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Non-TTY ``stderr`` must suppress the banner (R3.1).

    Overrides the autouse fixture's ``isatty=True`` shim so the real
    pipe-detection branch fires, mirroring CI logs / Docker bootstrap /
    ``concinno features list 2>&1 | jq`` invocations.
    """
    from concinno.cli import _first_run
    from concinno.cli._first_run import (
        marker_exists,
        maybe_print_first_run_banner,
    )

    # Override the autouse fixture's ``is_stderr_tty=True`` shim so the
    # production non-TTY suppression branch (R3.1) is exercised.
    monkeypatch.setattr(_first_run, "is_stderr_tty", lambda: False)

    printed = maybe_print_first_run_banner()
    captured = capsys.readouterr()

    assert printed is False
    assert captured.err == ""
    assert captured.out == ""
    # And critically: the marker must NOT be touched on the suppressed
    # path so a real interactive first run later still shows the banner.
    assert marker_exists() is False


def test_no_infinite_loop_on_oserror(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Read-only ``$HOME`` must not produce an infinite banner loop
    (Red 1 R1.4 + Red 3 R3.2 — convergent).

    The first invocation prints the banner (best-effort marker write
    raises OSError under the hood); the second and third invocations
    must short-circuit via the in-memory ``_session_marker_failed``
    flag and stay silent.
    """
    from concinno.cli import _first_run

    # Force every ``write_text`` to raise OSError, simulating a
    # read-only filesystem mount of $HOME without breaking other
    # ``Path`` operations (which a HOME-rebind to a regular file
    # would also do, but more bluntly).
    def _raise(*_a: object, **_kw: object) -> None:
        raise OSError("read-only filesystem (simulated)")

    monkeypatch.setattr(Path, "write_text", _raise)

    first = _first_run.maybe_print_first_run_banner()
    second = _first_run.maybe_print_first_run_banner()
    third = _first_run.maybe_print_first_run_banner()

    captured = capsys.readouterr()

    assert first is True, "first call must print the banner"
    assert second is False, "second call must be silent (no infinite loop)"
    assert third is False, "third call must remain silent"
    # Banner body should appear exactly once in the captured stderr.
    assert captured.err.count("Welcome to concinno") == 1


def test_oserror_logged_once(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The OSError path must emit exactly one WARNING per process."""
    from concinno.cli import _first_run

    def _raise(*_a: object, **_kw: object) -> None:
        raise OSError("read-only filesystem (simulated)")

    monkeypatch.setattr(Path, "write_text", _raise)

    with caplog.at_level(logging.WARNING, logger="concinno.first_run"):
        # Three consecutive calls — the warning must fire exactly once.
        _first_run.mark_seen()
        _first_run.mark_seen()
        _first_run.mark_seen()

    warnings = [
        rec for rec in caplog.records
        if rec.name == "concinno.first_run" and rec.levelno == logging.WARNING
    ]
    assert len(warnings) == 1, (
        f"expected exactly 1 WARNING, got {len(warnings)}: {warnings!r}"
    )
    assert "first-run marker could not be written" in warnings[0].getMessage()
