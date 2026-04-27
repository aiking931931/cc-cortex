"""Tests for ``concinno.release_authorization.acquire_for_upload``.

The 4.2.3 wiring landed an opt-in context manager that combines three
layers — auth string check, PyPI pre-check, atomic ReleaseLock — into
one guarded scope so the next ship cycle no longer hits the 400
already-exists race that bit Concinno 4.2.1.

This file pins the new behaviour:

- happy path acquires + releases the lock cleanly,
- PyPI 200 (version already taken) flips ``allowed`` to ``False`` with
  ``denied_at='race_prevention'``,
- another session holding the lock flips to ``denied_at='lock_collision'``,
- exceptions inside the body still release the lock,
- and the legacy ``check_authorization()`` signature is unchanged so
  existing callers keep working.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from concinno.coordination.release_lock import ReleaseLock
from concinno.release_authorization import (
    AuthorizationConfig,
    AuthorizationMode,
    UploadAuthorization,
    acquire_for_upload,
    check_authorization,
)

# ── Helpers ─────────────────────────────────────────────────────────


def _enabled_cfg() -> AuthorizationConfig:
    """Config with the gate enabled (default)."""
    return AuthorizationConfig(
        mode=AuthorizationMode.STRING_MATCH,
        disabled=False,
        source="test",
    )


def _disabled_cfg() -> AuthorizationConfig:
    """Config with the operator opted out."""
    return AuthorizationConfig(
        mode=AuthorizationMode.STRING_MATCH,
        disabled=True,
        source="test",
    )


@pytest.fixture(autouse=True)
def _isolated_lock_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the default lock directory to ``tmp_path`` for every test.

    ``ReleaseLock()`` (no args) defaults to ``~/.concinno/release_locks``;
    point that constant at a tmpdir so tests cannot interfere with each
    other or with a real running session on the dev box.
    """
    import concinno.coordination.release_lock as rl

    monkeypatch.setattr(rl, "DEFAULT_LOCK_DIR", tmp_path)
    return tmp_path


# ── TestAcquireForUpload ────────────────────────────────────────────


class TestAcquireForUpload:
    """Behaviour of the new context manager that wraps the publish gate."""

    def test_happy_path_allowed_lock_acquired_release_on_exit(
        self, _isolated_lock_dir: Path
    ) -> None:
        """Auth string present + PyPI says 404 + lock free → allowed."""
        chat = "ok go publish concinno 4.3.0"
        with patch(
            "concinno.coordination.twine_pre_check.pypi_version_taken",
            return_value=False,
        ):
            with acquire_for_upload(
                "concinno",
                "4.3.0",
                session="sess-happy",
                transcript_text=chat,
                config=_enabled_cfg(),
            ) as auth:
                assert isinstance(auth, UploadAuthorization)
                assert auth.allowed is True
                assert auth.reason == ""
                assert auth.denied_at == ""
                assert auth.lock_acquired is True

                # Lock is held while inside the body.
                held = ReleaseLock().check("concinno")
                assert held is not None
                assert held["holder_session"] == "sess-happy"
                assert held["version"] == "4.3.0"

        # Lock released on exit — next caller sees free.
        assert ReleaseLock().check("concinno") is None

    def test_pypi_version_already_taken_flips_to_denied(
        self, _isolated_lock_dir: Path
    ) -> None:
        """PyPI 200 → ``allowed=False`` with ``denied_at='race_prevention'``."""
        chat = "ok go publish concinno 4.2.1"
        with patch(
            "concinno.coordination.twine_pre_check.pypi_version_taken",
            return_value=True,
        ):
            with acquire_for_upload(
                "concinno",
                "4.2.1",
                session="sess-late",
                transcript_text=chat,
                config=_enabled_cfg(),
            ) as auth:
                assert auth.allowed is False
                assert auth.denied_at == "race_prevention"
                assert auth.lock_acquired is False
                # Reason should mention pypi / already so the LLM can route.
                assert "pypi" in auth.reason.lower() or "already" in auth.reason.lower()

        # No lock should have been taken.
        assert ReleaseLock().check("concinno") is None

    def test_lock_held_by_other_session_flips_to_denied(
        self, _isolated_lock_dir: Path
    ) -> None:
        """A non-stale lock held by another session → deny."""
        # Pre-create a fresh, valid lock owned by a different session.
        other = ReleaseLock()
        assert other.acquire("concinno", "4.3.0", session="sess-other") is True

        chat = "ok go publish concinno 4.3.0"
        with patch(
            "concinno.coordination.twine_pre_check.pypi_version_taken",
            return_value=False,
        ):
            with acquire_for_upload(
                "concinno",
                "4.3.0",
                session="sess-us",
                transcript_text=chat,
                config=_enabled_cfg(),
            ) as auth:
                assert auth.allowed is False
                assert auth.denied_at == "lock_collision"
                assert auth.lock_acquired is False
                assert "sess-other" in auth.reason

        # Other session's lock is still in place — we did not steal it.
        held = ReleaseLock().check("concinno")
        assert held is not None
        assert held["holder_session"] == "sess-other"

    def test_release_on_exception(self, _isolated_lock_dir: Path) -> None:
        """Lock is released even when the body raises."""
        chat = "ok go publish concinno 4.3.0"

        class _BoomError(RuntimeError):
            pass

        with patch(
            "concinno.coordination.twine_pre_check.pypi_version_taken",
            return_value=False,
        ):
            with pytest.raises(_BoomError):
                with acquire_for_upload(
                    "concinno",
                    "4.3.0",
                    session="sess-boom",
                    transcript_text=chat,
                    config=_enabled_cfg(),
                ) as auth:
                    assert auth.allowed is True
                    raise _BoomError("simulated upload failure")

        # Even though the body raised, the lock is gone.
        assert ReleaseLock().check("concinno") is None

    def test_denied_authorization_skips_lock_and_pre_check(
        self, _isolated_lock_dir: Path
    ) -> None:
        """No auth string in transcript → deny at ``authorization`` layer.

        Neither the PyPI pre-check nor the atomic lock should fire when
        the standard auth gate already rejected — keep denial cheap.
        """
        with patch(
            "concinno.coordination.twine_pre_check.pypi_version_taken",
        ) as pypi_mock:
            with acquire_for_upload(
                "concinno",
                "4.3.0",
                session="sess-no-auth",
                transcript_text="",  # no auth string
                config=_enabled_cfg(),
            ) as auth:
                assert auth.allowed is False
                assert auth.denied_at == "authorization"
                assert auth.lock_acquired is False
                # PyPI must NOT have been queried.
                pypi_mock.assert_not_called()

        # No lock was acquired.
        assert ReleaseLock().check("concinno") is None

    def test_disabled_config_short_circuits_both_new_layers(
        self, _isolated_lock_dir: Path
    ) -> None:
        """``disabled=True`` → allowed without PyPI query or lock acquisition.

        This is the load-bearing back-compat / opt-out test: the
        operator who set ``release_auth.disabled=True`` sees the same
        zero-friction behaviour as before 4.2.3.
        """
        with patch(
            "concinno.coordination.twine_pre_check.pypi_version_taken",
        ) as pypi_mock:
            with acquire_for_upload(
                "concinno",
                "4.3.0",
                session="sess-disabled",
                transcript_text="",
                config=_disabled_cfg(),
            ) as auth:
                assert auth.allowed is True
                assert auth.reason == ""
                assert auth.denied_at == ""
                assert auth.lock_acquired is False
                # Neither layer should have run.
                pypi_mock.assert_not_called()

        # No lock was taken — back-compat preserved.
        assert ReleaseLock().check("concinno") is None

    def test_back_compat_check_authorization_unchanged(self) -> None:
        """Legacy callers of ``check_authorization`` see no behaviour change.

        Old signature returns ``(allowed, reason)`` and never touches
        the new lock / pre-check layers — those activate **only** inside
        ``acquire_for_upload``. This test pins the contract.
        """
        cfg = _enabled_cfg()

        # Auth string absent → deny, just like before 4.2.3.
        allowed, reason = check_authorization(
            "twine_upload",
            "concinno",
            "4.3.0",
            transcript_text="no auth here",
            config=cfg,
        )
        assert allowed is False
        assert "go publish concinno 4.3.0" in reason

        # Auth string present → allow.
        allowed, reason = check_authorization(
            "twine_upload",
            "concinno",
            "4.3.0",
            transcript_text="please go publish concinno 4.3.0 now",
            config=cfg,
        )
        assert allowed is True
        assert reason == ""

        # disabled=True still allows (opt-out preserved).
        allowed, reason = check_authorization(
            "twine_upload",
            "concinno",
            "4.3.0",
            transcript_text="",
            config=_disabled_cfg(),
        )
        assert allowed is True


# ── Stale lock takeover via context manager ─────────────────────────


def test_acquire_for_upload_takes_over_stale_lock(
    _isolated_lock_dir: Path,
) -> None:
    """A stale (TTL-expired) lock is silently revoked on acquire.

    Belongs with the new context-manager tests because callers who
    relied on the *old* gate had no defence against a crashed session
    holding the markdown ``Active`` slot — this proves the new layer
    inherits ``ReleaseLock``'s stale-detection.
    """
    # Hand-craft a stale lock for ``concinno``: 24h old, well past TTL.
    stale_at = datetime.now(timezone.utc).replace(microsecond=0)
    # Stamp the timestamp far in the past via a manual write — using
    # the public API would refresh it.
    lock_path = _isolated_lock_dir / "concinno.lock"
    lock_path.write_text(
        json.dumps(
            {
                "pkg": "concinno",
                "version": "4.2.0",
                "holder_session": "sess-crashed",
                "host": "ghost",
                "acquired_at": stale_at.replace(year=stale_at.year - 1).isoformat(),
            }
        ),
        encoding="utf-8",
    )

    chat = "ok go publish concinno 4.3.0"
    with patch(
        "concinno.coordination.twine_pre_check.pypi_version_taken",
        return_value=False,
    ):
        with acquire_for_upload(
            "concinno",
            "4.3.0",
            session="sess-recovery",
            transcript_text=chat,
            config=_enabled_cfg(),
        ) as auth:
            assert auth.allowed is True
            assert auth.lock_acquired is True
            held = ReleaseLock().check("concinno")
            assert held is not None
            assert held["holder_session"] == "sess-recovery"

    # Lock released on exit.
    assert ReleaseLock().check("concinno") is None
