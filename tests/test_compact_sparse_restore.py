"""Tests for post-compact sparse restoration primitives.

Covers :meth:`Microcompactor.collect_top_files`,
:meth:`Microcompactor.collect_top_skills`, and
:meth:`Microcompactor.build_sparse_restore_block`. Ported behaviour
from ``compact.ts:122-131``.
"""

from __future__ import annotations

import pytest

from concinno.cache.microcompact import (
    POST_COMPACT_MAX_FILES_TO_RESTORE,
    POST_COMPACT_MAX_TOKENS_PER_FILE,
    POST_COMPACT_MAX_TOKENS_PER_SKILL,
    POST_COMPACT_SKILLS_TOKEN_BUDGET,
    SPARSE_TRUNCATION_MARKER,
    FileAccessRecord,
    FileSparseEntry,
    Microcompactor,
    SkillEntry,
    SkillSparseEntry,
    SparseRestoreConfig,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mc() -> Microcompactor:
    return Microcompactor(cache_dir=None, session_id="sparse-test")


def _file(
    path: str,
    *,
    content: str = "hello world",
    ts: float = 100.0,
    reads: int = 1,
    edits: int = 0,
    tokens: int | None = None,
) -> FileAccessRecord:
    return FileAccessRecord(
        path=path,
        last_access_ts=ts,
        access_count=reads,
        edit_count=edits,
        content=content,
        tokens=tokens,
    )


def _skill(
    name: str,
    *,
    body: str = "skill body",
    invocations: int = 1,
    ts: float = 100.0,
    tokens: int | None = None,
) -> SkillEntry:
    return SkillEntry(
        name=name,
        body=body,
        invocation_count=invocations,
        last_invocation_ts=ts,
        tokens=tokens,
    )


# ---------------------------------------------------------------------------
# Config defaults match CC constants
# ---------------------------------------------------------------------------


def test_sparse_restore_config_defaults_match_cc_parity() -> None:
    cfg = SparseRestoreConfig()
    assert cfg.max_files == POST_COMPACT_MAX_FILES_TO_RESTORE == 5
    assert cfg.tokens_per_file == POST_COMPACT_MAX_TOKENS_PER_FILE == 5_000
    assert cfg.tokens_per_skill == POST_COMPACT_MAX_TOKENS_PER_SKILL == 5_000
    assert cfg.total_skills_budget == POST_COMPACT_SKILLS_TOKEN_BUDGET == 25_000
    assert cfg.max_skills == 5  # derived: 25K budget / 5K per skill


# ---------------------------------------------------------------------------
# collect_top_files
# ---------------------------------------------------------------------------


def test_collect_top_files_empty_input() -> None:
    mc = _make_mc()
    assert mc.collect_top_files({}) == []


def test_collect_top_files_respects_max_files_cap() -> None:
    mc = _make_mc()
    history = {f"f{i}": _file(f"f{i}", ts=float(i)) for i in range(10)}
    out = mc.collect_top_files(history, SparseRestoreConfig(max_files=3))
    assert len(out) == 3
    # Newest (highest ts) should win
    assert [e.name for e in out] == ["f9", "f8", "f7"]


def test_collect_top_files_score_weights_edits_highest() -> None:
    mc = _make_mc()
    history = {
        "recent_only": _file("recent_only", ts=1000.0, reads=0, edits=0),
        "read_heavy": _file("read_heavy", ts=1000.0, reads=100, edits=0),
        "edit_heavy": _file("edit_heavy", ts=1000.0, reads=0, edits=10),
    }
    out = mc.collect_top_files(history, now=1000.0)
    names = [e.name for e in out]
    # edit_heavy (10*2=20) > read_heavy (100*0.5=50) → actually read_heavy wins
    # Check the actual weights: score is recency + 0.5*reads + 2*edits
    # read_heavy: 1 + 50 + 0 = 51
    # edit_heavy: 1 + 0 + 20 = 21
    # recent_only: 1 + 0 + 0 = 1
    assert names[0] == "read_heavy"
    assert names[1] == "edit_heavy"
    assert names[2] == "recent_only"
    # All scores positive and descending
    assert out[0].score > out[1].score > out[2].score


def test_collect_top_files_edit_dominates_on_equal_reads() -> None:
    mc = _make_mc()
    history = {
        "a": _file("a", ts=1000.0, reads=1, edits=0),
        "b": _file("b", ts=1000.0, reads=1, edits=1),
    }
    out = mc.collect_top_files(history, now=1000.0)
    assert out[0].name == "b"  # edit weight 2 > read weight 0.5


def test_collect_top_files_truncates_oversized_file() -> None:
    mc = _make_mc()
    # Force a giant file well over the default 5K tokens/file (20K chars).
    big = "X" * 100_000  # ~25_000 est tokens
    history = {"big": _file("big", content=big, ts=1.0)}
    out = mc.collect_top_files(history)
    assert len(out) == 1
    entry = out[0]
    assert entry.tokens <= POST_COMPACT_MAX_TOKENS_PER_FILE
    assert SPARSE_TRUNCATION_MARKER in entry.content


def test_collect_top_files_unchanged_when_under_cap() -> None:
    mc = _make_mc()
    small = "small body"
    history = {"s": _file("s", content=small, ts=1.0)}
    out = mc.collect_top_files(history)
    assert out[0].content == small
    assert SPARSE_TRUNCATION_MARKER not in out[0].content


def test_collect_top_files_uses_content_loader_when_record_has_no_content() -> None:
    mc = _make_mc()
    history = {
        "lazy": FileAccessRecord(
            path="lazy", last_access_ts=1.0, access_count=1, edit_count=0
        )
    }
    loads: list[str] = []

    def loader(path: str) -> str:
        loads.append(path)
        return "loaded body"

    out = mc.collect_top_files(history, content_loader=loader)
    assert loads == ["lazy"]
    assert out[0].content == "loaded body"


def test_collect_top_files_skips_records_without_content_or_loader() -> None:
    mc = _make_mc()
    history = {
        "no_content": FileAccessRecord(
            path="no_content", last_access_ts=1.0, access_count=1
        )
    }
    out = mc.collect_top_files(history)
    assert out == []


def test_collect_top_files_loader_failure_skips_entry() -> None:
    mc = _make_mc()
    history = {
        "broken": FileAccessRecord(
            path="broken", last_access_ts=1.0, access_count=1
        )
    }

    def loader(_: str) -> str:
        raise RuntimeError("loader exploded")

    out = mc.collect_top_files(history, content_loader=loader)
    assert out == []


def test_collect_top_files_respects_total_budget() -> None:
    mc = _make_mc()
    history = {
        f"f{i}": _file(f"f{i}", content="Y" * 20_000, ts=float(i))
        for i in range(5)
    }
    cfg = SparseRestoreConfig(max_files=5, total_files_budget=10_000)
    out = mc.collect_top_files(history, cfg)
    total_tokens = sum(e.tokens for e in out)
    assert total_tokens <= 10_000


def test_collect_top_files_max_files_zero_returns_empty() -> None:
    mc = _make_mc()
    history = {"a": _file("a", ts=1.0)}
    out = mc.collect_top_files(history, SparseRestoreConfig(max_files=0))
    assert out == []


# ---------------------------------------------------------------------------
# collect_top_skills
# ---------------------------------------------------------------------------


def test_collect_top_skills_empty_input() -> None:
    mc = _make_mc()
    assert mc.collect_top_skills({}) == []


def test_collect_top_skills_ranks_by_invocation_count() -> None:
    mc = _make_mc()
    registry = {
        "low": _skill("low", invocations=1, ts=1000.0),
        "high": _skill("high", invocations=10, ts=1000.0),
        "mid": _skill("mid", invocations=5, ts=1000.0),
    }
    out = mc.collect_top_skills(registry, now=1000.0)
    assert [e.name for e in out] == ["high", "mid", "low"]


def test_collect_top_skills_recency_breaks_ties_in_invocations() -> None:
    mc = _make_mc()
    registry = {
        "old": _skill("old", invocations=5, ts=0.0),
        "new": _skill("new", invocations=5, ts=1000.0),
    }
    out = mc.collect_top_skills(registry, now=1000.0)
    assert out[0].name == "new"


def test_collect_top_skills_respects_total_budget() -> None:
    mc = _make_mc()
    # 10 skills, each 6K tokens after estimate → 60K total, budget 25K.
    registry = {
        f"s{i}": _skill(f"s{i}", body="Z" * 24_000, invocations=100 - i, ts=1.0)
        for i in range(10)
    }
    out = mc.collect_top_skills(registry)
    total = sum(e.tokens for e in out)
    assert total <= POST_COMPACT_SKILLS_TOKEN_BUDGET


def test_collect_top_skills_truncates_instead_of_dropping() -> None:
    mc = _make_mc()
    # One skill way over per-skill cap.
    giant_body = "W" * 80_000  # ~20_000 est tokens > 5_000 cap
    registry = {"giant": _skill("giant", body=giant_body, invocations=1, ts=1.0)}
    out = mc.collect_top_skills(registry)
    assert len(out) == 1, "oversize skill must be truncated, not dropped"
    assert out[0].tokens <= POST_COMPACT_MAX_TOKENS_PER_SKILL
    assert SPARSE_TRUNCATION_MARKER in out[0].content


def test_collect_top_skills_max_skills_cap() -> None:
    mc = _make_mc()
    registry = {f"s{i}": _skill(f"s{i}", invocations=i, ts=1.0) for i in range(10)}
    out = mc.collect_top_skills(registry, SparseRestoreConfig(max_skills=3))
    assert len(out) == 3
    assert [e.name for e in out] == ["s9", "s8", "s7"]


# ---------------------------------------------------------------------------
# build_sparse_restore_block
# ---------------------------------------------------------------------------


def test_build_sparse_restore_block_empty_both_sides() -> None:
    mc = _make_mc()
    assert mc.build_sparse_restore_block([], []) == ""


def test_build_sparse_restore_block_contains_header_and_file_names() -> None:
    mc = _make_mc()
    files = [
        FileSparseEntry(name="src/a.py", content="print('a')", tokens=3, score=1.0),
        FileSparseEntry(name="src/b.py", content="print('b')", tokens=3, score=0.5),
    ]
    out = mc.build_sparse_restore_block(files, [])
    assert "Post-compact sparse restoration" in out
    assert "## File: src/a.py" in out
    assert "## File: src/b.py" in out
    assert "print('a')" in out
    assert "print('b')" in out


def test_build_sparse_restore_block_contains_skill_names() -> None:
    mc = _make_mc()
    skills = [
        SkillSparseEntry(
            name="kb_handoff", content="handoff body", tokens=3, score=5.0
        )
    ]
    out = mc.build_sparse_restore_block([], skills)
    assert "## Skill: kb_handoff" in out
    assert "handoff body" in out


def test_build_sparse_restore_block_both_sections_ordered() -> None:
    mc = _make_mc()
    files = [FileSparseEntry(name="a", content="A", tokens=1, score=1.0)]
    skills = [SkillSparseEntry(name="s", content="S", tokens=1, score=1.0)]
    out = mc.build_sparse_restore_block(files, skills)
    files_idx = out.index("# Restored files")
    skills_idx = out.index("# Restored skills")
    assert files_idx < skills_idx


def test_build_sparse_restore_block_is_deterministic() -> None:
    mc = _make_mc()
    files = [FileSparseEntry(name="a", content="A", tokens=1, score=1.0)]
    assert mc.build_sparse_restore_block(files, []) == mc.build_sparse_restore_block(
        files, []
    )


# ---------------------------------------------------------------------------
# Truncation helper edge cases
# ---------------------------------------------------------------------------


def test_truncation_marker_appears_at_seam() -> None:
    mc = _make_mc()
    body = "A" * 50_000 + "END_TAIL_MARK"
    history = {"big": _file("big", content=body, ts=1.0)}
    out = mc.collect_top_files(history)
    # Tail portion should still be preserved after truncation.
    assert "END_TAIL_MARK" in out[0].content
    assert SPARSE_TRUNCATION_MARKER in out[0].content


def test_truncation_degenerate_cap_emits_marker_only() -> None:
    mc = _make_mc()
    cfg = SparseRestoreConfig(tokens_per_file=1)  # 4 chars, smaller than marker
    history = {"big": _file("big", content="X" * 1000, ts=1.0)}
    out = mc.collect_top_files(history, cfg)
    assert len(out) == 1
    assert SPARSE_TRUNCATION_MARKER.strip() in out[0].content


def test_estimate_tokens_respects_override() -> None:
    # Access via classmethod for direct unit coverage.
    mc = _make_mc()
    assert mc._estimate_tokens("hello", override=42) == 42
    assert mc._estimate_tokens("", None) == 0
    assert mc._estimate_tokens("12345678", None) == 2  # len 8 // 4


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
