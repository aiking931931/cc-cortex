"""Tests for gaia polygon-area structured-JSON multipass + closure check.

Origin: GAIA 6359a0b1 orthogonal polygon area — Sonnet 4.6 (42/84/5) and
Opus 4.7 (1/6/0) free-form multipass both fail, root cause is arithmetic-
in-head error compounded by non-to-scale schematic geometry. The fix
(``_solve_polygon_structured_multipass``) asks the model for a strict
JSON object listing rectangles + per-direction edge sums, then Python
verifies horizontal/vertical closure and re-derives area from the
rectangles. Closure-invalid passes are dropped. Median of valid passes
is returned.

Tests focus on the closure validator, JSON extractor, multipass
orchestration, and routing wiring. Real-model end-to-end is done out-
of-band (see ``benchmarks/gaia/evidence/smoke_p01_polygon_*.json``).
"""

from __future__ import annotations

import json
import sys
from typing import Any

import pytest


@pytest.fixture
def gaia_agent_mod(monkeypatch):
    monkeypatch.setenv("HF_TOKEN", "test-dummy")
    sys.modules.pop("concinno.skills.public.agent.gaia_agent", None)
    from concinno.skills.public.agent import gaia_agent
    return gaia_agent


# ─────────────────────────── _extract_json_object ────────────────────────────


class TestExtractJsonObject:
    def test_pure_json(self, gaia_agent_mod):
        raw = '{"a": 1, "b": [2, 3]}'
        out = gaia_agent_mod._extract_json_object(raw)
        assert out is not None
        assert json.loads(out) == {"a": 1, "b": [2, 3]}

    def test_fenced_json(self, gaia_agent_mod):
        raw = "Here is the result:\n```json\n{\"x\": 42}\n```\nDone."
        out = gaia_agent_mod._extract_json_object(raw)
        assert out is not None
        assert json.loads(out) == {"x": 42}

    def test_fenced_no_lang(self, gaia_agent_mod):
        raw = "```\n{\"y\": 7}\n```"
        out = gaia_agent_mod._extract_json_object(raw)
        assert out is not None
        assert json.loads(out) == {"y": 7}

    def test_naked_after_prose(self, gaia_agent_mod):
        raw = "Reasoning... Output: {\"area\": 39}"
        out = gaia_agent_mod._extract_json_object(raw)
        assert out is not None
        assert json.loads(out) == {"area": 39}

    def test_no_json_returns_none(self, gaia_agent_mod):
        assert gaia_agent_mod._extract_json_object("just prose") is None

    def test_empty_returns_none(self, gaia_agent_mod):
        assert gaia_agent_mod._extract_json_object("") is None
        assert gaia_agent_mod._extract_json_object(None) is None  # type: ignore[arg-type]

    def test_picks_largest_block_when_multiple(self, gaia_agent_mod):
        # Greedy regex captures the outermost {...} block — sufficient
        # because the prompt requires JSON-only output anyway.
        raw = '{"outer": {"inner": "ok", "n": 3}}'
        out = gaia_agent_mod._extract_json_object(raw)
        assert out is not None
        parsed = json.loads(out)
        assert parsed["outer"]["n"] == 3


# ──────────────────────── _validate_polygon_pass ─────────────────────────────


def _good_obj(area: float = 39) -> dict:
    """Construct a minimal closure-valid polygon object."""
    return {
        "labels_visible": [10, 6, 4, 8, 6, 1, 1.5, 10, 2, 6, 4, 1],
        "rectangles": [
            {"width": 6.5, "height": 6, "explanation": "a"},
            {"width": 0, "height": 0, "explanation": "ignored"},  # filtered
        ] if False else [  # simple two-rect decomposition summing to area
            {"width": 6.5, "height": 6, "explanation": "rect1"},
            {"width": 0.0, "height": 0.0, "explanation": "skip"},
        ],
        "edge_sums": {
            "horizontal_right": 10,
            "horizontal_left": 10,
            "vertical_down": 8,
            "vertical_up": 8,
        },
        "computed_area": area,
    }


class TestValidatePolygonPass:
    def test_valid_closure_returns_recomputed(self, gaia_agent_mod):
        # Two rectangles summing to 39 with closure holding
        obj = {
            "labels_visible": [10, 6, 4, 8],
            "rectangles": [
                {"width": 10, "height": 3},
                {"width": 9, "height": 1},
            ],
            "edge_sums": {
                "horizontal_right": 10,
                "horizontal_left": 10,
                "vertical_down": 4,
                "vertical_up": 4,
            },
            "computed_area": 39,
        }
        assert gaia_agent_mod._validate_polygon_pass(obj) == 39

    def test_horizontal_closure_breaks(self, gaia_agent_mod):
        obj = {
            "rectangles": [{"width": 5, "height": 5}],
            "edge_sums": {
                "horizontal_right": 10,
                "horizontal_left": 8,  # ≠ 10, fails closure
                "vertical_down": 5,
                "vertical_up": 5,
            },
            "computed_area": 25,
        }
        assert gaia_agent_mod._validate_polygon_pass(obj) is None

    def test_vertical_closure_breaks(self, gaia_agent_mod):
        obj = {
            "rectangles": [{"width": 5, "height": 5}],
            "edge_sums": {
                "horizontal_right": 5,
                "horizontal_left": 5,
                "vertical_down": 5,
                "vertical_up": 7,  # mismatch
            },
            "computed_area": 25,
        }
        assert gaia_agent_mod._validate_polygon_pass(obj) is None

    def test_recompute_mismatch_rejected(self, gaia_agent_mod):
        # Rectangles sum to 35 but model claims 39 — Sonnet's arithmetic
        # error caught by Python's recompute step.
        obj = {
            "rectangles": [
                {"width": 7, "height": 5},  # 35
            ],
            "edge_sums": {
                "horizontal_right": 7,
                "horizontal_left": 7,
                "vertical_down": 5,
                "vertical_up": 5,
            },
            "computed_area": 39,  # wrong
        }
        assert gaia_agent_mod._validate_polygon_pass(obj) is None

    def test_empty_rectangles_rejected(self, gaia_agent_mod):
        obj = {
            "rectangles": [],
            "edge_sums": {
                "horizontal_right": 0, "horizontal_left": 0,
                "vertical_down": 0, "vertical_up": 0,
            },
            "computed_area": 0,
        }
        assert gaia_agent_mod._validate_polygon_pass(obj) is None

    def test_missing_rectangles_key_rejected(self, gaia_agent_mod):
        obj = {
            "edge_sums": {
                "horizontal_right": 0, "horizontal_left": 0,
                "vertical_down": 0, "vertical_up": 0,
            },
            "computed_area": 0,
        }
        assert gaia_agent_mod._validate_polygon_pass(obj) is None

    def test_zero_or_negative_dim_rejected(self, gaia_agent_mod):
        obj = {
            "rectangles": [{"width": 5, "height": -1}],
            "edge_sums": {
                "horizontal_right": 5, "horizontal_left": 5,
                "vertical_down": 1, "vertical_up": 1,
            },
            "computed_area": -5,
        }
        assert gaia_agent_mod._validate_polygon_pass(obj) is None

    def test_missing_edge_sums_rejected(self, gaia_agent_mod):
        obj = {
            "rectangles": [{"width": 5, "height": 5}],
            "computed_area": 25,
        }
        assert gaia_agent_mod._validate_polygon_pass(obj) is None

    def test_string_numbers_coerce(self, gaia_agent_mod):
        # Sonnet sometimes emits strings — float() should coerce.
        obj = {
            "rectangles": [
                {"width": "10", "height": "3"},
                {"width": "9", "height": "1"},
            ],
            "edge_sums": {
                "horizontal_right": "10", "horizontal_left": "10",
                "vertical_down": "4", "vertical_up": "4",
            },
            "computed_area": "39",
        }
        assert gaia_agent_mod._validate_polygon_pass(obj) == 39

    def test_within_tolerance_passes(self, gaia_agent_mod):
        # Closure off by 0.4 — within default 0.51 tolerance.
        obj = {
            "rectangles": [{"width": 5, "height": 5}],
            "edge_sums": {
                "horizontal_right": 5.0, "horizontal_left": 5.4,
                "vertical_down": 5.0, "vertical_up": 5.0,
            },
            "computed_area": 25,
        }
        assert gaia_agent_mod._validate_polygon_pass(obj) == 25


# ────────────────────────── _format_polygon_area ─────────────────────────────


class TestFormatPolygonArea:
    def test_integer_drops_decimal(self, gaia_agent_mod):
        assert gaia_agent_mod._format_polygon_area(39.0) == "39"
        assert gaia_agent_mod._format_polygon_area(39.04) == "39"

    def test_half_integer_kept(self, gaia_agent_mod):
        assert gaia_agent_mod._format_polygon_area(12.5) == "12.5"

    def test_close_to_integer_rounded(self, gaia_agent_mod):
        assert gaia_agent_mod._format_polygon_area(38.96) == "39"


# ───────────────────── _solve_polygon_structured_multipass ───────────────────


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
                return _FakeResp("{}")

        self.messages = _Messages(self)


def _valid_json_response(area: float) -> str:
    """Build a JSON response that passes closure + recompute.

    Rectangles are tailored so ``sum(w*h) == area``; closure is built
    from those same rectangles so horizontal_right == horizontal_left
    and vertical_down == vertical_up.
    """
    # Express area as a single rectangle width × 1 (height = 1) for
    # simplicity — closure trivially holds for a 1×area rectangle.
    rect_w = area
    rect_h = 1
    return json.dumps({
        "labels_visible": [int(rect_w), 1],
        "rectangles": [
            {"width": rect_w, "height": rect_h, "explanation": "single"},
        ],
        "edge_sums": {
            "horizontal_right": rect_w,
            "horizontal_left": rect_w,
            "vertical_down": rect_h,
            "vertical_up": rect_h,
        },
        "computed_area": area,
    })


class TestSolvePolygonStructuredMultipass:
    @pytest.fixture
    def img(self, tmp_path):
        pytest.importorskip("PIL")
        from PIL import Image
        p = tmp_path / "poly.png"
        Image.new("RGB", (1024, 800), "white").save(p)
        return str(p)

    def test_unanimous_valid_passes(self, gaia_agent_mod, img, monkeypatch):
        fake = _FakeAnthropic([_valid_json_response(39) for _ in range(5)])
        monkeypatch.setattr(gaia_agent_mod, "_get_anthropic", lambda: fake)
        voted, records = gaia_agent_mod._solve_polygon_structured_multipass(
            "What is the area of the polygon?", img,
            model="claude-sonnet-4-6", passes_count=5,
        )
        assert voted == "39"
        assert len(records) == 5
        assert all(r["valid"] for r in records)
        # Prompt must contain the structured-JSON anchor
        first_text = fake.calls[0]["messages"][0]["content"][1]["text"]
        assert "Orthogonal polygon area — structured analysis" in first_text
        assert "edge_sums" in first_text
        assert "Closure constraint" in first_text

    def test_majority_valid_take_median(self, gaia_agent_mod, img, monkeypatch):
        # 3 valid: 39 / 39 / 41 → median(sorted) = 39 (the 2nd of 3)
        fake = _FakeAnthropic([
            _valid_json_response(39),
            _valid_json_response(41),
            _valid_json_response(39),
        ])
        monkeypatch.setattr(gaia_agent_mod, "_get_anthropic", lambda: fake)
        voted, records = gaia_agent_mod._solve_polygon_structured_multipass(
            "Area?", img, passes_count=3,
        )
        assert voted == "39"
        assert sum(1 for r in records if r["valid"]) == 3

    def test_closure_invalid_filtered(self, gaia_agent_mod, img, monkeypatch):
        # Only middle pass is closure-valid; first and third break it.
        bad = json.dumps({
            "labels_visible": [],
            "rectangles": [{"width": 5, "height": 5}],
            "edge_sums": {
                "horizontal_right": 5, "horizontal_left": 7,
                "vertical_down": 5, "vertical_up": 5,
            },
            "computed_area": 25,
        })
        fake = _FakeAnthropic([bad, _valid_json_response(39), bad])
        monkeypatch.setattr(gaia_agent_mod, "_get_anthropic", lambda: fake)
        voted, records = gaia_agent_mod._solve_polygon_structured_multipass(
            "Area?", img, passes_count=3,
        )
        assert voted == "39"  # one valid pass — its area carries
        valid = [r for r in records if r["valid"]]
        assert len(valid) == 1
        assert valid[0]["area"] == 39

    def test_zero_valid_returns_empty(self, gaia_agent_mod, img, monkeypatch):
        bad = json.dumps({
            "rectangles": [{"width": 5, "height": 5}],
            "edge_sums": {
                "horizontal_right": 1, "horizontal_left": 9,  # broken
                "vertical_down": 5, "vertical_up": 5,
            },
            "computed_area": 25,
        })
        fake = _FakeAnthropic([bad, bad, bad])
        monkeypatch.setattr(gaia_agent_mod, "_get_anthropic", lambda: fake)
        voted, records = gaia_agent_mod._solve_polygon_structured_multipass(
            "Area?", img, passes_count=3,
        )
        assert voted == ""
        assert sum(1 for r in records if r["valid"]) == 0

    def test_non_json_response_drops_pass(self, gaia_agent_mod, img, monkeypatch):
        fake = _FakeAnthropic([
            "I cannot reliably solve this.",
            _valid_json_response(39),
        ])
        monkeypatch.setattr(gaia_agent_mod, "_get_anthropic", lambda: fake)
        voted, records = gaia_agent_mod._solve_polygon_structured_multipass(
            "Area?", img, passes_count=2,
        )
        assert voted == "39"
        assert sum(1 for r in records if r["valid"]) == 1

    def test_anthropic_init_failure_returns_empty(
        self, gaia_agent_mod, img, monkeypatch,
    ):
        def _boom():
            raise RuntimeError("no api key")
        monkeypatch.setattr(gaia_agent_mod, "_get_anthropic", _boom)
        voted, records = gaia_agent_mod._solve_polygon_structured_multipass(
            "Area?", img, passes_count=3,
        )
        assert voted == ""
        assert records == []

    def test_per_pass_exception_isolated(
        self, gaia_agent_mod, img, monkeypatch,
    ):
        class _Partial:
            def __init__(self):
                self.idx = 0
                outer = self

                class _Messages:
                    def create(self, **kw):
                        i = outer.idx
                        outer.idx += 1
                        if i == 1:
                            raise RuntimeError("transient")
                        return _FakeResp(_valid_json_response(39))
                self.messages = _Messages()

        monkeypatch.setattr(
            gaia_agent_mod, "_get_anthropic", lambda: _Partial(),
        )
        voted, records = gaia_agent_mod._solve_polygon_structured_multipass(
            "Area?", img, passes_count=3,
        )
        assert voted == "39"  # passes 0 and 2 valid
        valid = sum(1 for r in records if r["valid"])
        assert valid == 2


# ───────────────────────── routing wiring (priority) ─────────────────────────


class TestRoutingPriority:
    def test_structured_first_legacy_skipped_when_valid(
        self, gaia_agent_mod, tmp_path, monkeypatch,
    ):
        pytest.importorskip("PIL")
        from PIL import Image
        img = tmp_path / "poly.png"
        Image.new("RGB", (1024, 800), "white").save(img)

        called = {"local": 0, "structured": 0, "legacy": 0}

        def _fake_struct(question, path, *, model, passes_count):
            called["structured"] += 1
            return "39", [{"valid": True, "area": 39}]

        def _fake_legacy(question, path, *, model, passes_count):
            called["legacy"] += 1
            return "999", ["999"]

        monkeypatch.setattr(
            gaia_agent_mod, "_solve_polygon_structured_multipass", _fake_struct,
        )
        monkeypatch.setattr(
            gaia_agent_mod, "_solve_vision_anthropic_multipass", _fake_legacy,
        )
        monkeypatch.setattr(
            gaia_agent_mod, "_get_local_vision_llm",
            lambda: (_ for _ in ()).throw(AssertionError("no local")),
        )
        out = gaia_agent_mod._solve_vision_local(
            "What is the area of the orthogonal polygon?", str(img),
        )
        assert out == "39"
        assert called["structured"] == 1
        assert called["legacy"] == 0
        assert called["local"] == 0

    def test_structured_empty_falls_through_to_legacy(
        self, gaia_agent_mod, tmp_path, monkeypatch,
    ):
        pytest.importorskip("PIL")
        from PIL import Image
        img = tmp_path / "poly.png"
        Image.new("RGB", (1024, 800), "white").save(img)

        called = {"structured": 0, "legacy": 0}

        def _fake_struct(question, path, *, model, passes_count):
            called["structured"] += 1
            return "", [{"valid": False}]

        def _fake_legacy(question, path, *, model, passes_count):
            called["legacy"] += 1
            return "39", ["39"]

        monkeypatch.setattr(
            gaia_agent_mod, "_solve_polygon_structured_multipass", _fake_struct,
        )
        monkeypatch.setattr(
            gaia_agent_mod, "_solve_vision_anthropic_multipass", _fake_legacy,
        )
        out = gaia_agent_mod._solve_vision_local(
            "What is the area of the orthogonal polygon?", str(img),
        )
        assert out == "39"
        assert called["structured"] == 1
        assert called["legacy"] == 1

    def test_structured_disabled_uses_legacy(
        self, gaia_agent_mod, tmp_path, monkeypatch,
    ):
        pytest.importorskip("PIL")
        from PIL import Image
        img = tmp_path / "poly.png"
        Image.new("RGB", (1024, 800), "white").save(img)

        def _feature(name, default=True):
            if name == "gaia_polygon_structured_multipass":
                return False
            return default

        monkeypatch.setattr(gaia_agent_mod, "_feature_enabled", _feature)

        called = {"structured": 0, "legacy": 0}

        def _fake_struct(*a, **kw):
            called["structured"] += 1
            return "39", []

        def _fake_legacy(*a, **kw):
            called["legacy"] += 1
            return "39", ["39"]

        monkeypatch.setattr(
            gaia_agent_mod, "_solve_polygon_structured_multipass", _fake_struct,
        )
        monkeypatch.setattr(
            gaia_agent_mod, "_solve_vision_anthropic_multipass", _fake_legacy,
        )
        out = gaia_agent_mod._solve_vision_local(
            "What is the area of the orthogonal polygon?", str(img),
        )
        assert out == "39"
        assert called["structured"] == 0  # gated off
        assert called["legacy"] == 1  # legacy still runs


# ───────────────────────── feature_config registration ──────────────────────


class TestFeatureRegistration:
    def test_feature_registered(self):
        from concinno.feature_config import FEATURE_META
        assert "gaia_polygon_structured_multipass" in FEATURE_META

    def test_feature_meta_fields(self):
        from concinno.feature_config import FEATURE_META
        meta = FEATURE_META["gaia_polygon_structured_multipass"]
        assert meta["category"] == "context"
        assert meta["ziq_autotunable"] is False
        assert meta["cosmetic"] is False
        params = meta["params"]
        assert "passes_count" in params
        assert "model" in params
        assert params["passes_count"]["default"] == 5
        assert params["model"]["default"] == "claude-sonnet-4-6"
