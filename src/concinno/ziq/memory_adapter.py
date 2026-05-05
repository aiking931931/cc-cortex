"""concinno.ziq_memory_adapter — Lazy adapter to ``concinno-skills-memory``.

@module ziq_memory_adapter
@responsibility Bridge the ZIQ outcome bus to the noise-filter callback
    contract published by the standalone ``concinno-skills-memory``
    sub-package. The adapter lives in core ``concinno`` (not the
    sub-package) so that the bus has a registration entry point even
    when the sub-package is not installed.
@dependencies concinno.ziq_outcome_bus (always)
              concinno_skills_memory.ziq_outcome (lazy / optional)
@exports register_memory_noise_filter, NOISE_FILTER_OUTCOME_NAME

Design
------
``concinno-skills-memory`` is a standalone 0-dep package — it cannot
import ``concinno``. The wiring direction is inverted: this module
in ``concinno`` does the registration on demand, importing the
sub-package lazily inside :func:`register_memory_noise_filter`.

When the sub-package is missing (``pip install concinno`` without
``concinno-skills-memory``), :func:`register_memory_noise_filter`
returns ``None`` silently — the bus simply has no subscriber for
``memory.noise_filter`` and ``emit()`` for that tunable becomes a
fan-out of zero.

Outcome shape mismatch (sub-agent K wave-2 fix)
-----------------------------------------------
The sub-package's :class:`NoiseFilterCallback` Protocol returns a
:class:`NoiseFilterOutcome` (a float in ``[0, 1]``) given
``(query, fetched_layer, fetched_relevance)``. The bus subscriber
contract is :class:`Callable[[Outcome], None]`. The adapter wraps
the sub-package callback so it can sit on the bus: the wrapper
extracts the three positional inputs from ``Outcome.metadata``
(producer convention), invokes the callback, and uses the returned
scalar as a *post-hoc* signal. The wrapper does not re-emit (that
would loop) — it lets downstream consumers (e.g. an FTRL learner
attached as a separate subscriber) read the metadata's
``noise_filter_score`` field if/when it is added.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

# Re-export the canonical name so callers depending only on concinno
# can still address the same string.
NOISE_FILTER_OUTCOME_NAME = "memory.noise_filter"

__all__ = [
    "NOISE_FILTER_OUTCOME_NAME",
    "register_memory_noise_filter",
    "is_memory_skills_available",
]


def is_memory_skills_available() -> bool:
    """Probe whether ``concinno-skills-memory`` is importable.

    Best-effort; never raises. Useful for CLI / GUI surfaces that
    want to surface "Memory skills unwired" advisory text without
    actually triggering the bus registration side effect.
    """
    try:
        import importlib

        importlib.import_module("concinno_skills_memory.ziq_outcome")
        return True
    except Exception:
        return False


def register_memory_noise_filter(
    callback_override: Callable[..., Any] | None = None,
) -> Callable[[], None] | None:
    """Subscribe ``concinno-skills-memory``'s noise filter to the bus.

    Args:
        callback_override: Optional bypass of the sub-package's
            ``reference_noise_filter`` — accepts any callable matching
            ``NoiseFilterCallback``. Used by tests to inject spies.

    Returns:
        Unsubscribe callable on success.
        ``None`` when ``concinno-skills-memory`` is not importable
        (silent no-op so that ``concinno`` core remains usable
        without the sub-package).

    Behaviour:
        * Lazy import — only triggers when called.
        * Idempotent at the bus level: the bus dedupes by callback
          identity, so calling this twice with the same override
          subscribes twice (each emit fires twice). Tests should use
          the returned unsubscribe to clean up.
    """
    try:
        from concinno_skills_memory.ziq_outcome import (
            NOISE_FILTER_OUTCOME_NAME as _NAME,
        )
        from concinno_skills_memory.ziq_outcome import (
            reference_noise_filter,
        )
    except ImportError:
        return None
    except Exception:
        # Defensive: a corrupt sub-package install should not crash
        # the host. Fail closed (no subscription) rather than break
        # ``concinno`` core import.
        return None

    callback = callback_override or reference_noise_filter

    def _bus_subscriber(outcome: Any) -> None:
        """Bridge bus Outcome → sub-package callback signature.

        Pulls (query, fetched_layer, fetched_relevance) from
        ``outcome.metadata``. Missing fields fall back to safe
        defaults so a partially-populated emit cannot crash the
        callback. The callback's return value is a noise-filter
        score that downstream learners can opt to read; we attach
        it back onto the metadata in-place when possible (Outcome
        is frozen, so we no-op the attach when it is not mutable).
        """
        md = getattr(outcome, "metadata", {}) or {}
        try:
            query = str(md.get("query", "") or "")
            fetched_layer = int(md.get("fetched_layer", 1))
            fetched_relevance = float(md.get("fetched_relevance", 0.0))
            score = callback(query, fetched_layer, fetched_relevance)
        except Exception:
            # Sub-package callback raised — swallow so the bus's other
            # subscribers (if any) still fire. The bus itself also
            # catches subscriber exceptions but we belt-and-brace
            # because we own this adapter.
            return
        # Best-effort post-hoc score attachment; Outcome is a frozen
        # dataclass, so ``md`` is a separate dict that the producer
        # may inspect after the bus fan-out completes.
        try:
            md["noise_filter_score"] = float(score)
        except Exception:
            pass

    from concinno.ziq.outcome_bus import get_bus

    return get_bus().subscribe(_NAME, _bus_subscriber)
