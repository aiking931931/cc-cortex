"""Scope-completion tests for verbatim_relay 4.6.0.

These cover the second wave of callsites that the initial 4.6.0 ship did
not yet wire through ``with_feature_prefix``:

* ``concinno.cognitive_inject._build_summaries`` — the
  ``⚠ 相關糾正（摘要）`` correction-router warning that is fanned into
  ``additionalContext`` from ``build_rag_context``. Without branding,
  users see a stray ``⚠`` block in their transcript and assume Claude
  Code is hallucinating.

* ``concinno.hooks.wait_inject.build_context`` — the polling-watcher
  fan-in that surfaces ``📡 Active polling waits`` and recent status
  transitions. Likewise needed self-branding so the operator knows the
  block is Concinno output.

We do NOT cover ``concinno.polling.daemon`` — it logs via ``logger``
only and emits nothing into the LLM context.

Tests assert the prefix appears in default (``prefix``) mode and is
absent in legacy (``verbose``) mode, mirroring the assertion shape used
by the 4.6.0 baseline tests in ``test_relay_helpers.py``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Optional

import pytest

# ── Helpers ───────────────────────────────────────────────────


@dataclass
class _StubActive:
    """Minimal duck-type for ``polling.list_active`` records."""

    id: str
    kind: str
    status: str
    eta_seconds: int
    registered_at: str
    extra: dict


@dataclass
class _StubAlert:
    """Minimal duck-type for ``polling.read_alerts`` records."""

    id: str
    from_status: str
    to_status: str
    at: str
    last_status: str


def _patch_polling(
    monkeypatch: pytest.MonkeyPatch,
    *,
    actives: Iterable[_StubActive] = (),
    alerts: Iterable[_StubAlert] = (),
) -> None:
    """Install a fake ``concinno.polling`` module so ``wait_inject``
    can be exercised without a live wait queue on disk."""
    import sys
    import types

    fake = types.ModuleType("concinno.polling")
    fake.list_active = lambda: list(actives)  # type: ignore[attr-defined]
    fake.read_alerts = lambda drain=True: list(alerts)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "concinno.polling", fake)


# ── cognitive_inject._build_summaries ─────────────────────────


def test_cognitive_inject_summaries_branded_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default (``prefix``) mode wraps the correction summary."""
    monkeypatch.setenv("CONCINNO_VERBATIM_RELAY_MODE", "prefix")
    from concinno.cognitive_inject import _build_summaries

    learnings = [
        {
            "pattern_key": "verify-before-write",
            "correction_text": "always Read before Edit",
            "count": 3,
            "promoted": False,
        },
    ]
    out = _build_summaries(learnings, ["read", "edit"])
    assert out.startswith("[SHOW USER VERBATIM] [Concinno: cognitive_inject]")
    assert "相關糾正" in out
    assert "verify-before-write" in out


def test_cognitive_inject_summaries_legacy_mode_no_brand(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy (``verbose``) mode keeps the verbatim token but drops
    the ``[Concinno: …]`` self-brand for users who opt back into
    4.5.0 behaviour."""
    monkeypatch.setenv("CONCINNO_VERBATIM_RELAY_MODE", "verbose")
    from concinno.cognitive_inject import _build_summaries

    learnings = [
        {
            "pattern_key": "shallow-reasoning",
            "correction_text": "Read more before editing",
            "count": 5,
            "promoted": False,
        },
    ]
    out = _build_summaries(learnings, ["read"])
    assert out.startswith("[SHOW USER VERBATIM]")
    assert "[Concinno:" not in out
    assert "shallow-reasoning" in out


def test_cognitive_inject_summaries_off_mode_drops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``off`` mode collapses the chunk to empty so the caller can
    skip appending it to the context."""
    monkeypatch.setenv("CONCINNO_VERBATIM_RELAY_MODE", "off")
    from concinno.cognitive_inject import _build_summaries

    learnings = [
        {
            "pattern_key": "x",
            "correction_text": "y",
            "count": 1,
            "promoted": False,
        },
    ]
    out = _build_summaries(learnings, ["x"])
    assert out == ""


def test_cognitive_inject_summaries_no_hits_returns_empty() -> None:
    """No keyword matches still short-circuits cleanly (helper isn't
    called on the empty body, and the existing contract holds)."""
    from concinno.cognitive_inject import _build_summaries

    out = _build_summaries([], ["nothing"])
    assert out == ""


# ── wait_inject.build_context ─────────────────────────────────


def test_wait_inject_active_branded_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default (``prefix``) mode brands the polling-watcher block."""
    monkeypatch.setenv("CONCINNO_VERBATIM_RELAY_MODE", "prefix")
    _patch_polling(
        monkeypatch,
        actives=[
            _StubActive(
                id="w1",
                kind="pod",
                status="running",
                eta_seconds=600,
                registered_at="2026-04-29T00:00:00+00:00",
                extra={"preview": "test pod"},
            ),
        ],
    )
    from concinno.hooks.wait_inject import build_context

    out: Optional[str] = build_context()
    assert out is not None
    assert out.startswith("[SHOW USER VERBATIM] [Concinno: polling_watcher]")
    assert "Active polling waits" in out


def test_wait_inject_legacy_mode_no_brand(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy (``verbose``) mode keeps verbatim token, no Concinno brand."""
    monkeypatch.setenv("CONCINNO_VERBATIM_RELAY_MODE", "verbose")
    _patch_polling(
        monkeypatch,
        alerts=[
            _StubAlert(
                id="w2",
                from_status="running",
                to_status="ready",
                at="2026-04-29T01:00:00",
                last_status="job complete",
            ),
        ],
    )
    from concinno.hooks.wait_inject import build_context

    out = build_context()
    assert out is not None
    assert out.startswith("[SHOW USER VERBATIM]")
    assert "[Concinno:" not in out


def test_wait_inject_off_mode_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``off`` mode collapses the helper to ``""`` and ``build_context``
    promotes that to ``None`` so the caller cleanly skips the chunk."""
    monkeypatch.setenv("CONCINNO_VERBATIM_RELAY_MODE", "off")
    _patch_polling(
        monkeypatch,
        actives=[
            _StubActive(
                id="w3",
                kind="job",
                status="pending",
                eta_seconds=300,
                registered_at="2026-04-29T02:00:00+00:00",
                extra={},
            ),
        ],
    )
    from concinno.hooks.wait_inject import build_context

    assert build_context() is None


def test_wait_inject_empty_state_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No actives, no alerts → ``None`` regardless of relay mode."""
    monkeypatch.setenv("CONCINNO_VERBATIM_RELAY_MODE", "prefix")
    _patch_polling(monkeypatch)
    from concinno.hooks.wait_inject import build_context

    assert build_context() is None


# ── Wiring smoke checks ───────────────────────────────────────


def test_cognitive_inject_imports_helper() -> None:
    """``cognitive_inject`` exposes the helper at module scope so the
    branded callsite is discoverable by future maintainers grepping
    for ``with_feature_prefix``."""
    import concinno.cognitive_inject as mod

    assert hasattr(mod, "with_feature_prefix")


def test_wait_inject_imports_helper() -> None:
    """Same wiring smoke check for ``wait_inject``."""
    import concinno.hooks.wait_inject as mod

    assert hasattr(mod, "with_feature_prefix")
