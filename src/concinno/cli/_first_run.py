"""concinno._first_run — show one-time post-4.0.0 onboarding banner.

Prints the default-OFF rationale + set-profile shortcut on the very first
invocation of any ``concinno ...`` CLI command after 4.0.0+. Idempotent
via ``~/.concinno/.4_0_0_seen`` marker file (touched on first display).

Suppress: ``concinno features set-profile permissive`` already implies
the user has read the message; touch the marker so we don't reshow.
Or: ``touch ~/.concinno/.4_0_0_seen`` manually.
"""
from __future__ import annotations

import os
from pathlib import Path

__all__ = ["maybe_print_first_run_banner", "FIRST_RUN_MARKER_PATH"]


def _user_home() -> Path:
    """Resolve ~/.concinno/ — patched in tests to redirect to tmp_path."""
    return Path(os.environ.get("HOME") or os.path.expanduser("~"))


def _marker_path() -> Path:
    return _user_home() / ".concinno" / ".4_0_0_seen"


# Callable, evaluated lazily so tests can monkeypatch HOME after import.
FIRST_RUN_MARKER_PATH = _marker_path


def maybe_print_first_run_banner() -> bool:
    """Return True if the banner was printed; False otherwise."""
    marker = _marker_path()
    if marker.exists():
        return False
    # Best-effort touch — failure is non-fatal (banner still prints).
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch(exist_ok=True)
    except OSError:
        pass

    from concinno import __version__

    msg = (
        f"Welcome to concinno {__version__}. SEMVER-MAJOR 4.0.0+ ships features\n"
        "default-OFF to keep solo-dev workflows friction-free. To enable the\n"
        "recommended strict bundle, run:\n"
        "    concinno features set-profile strict\n"
        "To see what each feature does:\n"
        "    concinno features list\n"
        "This message will not appear again. State stored at:\n"
        f"    {marker}\n"
    )
    print(msg)
    return True
