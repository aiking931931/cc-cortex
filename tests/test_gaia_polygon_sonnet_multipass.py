"""Tests for gaia polygon-area Sonnet multi-pass routing (P0.1 fix).

Origin: GAIA 6359a0b1 orthogonal polygon area — local Gemma 4 Q4_K_M
mmproj reliably under-counts on polygon decomposition (concave-corner
rectangles missed). Fix routes the polygon-AREA question class to
Anthropic Sonnet vision with N-pass majority vote.

Tests focus on the routing + voting + feature-flag wiring. Real-model
end-to-end smoke is done out-of-band (see
``benchmarks/gaia/evidence/smoke_p01_polygon_*.json``) — unit tests
only mock the Anthropic client.
"""

from __future__ import annotations

import sys
from typing import Any

import pytest


@pytest.fixture
def gaia_agent_mod(monkeypatch):
    monkeypatch.setenv("HF_TOKEN", "test-dummy")
    sys.modules.pop("concinno.skills.public.agent.gaia_agent", None)
    from concinno.skills.public.agent import gaia_agent
    return gaia_agent


# ─────────────────────────── _majority_vote_numeric ──────────────────────────


class TestMajorityVoteNumeric:
    def test_unanimous_returns_value(self, gaia_agent_mod):
        assert gaia_agent_mod._majority_vote_numeric(
            ["39", "39", "39"]
        ) == "39"

    def test_simple_majority_wins(self, gaia_agent_mod):
        assert gaia_agent_mod._majority_vote_numeric(
            ["39", "38", "39"]
        ) == "39"

    def test_tie_keeps_first(self, gaia_agent_mod):
        # Two distinct values appear once each → first sample wins.
        assert gaia_agent_mod._majority_vote_numeric(
            ["12", "13"]
        ) == "12"

    def test_extracts_last_int_from_phrase(self, gaia_agent_mod):
        # FINAL ANSWER might be a phrase with a trailing integer
        assert gaia_agent_mod._majority_vote_numeric(
            ["The area is 26", "Area: 26", "26 square units"]
        ) == "26"

    def test_skips_empty_samples(self, gaia_agent_mod):
        assert gaia_agent_mod._majority_vote_numeric(
            ["", "39", "", "39", ""]
        ) == "39"

    def test_all_empty_returns_none(self, gaia_agent_mod):
        assert gaia_agent_mod._majority_vote_numeric(["", "", ""]) is None

    def test_no_integer_token_returns_none(self, gaia_agent_mod):
        assert gaia_agent_mod._majority_vote_numeric(
            ["unsure", "n/a", "?"]
        ) is None

    def test_empty_list_returns_none(self, gaia_agent_mod):
        assert gaia_agent_mod._majority_vote_numeric([]) is None

    def test_negative_int_supported(self, gaia_agent_mod):
        # _majority_vote_numeric must not regex-strip the leading
        # minus sign — used by future deltas / coordinates.
        assert gaia_agent_mod._majority_vote_numeric(
            ["-5", "-5", "-7"]
        ) == "-5"


# ───────────────────────── multipass routing wiring ──────────────────────────


class _FakeContent:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeResp:
    def __init__(self, text: str) -> None:
        self.content = [_FakeContent(text)]


class _FakeAnthropic:
    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)
        self.calls: list[dict[str, Any]] = []

        class _Messages:
            def __init__(self, outer: "_FakeAnthropic") -> None:
                self._outer = outer

            def create(self, **kw):
                self._outer.calls.append(kw)
                if self._outer._replies:
                    return _FakeResp(self._outer._replies.pop(0))
                return _FakeResp("FINAL ANSWER: 0")

        self.messages = _Messages(self)


class TestSolveVisionAnthropicMultipass:
    def test_unanimous_three_pass(self, gaia_agent_mod, tmp_path, monkeypatch):
        pytest.importorskip("PIL")
        from PIL import Image
        img = tmp_path / "poly.png"
        Image.new("RGB", (1024, 800), "white").save(img)

        fake = _FakeAnthropic([
            "Step 1...\nFINAL ANSWER: 39",
            "Step 1...\nFINAL ANSWER: 39",
            "Step 1...\nFINAL ANSWER: 39",
        ])
        monkeypatch.setattr(
            gaia_agent_mod, "_get_anthropic", lambda: fake,
        )
        voted, samples = gaia_agent_mod._solve_vision_anthropic_multipass(
            "What is the area of the orthogonal polygon?",
            str(img),
            model="claude-sonnet-4-6",
            passes_count=3,
        )
        assert voted == "39"
        assert samples == ["39", "39", "39"]
        assert len(fake.calls) == 3
        # Ensure the polygon-area procedure anchor is injected
        first_text = fake.calls[0]["messages"][0]["content"][1]["text"]
        assert "[Orthogonal polygon area procedure]" in first_text

    def test_majority_two_of_three(self, gaia_agent_mod, tmp_path, monkeypatch):
        pytest.importorskip("PIL")
        from PIL import Image
        img = tmp_path / "poly.png"
        Image.new("RGB", (1024, 800), "white").save(img)

        fake = _FakeAnthropic([
            "FINAL ANSWER: 39",
            "FINAL ANSWER: 38",
            "FINAL ANSWER: 39",
        ])
        monkeypatch.setattr(
            gaia_agent_mod, "_get_anthropic", lambda: fake,
        )
        voted, samples = gaia_agent_mod._solve_vision_anthropic_multipass(
            "What is the area of the polygon?",
            str(img),
            passes_count=3,
        )
        assert voted == "39"
        assert samples == ["39", "38", "39"]

    def test_anthropic_init_failure_returns_empty(
        self, gaia_agent_mod, tmp_path, monkeypatch,
    ):
        pytest.importorskip("PIL")
        from PIL import Image
        img = tmp_path / "poly.png"
        Image.new("RGB", (1024, 800), "white").save(img)

        def _boom() -> Any:
            raise RuntimeError("no api key")

        monkeypatch.setattr(gaia_agent_mod, "_get_anthropic", _boom)
        voted, samples = gaia_agent_mod._solve_vision_anthropic_multipass(
            "Area of polygon?", str(img), passes_count=3,
        )
        assert voted == ""
        assert samples == []

    def test_per_pass_exception_is_isolated(
        self, gaia_agent_mod, tmp_path, monkeypatch,
    ):
        pytest.importorskip("PIL")
        from PIL import Image
        img = tmp_path / "poly.png"
        Image.new("RGB", (1024, 800), "white").save(img)

        class _PartialFake:
            def __init__(self) -> None:
                self.idx = 0

                class _Messages:
                    def __init__(self, outer: "_PartialFake") -> None:
                        self._outer = outer

                    def create(self, **kw):
                        i = self._outer.idx
                        self._outer.idx += 1
                        if i == 1:
                            raise RuntimeError("transient API err")
                        return _FakeResp(f"FINAL ANSWER: 4{i}")

                self.messages = _Messages(self)

        monkeypatch.setattr(
            gaia_agent_mod, "_get_anthropic", lambda: _PartialFake(),
        )
        voted, samples = gaia_agent_mod._solve_vision_anthropic_multipass(
            "Area?", str(img), passes_count=3,
        )
        # Pass 0 → "40", pass 1 raises → "", pass 2 → "42".
        # Tie between 40/42 (each once) → first non-empty wins.
        assert samples == ["40", "", "42"]
        assert voted in ("40", "42")  # tie semantics: first one wins → "40"
        assert voted == "40"


# ─────────────────────── _solve_vision_local routing ─────────────────────────


class TestSolveVisionLocalRouting:
    def test_polygon_area_routes_to_sonnet_multipass(
        self, gaia_agent_mod, tmp_path, monkeypatch,
    ):
        pytest.importorskip("PIL")
        from PIL import Image
        img = tmp_path / "poly.png"
        Image.new("RGB", (1024, 800), "white").save(img)

        called = {"local": 0, "multipass": 0}

        def _fake_local_llm():  # local path must NOT be reached
            called["local"] += 1
            raise AssertionError("polygon-area Q must skip local LLM")

        def _fake_multipass(question, path, *, model, passes_count):
            called["multipass"] += 1
            assert passes_count == 3
            assert model == "claude-sonnet-4-6"
            return "39", ["39", "39", "39"]

        monkeypatch.setattr(
            gaia_agent_mod, "_get_local_vision_llm", _fake_local_llm,
        )
        monkeypatch.setattr(
            gaia_agent_mod, "_solve_vision_anthropic_multipass",
            _fake_multipass,
        )
        out = gaia_agent_mod._solve_vision_local(
            "What is the area of the orthogonal polygon?", str(img),
        )
        assert out == "39"
        assert called["multipass"] == 1
        assert called["local"] == 0

    def test_non_polygon_question_uses_local(
        self, gaia_agent_mod, tmp_path, monkeypatch,
    ):
        pytest.importorskip("PIL")
        from PIL import Image
        img = tmp_path / "chart.png"
        Image.new("RGB", (1024, 800), "white").save(img)

        called = {"local": 0, "multipass": 0}

        class _FakeLLM:
            def create_chat_completion(self, **kwargs):
                called["local"] += 1
                return {
                    "choices": [
                        {"message": {"content": "FINAL ANSWER: 42"}}
                    ]
                }

        def _fake_multipass(*a, **kw):
            called["multipass"] += 1
            return "0", ["0"]

        monkeypatch.setattr(
            gaia_agent_mod, "_get_local_vision_llm",
            lambda: _FakeLLM(),
        )
        monkeypatch.setattr(
            gaia_agent_mod, "_solve_vision_anthropic_multipass",
            _fake_multipass,
        )
        out = gaia_agent_mod._solve_vision_local(
            "What percentage of the chart is shaded?", str(img),
        )
        assert out == "42"
        assert called["local"] == 1
        assert called["multipass"] == 0

    def test_polygon_feature_off_keeps_local_path(
        self, gaia_agent_mod, tmp_path, monkeypatch,
    ):
        pytest.importorskip("PIL")
        from PIL import Image
        img = tmp_path / "poly.png"
        Image.new("RGB", (1024, 800), "white").save(img)

        def _feature(name, default=True):
            if name == "gaia_polygon_sonnet_multipass":
                return False
            return default

        monkeypatch.setattr(gaia_agent_mod, "_feature_enabled", _feature)

        called = {"local": 0, "multipass": 0}

        class _FakeLLM:
            def create_chat_completion(self, **kwargs):
                called["local"] += 1
                return {
                    "choices": [
                        {"message": {"content": "FINAL ANSWER: 26"}}
                    ]
                }

        def _fake_multipass(*a, **kw):
            called["multipass"] += 1
            return "39", ["39"]

        monkeypatch.setattr(
            gaia_agent_mod, "_get_local_vision_llm",
            lambda: _FakeLLM(),
        )
        monkeypatch.setattr(
            gaia_agent_mod, "_solve_vision_anthropic_multipass",
            _fake_multipass,
        )
        out = gaia_agent_mod._solve_vision_local(
            "What is the area of the polygon?", str(img),
        )
        # Feature off → legacy local path → "26"; multipass NOT called.
        assert out == "26"
        assert called["local"] == 1
        assert called["multipass"] == 0

    def test_multipass_empty_falls_back_to_local(
        self, gaia_agent_mod, tmp_path, monkeypatch,
    ):
        pytest.importorskip("PIL")
        from PIL import Image
        img = tmp_path / "poly.png"
        Image.new("RGB", (1024, 800), "white").save(img)

        called = {"local": 0, "multipass": 0}

        class _FakeLLM:
            def create_chat_completion(self, **kwargs):
                called["local"] += 1
                return {
                    "choices": [
                        {"message": {"content": "FINAL ANSWER: 26"}}
                    ]
                }

        def _fake_multipass(*a, **kw):
            called["multipass"] += 1
            return "", ["", "", ""]

        monkeypatch.setattr(
            gaia_agent_mod, "_get_local_vision_llm",
            lambda: _FakeLLM(),
        )
        monkeypatch.setattr(
            gaia_agent_mod, "_solve_vision_anthropic_multipass",
            _fake_multipass,
        )
        out = gaia_agent_mod._solve_vision_local(
            "What is the area of the polygon?", str(img),
        )
        # Multipass empty → fall through to local → "26"
        assert out == "26"
        assert called["multipass"] == 1
        assert called["local"] == 1


# ───────────────────────── feature_config wiring ─────────────────────────────


class TestFeatureMetaRegistration:
    def test_feature_registered_in_meta(self):
        from concinno.feature_config import FEATURE_META
        assert "gaia_polygon_sonnet_multipass" in FEATURE_META
        meta = FEATURE_META["gaia_polygon_sonnet_multipass"]
        assert meta["ziq_autotunable"] is False
        assert meta["cosmetic"] is False
        assert "passes_count" in meta["params"]
        assert "model" in meta["params"]
        assert meta["params"]["passes_count"]["default"] == 3
        assert meta["params"]["model"]["default"] == "claude-sonnet-4-6"

    def test_polygon_multipass_params_default(
        self, gaia_agent_mod, monkeypatch,
    ):
        # When config layer raises (no concinno config), defaults stand.
        def _raise(*_a, **_kw):
            raise RuntimeError("no config")

        monkeypatch.setattr(
            "concinno.core.config.get_config", _raise,
        )
        passes_count, model = gaia_agent_mod._polygon_multipass_params()
        assert passes_count == 3
        assert model == "claude-sonnet-4-6"

    def test_polygon_multipass_params_custom(
        self, gaia_agent_mod, monkeypatch,
    ):
        class _FakeCfg:
            def feature(self, name, key):
                if name != "gaia_polygon_sonnet_multipass":
                    return None
                return {"passes_count": 5, "model": "claude-opus-4-7"}.get(key)

        monkeypatch.setattr(
            "concinno.core.config.get_config", lambda: _FakeCfg(),
        )
        passes_count, model = gaia_agent_mod._polygon_multipass_params()
        assert passes_count == 5
        assert model == "claude-opus-4-7"

    def test_polygon_multipass_params_invalid_passes_falls_back(
        self, gaia_agent_mod, monkeypatch,
    ):
        class _FakeCfg:
            def feature(self, name, key):
                return "not-an-int" if key == "passes_count" else None

        monkeypatch.setattr(
            "concinno.core.config.get_config", lambda: _FakeCfg(),
        )
        passes_count, model = gaia_agent_mod._polygon_multipass_params()
        assert passes_count == 3
        assert model == "claude-sonnet-4-6"
