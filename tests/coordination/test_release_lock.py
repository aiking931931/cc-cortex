"""Tests for concinno.coordination.release_lock.

Coverage targets:

- Happy path acquire / release.
- Concurrent acquire from a different session is blocked.
- Stale lock past TTL is auto-revoked on next acquire.
- Release without an existing lock is idempotent (no exception).
- ``pypi_version_taken`` honours 200 / 404 from the JSON endpoint
  via mocked ``urllib.request.urlopen``.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from concinno.coordination.release_lock import (
    DEFAULT_TTL_MINUTES,
    ReleaseLock,
    pypi_version_taken,
)

# ── ReleaseLock acquire / release ─────────────────────────────────


def test_acquire_then_release_clean(tmp_path: Path) -> None:
    """Happy path: acquire, check, release."""
    lock = ReleaseLock(lock_dir=tmp_path)
    assert lock.acquire("concinno", "4.3.0", session="sess-a") is True

    held = lock.check("concinno")
    assert held is not None
    assert held["pkg"] == "concinno"
    assert held["version"] == "4.3.0"
    assert held["holder_session"] == "sess-a"
    assert "host" in held
    assert "acquired_at" in held

    lock.release("concinno")
    assert lock.check("concinno") is None


def test_double_acquire_blocked(tmp_path: Path) -> None:
    """Second session cannot grab a held, non-stale lock."""
    lock = ReleaseLock(lock_dir=tmp_path)
    assert lock.acquire("concinno", "4.3.0", session="sess-a") is True

    # Second session, fresh ReleaseLock instance — same lock dir.
    other = ReleaseLock(lock_dir=tmp_path)
    assert other.acquire("concinno", "4.3.0", session="sess-b") is False

    held = other.check("concinno")
    assert held is not None
    assert held["holder_session"] == "sess-a"


def test_same_session_reacquire_is_idempotent(tmp_path: Path) -> None:
    """Same holder re-acquiring refreshes the timestamp without blocking."""
    lock = ReleaseLock(lock_dir=tmp_path)
    assert lock.acquire("concinno", "4.3.0", session="sess-a") is True
    assert lock.acquire("concinno", "4.3.0", session="sess-a") is True


def test_stale_lock_auto_revoked(tmp_path: Path) -> None:
    """A lock past TTL is revoked silently on the next acquire."""
    lock = ReleaseLock(lock_dir=tmp_path)
    lock_path = tmp_path / "concinno.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    # Hand-write a stale lock 2 hours old (TTL is 30 min by default).
    stale_at = datetime.now(timezone.utc) - timedelta(
        seconds=DEFAULT_TTL_MINUTES * 60 + 3600,
    )
    lock_path.write_text(
        json.dumps(
            {
                "pkg": "concinno",
                "version": "4.2.0",
                "holder_session": "sess-old",
                "host": "ghost",
                "acquired_at": stale_at.isoformat(),
            }
        ),
        encoding="utf-8",
    )

    # check() returns None for stale (treats as free).
    assert lock.check("concinno") is None
    # raw() still surfaces the stale content for diagnostics.
    raw = lock.raw("concinno")
    assert raw is not None and raw["holder_session"] == "sess-old"

    # New session acquires successfully — stale takeover.
    assert lock.acquire("concinno", "4.3.0", session="sess-new") is True
    held = lock.check("concinno")
    assert held is not None
    assert held["holder_session"] == "sess-new"
    assert held["version"] == "4.3.0"


def test_release_idempotent(tmp_path: Path) -> None:
    """Releasing a non-existent lock is a no-op (no exception)."""
    lock = ReleaseLock(lock_dir=tmp_path)
    lock.release("concinno")
    lock.release("concinno")  # second time should also be silent.
    assert lock.check("concinno") is None


def test_unparseable_timestamp_treated_as_stale(tmp_path: Path) -> None:
    """Defensive: a corrupt acquired_at must not wedge the lock forever."""
    lock = ReleaseLock(lock_dir=tmp_path)
    (tmp_path / "concinno.lock").write_text(
        json.dumps(
            {
                "pkg": "concinno",
                "version": "4.2.0",
                "holder_session": "sess-x",
                "host": "h",
                "acquired_at": "not-a-timestamp",
            }
        ),
        encoding="utf-8",
    )
    assert lock.check("concinno") is None  # stale-by-corruption
    assert lock.acquire("concinno", "4.3.0", session="sess-new") is True


def test_list_active_skips_stale(tmp_path: Path) -> None:
    """list_active returns only non-stale locks."""
    lock = ReleaseLock(lock_dir=tmp_path)
    assert lock.acquire("pkg-fresh", "1.0.0", session="sess-a") is True

    # Plant a stale lock for a second package.
    stale_at = datetime.now(timezone.utc) - timedelta(
        seconds=DEFAULT_TTL_MINUTES * 60 + 60,
    )
    (tmp_path / "pkg-stale.lock").write_text(
        json.dumps(
            {
                "pkg": "pkg-stale",
                "version": "0.1.0",
                "holder_session": "sess-old",
                "host": "h",
                "acquired_at": stale_at.isoformat(),
            }
        ),
        encoding="utf-8",
    )

    actives = lock.list_active()
    pkgs = [a["pkg"] for a in actives]
    assert "pkg-fresh" in pkgs
    assert "pkg-stale" not in pkgs


def test_ttl_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``CONCINNO_RELEASE_LOCK_TTL_MIN`` shrinks the staleness window."""
    monkeypatch.setenv("CONCINNO_RELEASE_LOCK_TTL_MIN", "1")
    lock = ReleaseLock(lock_dir=tmp_path)

    # 5-minute-old lock should now be stale (TTL=1 min).
    old_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    (tmp_path / "concinno.lock").write_text(
        json.dumps(
            {
                "pkg": "concinno",
                "version": "4.2.0",
                "holder_session": "sess-old",
                "host": "h",
                "acquired_at": old_at.isoformat(),
            }
        ),
        encoding="utf-8",
    )
    assert lock.check("concinno") is None
    assert lock.acquire("concinno", "4.3.0", session="sess-new") is True


# ── pypi_version_taken — mocked urllib ────────────────────────────


class _FakeResponse:
    """Minimal stand-in for ``http.client.HTTPResponse`` context manager."""

    def __init__(self, status: int) -> None:
        self.status = status

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


def test_pypi_version_taken_404_returns_false() -> None:
    """404 from PyPI JSON endpoint = version is available."""
    import urllib.error

    err = urllib.error.HTTPError(
        url="https://pypi.org/pypi/concinno/9.9.9/json",
        code=404,
        msg="Not Found",
        hdrs=None,  # type: ignore[arg-type]
        fp=None,
    )
    with patch(
        "concinno.coordination.release_lock.urllib.request.urlopen",
        side_effect=err,
    ):
        assert pypi_version_taken("concinno", "9.9.9") is False


def test_pypi_version_taken_200_returns_true() -> None:
    """200 from PyPI JSON endpoint = version is taken."""
    with patch(
        "concinno.coordination.release_lock.urllib.request.urlopen",
        return_value=_FakeResponse(200),
    ):
        assert pypi_version_taken("concinno", "4.2.1") is True


def test_pypi_version_taken_5xx_propagates() -> None:
    """A non-404 HTTP error must NOT silently return False."""
    import urllib.error

    err = urllib.error.HTTPError(
        url="https://pypi.org/pypi/concinno/4.2.1/json",
        code=503,
        msg="Service Unavailable",
        hdrs=None,  # type: ignore[arg-type]
        fp=None,
    )
    with patch(
        "concinno.coordination.release_lock.urllib.request.urlopen",
        side_effect=err,
    ):
        with pytest.raises(urllib.error.HTTPError):
            pypi_version_taken("concinno", "4.2.1")
