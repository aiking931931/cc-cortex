"""Tests for music-notation detection + bass-clef hint integration.

Origin: GAIA 8f80e01c bass clef — local Gemma 4 vision needed a
mnemonic + word-reverse + time-unit hint to solve. Only music-notation
questions should receive the specialized prelude so non-music vision
baselines (e.g. d8152ad6 Dropbox plan) do not regress.
"""

from __future__ import annotations

import sys

import pytest


@pytest.fixture
def gaia_agent_mod(monkeypatch):
    monkeypatch.setenv("HF_TOKEN", "test-dummy")
    sys.modules.pop("concinno.skills.public.agent.gaia_agent", None)
    from concinno.skills.public.agent import gaia_agent
    return gaia_agent


class TestMusicNotationDetection:
    def test_bass_clef_matches(self, gaia_agent_mod):
        assert gaia_agent_mod._is_music_notation_question(
            "Which word is spelled by the bass clef notes?"
        )

    def test_treble_clef_matches(self, gaia_agent_mod):
        assert gaia_agent_mod._is_music_notation_question(
            "Read the treble clef and name the pitches."
        )

    def test_staff_matches(self, gaia_agent_mod):
        assert gaia_agent_mod._is_music_notation_question(
            "Count the noteheads on the staff."
        )

    def test_sheet_music_matches(self, gaia_agent_mod):
        assert gaia_agent_mod._is_music_notation_question(
            "This sheet music contains a puzzle."
        )

    def test_note_substring_does_not_match_arbitrary_text(
        self, gaia_agent_mod,
    ):
        # "notable" / "noticed" should not match; we keyed on whole-word
        # "note" / "notes" with \b boundaries.
        assert not gaia_agent_mod._is_music_notation_question(
            "What is the notable landmark in this picture?"
        )

    def test_note_whole_word_matches(self, gaia_agent_mod):
        assert gaia_agent_mod._is_music_notation_question(
            "Identify the notes in this image."
        )

    def test_non_music_does_not_match(self, gaia_agent_mod):
        assert not gaia_agent_mod._is_music_notation_question(
            "How many apples are on the table?"
        )

    def test_empty_does_not_match(self, gaia_agent_mod):
        assert not gaia_agent_mod._is_music_notation_question("")


class TestVisualReasoningScaffold:
    """2.24.0+: the old task-specific bass-clef / polygon hints have
    been replaced with a single generic visual-reasoning scaffold —
    no solution paths baked into the prompt (was test-set leakage)."""

    def test_scaffold_is_generic_not_bass_clef_specific(self, gaia_agent_mod):
        scaffold = gaia_agent_mod._VISUAL_REASONING_SCAFFOLD
        # Must NOT contain any GAIA-specific solution fragments.
        for leak in ("G B D F A", "A C E G", "Good Boys", "DECADE",
                     "decade=10", "right-to-left", "Walk the boundary",
                     "purple labels"):
            assert leak not in scaffold, (
                f"solution leak detected in generic scaffold: {leak!r}"
            )

    def test_scaffold_has_four_reasoning_steps(self, gaia_agent_mod):
        scaffold = gaia_agent_mod._VISUAL_REASONING_SCAFFOLD
        for marker in ("Step 1", "Step 2", "Step 3", "Step 4"):
            assert marker in scaffold

    def test_back_compat_aliases_point_to_scaffold(self, gaia_agent_mod):
        assert gaia_agent_mod._BASS_CLEF_HINT is gaia_agent_mod._VISUAL_REASONING_SCAFFOLD
        assert gaia_agent_mod._POLYGON_HINT is gaia_agent_mod._VISUAL_REASONING_SCAFFOLD


class TestUpscaleHelper:
    def test_small_image_upscaled(self, gaia_agent_mod, tmp_path):
        pytest.importorskip("PIL")
        from PIL import Image
        src = tmp_path / "small.png"
        Image.new("RGB", (100, 100), "white").save(src)
        out = gaia_agent_mod._upscale_image_if_small(str(src))
        assert out != str(src)
        assert Image.open(out).size == (400, 400)

    def test_large_image_untouched(self, gaia_agent_mod, tmp_path):
        pytest.importorskip("PIL")
        from PIL import Image
        src = tmp_path / "large.png"
        Image.new("RGB", (1024, 1024), "white").save(src)
        out = gaia_agent_mod._upscale_image_if_small(str(src))
        assert out == str(src)

    def test_missing_path_returns_input(self, gaia_agent_mod, tmp_path):
        # Non-existent path — PIL raises; helper returns the original
        bogus = str(tmp_path / "does_not_exist.png")
        assert gaia_agent_mod._upscale_image_if_small(bogus) == bogus

    def test_custom_min_side(self, gaia_agent_mod, tmp_path):
        pytest.importorskip("PIL")
        from PIL import Image
        src = tmp_path / "mid.png"
        Image.new("RGB", (400, 400), "white").save(src)
        # With min_side=200, a 400-wide image is already above threshold
        out = gaia_agent_mod._upscale_image_if_small(str(src), min_side=200)
        assert out == str(src)


class TestVisionLocalIntegration:
    """Verify music-mode wiring threads hint + upscale into the LLM call."""

    def test_music_mode_prepends_hint_and_upscales(
        self, gaia_agent_mod, tmp_path, monkeypatch,
    ):
        pytest.importorskip("PIL")
        from PIL import Image
        img = tmp_path / "clef.png"
        Image.new("RGB", (120, 80), "white").save(img)

        class _FakeLLM:
            def __init__(self):
                self.captured = None

            def create_chat_completion(self, **kwargs):
                self.captured = kwargs
                return {
                    "choices": [
                        {"message": {"content": "FINAL ANSWER: 90"}}
                    ]
                }

        fake = _FakeLLM()
        monkeypatch.setattr(
            gaia_agent_mod, "_get_local_vision_llm", lambda: fake,
        )
        out = gaia_agent_mod._solve_vision_local(
            "Read the bass clef staff and compute the answer.",
            str(img),
        )
        assert out == "90"
        text_block = fake.captured["messages"][0]["content"][0]["text"]
        # 2.x: music questions now get the L1 music-notation procedure
        # anchor (textbook clef line/space mnemonics + time-units), not
        # the generic L2 scaffold. Verify the L1 anchor is present.
        assert "[Music notation procedure]" in text_block
        assert "Bass clef lines" in text_block
        # None of the L0 leakage paths appear.
        for leak in ("DECADE", "Good Boys", "decade=10", "right-to-left"):
            assert leak not in text_block, f"solution leak: {leak!r}"

    def test_non_music_mode_omits_hint(
        self, gaia_agent_mod, tmp_path, monkeypatch,
    ):
        pytest.importorskip("PIL")
        from PIL import Image
        img = tmp_path / "plan.png"
        Image.new("RGB", (1024, 512), "white").save(img)

        captured = {}

        class _FakeLLM:
            def create_chat_completion(self, **kwargs):
                captured.update(kwargs)
                return {
                    "choices": [
                        {"message": {"content": "FINAL ANSWER: 0.03"}}
                    ]
                }

        monkeypatch.setattr(
            gaia_agent_mod, "_get_local_vision_llm", lambda: _FakeLLM(),
        )
        out = gaia_agent_mod._solve_vision_local(
            "What percentage of the chart is used?", str(img),
        )
        assert out == "0.03"
        text_block = captured["messages"][0]["content"][0]["text"]
        # 2.x: a non-music / non-polygon-area question with an image
        # falls through to the L2 generic visual-reasoning scaffold.
        # No L1 domain anchor leaks in.
        assert "[Music notation procedure]" not in text_block
        assert "[Orthogonal polygon area procedure]" not in text_block
        assert "[No-attachment web question procedure]" not in text_block


class TestPolygonCounting:
    def test_polygon_word_matches(self, gaia_agent_mod):
        assert gaia_agent_mod._is_polygon_counting_question(
            "How many edges does this polygon have?"
        )

    def test_sides_matches(self, gaia_agent_mod):
        assert gaia_agent_mod._is_polygon_counting_question(
            "Count the sides in the shape."
        )

    def test_vertices_matches(self, gaia_agent_mod):
        assert gaia_agent_mod._is_polygon_counting_question(
            "Total vertices across both figures?"
        )

    def test_non_polygon_does_not_match(self, gaia_agent_mod):
        assert not gaia_agent_mod._is_polygon_counting_question(
            "What color is the car?"
        )

    def test_polygon_hint_aliases_generic_scaffold(self, gaia_agent_mod):
        # 2.24.0: the old polygon-specific "walk the boundary" /
        # "labels are metadata" strings were task-specific leakage.
        # _POLYGON_HINT now aliases the generic visual-reasoning
        # scaffold; verify no solution-path strings remain.
        hint = gaia_agent_mod._POLYGON_HINT
        assert hint is gaia_agent_mod._VISUAL_REASONING_SCAFFOLD
        for leak in ("walk the boundary", "#edges == #vertices",
                     "purple labels"):
            assert leak.lower() not in hint.lower(), f"leak: {leak!r}"

    def test_polygon_mode_injects_hint(
        self, gaia_agent_mod, tmp_path, monkeypatch,
    ):
        pytest.importorskip("PIL")
        from PIL import Image
        img = tmp_path / "poly.png"
        Image.new("RGB", (1024, 800), "white").save(img)

        captured = {}

        class _FakeLLM:
            def create_chat_completion(self, **kwargs):
                captured.update(kwargs)
                return {
                    "choices": [
                        {"message": {"content": "FINAL ANSWER: 39"}}
                    ]
                }

        monkeypatch.setattr(
            gaia_agent_mod, "_get_local_vision_llm", lambda: _FakeLLM(),
        )
        out = gaia_agent_mod._solve_vision_local(
            "Count the total number of edges across both polygons.",
            str(img),
        )
        assert out == "39"
        text_block = captured["messages"][0]["content"][0]["text"]
        # Generic scaffold injected (no task-specific polygon solution).
        assert "Step 1" in text_block
        assert "Step 4" in text_block
        for leak in ("walk the boundary", "#edges == #vertices",
                     "purple labels"):
            assert leak.lower() not in text_block.lower()

    def test_polygon_feature_off_skips_hint(
        self, gaia_agent_mod, tmp_path, monkeypatch,
    ):
        pytest.importorskip("PIL")
        from PIL import Image
        img = tmp_path / "poly.png"
        Image.new("RGB", (1024, 800), "white").save(img)

        def _feature(name, default=True):
            return False if name == "polygon_counting_hint" else default
        monkeypatch.setattr(gaia_agent_mod, "_feature_enabled", _feature)

        captured = {}

        class _FakeLLM:
            def create_chat_completion(self, **kwargs):
                captured.update(kwargs)
                return {
                    "choices": [
                        {"message": {"content": "FINAL ANSWER: 38"}}
                    ]
                }

        monkeypatch.setattr(
            gaia_agent_mod, "_get_local_vision_llm", lambda: _FakeLLM(),
        )
        gaia_agent_mod._solve_vision_local(
            "How many edges are there?", str(img),
        )
        text_block = captured["messages"][0]["content"][0]["text"]
        # 2.x: polygon_counting_hint feature only gates the upscale
        # path. The dispatcher still falls through to the generic L2
        # scaffold when an image is present (any image deserves the
        # scaffold). What it does NOT inject is the L1 polygon-area
        # anchor (different feature toggle, not triggered by this Q).
        assert "[Orthogonal polygon area procedure]" not in text_block


class TestFeatureSwitches:
    """Verify each of the 7 gaia feature toggles wires through."""

    def test_feature_enabled_default_true(self, gaia_agent_mod, monkeypatch):
        # When config layer not wired or feature unknown, default=True.
        def _raise(*_a, **_kw):
            raise RuntimeError("no config")
        monkeypatch.setattr(
            "concinno.core.config.get_config", _raise,
        )
        assert gaia_agent_mod._feature_enabled("gaia_tool_router") is True

    def test_feature_enabled_default_false(self, gaia_agent_mod, monkeypatch):
        def _raise(*_a, **_kw):
            raise RuntimeError("no config")
        monkeypatch.setattr(
            "concinno.core.config.get_config", _raise,
        )
        assert gaia_agent_mod._feature_enabled(
            "unknown_feature", default=False,
        ) is False

    def test_feature_enabled_reads_config(self, gaia_agent_mod, monkeypatch):
        class _FakeCfg:
            def feature(self, name, key):
                return False

        monkeypatch.setattr(
            "concinno.core.config.get_config", lambda: _FakeCfg(),
        )
        assert gaia_agent_mod._feature_enabled("gaia_tool_router") is False

    def test_feature_enabled_none_falls_back_to_default(
        self, gaia_agent_mod, monkeypatch,
    ):
        class _FakeCfg:
            def feature(self, name, key):
                return None

        monkeypatch.setattr(
            "concinno.core.config.get_config", lambda: _FakeCfg(),
        )
        assert gaia_agent_mod._feature_enabled(
            "gaia_tool_router", default=True,
        ) is True

    def test_bassclef_feature_disabled_skips_hint(
        self, gaia_agent_mod, tmp_path, monkeypatch,
    ):
        pytest.importorskip("PIL")
        from PIL import Image
        img = tmp_path / "clef.png"
        Image.new("RGB", (120, 80), "white").save(img)

        # Hide the hint even though the question is music-notation.
        def _feature(name, default=True):
            return False if name == "bassclef_wordreverse" else default
        monkeypatch.setattr(gaia_agent_mod, "_feature_enabled", _feature)

        captured = {}

        class _FakeLLM:
            def create_chat_completion(self, **kwargs):
                captured.update(kwargs)
                return {
                    "choices": [
                        {"message": {"content": "FINAL ANSWER: unsure"}}
                    ]
                }

        monkeypatch.setattr(
            gaia_agent_mod, "_get_local_vision_llm", lambda: _FakeLLM(),
        )
        gaia_agent_mod._solve_vision_local(
            "Read the bass clef notes.", str(img),
        )
        text_block = captured["messages"][0]["content"][0]["text"]
        # bassclef_wordreverse off → no scaffold prelude for music-only Q.
        assert "Step 1 — Describe" not in text_block

    def test_image_upscale_feature_disabled_keeps_original(
        self, gaia_agent_mod, tmp_path, monkeypatch,
    ):
        pytest.importorskip("PIL")
        from PIL import Image
        img = tmp_path / "tiny.png"
        Image.new("RGB", (100, 80), "white").save(img)

        called = {"upscale": 0}
        real_upscale = gaia_agent_mod._upscale_image_if_small

        def _tracker(path, *a, **kw):
            called["upscale"] += 1
            return real_upscale(path, *a, **kw)

        monkeypatch.setattr(
            gaia_agent_mod, "_upscale_image_if_small", _tracker,
        )

        def _feature(name, default=True):
            return False if name == "image_upscale_4x" else default
        monkeypatch.setattr(gaia_agent_mod, "_feature_enabled", _feature)

        class _FakeLLM:
            def create_chat_completion(self, **kwargs):
                return {
                    "choices": [{"message": {"content": "FINAL ANSWER: x"}}]
                }

        monkeypatch.setattr(
            gaia_agent_mod, "_get_local_vision_llm", lambda: _FakeLLM(),
        )
        gaia_agent_mod._solve_vision_local(
            "Count the notes on the bass clef staff.", str(img),
        )
        assert called["upscale"] == 0

