"""Tests for gaia colour-coded numeric data hybrid solver.

Origin: GAIA df6561b2 — image with red and green numbers asks for
arithmetic over colour-tagged subsets. Hybrid pipeline: OpenCV
colour-mask each colour separately + narrow Sonnet OCR per isolated
image + Sonnet text-only compute on clean extracted lists. Stable PASS
17.056 in cont'd¹⁰.

Tests cover detection, helpers, orchestrator wiring, feature
registration. Real-model smoke is in
``benchmarks/gaia/evidence/run_p_red_green_stats_smoke.py``.
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


# ──────────────────── _is_colour_coded_numeric_data_question ────────────


class TestColourCodedDetection:
    def test_red_green_average_stdev(self, gaia_agent_mod):
        q = (
            "When you take the average of the standard population "
            "deviation of the red numbers and the standard sample "
            "deviation of the green numbers in this image, what is "
            "the result?"
        )
        assert gaia_agent_mod._is_colour_coded_numeric_data_question(q)

    def test_blue_yellow_sum(self, gaia_agent_mod):
        q = "Sum the blue numbers and the yellow numbers in the image."
        assert gaia_agent_mod._is_colour_coded_numeric_data_question(q)

    def test_single_colour_rejected(self, gaia_agent_mod):
        # Only one colour mentioned → not enough to disambiguate
        # which mask to extract per subset.
        q = "What is the sum of the red numbers in the image?"
        assert not gaia_agent_mod._is_colour_coded_numeric_data_question(q)

    def test_no_numbers_keyword_rejected(self, gaia_agent_mod):
        q = "What is the average of the red boxes and the green boxes?"
        assert not gaia_agent_mod._is_colour_coded_numeric_data_question(q)

    def test_no_arithmetic_op_rejected(self, gaia_agent_mod):
        q = "List the red numbers and the green numbers."
        assert not gaia_agent_mod._is_colour_coded_numeric_data_question(q)

    def test_empty_question_rejected(self, gaia_agent_mod):
        assert not gaia_agent_mod._is_colour_coded_numeric_data_question("")
        assert not gaia_agent_mod._is_colour_coded_numeric_data_question(None)  # type: ignore[arg-type]


# ────────────────────── _detect_colours_in_question ─────────────────────


class TestDetectColours:
    def test_red_then_green(self, gaia_agent_mod):
        q = "Average of red numbers and green numbers"
        assert gaia_agent_mod._detect_colours_in_question(q) == [
            "red", "green",
        ]

    def test_dedup_preserves_order(self, gaia_agent_mod):
        q = "blue and yellow and blue numbers"
        assert gaia_agent_mod._detect_colours_in_question(q) == [
            "blue", "yellow",
        ]

    def test_unknown_colour_skipped(self, gaia_agent_mod):
        q = "average of red numbers and chartreuse numbers"
        # chartreuse is not in _COLOUR_HSV_RANGES
        out = gaia_agent_mod._detect_colours_in_question(q)
        assert out == ["red"]

    def test_no_colours(self, gaia_agent_mod):
        assert gaia_agent_mod._detect_colours_in_question("hello world") == []


# ─────────────────────── _per_position_majority_numbers ─────────────────


class TestPerPositionMajorityNumbers:
    def test_unanimous(self, gaia_agent_mod):
        passes = [[1, 2, 3], [1, 2, 3], [1, 2, 3]]
        assert gaia_agent_mod._per_position_majority_numbers(passes) == [
            1, 2, 3,
        ]

    def test_per_position_mode(self, gaia_agent_mod):
        passes = [[1, 2, 3], [1, 5, 3], [1, 2, 7]]
        # idx 0: 1 (3x); idx 1: 2 (2x) vs 5 (1x) → 2; idx 2: 3 (2x) vs 7 (1x) → 3
        assert gaia_agent_mod._per_position_majority_numbers(passes) == [
            1, 2, 3,
        ]

    def test_modal_length_filter(self, gaia_agent_mod):
        # Two passes have len 3, one has len 2 → modal len 3, ignore len-2 pass.
        passes = [[1, 2, 3], [1, 2, 3], [9, 9]]
        assert gaia_agent_mod._per_position_majority_numbers(passes) == [
            1, 2, 3,
        ]

    def test_tie_picks_smallest(self, gaia_agent_mod):
        passes = [[7], [3]]
        # tie 7 vs 3 (1 each) → smallest = 3
        assert gaia_agent_mod._per_position_majority_numbers(passes) == [3]

    def test_empty_returns_none(self, gaia_agent_mod):
        assert gaia_agent_mod._per_position_majority_numbers([]) is None


# ────────────────────── _isolate_image_colour ──────────────────────────


class TestIsolateImageColour:
    def test_unknown_colour_returns_none(self, gaia_agent_mod, tmp_path):
        pytest.importorskip("PIL")
        from PIL import Image
        img = tmp_path / "x.png"
        Image.new("RGB", (50, 50), "white").save(img)
        assert gaia_agent_mod._isolate_image_colour(
            str(img), "chartreuse",
        ) is None

    def test_missing_image_returns_none(self, gaia_agent_mod, tmp_path):
        # cv2.imread returns None for non-existent file
        out = gaia_agent_mod._isolate_image_colour(
            str(tmp_path / "missing.png"), "red",
        )
        assert out is None

    def test_returns_png_bytes_for_valid_image(
        self, gaia_agent_mod, tmp_path,
    ):
        cv2 = pytest.importorskip("cv2")
        np = pytest.importorskip("numpy")
        # Tiny synthetic red image
        img_arr = np.zeros((20, 20, 3), dtype=np.uint8)
        img_arr[:, :] = [0, 0, 255]  # solid red in BGR
        img_path = tmp_path / "red.png"
        cv2.imwrite(str(img_path), img_arr)
        out = gaia_agent_mod._isolate_image_colour(str(img_path), "red")
        assert isinstance(out, bytes)
        assert out.startswith(b"\x89PNG")  # PNG magic header


# ───────────────────────── orchestrator wiring ──────────────────────────


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


class TestOrchestratorWiring:
    def test_fewer_than_2_colours_returns_empty(
        self, gaia_agent_mod, monkeypatch,
    ):
        # Question only mentions red → can't extract two subsets.
        out, info = gaia_agent_mod._solve_colour_coded_numeric_via_hybrid(
            "What is the sum of the red numbers?", "/x.png",
        )
        assert out == ""
        assert "fewer than 2 colour names" in info["error"]

    def test_isolate_fail_returns_empty(
        self, gaia_agent_mod, monkeypatch,
    ):
        monkeypatch.setattr(
            gaia_agent_mod, "_isolate_image_colour",
            lambda *a, **kw: None,
        )
        out, info = gaia_agent_mod._solve_colour_coded_numeric_via_hybrid(
            "Average of red numbers and green numbers",
            "/missing.png",
        )
        assert out == ""
        assert "isolate fail" in info["error"]

    def test_happy_path(self, gaia_agent_mod, monkeypatch):
        monkeypatch.setattr(
            gaia_agent_mod, "_isolate_image_colour",
            lambda *a, **kw: b"\x89PNG\xfake-bytes",
        )
        # OCR pass: returns same list each time
        ocr_calls: list[bytes] = []

        def _fake_ocr(image_bytes, *, model):
            ocr_calls.append(image_bytes)
            return [10, 20]

        monkeypatch.setattr(
            gaia_agent_mod, "_call_sonnet_single_colour_list_ocr",
            _fake_ocr,
        )
        # Compute call: returns operation plan JSON. Plan = mean of
        # mean(red) + mean(green); both lists are [10, 20] → both
        # means = 15 → mean of [15, 15] = 15.
        plan_json = (
            '{"intermediate": [{"name": "r_mean", "fn": "mean", '
            '"input": "red"}, {"name": "g_mean", "fn": "mean", '
            '"input": "green"}], "final": {"fn": "mean", '
            '"input": ["r_mean", "g_mean"]}, "round_decimals": 0}'
        )
        fake = _FakeAnthropic([plan_json])
        monkeypatch.setattr(
            gaia_agent_mod, "_get_anthropic", lambda: fake,
        )
        out, info = gaia_agent_mod._solve_colour_coded_numeric_via_hybrid(
            "Average of red numbers and green numbers",
            "/x.png", passes_count=2,
        )
        assert out == "15"
        assert info["stage"] == "done"
        # 2 colours × 2 passes = 4 OCR calls + 1 compute call
        assert len(ocr_calls) == 4
        assert info["extracted"] == {
            "red": [10.0, 20.0], "green": [10.0, 20.0],
        }

    def test_all_ocr_fail_returns_empty(
        self, gaia_agent_mod, monkeypatch,
    ):
        monkeypatch.setattr(
            gaia_agent_mod, "_isolate_image_colour",
            lambda *a, **kw: b"\x89PNG\xfake",
        )
        monkeypatch.setattr(
            gaia_agent_mod, "_call_sonnet_single_colour_list_ocr",
            lambda *a, **kw: None,
        )
        out, info = gaia_agent_mod._solve_colour_coded_numeric_via_hybrid(
            "Average of red numbers and green numbers",
            "/x.png", passes_count=2,
        )
        assert out == ""
        assert "all OCR fail" in info["error"]


# ──────────────────── _execute_statistics_plan ──────────────────────────


class TestExecuteStatisticsPlan:
    def test_simple_mean_no_intermediate(self, gaia_agent_mod):
        plan = {
            "intermediate": [],
            "final": {"fn": "mean", "input": "red"},
            "round_decimals": 1,
        }
        out, info = gaia_agent_mod._execute_statistics_plan(
            plan, {"red": [10, 20, 30]},
        )
        assert out == "20.0"

    def test_pstdev_then_stdev_then_mean(self, gaia_agent_mod):
        # Mirrors df6561b2 structure: pstdev(red), stdev(green),
        # mean of the two intermediates, rounded to 3 decimals.
        plan = {
            "intermediate": [
                {"name": "rp", "fn": "pstdev", "input": "red"},
                {"name": "gs", "fn": "stdev", "input": "green"},
            ],
            "final": {"fn": "mean", "input": ["rp", "gs"]},
            "round_decimals": 3,
        }
        red = [
            24, 74, 28, 54, 73, 33, 64, 73, 60, 53, 59, 40, 65, 76, 48,
            34, 62, 70, 31, 24, 51, 55, 78, 76, 41, 77, 51,
        ]
        green = [
            39, 29, 28, 72, 68, 47, 64, 74, 72, 40, 75, 26, 27, 37, 31,
            55, 44, 64, 65, 38, 46, 66, 35, 76, 61, 53, 49,
        ]
        out, info = gaia_agent_mod._execute_statistics_plan(
            plan, {"red": red, "green": green},
        )
        assert out == "17.056"
        assert "rp" in info["intermediates"]
        assert "gs" in info["intermediates"]

    def test_invalid_fn_rejected(self, gaia_agent_mod):
        plan = {
            "intermediate": [],
            "final": {"fn": "eval", "input": "red"},
        }
        out, info = gaia_agent_mod._execute_statistics_plan(
            plan, {"red": [1, 2, 3]},
        )
        assert out == ""
        assert "invalid final fn" in info["error"]

    def test_invalid_intermediate_input(self, gaia_agent_mod):
        plan = {
            "intermediate": [
                {"name": "x", "fn": "mean", "input": "purple"},
            ],
            "final": {"fn": "mean", "input": ["x"]},
        }
        out, info = gaia_agent_mod._execute_statistics_plan(
            plan, {"red": [1, 2, 3]},
        )
        assert out == ""
        assert "purple" in info["error"]

    def test_round_decimals_default(self, gaia_agent_mod):
        # No round_decimals → integer output for whole result
        plan = {
            "intermediate": [],
            "final": {"fn": "mean", "input": "red"},
        }
        out, info = gaia_agent_mod._execute_statistics_plan(
            plan, {"red": [10, 20, 30]},
        )
        assert out == "20"

    def test_non_dict_plan_rejected(self, gaia_agent_mod):
        out, info = gaia_agent_mod._execute_statistics_plan(
            "not a dict", {"red": [1]},
        )
        assert out == ""
        assert info["error"] == "plan not a dict"


# ────────────────────── feature_config registration ─────────────────────


class TestFeatureRegistration:
    def test_feature_registered(self):
        from concinno.feature_config import FEATURE_META
        assert "gaia_colour_coded_numeric_hybrid" in FEATURE_META

    def test_feature_meta_fields(self):
        from concinno.feature_config import FEATURE_META
        meta = FEATURE_META["gaia_colour_coded_numeric_hybrid"]
        assert meta["category"] == "context"
        assert meta["ziq_autotunable"] is False
        assert meta["cosmetic"] is False
        params = meta["params"]
        assert params["passes_count"]["default"] == 3
        assert params["model"]["default"] == "claude-sonnet-4-6"


# ─────────────────────── _solve_vision_local routing ────────────────────


class TestRoutingPriority:
    def test_colour_coded_routes_first(
        self, gaia_agent_mod, tmp_path, monkeypatch,
    ):
        pytest.importorskip("PIL")
        from PIL import Image
        img = tmp_path / "x.png"
        Image.new("RGB", (640, 480), "black").save(img)

        called = {"colour_hybrid": 0, "polygon_hybrid": 0, "local": 0}

        def _fake_colour_hybrid(question, path, *, model, passes_count):
            called["colour_hybrid"] += 1
            return "17.056", {"stage": "done"}

        def _fake_polygon_hybrid(*a, **kw):
            called["polygon_hybrid"] += 1
            return "999", {"stage": "done"}

        monkeypatch.setattr(
            gaia_agent_mod, "_solve_colour_coded_numeric_via_hybrid",
            _fake_colour_hybrid,
        )
        monkeypatch.setattr(
            gaia_agent_mod, "_solve_orthogonal_polygon_via_opencv_hybrid",
            _fake_polygon_hybrid,
        )
        monkeypatch.setattr(
            gaia_agent_mod, "_get_local_vision_llm",
            lambda: (_ for _ in ()).throw(AssertionError("no local")),
        )
        out = gaia_agent_mod._solve_vision_local(
            "What is the average of the standard deviation of the red "
            "numbers and the green numbers in this image?",
            str(img),
        )
        assert out == "17.056"
        assert called["colour_hybrid"] == 1
        assert called["polygon_hybrid"] == 0
