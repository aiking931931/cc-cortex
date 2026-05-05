"""Pre-upload check helper for would-be ``twine upload`` callers.

The 4.2.1 race had two failure modes the markdown ``Active`` section
couldn't catch:

1. Two sessions both think they own the lock → both upload → second
   gets PyPI 400 already-exists.
2. A version was already published in a previous session that didn't
   advance ``RELEASE_COORDINATION.md`` → next session re-uploads same
   version → 400 again.

This module returns a single ``(ok, reason)`` decision so the eventual
twine wrapper (next ship cycle) can short-circuit before invoking
``twine`` and surface a precise reason instead of a 400 stack trace.

We deliberately do **not** wrap ``twine`` here — wrapping is a separate
concern that needs its own design (subprocess args, retries, transcript
logging).  This file only owns the read-only "should we even try?"
decision so the wrapper can plug it in cleanly.
"""

from __future__ import annotations

import urllib.error

from .release_lock import ReleaseLock, pypi_version_taken


def check_before_upload(
    pkg: str,
    ver: str,
    session: str = "",
    require_lock_held: bool = True,
) -> tuple[bool, str]:
    """Return ``(ok, reason)`` for whether to proceed with twine upload.

    Args:
        pkg: Package name (e.g. ``"concinno"``).
        ver: Version string (e.g. ``"4.2.2"``).
        session: Current session id. Used to verify the holder of the
            ReleaseLock matches *us* — a different holder means a
            concurrent session is in the middle of publishing.
        require_lock_held: If True (default), reject when the
            :class:`ReleaseLock` is not held by *session*. Set False
            for callers that intend to acquire later in the flow.

    Returns:
        ``(True, "")`` when safe to upload.
        ``(False, <reason>)`` otherwise. The reason is human-readable
        and stable enough for the LLM to route on (e.g. mention
        "already on pypi" / "lock held by other").
    """
    # PyPI is the source of truth for "already exists" — check it first
    # because a positive result is a hard veto regardless of lock state.
    try:
        if pypi_version_taken(pkg, ver):
            return False, (
                f"{pkg} {ver} is already on PyPI — upload would 400. "
                f"Bump the version before retrying."
            )
    except urllib.error.URLError as exc:
        # Network down — fail-closed: treat unknown as "do not upload"
        # rather than reintroduce the race.
        return False, (
            f"PyPI version check failed ({exc!r}). "
            f"Refusing to upload without confirmation that {ver} is free."
        )

    if require_lock_held:
        held = ReleaseLock().check(pkg)
        if held is None:
            return False, (
                f"No active release lock for {pkg}. "
                f"Acquire via `concinno release-lock acquire {pkg} {ver}` first."
            )
        holder = str(held.get("holder_session", ""))
        if session and holder and holder != session:
            return False, (
                f"Release lock for {pkg} held by session {holder!r} "
                f"(we are {session!r}); concurrent publish in progress."
            )
        held_version = str(held.get("version", ""))
        if held_version and held_version != ver:
            return False, (
                f"Release lock for {pkg} is for version {held_version!r}, "
                f"not {ver!r}. Release the lock and re-acquire for {ver}."
            )

    return True, ""


__all__ = ["check_before_upload"]
