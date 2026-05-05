"""Tests for concinno.prompt_hooks_routes — route decision dispatcher.

Covers:
  - VALID_DECISIONS frozenset exported from prompt_hooks (2.11.0 contract)
  - RouteContext / RouteResult dataclass shapes
  - echo_advisory: pure, stderr output, truncation, stderr-closed fallback
  - validate_route_payload: decision != route, missing / bad route_to,
    unknown route_to, route_context non-dict, depth limit, unsafe str
    (shell meta / path traversal / control chars / length), reason
    type / safety
  - dispatch: valid round-trip, unknown route_to → unhandled, crashing
    handler → fail-open, unicode homoglyph rejection, ASCII-only
    enforcement
  - BUILTIN_ROUTES: 2.11.0 scope guard — all handlers map to
    echo_advisory, no register_route API leaked
  - 4 judge body integration: each ships a "route" option in its
    decision schema (string match — robust to minor reword)
"""

from __future__ import annotations

import sys

import pytest

from concinno.prompt_hooks import (
    ALL_JUDGES,
    CODE_QUALITY_JUDGE,
    EXCUSE_SCANNER_JUDGE,
    HALLUCINATION_JUDGE,
    VALID_DECISIONS,
    WIREDO_JUDGE,
)
from concinno.prompt_hooks_routes import (
    BUILTIN_ROUTES,
    RouteContext,
    RouteResult,
    dispatch,
    echo_advisory,
    validate_route_payload,
)

# ── Contract constants ────────────────────────────────────────


class TestValidDecisions:
    def test_is_frozenset(self):
        assert isinstance(VALID_DECISIONS, frozenset)

    def test_contains_block_allow_route(self):
        assert VALID_DECISIONS == frozenset({"block", "allow", "route"})

    def test_is_immutable(self):
        with pytest.raises(AttributeError):
            VALID_DECISIONS.add("reject")  # type: ignore[attr-defined]


# ── Judge body integration (each judge mentions route) ───────


class TestJudgeBodiesMentionRoute:
    @pytest.mark.parametrize(
        "judge",
        [HALLUCINATION_JUDGE, EXCUSE_SCANNER_JUDGE, CODE_QUALITY_JUDGE, WIREDO_JUDGE],
    )
    def test_judge_body_contains_route_option(self, judge):
        body = judge.prompt_body.lower()
        # each judge should mention "route" as a decision option
        assert '"route"' in body or "'route'" in body or "decision\": \"route" in body

    @pytest.mark.parametrize(
        "judge",
        [HALLUCINATION_JUDGE, EXCUSE_SCANNER_JUDGE, CODE_QUALITY_JUDGE, WIREDO_JUDGE],
    )
    def test_judge_body_references_route_to(self, judge):
        # each judge should document a route_to field semantic
        assert "route_to" in judge.prompt_body

    def test_all_judges_still_shipped(self):
        # Regression: extending schema shouldn't change judge count
        names = {j.name for j in ALL_JUDGES}
        assert names == {
            "hallucination_judge",
            "excuse_scanner_judge",
            "code_quality_judge",
            "wiredo_judge",
        }


# ── Dataclass shapes ─────────────────────────────────────────


class TestDataclassShapes:
    def test_route_context_frozen(self):
        ctx = RouteContext(route_to="echo_advisory")
        with pytest.raises(Exception):  # FrozenInstanceError subclass of AttributeError
            ctx.route_to = "other"  # type: ignore[misc]

    def test_route_context_default_payload_empty(self):
        ctx = RouteContext(route_to="echo_advisory")
        assert ctx.payload == {}
        assert ctx.reason == ""

    def test_route_result_fields(self):
        r = RouteResult(handled=True, action="advisory", message="ok")
        assert r.handled is True
        assert r.action == "advisory"
        assert r.message == "ok"

    def test_route_result_frozen(self):
        r = RouteResult(handled=True, action="advisory", message="ok")
        with pytest.raises(Exception):
            r.handled = False  # type: ignore[misc]


# ── echo_advisory handler ────────────────────────────────────


class TestEchoAdvisory:
    def test_logs_to_stderr(self, capsys):
        ctx = RouteContext(
            route_to="citation",
            payload={"claim": "X claims Y", "suggested_source": "I inferred"},
            reason="lacks citation",
        )
        result = echo_advisory(ctx)
        assert result.handled is True
        assert result.action == "advisory"
        err = capsys.readouterr().err
        assert "[concinno:route]" in err
        assert "citation" in err
        assert "lacks citation" in err

    def test_sorted_keys_for_determinism(self, capsys):
        ctx = RouteContext(
            route_to="deploy_recipe",
            payload={"z_key": "last", "a_key": "first", "m_key": "mid"},
        )
        echo_advisory(ctx)
        err = capsys.readouterr().err
        # order is alphabetical
        a_pos = err.index("a_key")
        m_pos = err.index("m_key")
        z_pos = err.index("z_key")
        assert a_pos < m_pos < z_pos

    def test_long_value_truncated(self, capsys):
        huge = "x" * 500
        ctx = RouteContext(
            route_to="expert_review",
            payload={"huge": huge},
        )
        echo_advisory(ctx)
        err = capsys.readouterr().err
        assert "..." in err
        # ensure the literal 500-char value isn't echoed whole
        assert huge not in err

    def test_missing_reason_emits_dash(self, capsys):
        ctx = RouteContext(route_to="echo_advisory", payload={"k": "v"})
        echo_advisory(ctx)
        err = capsys.readouterr().err
        assert ":: -" in err

    def test_stderr_closed_returns_noop(self, monkeypatch):
        """When stderr is closed, echo_advisory must not crash."""
        ctx = RouteContext(route_to="echo_advisory", payload={"k": "v"})

        class ClosedStream:
            def write(self, _):
                raise OSError("closed")
            def flush(self):
                raise OSError("closed")

        monkeypatch.setattr(sys, "stderr", ClosedStream())
        # print(file=sys.stderr) will hit ClosedStream.write and OSError
        # echo_advisory should catch and return noop
        result = echo_advisory(ctx)
        assert result.handled is True
        assert result.action == "noop"


# ── Validator ────────────────────────────────────────────────


class TestValidator:
    def _valid_payload(self):
        return {
            "decision": "route",
            "route_to": "echo_advisory",
            "route_context": {"k": "safe value"},
            "reason": "a plain reason",
        }

    def test_valid_passes(self):
        ok, err = validate_route_payload(self._valid_payload())
        assert ok, err

    def test_rejects_non_route_decision(self):
        p = self._valid_payload()
        p["decision"] = "block"
        ok, err = validate_route_payload(p)
        assert not ok
        assert "not 'route'" in err

    def test_rejects_missing_route_to(self):
        p = self._valid_payload()
        del p["route_to"]
        ok, err = validate_route_payload(p)
        assert not ok

    def test_rejects_unknown_route_to(self):
        p = self._valid_payload()
        p["route_to"] = "not_a_real_handler"
        ok, err = validate_route_payload(p)
        assert not ok
        assert "unknown route_to" in err

    def test_rejects_non_identifier_route_to(self):
        p = self._valid_payload()
        p["route_to"] = "echo advisory"  # space
        ok, _ = validate_route_payload(p)
        assert not ok

    def test_rejects_path_traversal_in_route_to(self):
        p = self._valid_payload()
        p["route_to"] = "../etc/passwd"
        ok, _ = validate_route_payload(p)
        assert not ok

    def test_rejects_unicode_homoglyph_route_to(self):
        # Cyrillic 'о' (U+043E) looks like ASCII 'o'
        p = self._valid_payload()
        p["route_to"] = "echо_advisory"  # second 'o' is cyrillic
        ok, _ = validate_route_payload(p)
        assert not ok

    def test_rejects_shell_meta_in_payload(self):
        p = self._valid_payload()
        p["route_context"] = {"cmd": "; rm -rf /"}
        ok, err = validate_route_payload(p)
        assert not ok
        assert "unsafe" in err.lower()

    def test_rejects_command_substitution_in_payload(self):
        p = self._valid_payload()
        p["route_context"] = {"cmd": "$(whoami)"}
        ok, _ = validate_route_payload(p)
        assert not ok

    def test_rejects_backtick_in_payload(self):
        p = self._valid_payload()
        p["route_context"] = {"cmd": "`id`"}
        ok, _ = validate_route_payload(p)
        assert not ok

    def test_rejects_path_traversal_in_payload(self):
        p = self._valid_payload()
        p["route_context"] = {"file": "/logs/../etc/secret"}
        ok, err = validate_route_payload(p)
        assert not ok
        assert "unsafe" in err.lower()

    def test_rejects_control_chars_in_payload(self):
        p = self._valid_payload()
        p["route_context"] = {"v": "bad\x00value"}
        ok, _ = validate_route_payload(p)
        assert not ok

    def test_allows_newline_tab_in_payload(self):
        p = self._valid_payload()
        p["route_context"] = {"v": "line1\nline2\ttab"}
        ok, err = validate_route_payload(p)
        assert ok, err

    def test_rejects_overlong_str_in_payload(self):
        p = self._valid_payload()
        p["route_context"] = {"v": "x" * 3000}
        ok, _ = validate_route_payload(p)
        assert not ok

    def test_rejects_deep_nesting(self):
        p = self._valid_payload()
        # 5 levels deep: a > b > c > d > e (depth counter starts at 0
        # at top-level; each dict increments)
        deep = {"a": {"b": {"c": {"d": {"e": "v"}}}}}
        p["route_context"] = deep
        ok, err = validate_route_payload(p)
        assert not ok
        assert "nested" in err.lower()

    def test_rejects_non_dict_route_context(self):
        p = self._valid_payload()
        p["route_context"] = ["not", "a", "dict"]
        ok, _ = validate_route_payload(p)
        assert not ok

    def test_rejects_non_string_key_in_payload(self):
        p = self._valid_payload()
        p["route_context"] = {42: "value"}  # type: ignore[dict-item]
        ok, _ = validate_route_payload(p)
        assert not ok

    def test_allows_primitive_values(self):
        p = self._valid_payload()
        p["route_context"] = {"n": 42, "f": 3.14, "b": True, "s": "ok"}
        ok, err = validate_route_payload(p)
        assert ok, err

    def test_rejects_non_string_reason(self):
        p = self._valid_payload()
        p["reason"] = 42  # type: ignore[assignment]
        ok, _ = validate_route_payload(p)
        assert not ok

    def test_rejects_unsafe_reason(self):
        p = self._valid_payload()
        p["reason"] = "; rm -rf /"
        ok, _ = validate_route_payload(p)
        assert not ok

    def test_allows_empty_reason(self):
        p = self._valid_payload()
        p["reason"] = ""
        ok, err = validate_route_payload(p)
        assert ok, err


# ── Dispatcher ───────────────────────────────────────────────


class TestDispatch:
    def test_roundtrip_valid_payload(self, capsys):
        result = dispatch({
            "decision": "route",
            "route_to": "citation",
            "route_context": {"claim": "X", "suggested_source": "ref"},
            "reason": "needs citation",
        })
        assert result.handled is True
        assert result.action == "advisory"
        assert "[concinno:route]" in capsys.readouterr().err

    def test_unknown_route_returns_reject(self):
        result = dispatch({
            "decision": "route",
            "route_to": "made_up_handler",
            "route_context": {},
            "reason": "",
        })
        assert result.handled is False
        assert result.action == "reject"
        assert "unknown route_to" in result.message

    def test_not_route_decision_returns_reject(self):
        result = dispatch({"decision": "block", "reason": "nope"})
        assert result.handled is False
        assert result.action == "reject"

    def test_crashing_handler_fails_open(self, monkeypatch, capsys):
        """If a registered handler raises, dispatch returns reject
        (never propagates to caller — CC would mis-interpret)."""

        def boom(_ctx):
            raise RuntimeError("intentional test crash")

        # Mutate the registry for this test only
        import concinno.prompt_hooks_routes as mod
        original = dict(mod.BUILTIN_ROUTES)
        monkeypatch.setattr(
            mod,
            "BUILTIN_ROUTES",
            {**original, "echo_advisory": boom},
        )
        result = dispatch({
            "decision": "route",
            "route_to": "echo_advisory",
            "route_context": {},
            "reason": "trigger crash",
        })
        assert result.handled is False
        assert result.action == "reject"
        assert "handler raised" in result.message
        # stderr should have received the error log
        assert "handler error" in capsys.readouterr().err


# ── 2.11.0 Scope guard ───────────────────────────────────────


class TestScope2_11_0:
    def test_no_register_route_api(self):
        """register_route is deliberately NOT in 2.11.0 exports.

        Red team FATAL-2 flagged it as arbitrary-exec surface.
        Commander verdict: defer to 2.12.0+ with capability manifest.
        """
        import concinno.prompt_hooks_routes as mod
        assert not hasattr(mod, "register_route")
        assert "register_route" not in mod.__all__

    def test_all_builtin_routes_are_echo_advisory(self):
        """2.11.0 ships advisory-only. All 5 declared route_to names
        must map to the same pure echo_advisory handler. Replacing
        any of these with exec-capable handlers requires red-blue
        CBUA review (2.12.0+ decision)."""
        for name in ("echo_advisory", "citation", "opus_reviewer",
                     "expert_review", "deploy_recipe"):
            assert BUILTIN_ROUTES[name] is echo_advisory, (
                f"2.11.0 scope violation: {name} must map to echo_advisory"
            )

    def test_builtin_routes_count_is_five(self):
        """Regression guard: 2.11.0 ships exactly these 5 route_to
        names. Adding more is a minor / major bump decision."""
        assert set(BUILTIN_ROUTES) == {
            "echo_advisory",
            "citation",
            "opus_reviewer",
            "expert_review",
            "deploy_recipe",
        }

    def test_public_exports_frozen(self):
        """2.11.0 public API surface."""
        import concinno.prompt_hooks_routes as mod
        assert set(mod.__all__) == {
            "RouteContext",
            "RouteResult",
            "BUILTIN_ROUTES",
            "echo_advisory",
            "validate_route_payload",
            "dispatch",
        }
