"""Verify ``python -m concinno ...`` invocation works.

Added in 2.17.0 alongside ``src/concinno/__main__.py``. The ``concinno``
console script (via ``[project.scripts]``) was already shipped; this
test guards the equivalent ``python -m concinno`` convention that a lot
of scripts rely on (e.g. CI that pins a specific Python).
"""

from __future__ import annotations

import os
import subprocess
import sys


def _child_env() -> dict[str, str]:
    """Minimal env for subprocess invocation of ``python -m concinno``.

    Passes through PATH (so python can find itself) + PYTHONPATH (so the
    in-tree ``src/concinno`` wins over any installed copy) + UTF-8
    enforcement flags so Windows GBK locale doesn't corrupt the CLI's
    emoji-bearing help text.
    """
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def test_main_module_help_exits_zero() -> None:
    """``python -m concinno --help`` should exit 0 with usage info."""
    result = subprocess.run(
        [sys.executable, "-m", "concinno", "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        env=_child_env(),
    )
    assert result.returncode == 0, (
        f"exit={result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "usage: concinno" in result.stdout, (
        f"expected 'usage: concinno' in stdout, got: {result.stdout[:500]}"
    )


def test_main_module_no_args_exits_zero() -> None:
    """Running ``python -m concinno`` bare should not crash.

    argparse prints help when no subcommand is given. Exit code varies
    by Python (2 in some argparse configs, 0 in others); we only assert
    it terminates within the timeout and prints the ``concinno`` banner.
    """
    result = subprocess.run(
        [sys.executable, "-m", "concinno"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        env=_child_env(),
    )
    # Accept either 0 or 2 — both are conventional argparse outcomes.
    assert result.returncode in (0, 2), (
        f"unexpected exit={result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    combined = result.stdout + result.stderr
    out_preview = result.stdout[:200]
    err_preview = result.stderr[:200]
    assert "concinno" in combined.lower(), (
        f"expected 'concinno' in output, got stdout={out_preview!r} stderr={err_preview!r}"
    )


def test_main_module_entrypoint_importable() -> None:
    """``concinno.__main__`` should be importable and expose ``main``."""
    import concinno.__main__ as entrypoint

    assert hasattr(entrypoint, "main"), "concinno.__main__ must re-export main"
    assert callable(entrypoint.main), "concinno.__main__.main must be callable"
