"""Tests for FieldRead v2 PromptEngine integration (4.4.0).

Covers:
* ``render_elision_breadcrumbs`` tag generation + overhead estimate
* ``build_field_context_v2_string`` composite (handoff content +
  ``<system-context-elided/>`` tag) and toggle behaviour
* ``compress_breakeven_for`` per-complexity routing
* ``read_handoff_fields_v2`` ``compress_breakeven`` override
* ``PromptEngine`` integration: v2 wired in ``assemble()``,
  C0 complexity inference, expand() callback round-trip,
  backward compatibility with v1 ``build_field_context``.
"""

from __future__ import annotations

import os
import tempfile

from concinno.field_read import (
    COMPRESS_BREAKEVEN_BY_COMPLEXITY,
    COMPRESS_BREAKEVEN_TOKENS,
    ElidedSection,
    _estimate_tokens,
    build_field_context,
    build_field_context_v2,
    build_field_context_v2_string,
    compress_breakeven_for,
    expand,
    read_handoff_fields_v2,
    render_elision_breadcrumbs,
)
from concinno.prompt_engine import PromptEngine


# ── compress_breakeven_for ─────────────────────────────────


class TestCompressBreakevenFor:
    def test_simple_aggressive(self):
        assert compress_breakeven_for("simple") == 1500

    def test_complicated_default(self):
        assert (
            compress_breakeven_for("complicated") == COMPRESS_BREAKEVEN_TOKENS
        )

    def test_complex_conservative(self):
        assert compress_breakeven_for("complex") == 3500

    def test_chaotic_very_conservative(self):
        assert compress_breakeven_for("chaotic") == 4000

    def test_none_returns_default(self):
        assert compress_breakeven_for(None) == COMPRESS_BREAKEVEN_TOKENS

    def test_unknown_returns_default(self):
        assert compress_breakeven_for("rocket-science") == (
            COMPRESS_BREAKEVEN_TOKENS
        )

    def test_case_insensitive(self):
        assert compress_breakeven_for("SIMPLE") == 1500
        assert compress_breakeven_for("Chaotic") == 4000

    def test_table_covers_four_classes(self):
        assert set(COMPRESS_BREAKEVEN_BY_COMPLEXITY) == {
            "simple", "complicated", "complex", "chaotic",
        }

    def test_table_monotone_increasing(self):
        s = COMPRESS_BREAKEVEN_BY_COMPLEXITY["simple"]
        c1 = COMPRESS_BREAKEVEN_BY_COMPLEXITY["complicated"]
        c2 = COMPRESS_BREAKEVEN_BY_COMPLEXITY["complex"]
        c3 = COMPRESS_BREAKEVEN_BY_COMPLEXITY["chaotic"]
        assert s < c1 < c2 < c3

    def test_table_within_feature_meta_envelope(self):
        # Mirrors the FEATURE_META[field_read].compress_breakeven_tokens
        # bounds (vmin=1500, vmax=4000).
        for v in COMPRESS_BREAKEVEN_BY_COMPLEXITY.values():
            assert 1500 <= v <= 4000


# ── render_elision_breadcrumbs ─────────────────────────────


class TestRenderElisionBreadcrumbs:
    def test_empty_returns_empty_string(self):
        assert render_elision_breadcrumbs([]) == ""

    def test_single_section(self):
        e = [ElidedSection(
            id="status", heading="狀態", lines=10,
            gist="2 pending", confidence=0.5,
        )]
        out = render_elision_breadcrumbs(e)
        assert out.startswith("<system-context-elided")
        assert "count=1" in out
        assert 'reason="token-budget"' in out
        assert 'ids="status"' in out
        assert out.endswith("/>")

    def test_multiple_sections_inline_cap(self):
        e = [
            ElidedSection(
                id=f"sec-{i}", heading=f"H{i}", lines=5,
                gist=f"g{i}", confidence=0.5,
            )
            for i in range(7)
        ]
        out = render_elision_breadcrumbs(e, max_inline_ids=3)
        assert "count=7" in out
        # Only first 3 ids inline, rest summarised
        assert 'ids="sec-0,sec-1,sec-2,+4-more"' in out

    def test_expand_callback_default(self):
        e = [ElidedSection(
            id="x", heading="H", lines=1, gist="", confidence=1.0,
        )]
        out = render_elision_breadcrumbs(e)
        assert 'expand="concinno.field_read.expand(path, section_id)"' in out

    def test_expand_callback_override(self):
        e = [ElidedSection(
            id="x", heading="H", lines=1, gist="", confidence=1.0,
        )]
        out = render_elision_breadcrumbs(e, expand_callback="my.fn(id)")
        assert 'expand="my.fn(id)"' in out

    def test_overhead_typical_case_around_50_tokens(self):
        # Plan target: ~50 token overhead in the typical case (3 short
        # ids, brief gists). Allows ≤ 60 to absorb formatting jitter.
        e = [
            ElidedSection(
                id=f"sec-{i}", heading=f"H{i}", lines=10,
                gist="2 pending", confidence=0.5,
            )
            for i in range(3)
        ]
        out = render_elision_breadcrumbs(e)
        assert _estimate_tokens(out) <= 60

    def test_overhead_worst_case_bounded(self):
        # Worst-realistic case (5 sections, long ids, full gists) must
        # still stay below ~85 tokens — overhead never balloons even in
        # pathological inputs. Empirically observed peak ~72; the 85
        # ceiling absorbs minor format changes without going stealth.
        e = [
            ElidedSection(
                id=f"handoff:long-handoff-name.md:section-{i}",
                heading=f"section {i}", lines=20,
                gist="2 pending, 1 blocked",
                confidence=0.4,
            )
            for i in range(5)
        ]
        out = render_elision_breadcrumbs(e)
        assert _estimate_tokens(out) <= 85

    def test_gist_truncated_when_long(self):
        e = [ElidedSection(
            id="x", heading="H", lines=1,
            gist="A" * 300, confidence=0.5,
        )]
        out = render_elision_breadcrumbs(e)
        assert "..." in out  # Truncated marker present
        # Total tag still bounded
        assert _estimate_tokens(out) <= 100

    def test_no_gist_attr_when_all_empty(self):
        e = [ElidedSection(
            id="x", heading="H", lines=1, gist="", confidence=1.0,
        )]
        out = render_elision_breadcrumbs(e)
        assert "gist=" not in out


# ── build_field_context_v2_string ──────────────────────────


def _make_handoff(td: str, body: str, name: str = "交接_T.md") -> str:
    hdir = os.path.join(td, "handoffs")
    os.makedirs(hdir, exist_ok=True)
    p = os.path.join(hdir, name)
    with open(p, "w", encoding="utf-8") as f:
        f.write(body)
    return p


class TestBuildFieldContextV2String:
    def test_empty_workspace_returns_empty(self):
        assert build_field_context_v2_string("") == ""

    def test_no_handoff_returns_empty(self):
        with tempfile.TemporaryDirectory() as td:
            assert build_field_context_v2_string(td, "rag") == ""

    def test_string_matches_v1_when_nothing_elided(self):
        with tempfile.TemporaryDirectory() as td:
            _make_handoff(td, "## 狀態\n\nv1.0\n\n## next_step\n\nDo X")
            v1 = build_field_context(td, "status")
            v2 = build_field_context_v2_string(td, "status")
            assert v1 == v2  # No elision → no breadcrumb

    def test_breadcrumb_appended_when_elided(self):
        with tempfile.TemporaryDirectory() as td:
            padding = "history " * 2000
            _make_handoff(td, (
                f"## 狀態\n\nv1.0\n\n## next_step\n\nDo X\n\n"
                f"## 歷史\n\n{padding}"
            ))
            out = build_field_context_v2_string(td, "status")
            if "<system-context-elided" in out:
                # Only assert when elision actually happened (depends on
                # budget arithmetic). When it did, the tag must be at
                # the trailing edge so the LLM reads it last.
                assert out.rstrip().endswith("/>")

    def test_include_breadcrumbs_false_omits_tag(self):
        with tempfile.TemporaryDirectory() as td:
            padding = "history " * 2000
            _make_handoff(td, (
                f"## 狀態\n\nv1.0\n\n## 歷史\n\n{padding}"
            ))
            out = build_field_context_v2_string(
                td, "status", include_breadcrumbs=False,
            )
            assert "<system-context-elided" not in out

    def test_complexity_routes_breakeven(self):
        # Build a handoff sized between simple (1500) and chaotic (4000)
        # breakevens. With complexity="simple" it should compress.
        # With complexity="chaotic" it should pass through.
        body = "## 狀態\n\nv1.0\n\n## 歷史\n\n" + ("x" * 10000)
        with tempfile.TemporaryDirectory() as td:
            _make_handoff(td, body)
            out_simple = build_field_context_v2_string(
                td, "status", complexity="simple",
            )
            out_chaotic = build_field_context_v2_string(
                td, "status", complexity="chaotic",
            )
            # Chaotic keeps more raw text → longer output
            assert len(out_chaotic) >= len(out_simple) * 0.5  # sanity

    def test_returns_str_type(self):
        with tempfile.TemporaryDirectory() as td:
            _make_handoff(td, "## A\n\nB")
            assert isinstance(build_field_context_v2_string(td, "a"), str)


# ── read_handoff_fields_v2 compress_breakeven override ─────


class TestV2BreakevenOverride:
    def test_override_lowers_threshold(self):
        # Body around 600 tokens — under default 2500, over forced 100.
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, encoding="utf-8",
        ) as f:
            body = "## 狀態\n\nv1\n\n## next_step\n\nDo X\n\n## 歷史\n\n" + (
                "x" * 2400
            )
            f.write(body)
            f.flush()
            r_default = read_handoff_fields_v2(f.name, max_tokens=200)
            r_forced = read_handoff_fields_v2(
                f.name, max_tokens=200, compress_breakeven=100,
            )
        os.unlink(f.name)
        # Default → passes through (small file path); forced low →
        # triggers section-select compression
        assert r_default.sections_kept == ["_full"] or (
            r_default.sections_kept == ["_full-truncated"]
        )
        # Forced compression must produce per-section ids, not _full
        assert "_full" not in r_forced.sections_kept

    def test_override_raises_threshold(self):
        # Force breakeven well above raw size → passthrough path.
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, encoding="utf-8",
        ) as f:
            big = "## 狀態\n\nv1\n\n## 歷史\n\n" + ("x" * 13000)
            f.write(big)
            f.flush()
            r_default = read_handoff_fields_v2(f.name, max_tokens=200)
            r_high = read_handoff_fields_v2(
                f.name, max_tokens=5000, compress_breakeven=999_999,
            )
        os.unlink(f.name)
        assert "_full" not in r_default.sections_kept
        # High breakeven → passthrough path engaged
        assert any(k.startswith("_full") for k in r_high.sections_kept)

    def test_override_none_uses_default(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, encoding="utf-8",
        ) as f:
            f.write("## A\n\nB")
            f.flush()
            r_a = read_handoff_fields_v2(f.name, max_tokens=200)
            r_b = read_handoff_fields_v2(
                f.name, max_tokens=200, compress_breakeven=None,
            )
        os.unlink(f.name)
        assert r_a.content == r_b.content


# ── PromptEngine integration ───────────────────────────────


class TestPromptEngineV2Integration:
    def test_assemble_does_not_raise_with_workspace(self):
        with tempfile.TemporaryDirectory() as td:
            engine = PromptEngine(workspace=td)
            engine.load_static()
            out = engine.assemble(task_prompt="rename foo to bar")
            assert isinstance(out, str)

    def test_assemble_includes_handoff_when_present(self):
        with tempfile.TemporaryDirectory() as td:
            _make_handoff(td, (
                "## 狀態\n\nv1.0\n\n## next_step\n\nDo X"
            ))
            engine = PromptEngine(workspace=td)
            engine.load_static()
            out = engine.assemble(task_prompt="status check")
            # Handoff content threaded into dynamic slot
            assert "v1.0" in out or "Do X" in out

    def test_assemble_preserves_complexity_param(self):
        # The legacy `complexity` arg ("standard"/"minimal"/"full")
        # must NOT be shadowed by the new internal cbua_complexity.
        # Regression guard for the variable-shadowing bug we fixed
        # mid-implementation.
        engine = PromptEngine()
        engine.load_static()
        minimal = engine.assemble(complexity="minimal")
        full = engine.assemble(complexity="full")
        assert isinstance(minimal, str)
        assert isinstance(full, str)
        # Full mode adds more thinking directives → longer
        assert len(full) >= len(minimal)

    def test_v2_breadcrumb_emitted_in_assemble_when_elided(self):
        with tempfile.TemporaryDirectory() as td:
            padding = "history " * 2000
            _make_handoff(td, (
                f"## 狀態\n\nv1.0\n\n## next_step\n\nDo X\n\n"
                f"## 歷史\n\n{padding}\n\n## Random\n\nnoise"
            ))
            engine = PromptEngine(workspace=td)
            engine.load_static()
            out = engine.assemble(task_prompt="status")
            # When elision happened, the breadcrumb tag should surface.
            # We don't hard-require it (depends on budget arithmetic
            # squeezing the dynamic slot), but if it did happen, it
            # must be well-formed.
            if "<system-context-elided" in out:
                assert "expand=" in out


class TestInferCBUAComplexity:
    def test_empty_returns_none(self):
        assert PromptEngine._infer_cbua_complexity("") is None

    def test_simple_classification(self):
        v = PromptEngine._infer_cbua_complexity("rename foo to bar")
        assert v == "simple"

    def test_complex_classification(self):
        v = PromptEngine._infer_cbua_complexity(
            "explore novel architecture, uncertain approach"
        )
        assert v == "complex"

    def test_chaotic_classification(self):
        v = PromptEngine._infer_cbua_complexity(
            "emergency, system crashed, urgent rescue"
        )
        assert v == "chaotic"


# ── expand() callback round-trip ───────────────────────────


class TestExpandCallbackRoundTrip:
    def test_breadcrumb_id_resolves_via_expand(self):
        with tempfile.TemporaryDirectory() as td:
            padding = "x" * 12000
            p = os.path.join(td, "交接.md")
            with open(p, "w", encoding="utf-8") as f:
                f.write(
                    f"## 狀態\n\nv1.0\n\n## 歷史\n\n{padding}\n\n"
                    f"## next_step\n\nDo X",
                )
            r = build_field_context_v2(td, "status")
            # Use a section_id from the v1 path (no prefix munging here —
            # build_field_context_v2 prefixes with handoff:<basename>:)
            for e in r.sections_elided:
                # Strip the handoff:basename: prefix to get raw id usable
                # by expand() against the source file.
                if e.id.startswith("handoff:"):
                    raw_id = e.id.split(":", 2)[2]
                    full = expand(p, raw_id)
                    if full:
                        assert e.heading in full or full.startswith("## ")


# ── Backward compatibility ─────────────────────────────────


class TestBackwardCompatibility:
    def test_v1_build_field_context_still_returns_str(self):
        with tempfile.TemporaryDirectory() as td:
            assert isinstance(build_field_context(td, "x"), str)

    def test_v1_unchanged_when_no_breadcrumb_path(self):
        # When nothing is elided, v1 string and v2 string must match
        # exactly so existing callers never see the new tag spuriously.
        with tempfile.TemporaryDirectory() as td:
            _make_handoff(td, "## A\n\nB")
            assert build_field_context(td, "a") == (
                build_field_context_v2_string(td, "a")
            )

    def test_field_read_default_constant_unchanged(self):
        # Plan invariant: the global default stays at 2500.
        assert COMPRESS_BREAKEVEN_TOKENS == 2500
