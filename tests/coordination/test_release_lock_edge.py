"""Edge-case tests for ``concinno.coordination.release_lock``.

Complements ``test_release_lock.py`` (happy paths + the most obvious
stale / corruption cases) by covering the slow-path branches the wave-2
ship cycle introduced but the original 11-test suite did not exercise.

Coverage focus (no overlap with the existing file):

- ``_ttl_seconds`` env-var sanitiser (invalid / negative / zero values
  must fall back to the default — a typo must not silently disable
  staleness detection forever).
- ``pypi_version_taken`` raises on transport errors (``URLError``)
  rather than silently saying "available", because the wrapper layer
  in ``twine_pre_check`` relies on the exception to fail-closed.
- ``acquire`` honours an explicit ``host=`` override so multi-tenant
  CI / RunPod can stamp a meaningful holder identity rather than the
  pod's gethostname.
- ``acquire`` creates ``lock_dir`` lazily when the user has never run
  the CLI before (matches ``DEFAULT_LOCK_DIR`` first-time install).
"""

from __future__ import annotations

import urllib.error
from pathlib import Path
from unittest.mock import patch

import pytest

from concinno.coordination.release_lock import (
    DEFAULT_TTL_MINUTES,
    ReleaseLock,
    _ttl_seconds,
    pypi_version_taken,
)

# ── _ttl_seconds env sanitiser ────────────────────────────────────


@pytest.mark.parametrize(
    "raw_value",
    ["abc", "-5", "0", "  ", "1.5", "999999999999999999999"],
)
def test_ttl_seconds_invalid_env_falls_back(
    monkeypatch: pytest.MonkeyPatch, raw_value: str,
) -> None:
    """Garbage / non-positive env values must NOT disable staleness.

    A typo like ``CONCINNO_RELEASE_LOCK_TTL_MIN=0`` must fall back to
    the 30-minute default, otherwise a stale crashed session wedges
    every future release. Negative / non-int values likewise.
    """
    monkeypatch.setenv("CONCINNO_RELEASE_LOCK_TTL_MIN", raw_value)
    if raw_value == "999999999999999999999":
        # Overflow path: int() succeeds in Python (arbitrary precision)
        # so this returns the absurd minute count * 60. We just assert
        # the code does not crash and returns a positive int.
        assert _ttl_seconds() > 0
        return
    assert _ttl_seconds() == DEFAULT_TTL_MINUTES * 60


def test_ttl_seconds_unset_env_uses_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Absence of the env var → default 30 minutes."""
    monkeypatch.delenv("CONCINNO_RELEASE_LOCK_TTL_MIN", raising=False)
    assert _ttl_seconds() == DEFAULT_TTL_MINUTES * 60


# ── pypi_version_taken transport-error path ───────────────────────


def test_pypi_version_taken_urlerror_propagates() -> None:
    """Transport-layer failure must raise (not silently 'available').

    ``twine_pre_check.check_before_upload`` relies on the exception to
    fail-closed. If we swallowed it the 4.2.1 PyPI 400 race comes back.
    """
    err = urllib.error.URLError("Network unreachable")
    with patch(
        "concinno.coordination.release_lock.urllib.request.urlopen",
        side_effect=err,
    ):
        with pytest.raises(urllib.error.URLError):
            pypi_version_taken("concinno", "4.2.1")


# ── acquire host override ────────────────────────────────────────


def test_acquire_honours_explicit_host(tmp_path: Path) -> None:
    """Caller-supplied ``host=`` overrides ``socket.gethostname()``."""
    lock = ReleaseLock(lock_dir=tmp_path)
    assert lock.acquire(
        "concinno", "4.3.0", session="sess-a", host="runpod-pod-xyz",
    )
    held = lock.check("concinno")
    assert held is not None
    assert held["host"] == "runpod-pod-xyz"


# ── acquire creates lock_dir lazily ──────────────────────────────


def test_acquire_creates_missing_lock_dir(tmp_path: Path) -> None:
    """A fresh-install user without ``~/.concinno/release_locks/`` must
    still be able to acquire — the dir is created on first acquire."""
    nested = tmp_path / "deeply" / "nested" / "release_locks"
    assert not nested.exists()

    lock = ReleaseLock(lock_dir=nested)
    assert lock.acquire("concinno", "4.3.0", session="sess-a") is True
    assert nested.exists()
    assert (nested / "concinno.lock").exists()


# ── acquire after release reuses path cleanly ────────────────────


def test_acquire_after_release_succeeds(tmp_path: Path) -> None:
    """Release → re-acquire (same or different session) must succeed.

    Guards against an OS quirk where the sentinel lockfile fd was held
    open across release(), preventing the next acquire from grabbing
    the advisory lock on the same path.
    """
    lock = ReleaseLock(lock_dir=tmp_path)
    assert lock.acquire("concinno", "4.3.0", session="sess-a") is True
    lock.release("concinno")
    assert lock.check("concinno") is None

    other = ReleaseLock(lock_dir=tmp_path)
    assert other.acquire("concinno", "4.4.0", session="sess-b") is True
    held = other.check("concinno")
    assert held is not None
    assert held["holder_session"] == "sess-b"
    assert held["version"] == "4.4.0"
