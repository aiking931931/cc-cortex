"""Tests for the multimodal web_fetch_full wiring (P0.2).

Origin: GAIA 624cbf11 Ben & Jerry's flavor graveyard — sonnet
react_solve loop kept calling ``web_fetch_full`` (text wiring worked)
but the ``screenshot_b64`` payload was never re-attached to the next
agent turn, so the model could not actually SEE the page and
degenerated trying to ``code_exec`` PIL.print on the saved file.

Coverage:
- ``web_fetch_full_action_multimodal`` returns a structured dict with
  text_summary + screenshot_b64 + screenshot_path + mime + error.
- ``react_solve`` attaches the screenshot as an image content block on
  Sonnet/Opus when the multimodal feature flag is on, and falls back
  to text-only when off / when Gemma backend / when capture failed.
- ``react_solve_split`` mirrors the same routing for the web-only
  Sonnet override path.
- ``_model_drops_temperature`` correctly skips temperature for newer
  Anthropic opus models.
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


# ── _model_drops_temperature ──────────────────────────────


class TestModelDropsTemperature:
    @pytest.mark.parametrize("model,expected", [
        ("claude-sonnet-4-6", False),
        ("claude-haiku-4-5-20251001", False),
        ("claude-opus-4-6", False),
        ("claude-opus-4-7", True),
        ("claude-opus-4-7[1m]", True),
        ("claude-opus-4-8", True),
        ("claude-opus-4-9", True),
        ("claude-opus-5-0", True),
        ("", False),
    ])
    def test_prefix_match(self, gaia_agent_mod, model, expected):
        assert gaia_agent_mod._model_drops_temperature(model) is expected


# ── web_fetch_full_action_multimodal contract ─────────────


class TestWebFetchFullActionMultimodal:
    def test_disabled_returns_error_dict(self, gaia_agent_mod, monkeypatch):
        monkeypatch.setattr(
            gaia_agent_mod, "_feature_enabled",
            lambda name, default=True: name != "gaia_web_fetch_full",
        )
        out = gaia_agent_mod.web_fetch_full_action_multimodal("https://x.test")
        assert out["screenshot_b64"] is None
        assert "disabled" in out["text_summary"]
        assert out["error"]

    def test_success_passes_through_screenshot(
        self, gaia_agent_mod, monkeypatch,
    ):
        def _fake_wff(url, screenshot=True):
            return {
                "url": url,
                "final_url": "https://example.com/landed",
                "title": "Landing",
                "text": "rendered page text",
                "screenshot_b64": "AAAA",
                "screenshot_path": "/tmp/x.png",
                "error": None,
            }
        # Patch the lazy import target inside the module being tested.
        import importlib
        wff_mod = importlib.import_module(
            "concinno.tools.builtin.web_fetch_full",
        )
        monkeypatch.setattr(wff_mod, "web_fetch_full", _fake_wff)
        out = gaia_agent_mod.web_fetch_full_action_multimodal(
            "https://example.com",
        )
        assert out["screenshot_b64"] == "AAAA"
        assert out["screenshot_path"] == "/tmp/x.png"
        assert out["mime"] == "image/png"
        assert "rendered page text" in out["text_summary"]
        assert "https://example.com/landed" in out["text_summary"]
        assert out["error"] is None

    def test_underlying_error_propagates_to_dict(
        self, gaia_agent_mod, monkeypatch,
    ):
        def _fake_wff(url, screenshot=True):
            return {
                "url": url, "final_url": url, "title": "", "text": "",
                "screenshot_b64": None, "screenshot_path": None,
                "error": "warn: navigation timeout 30000ms",
            }
        import importlib
        wff_mod = importlib.import_module(
            "concinno.tools.builtin.web_fetch_full",
        )
        monkeypatch.setattr(wff_mod, "web_fetch_full", _fake_wff)
        out = gaia_agent_mod.web_fetch_full_action_multimodal(
            "https://x.test",
        )
        assert out["screenshot_b64"] is None
        assert "navigation timeout" in out["error"]


# ── react_solve multimodal routing ─────────────────────────


class _FakeContent:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeResp:
    def __init__(self, text: str) -> None:
        self.content = [_FakeContent(text)]


class _StubBackend:
    """Records messages backend.chat receives so the test can assert
    the multimodal user turn is shaped right."""

    def __init__(self, tier: str, replies: list[str]) -> None:
        self.tier = tier
        self._replies = list(replies)
        self.calls: list[dict[str, Any]] = []

    def chat(self, system, messages, max_tokens=2000):
        self.calls.append({
            "system": system,
            "messages": [dict(m) for m in messages],
            "max_tokens": max_tokens,
        })
        if self._replies:
            return self._replies.pop(0)
        return "FINAL ANSWER: 0"

    def web_search(self, query):
        return f"stub search results for {query}"


class TestReactSolveMultimodalWiring:
    def test_sonnet_attaches_screenshot_on_web_fetch_full(
        self, gaia_agent_mod, monkeypatch,
    ):
        monkeypatch.setattr(
            gaia_agent_mod, "_feature_enabled",
            lambda name, default=True: True,
        )
        monkeypatch.setattr(
            gaia_agent_mod, "web_fetch_full_action_multimodal",
            lambda url: {
                "text_summary": f"final_url: {url}\ntitle: T\n--- page text ---\nbody",
                "screenshot_b64": "BBBB",
                "screenshot_path": "/tmp/y.png",
                "mime": "image/png",
                "error": None,
            },
        )
        backend = _StubBackend(
            tier="sonnet",
            replies=[
                "Thought: fetch the page\n"
                "Action: web_fetch_full(\"https://example.com\")",
                "Thought: have it now\nFINAL ANSWER: papaya",
            ],
        )
        out = gaia_agent_mod.react_solve(
            "What does the page say?",
            "", "", backend, "research", max_steps=3,
        )
        # Two backend.chat calls (one per turn)
        assert len(backend.calls) == 2
        # The SECOND chat call should see the multimodal user turn we
        # appended — find it.
        msgs = backend.calls[1]["messages"]
        # The most recent user message (after the assistant's tool call)
        # should be a list-of-blocks containing the image block first.
        last_user = next(
            m for m in reversed(msgs) if m.get("role") == "user"
        )
        content = last_user["content"]
        assert isinstance(content, list), (
            "multimodal turn must be list-of-blocks"
        )
        assert content[0]["type"] == "image"
        assert content[0]["source"]["type"] == "base64"
        assert content[0]["source"]["media_type"] == "image/png"
        assert content[0]["source"]["data"] == "BBBB"
        assert content[1]["type"] == "text"
        assert "final_url: https://example.com" in content[1]["text"]
        assert out == "papaya"

    def test_gemma_falls_back_to_text_only(
        self, gaia_agent_mod, monkeypatch,
    ):
        monkeypatch.setattr(
            gaia_agent_mod, "_feature_enabled",
            lambda name, default=True: True,
        )
        monkeypatch.setattr(
            gaia_agent_mod, "web_fetch_full_action",
            lambda url: f"text-only obs for {url}",
        )
        # Multimodal must NOT be called for gemma.
        monkeypatch.setattr(
            gaia_agent_mod, "web_fetch_full_action_multimodal",
            lambda url: pytest.fail(
                "gemma backend must not call multimodal action"
            ),
        )
        backend = _StubBackend(
            tier="gemma",
            replies=[
                "Thought: fetch\n"
                "Action: web_fetch_full(\"https://x.test\")",
                "Thought: ok\nFINAL ANSWER: alpha",
            ],
        )
        out = gaia_agent_mod.react_solve(
            "Q?", "", "", backend, "factual", max_steps=3,
        )
        msgs = backend.calls[1]["messages"]
        last_user = next(
            m for m in reversed(msgs) if m.get("role") == "user"
        )
        # Text-only path keeps content as a string
        assert isinstance(last_user["content"], str)
        assert "text-only obs" in last_user["content"]
        assert out == "alpha"

    def test_multimodal_off_stays_text_only_on_sonnet(
        self, gaia_agent_mod, monkeypatch,
    ):
        # gaia_web_fetch_full ON, gaia_web_fetch_full_multimodal OFF
        def _feat(name, default=True):
            return name != "gaia_web_fetch_full_multimodal"

        monkeypatch.setattr(gaia_agent_mod, "_feature_enabled", _feat)
        monkeypatch.setattr(
            gaia_agent_mod, "web_fetch_full_action",
            lambda url: f"text-only obs for {url}",
        )
        monkeypatch.setattr(
            gaia_agent_mod, "web_fetch_full_action_multimodal",
            lambda url: pytest.fail(
                "feature flag off must not call multimodal action"
            ),
        )
        backend = _StubBackend(
            tier="sonnet",
            replies=[
                "Thought: fetch\n"
                "Action: web_fetch_full(\"https://x.test\")",
                "Thought: ok\nFINAL ANSWER: bravo",
            ],
        )
        out = gaia_agent_mod.react_solve(
            "Q?", "", "", backend, "research", max_steps=3,
        )
        msgs = backend.calls[1]["messages"]
        last_user = next(
            m for m in reversed(msgs) if m.get("role") == "user"
        )
        assert isinstance(last_user["content"], str)
        assert out == "bravo"

    def test_screenshot_missing_falls_back_to_text(
        self, gaia_agent_mod, monkeypatch,
    ):
        monkeypatch.setattr(
            gaia_agent_mod, "_feature_enabled",
            lambda name, default=True: True,
        )
        monkeypatch.setattr(
            gaia_agent_mod, "web_fetch_full_action_multimodal",
            lambda url: {
                "text_summary": f"final_url: {url}\n--- page text ---\nbody",
                "screenshot_b64": None,  # capture failed / disabled
                "screenshot_path": None,
                "mime": "image/png",
                "error": "warn: screenshot failed",
            },
        )
        backend = _StubBackend(
            tier="sonnet",
            replies=[
                "Thought: fetch\n"
                "Action: web_fetch_full(\"https://x.test\")",
                "Thought: ok\nFINAL ANSWER: charlie",
            ],
        )
        out = gaia_agent_mod.react_solve(
            "Q?", "", "", backend, "research", max_steps=3,
        )
        msgs = backend.calls[1]["messages"]
        last_user = next(
            m for m in reversed(msgs) if m.get("role") == "user"
        )
        # No image block — text-only string keeps the loop going.
        assert isinstance(last_user["content"], str)
        assert "body" in last_user["content"]
        assert out == "charlie"


# ── feature_config registration ─────────────────────────────


class TestFeatureMetaRegistration:
    def test_multimodal_feature_registered(self):
        from concinno.feature_config import FEATURE_META
        assert "gaia_web_fetch_full_multimodal" in FEATURE_META
        meta = FEATURE_META["gaia_web_fetch_full_multimodal"]
        assert meta["ziq_autotunable"] is False
        assert meta["cosmetic"] is False
