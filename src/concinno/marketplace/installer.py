"""Subprocess wrapper around ``pip install`` / ``pip uninstall``.

Defense in layers:

* Distribution name validated against
  :func:`concinno.marketplace.discovery.is_valid_dist_name` — we refuse
  arbitrary package names so a compromised frontend cannot push
  ``--upgrade requests`` or shell metacharacters into the call.
* Version string validated against a strict regex.
* ``subprocess.run`` invoked with ``shell=False`` and an args list —
  no concatenation, no ``shell=True``.
* 180-second hard timeout per design doc §1.6.
* Atomic file lock at ``~/.concinno/marketplace.lock`` (atomic mkdir,
  60-second stale TTL) prevents concurrent install of the same package
  from two browser tabs.

The caller (``gui.server._register_marketplace_routes``) is responsible
for the twice-click confirm gate; this module assumes authorisation
already happened.
"""
from __future__ import annotations

import logging
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from concinno.marketplace.discovery import is_valid_dist_name

logger = logging.getLogger("concinno.marketplace.installer")


# 180s ≈ a generous upper bound for ``pip install`` on a slow link with
# a heavy wheel. Beyond that the GUI shows "broken — see stderr" so the
# operator can follow up via real terminal pip.
PIP_TIMEOUT_SEC = 180

# Lock TTL: stale locks older than this are forcibly cleared. Keeps the
# UI responsive when a previous install crashed without releasing the
# directory.
LOCK_STALE_TTL_SEC = 60

# Strict version regex. Accepts the subset of PEP 440 that PyPI returns
# in practice: digits + dots + a small set of pre/post markers. Refuses
# anything containing shell metacharacters.
_VERSION_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)*(?:[.\-+a-zA-Z0-9]*)$")


@dataclass(frozen=True)
class InstallResult:
    """Result of a pip subprocess invocation."""

    ok: bool
    stdout: str
    stderr: str
    return_code: int


class InstallError(RuntimeError):
    """Raised when the package or version fails validation, or when a
    concurrent install is already in flight.
    """


def _lock_path() -> Path:
    return Path.home() / ".concinno" / "marketplace.lock"


def _acquire_lock(*, now: float | None = None) -> Path:
    """Acquire the marketplace lock via atomic mkdir.

    Args:
        now: Override clock (test injection). When omitted uses
            :func:`time.time`.

    Returns:
        Path of the acquired lock dir.

    Raises:
        InstallError: When another install is already in flight and the
            existing lock is fresh.
    """
    now = now if now is not None else time.time()
    lock = _lock_path()
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        lock.mkdir(exist_ok=False)
        return lock
    except FileExistsError:
        # Stale lock recovery: clear if older than TTL.
        try:
            stat = lock.stat()
        except OSError as exc:
            raise InstallError(f"lock stat failed: {exc}") from exc
        age = now - stat.st_mtime
        if age <= LOCK_STALE_TTL_SEC:
            raise InstallError(
                "another marketplace install is in progress "
                f"(lock age {int(age)}s); retry in a moment"
            )
        # Stale: clear the dir. We use rmdir so we never delete files
        # the user might have written here by mistake.
        try:
            lock.rmdir()
        except OSError as exc:
            raise InstallError(f"stale lock cleanup failed: {exc}") from exc
        try:
            lock.mkdir(exist_ok=False)
            return lock
        except OSError as exc:
            raise InstallError(f"lock acquire after cleanup failed: {exc}") from exc


def _release_lock(lock: Path) -> None:
    """Best-effort release of the lock dir. Never raises."""
    try:
        lock.rmdir()
    except OSError:
        pass


def _validate_version(version: str | None) -> str | None:
    """Validate a version pin or return None for "latest"."""
    if version is None:
        return None
    if not isinstance(version, str):
        raise InstallError("version must be a string or None")
    if not _VERSION_RE.match(version):
        raise InstallError(
            "version pin contains illegal characters; expected PEP 440"
        )
    return version


def _build_install_args(name: str, version: str | None) -> list[str]:
    """Compose the ``pip install`` argv with no shell injection paths."""
    if version is None:
        spec = name
    else:
        spec = f"{name}=={version}"
    return [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-input",
        spec,
    ]


def _build_uninstall_args(name: str) -> list[str]:
    """Compose the ``pip uninstall -y`` argv."""
    return [
        sys.executable,
        "-m",
        "pip",
        "uninstall",
        "--disable-pip-version-check",
        "-y",
        name,
    ]


def install_pkg(
    name: str,
    version: str | None = None,
    *,
    runner: Callable[..., Any] | None = None,
    skip_lock: bool = False,
) -> InstallResult:
    """Install a ``concinno-skills-*`` distribution via pip.

    Args:
        name: Distribution name (must match ``concinno-skills-<slug>``).
        version: Optional pinned version. When None, pip resolves to
            the latest compatible release.
        runner: Optional callable replacing :func:`subprocess.run`
            (test injection — must accept the same args/kwargs).
        skip_lock: When True, skip the file lock (test injection only).

    Returns:
        :class:`InstallResult` capturing stdout/stderr/returncode.

    Raises:
        InstallError: Validation failure or concurrent install in
            flight.
    """
    if not is_valid_dist_name(name):
        raise InstallError(
            f"package name {name!r} does not match "
            "concinno-skills-<slug>; refusing"
        )
    version = _validate_version(version)

    args = _build_install_args(name, version)
    runner = runner or subprocess.run

    lock_handle: Path | None = None
    if not skip_lock:
        lock_handle = _acquire_lock()
    try:
        proc = runner(
            args,
            capture_output=True,
            text=True,
            timeout=PIP_TIMEOUT_SEC,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        if lock_handle is not None:
            _release_lock(lock_handle)
        return InstallResult(
            ok=False,
            stdout=str(getattr(exc, "stdout", "") or ""),
            stderr=f"pip install timed out after {PIP_TIMEOUT_SEC}s",
            return_code=-1,
        )
    finally:
        if lock_handle is not None:
            _release_lock(lock_handle)

    rc = int(getattr(proc, "returncode", 1))
    return InstallResult(
        ok=rc == 0,
        stdout=str(getattr(proc, "stdout", "") or ""),
        stderr=str(getattr(proc, "stderr", "") or ""),
        return_code=rc,
    )


def uninstall_pkg(
    name: str,
    *,
    runner: Callable[..., Any] | None = None,
    skip_lock: bool = False,
) -> InstallResult:
    """Uninstall a ``concinno-skills-*`` distribution via pip.

    See :func:`install_pkg` for arg semantics.
    """
    if not is_valid_dist_name(name):
        raise InstallError(
            f"package name {name!r} does not match "
            "concinno-skills-<slug>; refusing"
        )

    args = _build_uninstall_args(name)
    runner = runner or subprocess.run

    lock_handle: Path | None = None
    if not skip_lock:
        lock_handle = _acquire_lock()
    try:
        proc = runner(
            args,
            capture_output=True,
            text=True,
            timeout=PIP_TIMEOUT_SEC,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        if lock_handle is not None:
            _release_lock(lock_handle)
        return InstallResult(
            ok=False,
            stdout=str(getattr(exc, "stdout", "") or ""),
            stderr=f"pip uninstall timed out after {PIP_TIMEOUT_SEC}s",
            return_code=-1,
        )
    finally:
        if lock_handle is not None:
            _release_lock(lock_handle)

    rc = int(getattr(proc, "returncode", 1))
    return InstallResult(
        ok=rc == 0,
        stdout=str(getattr(proc, "stdout", "") or ""),
        stderr=str(getattr(proc, "stderr", "") or ""),
        return_code=rc,
    )
