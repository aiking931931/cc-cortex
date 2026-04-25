"""Tests for ``concinno.skills.schema`` (concinno 2.35.0)."""

from __future__ import annotations

import pytest

from concinno.skills.schema import (
    EVENT_TYPES,
    EventBinding,
    parse_event_bindings,
)

# ── EventBinding validators ─────────────────────────────────────────────


class TestEventBindingDefaults:
    def test_minimal_binding_uses_defaults(self):
        b = EventBinding(event="Stop", invoke="my_skill")
        assert b.event == "Stop"
        assert b.invoke == "my_skill"
        assert b.when is None
        assert b.priority == 50
        assert b.cooldown_seconds == 0.0

    def test_full_binding_round_trip(self):
        b = EventBinding(
            event="PostToolUse",
            invoke="triage",
            when='tool_name == "Edit"',
            priority=90,
            cooldown_seconds=12.5,
        )
        assert b.priority == 90
        assert b.cooldown_seconds == 12.5
        assert b.when == 'tool_name == "Edit"'


class TestEventBindingValidation:
    def test_unknown_event_rejected(self):
        with pytest.raises(ValueError, match="unknown event"):
            EventBinding(event="DefinitelyNotARealEvent", invoke="x")

    def test_priority_below_range_rejected(self):
        with pytest.raises(Exception):  # noqa: PT011 - pydantic ValidationError
            EventBinding(event="Stop", invoke="x", priority=-1)

    def test_priority_above_range_rejected(self):
        with pytest.raises(Exception):  # noqa: PT011
            EventBinding(event="Stop", invoke="x", priority=101)

    def test_negative_cooldown_rejected(self):
        with pytest.raises(Exception):  # noqa: PT011
            EventBinding(event="Stop", invoke="x", cooldown_seconds=-1.0)

    def test_cooldown_above_24h_rejected(self):
        with pytest.raises(Exception):  # noqa: PT011
            EventBinding(event="Stop", invoke="x", cooldown_seconds=86_401.0)

    def test_invoke_must_not_be_empty(self):
        with pytest.raises(Exception):  # noqa: PT011
            EventBinding(event="Stop", invoke="")

    def test_invoke_capped_at_200_chars(self):
        too_long = "x" * 201
        with pytest.raises(Exception):  # noqa: PT011
            EventBinding(event="Stop", invoke=too_long)

    def test_when_capped_at_500_chars(self):
        too_long = "x" * 501
        with pytest.raises(Exception):  # noqa: PT011
            EventBinding(event="Stop", invoke="ok", when=too_long)

    def test_extra_fields_rejected_for_forward_compat_safety(self):
        # extra="forbid" guards against typos like "even"/"trigger" silently
        # being ignored — better to fail loudly so the SKILL.md author
        # learns the canonical key name on first try.
        with pytest.raises(Exception):  # noqa: PT011
            EventBinding(
                event="Stop", invoke="x", typo_field="oops",
            )

    def test_all_canonical_events_accepted(self):
        for evt in EVENT_TYPES:
            EventBinding(event=evt, invoke="ok")


# ── parse_event_bindings ────────────────────────────────────────────────


class TestParseEventBindings:
    def test_missing_key_returns_empty(self):
        assert parse_event_bindings({}) == []

    def test_non_list_value_returns_empty(self):
        # The YAML-lite parser may return a string for inline scalars;
        # we treat anything non-list as "no bindings declared".
        assert parse_event_bindings({"event_bindings": "wrong shape"}) == []
        assert parse_event_bindings({"event_bindings": 42}) == []

    def test_well_formed_list_validates_all(self):
        meta = {
            "event_bindings": [
                {"event": "Stop", "invoke": "a"},
                {"event": "PostToolUse", "invoke": "b", "priority": 90},
            ]
        }
        out = parse_event_bindings(meta)
        assert len(out) == 2
        assert out[0].invoke == "a"
        assert out[1].priority == 90

    def test_malformed_entry_is_skipped_not_fatal(self):
        # One bad binding should not nuke the rest — author-friendly
        # by design (SKILL.md is human-edited markdown).
        meta = {
            "event_bindings": [
                {"event": "Stop", "invoke": "good"},
                {"event": "NotReal", "invoke": "bad"},
                {"event": "PostToolUse", "invoke": "good2"},
            ]
        }
        out = parse_event_bindings(meta)
        assert [b.invoke for b in out] == ["good", "good2"]

    def test_non_dict_entries_skipped(self):
        meta = {
            "event_bindings": [
                {"event": "Stop", "invoke": "ok"},
                "string-not-a-dict",
                42,
                None,
            ]
        }
        out = parse_event_bindings(meta)
        assert len(out) == 1
        assert out[0].invoke == "ok"

    def test_empty_list_returns_empty(self):
        assert parse_event_bindings({"event_bindings": []}) == []
