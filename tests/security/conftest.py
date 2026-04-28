"""tests.security shared fixtures (intentionally minimal).

The breaker-registry reset that this file used to ship was promoted
to ``tests/conftest.py`` in W3 (post-ship coda) so the same
isolation applies across the whole test tree, not just the security
suite. Keeping a local autouse copy was a cascade — every security
test reset the registry twice before and twice after itself (root
autouse + local autouse). The audit (W3.x carryover #1, HIGH-1)
removed the duplicate.

If the root fixture is ever relocated, restore the local one here:

.. code-block:: python

    import pytest
    from concinno.security.circuit_breaker_guard import (
        reset_shared_breaker_registry,
    )

    @pytest.fixture(autouse=True)
    def _reset_shared_breaker_registry() -> None:
        reset_shared_breaker_registry()
        yield
        reset_shared_breaker_registry()
"""

from __future__ import annotations
