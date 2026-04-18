"""Tests for concinno.field_read — selective field extraction."""

from __future__ import annotations

import os
import tempfile

from concinno.field_read import (
    COMPRESS_BREAKEVEN_TOKENS,
    FieldReadConfig,
    FieldReadResult,
    MemoryEntry,
    Section,
    _derive_gist,
    _estimate_tokens,
    _extract_keywords,
    _find_handoff_files,
    _heading_slug,
    _is_handoff_index,
    _parse_sections,
    _read_file_body,
    _score_elision_confidence,
    _score_memory_entry,
    _score_section,
    build_field_context,
    build_field_context_v2,
    expand,
    read_handoff_fields,
    read_handoff_fields_v2,
    read_memory_fields,
    read_memory_fields_v2,
)

# ── Token Estimation ──────────────────────────────────────


class TestEstimateTokens:
    def test_empty(self):
        assert _estimate_tokens("") == 0

    def test_ascii(self):
        t = _estimate_tokens("hello world")
        assert t > 0

    def test_cjk(self):
        t = _estimate_tokens("交接壓縮注入")
        assert t >= 6  # 6 CJK chars × 1.5 = 9


# ── Section Parser ────────────────────────────────────────


class TestParseSections:
    def test_basic_sections(self):
        text = "## 狀態\n\nv1.0 live\n\n## next_step\n\nDo X"
        sections = _parse_sections(text)
        assert len(sections) == 2
        assert sections[0].title == "狀態"
        assert "v1.0" in sections[0].content
        assert sections[1].title == "next_step"

    def test_strip_frontmatter(self):
        text = "---\ntags: [test]\n---\n\n## 狀態\n\nactive"
        sections = _parse_sections(text)
        assert len(sections) == 1
        assert sections[0].title == "狀態"
        assert "tags" not in sections[0].content

    def test_preamble(self):
        text = "Some intro text\n\n## Section\n\nContent"
        sections = _parse_sections(text)
        assert sections[0].title == "_preamble"
        assert "intro" in sections[0].content

    def test_empty_text(self):
        assert _parse_sections("") == []

    def test_no_headers(self):
        text = "Just some text without headers"
        sections = _parse_sections(text)
        assert len(sections) == 1
        assert sections[0].title == "_preamble"

    def test_tokens_computed(self):
        sections = _parse_sections("## Title\n\nSome content here")
        assert sections[0].tokens > 0


# ── Section Scoring ───────────────────────────────────────


class TestScoreSection:
    def test_always_sections_high_priority(self):
        sec = Section(title="next_step", content="Do X")
        score = _score_section(sec, [])
        assert score >= 100

    def test_status_section(self):
        sec = Section(title="狀態總覽", content="active")
        assert _score_section(sec, []) >= 100

    def test_actionable_boost(self):
        sec = Section(title="tasks", content="⬜ Build feature\n⬜ Test")
        score_with = _score_section(sec, [])
        sec_no = Section(title="tasks", content="Done")
        score_without = _score_section(sec_no, [])
        assert score_with > score_without

    def test_keyword_match_title(self):
        sec = Section(title="RAG 模組", content="Some content")
        score = _score_section(sec, ["rag"])
        assert score >= 30

    def test_keyword_match_content(self):
        sec = Section(title="Notes", content="Fixed the RAG pipeline")
        score = _score_section(sec, ["rag"])
        assert score >= 10

    def test_no_match_zero(self):
        sec = Section(title="History", content="Old stuff")
        assert _score_section(sec, ["quantum"]) == 0


# ── Keyword Extraction ────────────────────────────────────


class TestExtractKeywords:
    def test_basic(self):
        kw = _extract_keywords("Fix the RAG pipeline bug")
        assert "fix" in kw
        assert "rag" in kw
        assert "pipeline" in kw
        assert "bug" in kw

    def test_stops_removed(self):
        kw = _extract_keywords("the is a an of in for")
        assert len(kw) == 0

    def test_cjk_keywords(self):
        kw = _extract_keywords("修復壓縮模組")
        assert len(kw) > 0

    def test_empty(self):
        assert _extract_keywords("") == []


# ── Read File Body ────────────────────────────────────────


class TestReadFileBody:
    def test_plain_file(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, encoding="utf-8",
        ) as f:
            f.write("Hello world\nSecond line")
            f.flush()
            result = _read_file_body(f.name)
        os.unlink(f.name)
        assert "Hello world" in result

    def test_frontmatter_stripped(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, encoding="utf-8",
        ) as f:
            f.write("---\nname: test\ntype: user\n---\n\nBody content")
            f.flush()
            result = _read_file_body(f.name)
        os.unlink(f.name)
        assert "Body content" in result
        assert "name: test" not in result

    def test_missing_file(self):
        assert _read_file_body("/nonexistent/path.md") == ""

    def test_max_chars(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, encoding="utf-8",
        ) as f:
            f.write("X" * 5000)
            f.flush()
            result = _read_file_body(f.name, max_chars=100)
        os.unlink(f.name)
        assert len(result) <= 100


# ── Handoff Field Reader ──────────────────────────────────


class TestReadHandoffFields:
    def test_missing_file(self):
        assert read_handoff_fields("/no/such/file.md") == ""

    def test_empty_path(self):
        assert read_handoff_fields("") == ""

    def test_extracts_always_sections(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, encoding="utf-8",
        ) as f:
            # Make content large enough to trigger compression (>2500t)
            padding = "x" * 12000  # ~3000+ tokens
            f.write(
                f"## 狀態\n\nv1.0 live\n\n## next_step\n\nDo X\n\n"
                f"## 歷史\n\n{padding}\n",
            )
            f.flush()
            result = read_handoff_fields(f.name, max_tokens=500)
        os.unlink(f.name)
        assert "狀態" in result
        assert "next_step" in result

    def test_keyword_filtering(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, encoding="utf-8",
        ) as f:
            padding = "y" * 12000
            f.write(
                f"## RAG\n\nRAG pipeline stuff\n\n## 歷史\n\n{padding}\n",
            )
            f.flush()
            result = read_handoff_fields(f.name, ["rag"], max_tokens=500)
        os.unlink(f.name)
        assert "RAG" in result

    def test_respects_budget(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, encoding="utf-8",
        ) as f:
            padding = "z" * 12000
            f.write(
                f"## 狀態\n\n{'A' * 2000}\n\n## next_step\n\n{padding}\n",
            )
            f.flush()
            result = read_handoff_fields(f.name, max_tokens=100)
        os.unlink(f.name)
        tokens = _estimate_tokens(result)
        assert tokens <= 150  # Some tolerance for formatting

    def test_small_file_passthrough(self):
        """Files under COMPRESS_BREAKEVEN_TOKENS pass through uncompressed."""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, encoding="utf-8",
        ) as f:
            small = "## 狀態\n\nv1.0\n\n## next_step\n\nDo X"
            f.write(small)
            f.flush()
            result = read_handoff_fields(f.name, max_tokens=5000)
        os.unlink(f.name)
        # Small file → full passthrough (no compression)
        assert "v1.0" in result
        assert "Do X" in result


# ── Memory Field Reader ───────────────────────────────────


class TestMemoryEntry:
    def test_score_match(self):
        entry = MemoryEntry(
            title="RAG 技術樹",
            filename="rag.md",
            description="RAG pipeline architecture",
        )
        score = _score_memory_entry(entry, ["rag", "pipeline"])
        assert score > 0

    def test_score_no_match(self):
        entry = MemoryEntry(
            title="Deploy",
            filename="deploy.md",
            description="VPS deployment",
        )
        assert _score_memory_entry(entry, ["quantum"]) == 0.0

    def test_score_no_keywords(self):
        entry = MemoryEntry(title="X", filename="x.md", description="Y")
        assert _score_memory_entry(entry, []) == 0.0


class TestReadMemoryFields:
    def test_missing_dir(self):
        assert read_memory_fields("/no/such/dir") == ""

    def test_no_keywords(self):
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "MEMORY.md"), "w", encoding="utf-8") as f:
                f.write("- [Test](test.md) — test entry\n")
            assert read_memory_fields(td, []) == ""

    def test_matches_entries(self):
        with tempfile.TemporaryDirectory() as td:
            # Write index
            with open(os.path.join(td, "MEMORY.md"), "w", encoding="utf-8") as f:
                f.write("- [RAG Tree](rag.md) — RAG pipeline architecture\n")
                f.write("- [Deploy](deploy.md) — VPS deployment\n")
            # Write memory file
            with open(os.path.join(td, "rag.md"), "w", encoding="utf-8") as f:
                f.write("---\ntype: project\n---\n\nRAG uses BM25 + dense\n")

            result = read_memory_fields(td, ["rag", "pipeline"], max_tokens=500)
            assert "RAG" in result
            assert "Deploy" not in result

    def test_respects_max_entries(self):
        with tempfile.TemporaryDirectory() as td:
            lines = []
            for i in range(10):
                lines.append(f"- [Item{i}](item{i}.md) — rag item {i}\n")
            with open(os.path.join(td, "MEMORY.md"), "w", encoding="utf-8") as f:
                f.writelines(lines)

            result = read_memory_fields(
                td, ["rag"], max_tokens=5000, max_entries=3,
            )
            # Should have at most 3 entries
            assert result.count("- **Item") <= 3


# ── Handoff File Discovery ────────────────────────────────


class TestIsHandoffIndex:
    def test_valid(self):
        assert _is_handoff_index("交接_CCC.md")

    def test_archive_excluded(self):
        assert not _is_handoff_index("交接_CCC_archive.md")

    def test_summary_excluded(self):
        assert not _is_handoff_index("交接_CCC_summary.md")

    def test_non_handoff(self):
        assert not _is_handoff_index("README.md")


class TestFindHandoffFiles:
    def test_finds_in_handoffs_dir(self):
        with tempfile.TemporaryDirectory() as td:
            hdir = os.path.join(td, "handoffs")
            os.makedirs(hdir)
            hf = os.path.join(hdir, "交接_Test.md")
            with open(hf, "w", encoding="utf-8") as f:
                f.write("## 狀態\n\ntest")
            found = _find_handoff_files(td)
            assert any("交接_Test.md" in p for p in found)

    def test_skips_archive(self):
        with tempfile.TemporaryDirectory() as td:
            hdir = os.path.join(td, "handoffs")
            os.makedirs(hdir)
            with open(
                os.path.join(hdir, "交接_Test_archive.md"), "w", encoding="utf-8",
            ) as f:
                f.write("old")
            found = _find_handoff_files(td)
            assert len(found) == 0

    def test_empty_workspace(self):
        with tempfile.TemporaryDirectory() as td:
            assert _find_handoff_files(td) == []


# ── Build Field Context (Orchestrator) ────────────────────


class TestBuildFieldContext:
    def test_empty_workspace(self):
        assert build_field_context("") == ""

    def test_no_files_found(self):
        with tempfile.TemporaryDirectory() as td:
            assert build_field_context(td) == ""

    def test_with_handoff(self):
        with tempfile.TemporaryDirectory() as td:
            hdir = os.path.join(td, "handoffs")
            os.makedirs(hdir)
            # Write a handoff large enough to trigger compression
            padding = "history " * 2000
            with open(
                os.path.join(hdir, "交接_Test.md"), "w", encoding="utf-8",
            ) as f:
                f.write(
                    f"## 狀態\n\nv1.0\n\n## next_step\n\nDo X\n\n"
                    f"## 歷史\n\n{padding}\n",
                )
            result = build_field_context(td, "test status")
            assert "交接_Test.md" in result

    def test_config_override(self):
        cfg = FieldReadConfig(handoff_budget=50, memory_budget=50)
        with tempfile.TemporaryDirectory() as td:
            result = build_field_context(td, config=cfg)
            assert result == ""  # No files → empty


# ── ZIQ Breakeven Gate ────────────────────────────────────


# ── v2 Metadata-First API (2.6.0) ─────────────────────────


class TestHeadingSlug:
    def test_basic_ascii(self):
        assert _heading_slug("Next Step") == "next-step"

    def test_cjk_preserved(self):
        assert _heading_slug("未解決") == "未解決"

    def test_punct_stripped(self):
        assert _heading_slug("鐵律/約束!") == "鐵律-約束"

    def test_cap_30(self):
        s = _heading_slug("a" * 60)
        assert len(s) <= 30

    def test_empty(self):
        assert _heading_slug("") == ""


class TestDeriveGist:
    def test_pending_markers_count_based(self):
        body = "⬜ Task A\n⬜ Task B\n⏸ Task C\n✅ Task D"
        gist = _derive_gist(body)
        assert "2 pending" in gist
        assert "1 blocked" in gist
        assert "1 done" in gist

    def test_yaml_keys(self):
        body = "name: foo\ntype: bar\nversion: 1.0\n\nSome body"
        gist = _derive_gist(body)
        assert gist.startswith("keys:")
        assert "name" in gist
        assert "type" in gist

    def test_bullet_first(self):
        body = "- First important bullet point\n- Second\n- Third"
        gist = _derive_gist(body)
        assert "First important bullet point" in gist

    def test_plain_first_line(self):
        body = "A plain description line.\nSecond line."
        gist = _derive_gist(body)
        assert "plain description" in gist

    def test_empty(self):
        assert _derive_gist("") == ""


class TestElisionConfidence:
    def test_always_section_low_confidence(self):
        sec = Section(title="next_step", content="Do X")
        c = _score_elision_confidence(sec, [])
        assert c < 0.5  # should NOT be elided

    def test_pending_marker_low_confidence(self):
        sec = Section(title="tasks", content="⬜ Build feature")
        c = _score_elision_confidence(sec, [])
        sec_clean = Section(title="tasks", content="done long ago")
        c_clean = _score_elision_confidence(sec_clean, [])
        assert c < c_clean

    def test_keyword_hit_lowers_confidence(self):
        sec = Section(title="RAG module", content="RAG pipeline")
        c_match = _score_elision_confidence(sec, ["rag"])
        c_none = _score_elision_confidence(sec, [])
        assert c_match < c_none

    def test_clamped_0_to_1(self):
        sec = Section(
            title="next_step 未解決",
            content="⬜ ⬜ " * 100 + "x" * 5000,
        )
        c = _score_elision_confidence(sec, ["x"] * 20)
        assert 0.0 <= c <= 1.0


class TestReadHandoffFieldsV2:
    def _write(self, td: str, body: str) -> str:
        p = os.path.join(td, "交接.md")
        with open(p, "w", encoding="utf-8") as f:
            f.write(body)
        return p

    def test_v2_returns_result(self):
        with tempfile.TemporaryDirectory() as td:
            padding = "x" * 12000
            p = self._write(td, (
                f"## 狀態\n\nv1.0\n\n## next_step\n\nDo X\n\n"
                f"## 歷史\n\n{padding}\n\n## Random\n\nnoise"
            ))
            r = read_handoff_fields_v2(p, max_tokens=200)
            assert isinstance(r, FieldReadResult)
            assert isinstance(r.content, str)
            assert isinstance(r.sections_elided, list)

    def test_v2_elided_breadcrumbs_present(self):
        with tempfile.TemporaryDirectory() as td:
            padding = "x" * 12000
            p = self._write(td, (
                f"## 狀態\n\nv1.0\n\n## next_step\n\nDo X\n\n"
                f"## 歷史\n\n{padding}\n\n## Random\n\nnoise\n\n"
                f"## Old\n\nancient stuff"
            ))
            r = read_handoff_fields_v2(p, max_tokens=100)
            # With budget=100 and 5 sections, some must be elided
            assert len(r.sections_elided) >= 1
            for e in r.sections_elided:
                assert e.id
                assert e.heading
                assert 0.0 <= e.confidence <= 1.0

    def test_v2_confidence_1_when_nothing_elided(self):
        with tempfile.TemporaryDirectory() as td:
            # small file → breakeven gate → full passthrough
            p = self._write(td, "## 狀態\n\nv1.0\n\n## next_step\n\nDo X")
            r = read_handoff_fields_v2(p, max_tokens=5000)
            assert r.confidence == 1.0
            assert r.sections_elided == []
            assert r.expand_hint == ""

    def test_v2_expand_hint_nonempty_when_elided(self):
        with tempfile.TemporaryDirectory() as td:
            padding = "x" * 12000
            p = self._write(td, (
                f"## 狀態\n\nv1.0\n\n## 歷史\n\n{padding}\n\n"
                f"## Random\n\nnoise"
            ))
            r = read_handoff_fields_v2(p, max_tokens=80)
            if r.sections_elided:
                assert r.expand_hint != ""
                assert "expand" in r.expand_hint

    def test_v2_section_ids_stable_across_reads(self):
        with tempfile.TemporaryDirectory() as td:
            padding = "x" * 12000
            p = self._write(td, (
                f"## 狀態\n\nv1.0\n\n## next_step\n\nDo X\n\n"
                f"## 歷史\n\n{padding}"
            ))
            r1 = read_handoff_fields_v2(p, max_tokens=100)
            r2 = read_handoff_fields_v2(p, max_tokens=100)
            ids1 = sorted([e.id for e in r1.sections_elided])
            ids2 = sorted([e.id for e in r2.sections_elided])
            assert ids1 == ids2

    def test_v2_pending_section_low_confidence(self):
        with tempfile.TemporaryDirectory() as td:
            padding = "x" * 12000
            p = self._write(td, (
                f"## 狀態\n\nv1.0\n\n## 未解決\n\n⬜ pending task\n\n"
                f"## 歷史\n\n{padding}"
            ))
            r = read_handoff_fields_v2(p, max_tokens=100)
            # 未解決 should either be kept OR elided with low confidence
            for e in r.sections_elided:
                if "未解決" in e.heading:
                    assert e.confidence < 0.5

    def test_v2_kept_plus_elided_covers_all_sections(self):
        with tempfile.TemporaryDirectory() as td:
            padding = "x" * 12000
            sections = ["狀態", "next_step", "歷史", "Random", "Old"]
            body = "\n\n".join(f"## {s}\n\nbody {s}" for s in sections)
            body += f"\n\n## Padding\n\n{padding}"
            p = self._write(td, body)
            r = read_handoff_fields_v2(p, max_tokens=100)
            total = len(r.sections_kept) + len(r.sections_elided)
            # All 6 ## sections must appear in kept or elided
            assert total >= 6

    def test_v2_empty_source(self):
        r = read_handoff_fields_v2("/no/such/file.md", max_tokens=100)
        assert r.content == ""
        assert r.sections_elided == []
        assert r.confidence == 1.0
        assert r.expand_hint == ""


class TestLegacyAPIBackwardCompat:
    def test_legacy_returns_str(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, encoding="utf-8",
        ) as f:
            f.write("## 狀態\n\nv1.0\n\n## next_step\n\nDo X")
            f.flush()
            result = read_handoff_fields(f.name, max_tokens=5000)
        os.unlink(f.name)
        assert isinstance(result, str)

    def test_legacy_equals_v2_content(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, encoding="utf-8",
        ) as f:
            padding = "x" * 12000
            f.write(
                f"## 狀態\n\nv1.0\n\n## next_step\n\nDo X\n\n"
                f"## 歷史\n\n{padding}",
            )
            f.flush()
            v1 = read_handoff_fields(f.name, max_tokens=200)
            v2 = read_handoff_fields_v2(f.name, max_tokens=200)
        os.unlink(f.name)
        # Bytewise equality between v1 str and v2.content
        assert v1 == v2.content

    def test_legacy_memory_returns_str(self):
        with tempfile.TemporaryDirectory() as td:
            with open(
                os.path.join(td, "MEMORY.md"), "w", encoding="utf-8",
            ) as f:
                f.write("- [RAG](rag.md) — RAG pipeline\n")
            result = read_memory_fields(td, ["rag"])
        assert isinstance(result, str)

    def test_legacy_build_context_returns_str(self):
        with tempfile.TemporaryDirectory() as td:
            result = build_field_context(td, "x")
        assert isinstance(result, str)


class TestExpand:
    def test_expand_retrieves_full_section(self):
        with tempfile.TemporaryDirectory() as td:
            padding = "x" * 12000
            p = os.path.join(td, "交接.md")
            full_body = f"detailed history contents {padding}"
            with open(p, "w", encoding="utf-8") as f:
                f.write(
                    f"## 狀態\n\nv1.0\n\n## 歷史\n\n{full_body}\n\n"
                    f"## next_step\n\nDo X",
                )
            r = read_handoff_fields_v2(p, max_tokens=50)
            history_elided = [
                e for e in r.sections_elided if "歷史" in e.heading
            ]
            if history_elided:
                full = expand(p, history_elided[0].id)
                assert "detailed history" in full
                assert full.startswith("## 歷史")

    def test_expand_missing_id(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "x.md")
            with open(p, "w", encoding="utf-8") as f:
                f.write("## A\n\nbody")
            assert expand(p, "nonexistent-id") == ""

    def test_expand_missing_file(self):
        assert expand("/no/such/file.md", "any") == ""

    def test_expand_empty_args(self):
        assert expand("", "id") == ""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, encoding="utf-8",
        ) as f:
            f.write("## X\n\ny")
            f.flush()
            assert expand(f.name, "") == ""
        os.unlink(f.name)

    def test_expand_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "x.md")
            with open(p, "w", encoding="utf-8") as f:
                f.write("## Section A\n\nbody content")
            out1 = expand(p, "section-a")
            out2 = expand(p, "section-a")
            assert out1 == out2
            assert "body content" in out1


class TestMemoryV2:
    def test_memory_no_keywords_emits_breadcrumbs(self):
        with tempfile.TemporaryDirectory() as td:
            with open(
                os.path.join(td, "MEMORY.md"), "w", encoding="utf-8",
            ) as f:
                f.write("- [RAG](rag.md) — RAG pipeline\n")
                f.write("- [Deploy](deploy.md) — VPS deploy\n")
            r = read_memory_fields_v2(td, [])
            # No keywords → no content, but breadcrumbs emitted
            assert r.content == ""
            assert len(r.sections_elided) >= 1

    def test_memory_v2_returns_result(self):
        with tempfile.TemporaryDirectory() as td:
            with open(
                os.path.join(td, "MEMORY.md"), "w", encoding="utf-8",
            ) as f:
                f.write("- [RAG](rag.md) — RAG pipeline architecture\n")
            with open(os.path.join(td, "rag.md"), "w", encoding="utf-8") as f:
                f.write("RAG details here")
            r = read_memory_fields_v2(td, ["rag"], max_tokens=500)
            assert isinstance(r, FieldReadResult)
            assert "RAG" in r.content


class TestBuildFieldContextV2:
    def test_v2_returns_result(self):
        with tempfile.TemporaryDirectory() as td:
            r = build_field_context_v2(td, "x")
            assert isinstance(r, FieldReadResult)

    def test_v2_empty_workspace(self):
        r = build_field_context_v2("", "x")
        assert r.content == ""
        assert r.sections_elided == []

    def test_v2_prefixes_section_ids(self):
        with tempfile.TemporaryDirectory() as td:
            hdir = os.path.join(td, "handoffs")
            os.makedirs(hdir)
            padding = "x" * 12000
            with open(
                os.path.join(hdir, "交接_T.md"), "w", encoding="utf-8",
            ) as f:
                f.write(
                    f"## 狀態\n\nv1\n\n## next_step\n\nY\n\n"
                    f"## 歷史\n\n{padding}",
                )
            r = build_field_context_v2(td, "status")
            # Elided ids should be prefixed with `handoff:<basename>:`
            for e in r.sections_elided:
                assert e.id.startswith("handoff:")


class TestZIQBreakevenGate:
    def test_breakeven_constant(self):
        assert COMPRESS_BREAKEVEN_TOKENS == 2500

    def test_small_file_no_compression(self):
        """Content under 2500t should pass through without section filtering."""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, encoding="utf-8",
        ) as f:
            content = "## 狀態\n\nv1.0 live\n\n## History\n\nSome old stuff"
            f.write(content)
            f.flush()
            result = read_handoff_fields(f.name, max_tokens=5000)
        os.unlink(f.name)
        # Full content should be present (no filtering)
        assert "History" in result
        assert "old stuff" in result

    def test_large_file_compressed(self):
        """Content over 2500t should be selectively compressed."""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, encoding="utf-8",
        ) as f:
            # Create content well over 2500 tokens
            big_history = "x" * 15000  # ~3750 tokens
            content = (
                f"## 狀態\n\nv2.0\n\n## next_step\n\nBuild Y\n\n"
                f"## 歷史\n\n{big_history}\n\n## Random\n\nIrrelevant"
            )
            f.write(content)
            f.flush()
            result = read_handoff_fields(f.name, max_tokens=200)
        os.unlink(f.name)
        # Should include high-priority sections
        assert "狀態" in result or "next_step" in result
        # Should NOT include low-priority unmatched sections fully
        tokens = _estimate_tokens(result)
        assert tokens <= 250  # Budget + tolerance
