"""Tests for the Stage -1 intent anchor step in on_prompt_submit (2.35.0).

Exercises ``_stage_minus_1_anchor`` directly with the StateStore-backed
namespaces it depends on:
    * ``c0_route``  — set by the prior CBUA forced-classification step
    * ``intent_anchor`` — the namespace it reads / writes
"""

from __future__ import annotations

import os
import tempfile

from concinno.core.state_store import StateStore
from concinno.hooks.on_prompt_submit import _stage_minus_1_anchor


def _fresh_context() -> tuple[str, str]:
    tmp = tempfile.mkdtemp()
    cache_dir = os.path.join(tmp, ".concinno_cache")
    return cache_dir, "sess-stage-neg1"


class TestStageMinus1FirstTurn:
    def test_first_turn_complicated_returns_block(self):
        cache_dir, sid = _fresh_context()
        # Simulate Step 6 having stored a Complicated classification.
        StateStore(cache_dir).write(
            "c0_route", sid, {"complexity": "complicated"},
        )
        prompt = (
            "Please answer with the integer count of studio albums "
            "released as of 2009."
        )
        ctx = _stage_minus_1_anchor(prompt, cache_dir, sid)
        assert ctx is not None
        assert "Stage -1" in ctx
        # Anchor was persisted for IntentAnchorGuard re-injection
        state = StateStore(cache_dir).read("intent_anchor", sid, default={})
        assert state.get("summary")
        assert state.get("intent")  # v2.9 alias
        assert state.get("intent_source") == "stage_minus_1"

    def test_first_turn_persists_done_spec_and_constraints(self):
        cache_dir, sid = _fresh_context()
        StateStore(cache_dir).write(
            "c0_route", sid, {"complexity": "complicated"},
        )
        prompt = (
            "Please answer with the integer count of studio albums "
            "released as of 2009. You must not include compilations."
        )
        _stage_minus_1_anchor(prompt, cache_dir, sid)
        state = StateStore(cache_dir).read("intent_anchor", sid, default={})
        assert state.get("done_spec")
        assert state.get("constraints")


class TestStageMinus1SimpleSkip:
    def test_simple_complexity_returns_none(self):
        cache_dir, sid = _fresh_context()
        StateStore(cache_dir).write(
            "c0_route", sid, {"complexity": "simple"},
        )
        ctx = _stage_minus_1_anchor(
            "Just a quick answer please.", cache_dir, sid,
        )
        assert ctx is None
        # State must remain untouched — no anchor written under Simple.
        state = StateStore(cache_dir).read("intent_anchor", sid, default={})
        assert state == {}


class TestStageMinus1IdempotentReentry:
    def test_second_turn_returns_none_when_anchor_exists(self):
        cache_dir, sid = _fresh_context()
        StateStore(cache_dir).write(
            "c0_route", sid, {"complexity": "complicated"},
        )
        # First turn — captures.
        _stage_minus_1_anchor("Build a REST API.", cache_dir, sid)
        # Second turn — must not re-inject (would double-count).
        ctx = _stage_minus_1_anchor("Add auth.", cache_dir, sid)
        assert ctx is None

    def test_back_compat_legacy_intent_key_blocks_recapture(self):
        cache_dir, sid = _fresh_context()
        # Simulate a v2.9 session that wrote only the legacy ``intent`` key.
        StateStore(cache_dir).write(
            "intent_anchor", sid, {"intent": "Already captured"},
        )
        StateStore(cache_dir).write(
            "c0_route", sid, {"complexity": "complicated"},
        )
        ctx = _stage_minus_1_anchor("Brand new ask.", cache_dir, sid)
        # Old key counts as "anchor exists" — Stage -1 stays out of the way.
        assert ctx is None


class TestStageMinus1NoOps:
    def test_empty_prompt_returns_none(self):
        cache_dir, sid = _fresh_context()
        assert _stage_minus_1_anchor("", cache_dir, sid) is None

    def test_short_prompt_returns_none(self):
        cache_dir, sid = _fresh_context()
        assert _stage_minus_1_anchor("hi", cache_dir, sid) is None

    def test_missing_cache_dir_returns_none(self):
        assert _stage_minus_1_anchor("anything goes here", "", "sid") is None

    def test_missing_session_id_returns_none(self):
        tmp = tempfile.mkdtemp()
        cache_dir = os.path.join(tmp, ".concinno_cache")
        assert _stage_minus_1_anchor("anything goes", cache_dir, "") is None

    def test_default_complicated_when_c0_state_absent(self):
        # Step 6 didn't run / persisted nothing — Stage -1 must still
        # capture (default complexity == complicated, not simple).
        cache_dir, sid = _fresh_context()
        ctx = _stage_minus_1_anchor(
            "Build a complete REST API with auth.", cache_dir, sid,
        )
        assert ctx is not None


class TestStageMinus1RenderContents:
    def test_block_includes_all_fields_when_extracted(self):
        cache_dir, sid = _fresh_context()
        StateStore(cache_dir).write(
            "c0_route", sid, {"complexity": "complicated"},
        )
        prompt = (
            "Please answer with the integer count of items, "
            "but you must not include compilations."
        )
        ctx = _stage_minus_1_anchor(prompt, cache_dir, sid)
        assert ctx is not None
        assert "原始意圖" in ctx
        # heuristic should fire on this prompt for both extras
        assert "做完長什麼樣" in ctx
        assert "限制" in ctx
