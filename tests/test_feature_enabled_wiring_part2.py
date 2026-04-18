"""Regression tests for 2.7.2 Gap 4 — user-facing kill switches.

Before 2.7.2, ``_DEFAULT_CONFIG`` exposed ``auto_compact`` and
``memory_file_enabled`` flags with no runtime consumer — grep across
``src/concinno/`` found the keys only in ``config.py`` and CLI help
text. Users could flip them without any effect.

2.7.2 wires both flags to their canonical library entry points:

* ``auto_compact`` → :meth:`AutoCompactor.should_trigger` (and by
  cascade :meth:`AutoCompactor.run`) returns ``"noop"`` / ``None``
  when False.
* ``memory_file_enabled`` → :meth:`SessionMemory.should_update` (and
  by cascade :meth:`SessionMemory.update`) returns False / None when
  False.

These tests pin the invariant: flipping the flag stops the behaviour.
Breaking the wiring silently takes us back to the 2.6/2.7.x ghost-
config bug for these two keys.
"""

from __future__ import annotations

from unittest.mock import patch

from concinno.cache.autocompact import (
    AutoCompactor,
    CompactRequest,
    CompactResult,
    CompactSink,
)
from concinno.cache.session_memory import (
    DistillInput,
    DistillOutput,
    SessionMemory,
)

# ── auto_compact wiring ──────────────────────────────────────


class _NoopCompactSink(CompactSink):
    def summarize(self, req: CompactRequest) -> CompactResult:
        return CompactResult(
            success=True, summary_tokens=1, reclaimed_tokens=1,
        )


def _make_autocompactor(tmp_path) -> AutoCompactor:
    """Build a minimally-configured AutoCompactor for gating tests."""
    return AutoCompactor(
        cache_dir=str(tmp_path),
        session_id="test_session",
        sink=_NoopCompactSink(),
    )


def test_autocompact_disabled_short_circuits_should_trigger(tmp_path):
    """``auto_compact=False`` → should_trigger returns 'noop' regardless of tokens."""
    ac = _make_autocompactor(tmp_path)
    # Use a token count well above the threshold so the only reason
    # for "noop" is the kill switch.
    huge_tokens = ac._model_budget  # at budget = definitely over threshold
    with patch("concinno.config.get", return_value=False):
        assert ac.should_trigger(current_tokens=huge_tokens) == "noop"


def test_autocompact_enabled_still_triggers(tmp_path):
    """``auto_compact=True`` (default) → should_trigger behaves as before."""
    ac = _make_autocompactor(tmp_path)
    huge_tokens = ac._model_budget
    with patch("concinno.config.get", return_value=True):
        # Must NOT be "noop" — over threshold should classify as
        # warning or error (never noop with the kill switch off).
        assert ac.should_trigger(current_tokens=huge_tokens) != "noop"


def test_autocompact_run_returns_none_when_disabled(tmp_path):
    """``auto_compact=False`` → run() returns None without touching sink."""
    sink = _NoopCompactSink()
    ac = AutoCompactor(
        cache_dir=str(tmp_path),
        session_id="test_run",
        sink=sink,
    )
    with patch("concinno.config.get", return_value=False):
        # Even with huge tokens, run should exit at the should_trigger
        # "noop" branch before calling the sink.
        result = ac.run(current_tokens=ac._model_budget)
    assert result is None


def test_autocompact_config_crash_fails_open(tmp_path):
    """Config read raising does NOT block autocompact (fail-soft)."""
    ac = _make_autocompactor(tmp_path)
    huge_tokens = ac._model_budget
    with patch(
        "concinno.config.get",
        side_effect=RuntimeError("config module gone"),
    ):
        # Fail-soft: treat as enabled so compaction still runs when
        # it needs to, rather than silently breaking context limits.
        assert ac.should_trigger(current_tokens=huge_tokens) != "noop"


# ── memory_file_enabled wiring ───────────────────────────────


class _NoopDistillSink:
    def distill(self, inp: DistillInput) -> DistillOutput:
        return DistillOutput(markdown="# memory\n")


def _make_session_memory(tmp_path) -> SessionMemory:
    """Build a SessionMemory configured to always trigger update."""
    sm = SessionMemory(
        cache_dir=str(tmp_path),
        session_id="gap4_test",
        sink=_NoopDistillSink(),
        init_threshold=1,
        update_threshold=1,
    )
    # Push the tool count so the normal path would return True.
    sm._state.tool_count_total = 1000
    return sm


def test_memory_file_disabled_short_circuits_should_update(tmp_path):
    """``memory_file_enabled=False`` → should_update returns False."""
    sm = _make_session_memory(tmp_path)
    with patch("concinno.config.get", return_value=False):
        assert sm.should_update() is False


def test_memory_file_enabled_still_updates(tmp_path):
    """``memory_file_enabled=True`` (default) → should_update behaves as before."""
    sm = _make_session_memory(tmp_path)
    with patch("concinno.config.get", return_value=True):
        # tool_count_total=1000, init_threshold=1, md missing ⇒ True.
        assert sm.should_update() is True


def test_memory_file_update_returns_none_when_disabled(tmp_path):
    """``memory_file_enabled=False`` → update() returns None without distilling."""
    sm = _make_session_memory(tmp_path)
    with patch("concinno.config.get", return_value=False):
        result = sm.update()
    assert result is None


def test_memory_file_config_crash_fails_open(tmp_path):
    """Config read raising does NOT block memory file (fail-soft)."""
    sm = _make_session_memory(tmp_path)
    with patch(
        "concinno.config.get",
        side_effect=RuntimeError("config module gone"),
    ):
        # Fail-soft: treat as enabled so distillation still runs.
        assert sm.should_update() is True
