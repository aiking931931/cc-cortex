"""concinno.gui — localhost config dashboard.

@module concinno.gui
@responsibility Visualize and edit every ``FEATURE_META`` switch and
    ``~/.concinno/*.json`` file (plus harness-layer permission read-only)
    without round-tripping through an LLM. Pairs with
    ``~/.claude/rules/switches.md`` two-layer gate SOP.

The LLM can change config via normal CLI / file writes; the operator can
change config via this GUI. Both paths converge on the same config SoT.
ZIQ auto-tune continues to write its own posterior file — the GUI
displays that alongside the manual value so priority ties are obvious.

Launched with::

    pip install 'concinno[gui]'
    concinno gui [--host 127.0.0.1] [--port 8400]
"""

from __future__ import annotations

__all__ = ["create_app", "run"]


def create_app():
    """Lazy import — ``fastapi`` is an optional extra."""
    from .server import create_app as _f
    return _f()


def run(host: str = "127.0.0.1", port: int = 8400, reload: bool = False) -> None:
    """Entry point for ``concinno gui``."""
    from .server import run as _r
    _r(host=host, port=port, reload=reload)
