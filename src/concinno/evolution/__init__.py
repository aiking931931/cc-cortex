"""concinno.evolution — Hermes Port wave-3 HP5 (W4 / 4.6.0).

Optional GEPA (Genetic Pareto-efficient evolutionary search via LLM
reflection) integration. The upstream ``gepa`` package is MIT,
imported lazily from :mod:`concinno.evolution.gepa_adapter` so that
``pip install concinno`` keeps a zero-runtime-dep core. Operators
who want the search loop run::

    pip install "concinno[evolution]"

After install, :class:`GepaAdapter` exposes the minimal seam between
concinno's Skill / prompt artefacts and a GEPA search run.

Why optional
------------
GEPA's reflection step calls an external LLM. Even with concinno's
own caching that is real cost; we do not pay for it on the silent
no-op path. The adapter raises a precise ``EvolutionExtraNotInstalled``
when the operator forgets the extras, so failure mode is "install
the extra, retry" rather than a generic ``ModuleNotFoundError``.

ZIQ alignment
-------------
Per the W4 ship plan, GEPA fits the ``posterior ∝ SPS × FTRL``
shape: GEPA's Pareto frontier acts as the SPS scorer (structural
prior over candidate prompts), concinno's existing FTRL outcome bus
provides the on-line learning signal. Wiring the two is the next
step (see ``gepa_adapter.GepaAdapter.attach_outcome_bus``); first
ship is API-shape only so consumers can build against it without
waiting for the full integration.

Public surface (4.6.0)
----------------------
* :class:`GepaAdapter` — wraps a ``gepa.run`` call with concinno's
  artefact loader / saver.
* :class:`EvolutionExtraNotInstalled` — distinct exception so callers
  can give a tailored install hint instead of a stacktrace.
* :func:`is_available` — cheap probe (no import side-effects); use
  in CLI ``concinno features`` to surface the extras state.
"""

from __future__ import annotations

from concinno.evolution.gepa_adapter import (
    EvolutionExtraNotInstalled,
    GepaAdapter,
    is_available,
)

__all__ = [
    "EvolutionExtraNotInstalled",
    "GepaAdapter",
    "is_available",
]
