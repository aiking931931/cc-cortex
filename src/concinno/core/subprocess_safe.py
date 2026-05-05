"""concinno.core.subprocess_safe — Subprocess that never flashes a console.

@module core.subprocess_safe
@responsibility Drop-in replacements for ``subprocess.run`` and
    ``subprocess.Popen`` that suppress the transient console window on
    Windows. On non-Windows platforms the wrappers forward to the stdlib
    untouched. Auto-applies ``creationflags=CREATE_NO_WINDOW`` only when
    the caller did not already pass ``creationflags`` or ``startupinfo``.
@dependencies stdlib subprocess + sys
@exports run, Popen, CREATE_NO_WINDOW, FLAGS

Background:
    On Windows, when the parent process has no attached console (e.g. CC
    spawned via pythonw.exe / a service / a hook), Python's
    ``subprocess`` will allocate one for the child unless explicitly
    suppressed. Each suppressed-or-not bare ``subprocess.run(["git",
    ...])`` flashes a black CMD window for a few hundred milliseconds.
    Hot-path call sites (per-tool guards, per-Stop git diffs, per-turn
    notify Popen) make the flash pattern visible to the user and is the
    direct user-reported "煩" complaint of 2026-04-25.

Why a wrapper instead of monkey-patching stdlib:
    Monkey-patching ``subprocess`` globally affects user code, plugin
    code, and library code that has nothing to do with concinno. A
    targeted wrapper is the smallest blast radius — concinno's own call
    sites import this module; everything else is unchanged.

Usage:
    from concinno.core.subprocess_safe import run, Popen
    run(["git", "diff", "--name-only", "HEAD"], capture_output=True)
    Popen(["powershell", "-Command", script])

Or as a drop-in module replacement::

    from concinno.core import subprocess_safe as subprocess
    subprocess.run(...)
    subprocess.Popen(...)
"""

from __future__ import annotations

import subprocess as _subprocess
import sys as _sys
from typing import Any

# Windows ``CREATE_NO_WINDOW`` magic value (0x08000000). Defined here as a
# constant so non-Windows imports do not need ``subprocess.CREATE_NO_WINDOW``
# to exist — it's Windows-only on the stdlib.
CREATE_NO_WINDOW: int = 0x08000000

# Bitmask applied to subprocess calls when the caller didn't supply one.
# Empty (0) on non-Windows, CREATE_NO_WINDOW on win32.
FLAGS: int = CREATE_NO_WINDOW if _sys.platform == "win32" else 0


def _inject_flags(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Add ``creationflags=FLAGS`` when the caller didn't already opt in.

    Respects callers that intentionally passed their own ``creationflags``
    (bitwise-or with FLAGS rather than overwriting), and skips entirely
    when the caller passed a custom ``startupinfo`` (assume they know what
    they're doing — pywinauto / hidden_worker etc).
    """
    if _sys.platform != "win32":
        return kwargs
    if "startupinfo" in kwargs:
        return kwargs
    existing = kwargs.get("creationflags", 0)
    kwargs["creationflags"] = existing | FLAGS
    return kwargs


def run(*args: Any, **kwargs: Any) -> _subprocess.CompletedProcess:
    """``subprocess.run`` with auto-suppressed Windows console.

    Forwards all positional and keyword arguments to ``subprocess.run``.
    On Windows, sets ``creationflags |= CREATE_NO_WINDOW`` unless the
    caller already passed a ``startupinfo`` object.
    """
    kwargs = _inject_flags(kwargs)
    return _subprocess.run(*args, **kwargs)


def Popen(*args: Any, **kwargs: Any) -> _subprocess.Popen:  # noqa: N802
    """``subprocess.Popen`` with auto-suppressed Windows console.

    Mirrors :func:`run`. Returns a regular ``subprocess.Popen`` instance
    so existing call sites that read ``.stdout`` / ``.communicate()`` /
    ``.poll()`` keep working unchanged.
    """
    kwargs = _inject_flags(kwargs)
    return _subprocess.Popen(*args, **kwargs)


__all__ = ["run", "Popen", "CREATE_NO_WINDOW", "FLAGS"]
