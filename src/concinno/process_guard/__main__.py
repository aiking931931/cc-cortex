"""Allow ``python -m concinno.process_guard`` to invoke the CLI.

Without this module the package import succeeds but ``runpy`` cannot find
an executable entry point, raising ``No module named
concinno.process_guard.__main__``. The user-invocable cortex-guard skill
documents ``python -m concinno.process_guard`` as the canonical command, so
we expose ``cli.main`` here for runpy to call.
"""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    main()
