"""Tests for the 4.3.0 ``release_authorization`` integration helpers.

Covers the public-facing wrappers introduced for Plan A Week 1:

- :func:`acquire_release_lock` / :func:`release_release_lock`
- :func:`pre_publish_check` (returns :class:`PreCheckResult`)
- :class:`LockAcquireError` (returned, not raised, on contention)

All checks are advisory: helpers never raise, never prompt the user,
and never reintroduce the publish gate the user has permanently
opted out of (``release_auth.disabled=True`` per
``rules/L1/release_coord.md``). These tests pin that contract.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from concinno.coordination.release_lock import ReleaseLock
from concinno.release_authorization import (
    LockAcquireError,
    PreCheckResult,
    acquire_release_lock,
    pre_publish_check,
    release_release_lock,
)

# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolated_lock_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Redirect default lock directory to ``tmp_path`` for every test.

    Prevents tests from interfering with each other or with a real
    running session on the dev box.
    """
    import concinno.coordination.release_lock as rl

    monkeypatch.setattr(rl, "DEFAULT_LOCK_DIR", tmp_path)
    return tmp_path


@pytest.fixture(autouse=True)
def _silence_ziq_bus(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable the ZIQ outcome bus so emit() side-effects don't bleed
    into other tests' state. ``pre_publish_check`` graceful-degrades
    when the bus is disabled.
    """
    monkeypatch.setenv("CONCINNO_ZIQ_BUS_DISABLED", "1")


@pytest.fixture
def valid_pyproject(tmp_path: Path) -> Path:
    """Write a minimal valid pyproject.toml with name=concinno + version."""
    pp = tmp_path / "pyproject.toml"
    pp.write_text(
        '[project]\nname = "concinno"\nversion = "4.3.0"\n',
        encoding="utf-8",
    )
    return pp


@pytest.fixture
def valid_changelog(tmp_path: Path) -> Path:
    cl = tmp_path / "CHANGELOG.md"
    cl.write_text(
        "# Changelog\n\n## [Unreleased]\n\n## [4.3.0] - 2026-05-04\n\n"
        "## [4.2.5] - 2026-04-27\n",
        encoding="utf-8",
    )
    return cl


@pytest.fixture
def valid_dist(tmp_path: Path) -> Path:
    dd = tmp_path / "dist"
    dd.mkdir()
    # twine check tolerates empty wheel/sdist when invoked via -m, but
    # we patch the subprocess in tests anyway.
    (dd / "concinno-4.3.0-py3-none-any.whl").write_bytes(b"PK\x03\x04")
    (dd / "concinno-4.3.0.tar.gz").write_bytes(b"\x1f\x8b")
    return dd


# ── 1. acquire_release_lock happy path ──────────────────────────────


class TestAcquireReleaseLock:
    """Behaviour of the public lock wrapper."""

    def test_acquire_succeeds_and_writes_lock_file(
        self, _isolated_lock_dir: Path
    ) -> None:
        """Successful acquire returns ``(True, None)`` and persists the
        lock JSON (so a second process / next caller can see it)."""
        ok, err = acquire_release_lock(
            "concinno", "4.3.0", session="sess-A", host="dev-host"
        )
        assert ok is True
        assert err is None
        # Lock file exists with the expected schema.
        held = ReleaseLock().check("concinno")
        assert held is not None
        assert held["holder_session"] == "sess-A"
        assert held["host"] == "dev-host"
        assert held["version"] == "4.3.0"
        assert "acquired_at" in held
        # Cleanup
        release_release_lock("concinno")
        assert ReleaseLock().check("concinno") is None

    def test_lock_contention_returns_error_not_raises(
        self, _isolated_lock_dir: Path
    ) -> None:
        """Second caller for the same package gets
        ``(False, LockAcquireError)`` — never an exception."""
        ok1, err1 = acquire_release_lock(
            "concinno", "4.3.0", session="sess-first"
        )
        assert ok1 is True
        assert err1 is None

        # Second caller, different session — must not raise.
        ok2, err2 = acquire_release_lock(
            "concinno", "4.3.0", session="sess-second"
        )
        assert ok2 is False
        assert isinstance(err2, LockAcquireError)
        assert err2.package == "concinno"
        assert err2.holder["holder_session"] == "sess-first"
        # The error is *type* Exception so callers who want raising
        # behaviour can `raise err2`, but the function itself never
        # raises.
        assert isinstance(err2, Exception)

        release_release_lock("concinno")

    def test_stale_lock_auto_revoked_after_ttl(
        self, _isolated_lock_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A lock older than the TTL is revoked transparently —
        :func:`acquire_release_lock` succeeds for the new caller."""
        # Drop TTL to 1 minute so we can fake a stale acquired_at.
        monkeypatch.setenv("CONCINNO_RELEASE_LOCK_TTL_MIN", "1")

        # Forge a stale lock 2 hours ago.
        lock_dir = _isolated_lock_dir
        lock_dir.mkdir(parents=True, exist_ok=True)
        stale_path = lock_dir / "concinno.lock"
        stale_ts = (
            datetime.now(timezone.utc) - timedelta(hours=2)
        ).isoformat()
        stale_path.write_text(
            json.dumps(
                {
                    "pkg": "concinno",
                    "version": "4.2.5",
                    "holder_session": "ghost-session",
                    "host": "ghost-host",
                    "acquired_at": stale_ts,
                }
            ),
            encoding="utf-8",
        )

        # New caller should win — the underlying ReleaseLock auto-revokes
        # the stale entry inside acquire().
        ok, err = acquire_release_lock(
            "concinno", "4.3.0", session="sess-new"
        )
        assert ok is True
        assert err is None
        held = ReleaseLock().check("concinno")
        assert held is not None
        assert held["holder_session"] == "sess-new"
        release_release_lock("concinno")

    def test_lock_disabled_via_env_is_noop(
        self, _isolated_lock_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``CONCINNO_RELEASE_LOCK_DISABLED=1`` short-circuits both
        acquire and release — no lock file written, no contention check.
        Used by 1-host CI / dev workflows."""
        monkeypatch.setenv("CONCINNO_RELEASE_LOCK_DISABLED", "1")

        ok, err = acquire_release_lock(
            "concinno", "4.3.0", session="sess-noop"
        )
        assert ok is True
        assert err is None
        # No lock file should have been created on disk.
        assert not (_isolated_lock_dir / "concinno.lock").exists()
        # release_release_lock is also a no-op.
        release_release_lock("concinno")  # must not raise

    def test_lock_works_when_release_auth_disabled(
        self, _isolated_lock_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``release_auth.disabled=True`` is about *publish authorization*
        and must NOT disable the *concurrency* lock. Two parallel
        sessions with the publish gate opted out still cannot both
        upload the same version."""
        # User has opted out of the publish gate.
        monkeypatch.setenv("CONCINNO_RELEASE_AUTH_DISABLED", "1")

        ok1, err1 = acquire_release_lock(
            "concinno", "4.3.0", session="sess-A"
        )
        assert ok1 is True
        assert err1 is None

        # Concurrent session must still be told the lock is taken.
        ok2, err2 = acquire_release_lock(
            "concinno", "4.3.0", session="sess-B"
        )
        assert ok2 is False
        assert isinstance(err2, LockAcquireError)
        release_release_lock("concinno")


# ── 2. pre_publish_check ────────────────────────────────────────────


class TestPrePublishCheck:
    """The advisory four-check bundle. Never raises, never prompts."""

    def test_all_green_returns_passed(
        self,
        tmp_path: Path,
        valid_pyproject: Path,
        valid_changelog: Path,
        valid_dist: Path,
    ) -> None:
        """All four checks OK → ``passed=True`` with empty reasons."""
        # Mock subprocess + PyPI probe.
        with (
            patch(
                "concinno.coordination.release_lock.pypi_version_taken",
                return_value=False,
            ),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "Checking ... PASSED"
            mock_run.return_value.stderr = ""
            result = pre_publish_check(
                target_version="4.3.0",
                package="concinno",
                dist_dir=valid_dist,
                pyproject=valid_pyproject,
                changelog=valid_changelog,
                run_tests=False,
            )
        assert isinstance(result, PreCheckResult)
        assert result.passed is True
        assert result.reasons == ()
        # All three checks ran (tests is opt-in and stays absent).
        assert "twine_check" in result.details
        assert "pypi_registry" in result.details
        assert "version_sync" in result.details
        assert "tests" not in result.details

    def test_version_mismatch_detected(
        self,
        tmp_path: Path,
        valid_changelog: Path,
        valid_dist: Path,
    ) -> None:
        """pyproject.toml ``4.2.5`` vs target ``4.3.0`` → version_sync
        check fails (and the call still doesn't raise)."""
        # Mismatch: pyproject still on 4.2.5.
        pp = tmp_path / "pyproject.toml"
        pp.write_text(
            '[project]\nname = "concinno"\nversion = "4.2.5"\n',
            encoding="utf-8",
        )
        with (
            patch(
                "concinno.coordination.release_lock.pypi_version_taken",
                return_value=False,
            ),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "PASSED"
            mock_run.return_value.stderr = ""
            result = pre_publish_check(
                target_version="4.3.0",
                package="concinno",
                dist_dir=valid_dist,
                pyproject=pp,
                changelog=valid_changelog,
            )
        assert result.passed is False
        joined = " | ".join(result.reasons)
        assert "version_sync" in joined
        assert "4.2.5" in joined
        # Other checks still ran successfully.
        assert result.details["pypi_registry"]["taken"] is False

    def test_pypi_registry_already_taken_detected(
        self,
        valid_pyproject: Path,
        valid_changelog: Path,
        valid_dist: Path,
    ) -> None:
        """200 from pypi.org/<pkg>/<ver>/json → pypi_registry fail."""
        with (
            patch(
                "concinno.coordination.release_lock.pypi_version_taken",
                return_value=True,
            ),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "PASSED"
            mock_run.return_value.stderr = ""
            result = pre_publish_check(
                target_version="4.3.0",
                package="concinno",
                dist_dir=valid_dist,
                pyproject=valid_pyproject,
                changelog=valid_changelog,
            )
        assert result.passed is False
        joined = " | ".join(result.reasons)
        assert "pypi_registry" in joined
        assert "already on PyPI" in joined or "would 400" in joined
        assert result.details["pypi_registry"]["taken"] is True

    def test_does_not_raise_or_prompt_when_release_auth_disabled(
        self,
        valid_pyproject: Path,
        valid_changelog: Path,
        valid_dist: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """User permanent opt-out (``release_auth.disabled=True``) does
        NOT disable the advisory checks — they still run for information.
        But the function never raises and never asks the user (no
        AskUser, no gate). Caller decides what to do with the result.

        This pins the directive from
        ``feedback_publish_authorization_permanently_disabled.md``:
        the integration ships information, not gates.
        """
        monkeypatch.setenv("CONCINNO_RELEASE_AUTH_DISABLED", "1")
        with (
            patch(
                "concinno.coordination.release_lock.pypi_version_taken",
                return_value=True,  # would block in old gate
            ),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "PASSED"
            mock_run.return_value.stderr = ""
            # Must return a result, never raise.
            result = pre_publish_check(
                target_version="4.3.0",
                package="concinno",
                dist_dir=valid_dist,
                pyproject=valid_pyproject,
                changelog=valid_changelog,
            )
        assert isinstance(result, PreCheckResult)
        # PyPI check still ran and reported the conflict — that's the
        # *information* the caller asked for. The function did not
        # convert it into a gate or AskUser prompt.
        assert result.passed is False
        assert any("pypi_registry" in r for r in result.reasons)

    def test_dist_dir_missing_detected(
        self,
        tmp_path: Path,
        valid_pyproject: Path,
        valid_changelog: Path,
    ) -> None:
        """No ``dist/`` directory → twine_check fails advisory-style."""
        with patch(
            "concinno.coordination.release_lock.pypi_version_taken",
            return_value=False,
        ):
            result = pre_publish_check(
                target_version="4.3.0",
                package="concinno",
                dist_dir=tmp_path / "dist-does-not-exist",
                pyproject=valid_pyproject,
                changelog=valid_changelog,
            )
        assert result.passed is False
        assert any("twine_check" in r for r in result.reasons)


# ── 3. ZIQ outcome wiring ──────────────────────────────────────────


class TestZIQOutcomeEmission:
    """``pre_publish_check`` emits a ZIQ outcome on every call (when
    the bus is enabled). Tunable id is namespaced
    ``release_authorization.pre_publish_check`` so callers can subscribe
    without colliding with existing tunables."""

    def test_emits_outcome_with_pass_reward_one(
        self,
        valid_pyproject: Path,
        valid_changelog: Path,
        valid_dist: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Re-enable the bus for this single test.
        monkeypatch.delenv("CONCINNO_ZIQ_BUS_DISABLED", raising=False)
        from concinno.ziq_outcome_bus import Outcome, get_bus

        captured: list[Outcome] = []
        unsubscribe = get_bus().subscribe(
            "release_authorization.pre_publish_check",
            captured.append,
        )
        try:
            with (
                patch(
                    "concinno.coordination.release_lock.pypi_version_taken",
                    return_value=False,
                ),
                patch("subprocess.run") as mock_run,
            ):
                mock_run.return_value.returncode = 0
                mock_run.return_value.stdout = "PASSED"
                mock_run.return_value.stderr = ""
                result = pre_publish_check(
                    target_version="4.3.0",
                    package="concinno",
                    dist_dir=valid_dist,
                    pyproject=valid_pyproject,
                    changelog=valid_changelog,
                )
        finally:
            unsubscribe()

        assert result.passed is True
        assert len(captured) == 1
        out = captured[0]
        assert out.tunable == "release_authorization.pre_publish_check"
        assert out.reward == 1.0
        assert out.value == 0  # zero failed checks
        assert out.metadata["package"] == "concinno"
        assert out.metadata["version"] == "4.3.0"
