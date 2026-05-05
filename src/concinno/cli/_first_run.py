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
* Non-TTY ``stderr`` — when ``sys.stderr.isatty()`` is False the banner
  is suppressed so CI logs, ``CMD ["concinno", ...]`` Docker bootstrap,
  and ``concinno features list 2>&1 | jq`` style pipelines stay clean.
* ``touch ~/.concinno/.4_0_0_seen`` — manual opt-out.
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

__all__ = [
    "FIRST_RUN_MARKER_PATH",
    "BANNER_DISABLE_ENV",
    "is_banner_env_disabled",
    "is_stderr_tty",
    "mark_seen",
    "marker_exists",
    "maybe_print_first_run_banner",
    "should_skip_banner_for_argv",
]

# In-memory session flag flipped on first read-only-HOME OSError. Once
# set, ``maybe_print_first_run_banner`` short-circuits for the rest of
# this Python process so a user with a read-only ``$HOME`` does not see
# the banner re-printed on every subsequent invocation in the same
# process (see Red 1 R1.4 + Red 3 R3.2 — convergent infinite-loop bug).
_session_marker_failed: bool = False

# Module-level latch that ensures the OSError warning fires exactly once
# per process even if ``mark_seen`` is called repeatedly (e.g. tests).
_oserror_warning_emitted: bool = False

_logger = logging.getLogger("concinno.first_run")

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


def is_stderr_tty() -> bool:
    """Return True iff ``sys.stderr`` reports as an interactive TTY.

    Wrapped as a module-level function so tests (which run under pytest's
    capsys, where ``sys.stderr`` is a non-TTY ``EncodedFile`` whose
    C-level ``isatty`` cannot be monkeypatched on the class) can stub
    this gate directly via ``monkeypatch.setattr`` instead of fighting
    the capture machinery.
    """
    isatty = getattr(sys.stderr, "isatty", None)
    return bool(callable(isatty) and isatty())


def marker_exists() -> bool:
    """Return True if the on-disk first-run marker is present."""
    return _marker_path().exists()


def mark_seen() -> Path:
    """Best-effort write the marker so the banner stops firing.

    Used by ``set-profile`` to acknowledge the user has consciously
    chosen a baseline (chicken-and-egg: invoking ``set-profile``
    immediately silences the banner).

    Returns the marker path even on best-effort failure so callers can
    log it; the IO failure itself is logged at WARNING (exactly once
    per process via :data:`_oserror_warning_emitted`) and surfaced via
    :data:`_session_marker_failed` so :func:`maybe_print_first_run_banner`
    can short-circuit subsequent invocations and avoid the read-only
    ``$HOME`` infinite-banner loop (Red 1 R1.4 + Red 3 R3.2).
    """
    global _session_marker_failed, _oserror_warning_emitted

    marker = _marker_path()
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        # Write a timestamp body so the file isn't a 0-byte mystery on
        # disk; use UTC ISO-8601 for unambiguous parsing.
        marker.write_text(
            datetime.now(timezone.utc).isoformat(timespec="seconds") + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        # Best-effort: the calling CLI command should still proceed,
        # but flip the in-memory session flag so we do not re-print
        # the banner on every subsequent call within this process.
        _session_marker_failed = True
        if not _oserror_warning_emitted:
            _oserror_warning_emitted = True
            _logger.warning(
                "concinno first-run marker could not be written to %s "
                "(%s); banner will be suppressed for the rest of this "
                "process. Set %s=0 to silence permanently or fix HOME "
                "permissions to persist the marker.",
                marker,
                exc,
                BANNER_DISABLE_ENV,
            )
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
    * ``sys.stderr`` is not a TTY (CI logs / Docker bootstrap / pipes).
    * The on-disk marker already exists.
    * The current argv invokes a banner-skipping subcommand
      (``set-profile``) — chicken-and-egg.
    * A previous :func:`mark_seen` call in this process raised OSError
      (read-only ``$HOME``); the in-memory ``_session_marker_failed``
      flag prevents the infinite-banner loop documented in Red 1 R1.4
      and Red 3 R3.2.

    Output goes to ``stderr`` so it never pollutes stdout consumers
    (e.g. ``concinno features list --json | jq``), and the TTY check
    above ensures it never pollutes stderr consumers either.
    """
    if is_banner_env_disabled():
        return False
    if should_skip_banner_for_argv():
        return False
    if _session_marker_failed:
        # The marker write already failed this process — banner has
        # been shown once and we refuse to re-print on every call.
        return False
    # Suppress on non-TTY stderr so CI logs / Docker boot / pipes stay
    # clean. Routed through ``is_stderr_tty`` so the test suite (whose
    # capsys-wrapped stderr is a non-patchable C-level method_descriptor)
    # can stub the gate at module scope.
    if not is_stderr_tty():
        return False
    marker = _marker_path()
    if marker.exists():
        return False
    # Best-effort touch — failure flips ``_session_marker_failed`` so a
    # follow-up invocation will short-circuit instead of re-printing.
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
