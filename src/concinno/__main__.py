"""Allow ``python -m concinno ...`` invocation.

The ``concinno`` console script (registered via ``[project.scripts]`` in
``pyproject.toml``) is the canonical entry point, but many Python users
reach for ``python -m <pkg>`` by convention — especially when multiple
Python interpreters are present and they want to pin which one runs the
CLI. This shim exists to honour that convention.
"""

from __future__ import annotations

from concinno.cli.main import main

if __name__ == "__main__":
    main()
