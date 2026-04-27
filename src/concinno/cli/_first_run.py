"""concinno._first_run — show one-time post-4.0.0 onboarding banner.

Prints the default-OFF rationale + set-profile shortcut on the very first
invocation of any ``concinno ...`` CLI command after 4.0.0+. Idempotent
via ``~/.concinno/.4_0_0_seen`` marker file (touched on first display).

Suppress paths:

* ``concinno features set-profile {strict|permissive|dev}`` — applying any
  profile implies the user has consciously chosen a baseline; the
  ``set-profile`` command writes the marker itself via :func:`mark_seen`
  *and* skips the banner (chicken-and-egg) by routing through
  :func:`should_skip_banner_for_argv`.
* ``CONCINNO_FIRST_RUN_BANNER=0`` (also ``false`` / ``no`` / ``off``) —
  silence the banner entirely without touching the marker, e.g. for CI.
* ``touch ~/.concinno/.4_0_0_seen`` — manual opt-out.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

__all__ = [
    "FIRST_RUN_MARKER_PATH",
    "BANNER_DISABLE_ENV",
    "is_banner_env_disabled",
    "mark_seen",
    "marker_exists",
    "maybe_print_first_run_banner",
    "should_skip_banner_for_argv",
]

# Env var that lets users / CI suppress the one-time banner without
# writing the on-disk marker (so a real first run on a developer's
# laptop later still shows it). Truthy values that disable the banner:
# ``0`` / ``false`` / ``no`` / ``off`` (case-insensitive).
BANNER_DISABLE_ENV = "CONCINNO_FIRST_RUN_BANNER"

_DISABLE_VALUES = frozenset({"0", "false", "no", "off"})

# Subcommand names whose semantics imply the user has already engaged
# with the 4.0.0 onboarding flow — invoking them must NOT trigger the
# banner. Currently: only ``set-profile`` (chicken-and-egg).
_BANNER_SKIPPING_SUBCOMMANDS: tuple[str, ...] = ("set-profile",)


def _user_home() -> Path:
    """Resolve ~/.concinno/ — patched in tests to redirect to tmp_path."""
    return Path(os.environ.get("HOME") or os.path.expanduser("~"))


def _marker_path() -> Path:
    return _user_home() / ".concinno" / ".4_0_0_seen"


# Callable, evaluated lazily so tests can monkeypatch HOME after import.
FIRST_RUN_MARKER_PATH = _marker_path


def is_banner_env_disabled() -> bool:
    """Return True if ``CONCINNO_FIRST_RUN_BANNER`` is set to a falsy value."""
    raw = os.environ.get(BANNER_DISABLE_ENV)
    if raw is None:
        return False
    return raw.strip().lower() in _DISABLE_VALUES


def marker_exists() -> bool:
    """Return True if the on-disk first-run marker is present."""
    return _marker_path().exists()


def mark_seen() -> Path:
    """Best-effort write the marker so the banner stops firing.

    Used by ``set-profile`` to acknowledge the user has consciously
    chosen a baseline (chicken-and-egg: invoking ``set-profile``
    immediately silences the banner).

    Returns the marker path even on best-effort failure so callers can
    log it; the IO failure itself is swallowed (non-fatal).
    """
    marker = _marker_path()
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        # Write a timestamp body so the file isn't a 0-byte mystery on
        # disk; use UTC ISO-8601 for unambiguous parsing.
        marker.write_text(
            datetime.now(timezone.utc).isoformat(timespec="seconds") + "\n",
            encoding="utf-8",
        )
    except OSError:
        # Best-effort: the calling CLI command should still proceed.
        pass
    return marker


def should_skip_banner_for_argv(argv: list[str] | None = None) -> bool:
    """Decide whether to skip banner display for a given argv vector.

    Currently skips when the user invoked any ``set-profile`` subcommand
    (chicken-and-egg: applying a profile already implies acknowledgement).

    ``argv`` defaults to ``sys.argv[1:]`` when None.
    """
    tokens = list(argv) if argv is not None else sys.argv[1:]
    return any(tok in _BANNER_SKIPPING_SUBCOMMANDS for tok in tokens)


def maybe_print_first_run_banner() -> bool:
    """Return True if the banner was printed; False otherwise.

    Skips silently when:

    * ``CONCINNO_FIRST_RUN_BANNER`` env var is falsy.
    * The on-disk marker already exists.
    * The current argv invokes a banner-skipping subcommand
      (``set-profile``) — chicken-and-egg.

    Output goes to ``stderr`` so it never pollutes stdout consumers
    (e.g. ``concinno features list --json | jq``).
    """
    if is_banner_env_disabled():
        return False
    if should_skip_banner_for_argv():
        return False
    marker = _marker_path()
    if marker.exists():
        return False
    # Best-effort touch — failure is non-fatal (banner still prints).
    mark_seen()

    from concinno import __version__

    msg = (
        f"Welcome to concinno {__version__}. SEMVER-MAJOR 4.0.0+ ships features\n"
        "default-OFF to keep solo-dev workflows friction-free. To enable the\n"
        "recommended strict bundle, run:\n"
        "    concinno features set-profile strict\n"
        "To acknowledge default-OFF and silence this banner, run:\n"
        "    concinno features set-profile permissive\n"
        "To suppress this banner without writing the marker, set:\n"
        f"    {BANNER_DISABLE_ENV}=0\n"
        f"Marker stored at: {marker}\n"
    )
    print(msg, file=sys.stderr)
    return True
