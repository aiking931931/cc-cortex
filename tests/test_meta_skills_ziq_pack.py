"""tests.test_meta_skills_ziq_pack — ZIQRoutedSkillPack unit tests.

Verifies:
  - Top-k selection ranks by literal/semantic match of query terms
  - update_outcome persists to JSON + reranking shifts after feedback
  - Empty / nonsense queries still return k results deterministically
  - Stats file survives corruption (resets to fresh)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from concinno.meta_skills.ziq_pack import ZIQRoutedSkillPack


class _StubTool:
    is_concurrency_safe = True

    def __init__(self, name: str, description: str) -> None:
        self.name = name
        self.description = description

    def call(self, **kwargs: Any) -> str:  # noqa: ARG002
        return self.name


def _pool() -> list[_StubTool]:
    return [
        _StubTool("PdfRead", "extract text from local PDF files via pypdf"),
        _StubTool("HtmlToText", "convert html to clean markdown via trafilatura"),
        _StubTool(
            "DuckDbQuery",
            "run analytical SQL over local csv parquet json via duckdb",
        ),
        _StubTool("RssFetch", "fetch and parse rss atom feed via feedparser"),
        _StubTool(
            "Shell", "execute bash shell commands with guards and timeout"
        ),
        _StubTool("FileRead", "read a local file into memory"),
        _StubTool("FileWrite", "write string content to a local file"),
        _StubTool(
            "FileGrep", "search file contents with a regex pattern"
        ),
        _StubTool("FileGlob", "list files matching a glob pattern"),
        _StubTool(
            "WebSearch", "search the web and return top urls with snippets"
        ),
    ]


@pytest.fixture
def stats_file(tmp_path: Path) -> Path:
    return tmp_path / "ziq_stats.json"


def test_select_top_k_matches_by_query_text(stats_file: Path) -> None:
    pack = ZIQRoutedSkillPack(_pool(), k=3, stats_path=stats_file)
    names = [t.name for t in pack.select_top_k("read pdf file and extract text")]
    # PdfRead's description contains all three stems; it must rank in top-3.
    assert "PdfRead" in names


def test_sql_query_routes_to_duckdb(stats_file: Path) -> None:
    pack = ZIQRoutedSkillPack(_pool(), k=2, stats_path=stats_file)
    names = [t.name for t in pack.select_top_k("run sql analytical query over csv")]
    assert "DuckDbQuery" in names


def test_html_query_routes_to_htmltotext(stats_file: Path) -> None:
    pack = ZIQRoutedSkillPack(_pool(), k=3, stats_path=stats_file)
    names = [t.name for t in pack.select_top_k("convert html markup to plain markdown")]
    assert "HtmlToText" in names


def test_outcome_persists_and_rerankes(stats_file: Path) -> None:
    # With FTRL favoring Shell (high success) vs FileRead (low),
    # we expect Shell to outrank FileRead on an ambiguous query.
    pack = ZIQRoutedSkillPack(
        _pool(), k=10, stats_path=stats_file, alpha=0.1, beta=5.0, gamma=0.0,
    )
    query = "file operations"
    before = pack.debug_scores(query)

    # Hammer Shell with successes; FileRead with failures.
    for _ in range(200):
        pack.update_outcome("Shell", success=True, latency_ms=50.0)
        pack.update_outcome("FileRead", success=False, latency_ms=5000.0)

    after = pack.debug_scores(query)
    assert after["Shell"] > before["Shell"]
    assert after["FileRead"] < before["FileRead"]
    assert after["Shell"] > after["FileRead"]


def test_stats_file_is_written_on_update(stats_file: Path) -> None:
    pack = ZIQRoutedSkillPack(_pool(), k=3, stats_path=stats_file)
    assert not stats_file.exists()
    pack.update_outcome("Shell", success=True, latency_ms=100.0)
    assert stats_file.exists()
    import json
    data = json.loads(stats_file.read_text("utf-8"))
    assert "tools" in data
    assert "Shell" in data["tools"]
    assert data["tools"]["Shell"]["n"] == 1


def test_corrupt_stats_resets_gracefully(stats_file: Path) -> None:
    stats_file.write_text("{not valid json", encoding="utf-8")
    pack = ZIQRoutedSkillPack(_pool(), k=3, stats_path=stats_file)
    # Should not raise. Scores should be computable.
    scores = pack.debug_scores("pdf file")
    assert len(scores) == len(_pool())


def test_empty_query_returns_k_results(stats_file: Path) -> None:
    pack = ZIQRoutedSkillPack(_pool(), k=4, stats_path=stats_file)
    results = pack.select_top_k("")
    assert len(results) == 4


def test_unknown_tool_outcome_is_dropped(stats_file: Path) -> None:
    pack = ZIQRoutedSkillPack(_pool(), k=3, stats_path=stats_file)
    # Silently dropped — must not raise, must not create a phantom entry.
    pack.update_outcome("NoSuchTool", success=True, latency_ms=10.0)
    if stats_file.exists():
        import json
        data = json.loads(stats_file.read_text("utf-8"))
        assert "NoSuchTool" not in data.get("tools", {})


def test_duplicate_names_rejected() -> None:
    pool = [_StubTool("a", "x"), _StubTool("a", "y")]
    with pytest.raises(ValueError, match="duplicate tool names"):
        ZIQRoutedSkillPack(pool, k=1)


def test_k_must_be_positive() -> None:
    with pytest.raises(ValueError, match=">= 1"):
        ZIQRoutedSkillPack([_StubTool("a", "x")], k=0)


def test_empty_pool_rejected() -> None:
    with pytest.raises(ValueError, match="at least one tool"):
        ZIQRoutedSkillPack([], k=1)
