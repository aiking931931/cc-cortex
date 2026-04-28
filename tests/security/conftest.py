"""tests.security shared fixtures.

Reset the process-wide ``_shared_breaker_registry`` between tests so
each ``CircuitBreakerGuard`` constructed with the default
``share_state_with=None`` starts from a clean slate. The singleton
was introduced in 4.5.0 (carryover task #4 from the W2 R+B+G ship-
gate verdict) so that two default guards converge on the same
breaker state for a logical resource. Tests that want to assert
fresh counters need this isolation hook; without it, accumulated
failures from earlier cases leak across.

Tests that explicitly want shared state can still pass
``share_state_with=<other>`` between siblings — that path bypasses
the singleton lookup and is unaffected by this fixture.
"""

from __future__ import annotations

import pytest

from concinno.security.circuit_breaker_guard import reset_shared_breaker_registry


@pytest.fixture(autouse=True)
def _reset_shared_breaker_registry() -> None:
    reset_shared_breaker_registry()
    yield
    reset_shared_breaker_registry()
