"""concinno.gui.auth — Bearer-token auth for the localhost GUI.

@module concinno.gui.auth
@responsibility Generate / persist / read / verify a per-process bearer
    token used to gate every ``/api/*`` route on the localhost
    Concinno GUI. Token lives on disk so VS Code (or the federated
    switcher) can pick it up without inter-process plumbing.

Design notes:

* Token path is OS-aware:
    * Windows: ``%LOCALAPPDATA%\\concinno\\gui_token`` -- per-user ACL is
      the OS-level guard (the directory inherits user-only perms when
      created under LocalAppData; we further chmod 0600 on POSIX hosts).
    * POSIX: ``~/.concinno/gui_token`` mode 0600 (atomic rename pattern
      so chmod fires *before* the file is publicly visible).
* Atomic write: ``<path>.tmp`` -> ``os.chmod(0600)`` -> ``os.rename``.
  Mirrors the race-condition mitigation called out in the design doc
  §7-3 (chmod-after-create races on multi-user POSIX hosts).
* Verifier uses :func:`hmac.compare_digest` so a length-leaky string
  compare cannot be timing-side-channel'd by a same-host attacker.
* Token is generated with :func:`secrets.token_urlsafe` (32 bytes ->
  ~43 base64url chars). Cleared on each ``create_app()``.
* This module never logs the token value. Path is OK; value is not.

Used by: ``concinno.gui.server.create_app`` (writes), VS Code extension
``aiKing.gui.openConcinno`` (reads), federated switcher (reads from both
``~/.concinno/gui_token`` and ``~/.sancio/gui_token``).
"""

from __future__ import annotations

import hmac
import os
import secrets
import sys
from pathlib import Path
from typing import Optional

__all__ = [
    "get_token_path",
    "generate_token",
    "write_token",
    "read_token",
    "verify_token_header",
    "TokenAuthError",
]


class TokenAuthError(RuntimeError):
    """Raised when token-file operations fail in a fail-fast manner."""


def get_token_path() -> Path:
    """Return the OS-appropriate path where the GUI bearer token is stored.

    * Windows: ``%LOCALAPPDATA%\\concinno\\gui_token`` (falls back to
      ``~/.concinno/gui_token`` if ``LOCALAPPDATA`` is unset).
    * POSIX: ``~/.concinno/gui_token``.
    """
    if sys.platform == "win32":
        local_app = os.environ.get("LOCALAPPDATA")
        if local_app:
            return Path(local_app) / "concinno" / "gui_token"
    return Path.home() / ".concinno" / "gui_token"


def generate_token() -> str:
    """Cryptographically strong URL-safe token (~43 chars, 256-bit entropy)."""
    return secrets.token_urlsafe(32)


def write_token(token: str, *, path: Optional[Path] = None) -> Path:
    """Atomically write ``token`` to the token file.

    Order of operations (matters on POSIX multi-user hosts):

    1. Ensure parent dir exists.
    2. Write to ``<path>.tmp``.
    3. ``chmod 0600`` on the tmp file (POSIX only -- Windows uses ACL
       inheritance from parent).
    4. ``os.replace`` (atomic on the same filesystem).

    The chmod-before-rename order means readers never observe the file
    with default umask perms; either it does not exist yet, or it
    exists with mode 0600.

    Args:
        token: Opaque bearer token string. Must be non-empty.
        path: Override destination (defaults to :func:`get_token_path`).

    Returns:
        The final path the token was written to.

    Raises:
        TokenAuthError: If the token is empty or write fails.
    """
    if not isinstance(token, str) or not token:
        raise TokenAuthError("token must be a non-empty string")

    target = path or get_token_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    try:
        tmp.write_text(token, encoding="utf-8")
        if sys.platform != "win32":
            try:
                os.chmod(tmp, 0o600)
            except OSError as e:  # pragma: no cover - rare
                raise TokenAuthError(f"chmod 0600 on {tmp}: {e}") from e
        os.replace(tmp, target)
    except Exception:
        # Best-effort cleanup on failure.
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return target


def read_token(*, path: Optional[Path] = None) -> Optional[str]:
    """Read the token file. Return None if absent / unreadable.

    Used by clients (VS Code extension, federated switcher) to fetch
    the current per-process token.
    """
    target = path or get_token_path()
    try:
        if not target.is_file():
            return None
        value = target.read_text(encoding="utf-8").strip()
        return value or None
    except OSError:
        return None


def verify_token_header(
    authorization: Optional[str], expected_token: str,
) -> bool:
    """Return True iff ``authorization`` is exactly ``Bearer <expected_token>``.

    Comparison is constant-time via :func:`hmac.compare_digest` to
    defeat timing side channels. Case-insensitive only on the
    ``Bearer`` scheme prefix; the token itself is case-sensitive.
    """
    if not authorization or not expected_token:
        return False
    parts = authorization.strip().split(None, 1)
    if len(parts) != 2:
        return False
    scheme, token = parts
    if scheme.lower() != "bearer":
        return False
    # Strip incidental quoting (`Bearer "abc"`).
    token = token.strip().strip('"').strip("'")
    return hmac.compare_digest(token, expected_token)
