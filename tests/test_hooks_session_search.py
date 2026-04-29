"""Tests for the concinno-skills-session-search wiring inside concinno hooks.

Specifically verifies the 4.6.0 / W4 change to
``concinno.hooks.on_stop._session_search_lifecycle_stop`` that resolves
``transcript_path`` from ``session_id + cwd`` before building
:class:`LifecycleContext`.

The wire is inherently optional (the sub-pkg may not be installed).
These tests run only when the sub-pkg IS installed; otherwise they
``pytest.skip`` so they never break a minimal-deps CI run.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("concinno_skills_session_search")


def test_thunk_passes_resolved_transcript_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the CC transcript file exists, the thunk should resolve it
    and the sub-pkg should record an ``enqueued`` action."""
    monkeypatch.setenv(
        "CONCINNO_SESSION_SEARCH_QUEUE", str(tmp_path / "q.jsonl")
    )
    monkeypatch.setenv("CONCINNO_SESSION_SEARCH_DB", str(tmp_path / "i.db"))
    monkeypatch.setenv("CONCINNO_CC_PROJECTS_ROOT", str(tmp_path / "projects"))
    monkeypatch.setitem(sys.modules, "anthropic", None)

    # Plant a synthetic transcript at the layout the resolver expects.
    from concinno_skills_session_search.transcript_resolver import encode_cwd

    encoded = encode_cwd("z:/proj")
    project_dir = tmp_path / "projects" / encoded
    transcript = project_dir / "S-WIRE.jsonl"
    project_dir.mkdir(parents=True)
    transcript.write_text("hello-from-wire-test", encoding="utf-8")

    # Drive the actual concinno hook callsite.
    from concinno.hooks.on_stop import _session_search_lifecycle_stop

    _session_search_lifecycle_stop(
        {"session_id": "S-WIRE", "cwd": "z:/proj"}
    )

    # The sub-pkg's queue should now have one row pointing at the
    # transcript file the wire resolved.
    from concinno_skills_session_search import queue as _queue

    rows = _queue.read_all()
    assert len(rows) == 1
    assert rows[0].session_id == "S-WIRE"
    assert rows[0].transcript_path == str(transcript)


def test_thunk_silent_when_session_id_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty session id should not enqueue anything (and not raise)."""
    monkeypatch.setenv(
        "CONCINNO_SESSION_SEARCH_QUEUE", str(tmp_path / "q.jsonl")
    )
    monkeypatch.setenv("CONCINNO_CC_PROJECTS_ROOT", str(tmp_path / "projects"))

    from concinno.hooks.on_stop import _session_search_lifecycle_stop

    _session_search_lifecycle_stop({"session_id": "", "cwd": "z:/proj"})

    from concinno_skills_session_search import queue as _queue

    assert _queue.depth() == 0


def test_thunk_silent_when_transcript_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the resolver returns None (file absent), ``transcript_path``
    is None — sub-pkg falls back to legacy id-only noop. The hook must
    not raise."""
    monkeypatch.setenv(
        "CONCINNO_SESSION_SEARCH_QUEUE", str(tmp_path / "q.jsonl")
    )
    monkeypatch.setenv("CONCINNO_CC_PROJECTS_ROOT", str(tmp_path / "projects"))
    monkeypatch.setitem(sys.modules, "anthropic", None)

    from concinno.hooks.on_stop import _session_search_lifecycle_stop

    # No transcript file planted → resolver returns None.
    _session_search_lifecycle_stop(
        {"session_id": "S-NOFILE", "cwd": "z:/proj"}
    )

    from concinno_skills_session_search import queue as _queue

    # No file means no transcript_path → sub-pkg goes to noop branch.
    assert _queue.depth() == 0


def test_thunk_swallows_subpkg_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Any sub-pkg exception must NOT propagate out of the hook (host
    pipeline contract)."""
    monkeypatch.setenv(
        "CONCINNO_SESSION_SEARCH_QUEUE", str(tmp_path / "q.jsonl")
    )
    # Force resolver to misbehave by pointing at a path it cannot stat.
    monkeypatch.setenv(
        "CONCINNO_CC_PROJECTS_ROOT",
        str(tmp_path / "projects"),
    )

    from concinno.hooks.on_stop import _session_search_lifecycle_stop

    # Should not raise even with bizarre cwd.
    _session_search_lifecycle_stop(
        {"session_id": "S-WEIRD", "cwd": "\x00\x01invalid"}
    )
