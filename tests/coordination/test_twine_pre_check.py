"""Tests for ``concinno.coordination.twine_pre_check.check_before_upload``.

The wave-2 wrapper-decision module ships with zero dedicated tests; the
existing ``test_release_lock.py`` only covers the underlying
``ReleaseLock`` and ``pypi_version_taken`` primitives. This file
exercises the ``(ok, reason)`` decision tree the eventual twine wrapper
will route on:

1. PyPI says version exists → blocked (root cause of the 4.2.1 400).
2. PyPI says version absent + ``require_lock_held=False`` → ok.
3. PyPI absent + lock held by us → ok.
4. PyPI absent + lock held by another session → blocked.
5. PyPI absent + lock held but version mismatch → blocked.
6. PyPI absent + ``require_lock_held=True`` but no lock → blocked.
7. Network failure (``URLError``) → fail-closed (blocked, not "ok").
8. Non-404 PyPI HTTP error (e.g. 503) propagates so the wrapper logs it.

We monkey-patch ``pypi_version_taken`` and ``ReleaseLock`` rather than
hitting the live PyPI endpoint; both are imported into the
``twine_pre_check`` namespace, so we patch *that* binding (not the
module they originate in) — see the patch targets below.
"""

from __future__ import annotations

import urllib.error
from typing import Optional
from unittest.mock import MagicMock, patch

from concinno.coordination.twine_pre_check import check_before_upload

# ── helper: mock a ReleaseLock.check() result ─────────────────────


def _mock_release_lock_with(
    held: Optional[dict],
) -> MagicMock:
    """Return a MagicMock that mimics ``ReleaseLock`` so ``ReleaseLock().check(pkg)``
    yields ``held``."""
    instance = MagicMock()
    instance.check.return_value = held
    cls = MagicMock(return_value=instance)
    return cls


# ── 1. PyPI says version exists ───────────────────────────────────


def test_blocked_when_version_already_on_pypi() -> None:
    """A version already on PyPI must veto upload regardless of lock state."""
    with patch(
        "concinno.coordination.twine_pre_check.pypi_version_taken",
        return_value=True,
    ):
        ok, reason = check_before_upload(
            "concinno", "4.2.1", session="sess-a", require_lock_held=False,
        )
    assert ok is False
    assert "already on pypi" in reason.lower() or "already" in reason.lower()


# ── 2. require_lock_held=False short-circuits lock check ──────────


def test_ok_when_pypi_clear_and_lock_check_disabled() -> None:
    """If the caller intends to acquire later, lock state is not checked."""
    with patch(
        "concinno.coordination.twine_pre_check.pypi_version_taken",
        return_value=False,
    ):
        ok, reason = check_before_upload(
            "concinno", "4.3.0", session="sess-a", require_lock_held=False,
        )
    assert ok is True
    assert reason == ""


# ── 3. Lock held by us, version matches → ok ─────────────────────


def test_ok_when_we_hold_the_lock_for_this_version() -> None:
    """Happy path: we own the lock, PyPI is clear, version matches."""
    held = {
        "pkg": "concinno",
        "version": "4.3.0",
        "holder_session": "sess-a",
        "host": "h",
        "acquired_at": "2026-04-27T00:00:00+00:00",
    }
    with patch(
        "concinno.coordination.twine_pre_check.pypi_version_taken",
        return_value=False,
    ), patch(
        "concinno.coordination.twine_pre_check.ReleaseLock",
        _mock_release_lock_with(held),
    ):
        ok, reason = check_before_upload(
            "concinno", "4.3.0", session="sess-a", require_lock_held=True,
        )
    assert ok is True
    assert reason == ""


# ── 4. Lock held by another session → blocked ────────────────────


def test_blocked_when_lock_held_by_other_session() -> None:
    """Concurrent publisher: another session owns the lock."""
    held = {
        "pkg": "concinno",
        "version": "4.3.0",
        "holder_session": "sess-other",
        "host": "h",
        "acquired_at": "2026-04-27T00:00:00+00:00",
    }
    with patch(
        "concinno.coordination.twine_pre_check.pypi_version_taken",
        return_value=False,
    ), patch(
        "concinno.coordination.twine_pre_check.ReleaseLock",
        _mock_release_lock_with(held),
    ):
        ok, reason = check_before_upload(
            "concinno", "4.3.0", session="sess-a", require_lock_held=True,
        )
    assert ok is False
    assert "sess-other" in reason
    assert "concurrent" in reason.lower()


# ── 5. Lock held but for wrong version → blocked ─────────────────


def test_blocked_when_lock_version_mismatch() -> None:
    """Lock acquired for 4.3.0 must not grant upload of 4.4.0."""
    held = {
        "pkg": "concinno",
        "version": "4.3.0",
        "holder_session": "sess-a",
        "host": "h",
        "acquired_at": "2026-04-27T00:00:00+00:00",
    }
    with patch(
        "concinno.coordination.twine_pre_check.pypi_version_taken",
        return_value=False,
    ), patch(
        "concinno.coordination.twine_pre_check.ReleaseLock",
        _mock_release_lock_with(held),
    ):
        ok, reason = check_before_upload(
            "concinno", "4.4.0", session="sess-a", require_lock_held=True,
        )
    assert ok is False
    assert "4.3.0" in reason
    assert "4.4.0" in reason


# ── 6. require_lock_held=True but no lock → blocked ──────────────


def test_blocked_when_require_lock_held_but_lock_absent() -> None:
    """Caller said 'lock must already be acquired' but ReleaseLock.check() = None."""
    with patch(
        "concinno.coordination.twine_pre_check.pypi_version_taken",
        return_value=False,
    ), patch(
        "concinno.coordination.twine_pre_check.ReleaseLock",
        _mock_release_lock_with(None),
    ):
        ok, reason = check_before_upload(
            "concinno", "4.3.0", session="sess-a", require_lock_held=True,
        )
    assert ok is False
    assert "no active release lock" in reason.lower()
    assert "concinno release-lock acquire" in reason


# ── 7. Network failure → fail-closed ─────────────────────────────


def test_blocked_when_pypi_network_unreachable() -> None:
    """``URLError`` from PyPI must fail-closed — not silently say 'ok'.

    Otherwise the 4.2.1 400 race is reintroduced any time a captive
    portal / firewall blocks pypi.org.
    """
    err = urllib.error.URLError("Network unreachable")
    with patch(
        "concinno.coordination.twine_pre_check.pypi_version_taken",
        side_effect=err,
    ):
        ok, reason = check_before_upload(
            "concinno", "4.3.0", session="sess-a", require_lock_held=False,
        )
    assert ok is False
    assert "pypi version check failed" in reason.lower()
    assert "refusing to upload" in reason.lower()


# ── 8. Non-404 PyPI HTTP error → fail-closed (HTTPError ⊂ URLError) ──


def test_blocked_on_pypi_http_5xx_fail_closed() -> None:
    """A 503 from PyPI must fail-closed, not say "available".

    ``urllib.error.HTTPError`` is a subclass of ``URLError`` so the
    ``except URLError`` clause in ``check_before_upload`` catches it.
    The wrapper layer relies on this: 'pypi was sick' + 'pypi said
    no' both block, only an explicit 404 from ``pypi_version_taken``
    (which the helper translates to ``return False``) lets us proceed.
    """
    err = urllib.error.HTTPError(
        url="https://pypi.org/pypi/concinno/4.3.0/json",
        code=503,
        msg="Service Unavailable",
        hdrs=None,  # type: ignore[arg-type]
        fp=None,
    )
    with patch(
        "concinno.coordination.twine_pre_check.pypi_version_taken",
        side_effect=err,
    ):
        ok, reason = check_before_upload(
            "concinno", "4.3.0", session="sess-a", require_lock_held=False,
        )
    assert ok is False
    assert "pypi version check failed" in reason.lower()
    assert "503" in reason or "service unavailable" in reason.lower()
