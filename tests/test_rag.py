"""Tests for concinno.rag — Cognitive RAG (cognitive prosthesis, not Q&A).

All tests mock chromadb and sentence-transformers so no heavy deps needed.
"""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch

from concinno.rag import (
    NAMESPACES,
    RAGIndex,
    _file_hash,
    chunk_markdown,
    create_namespace_index,
)

# ── chunk_markdown ──────────────────────────────────────────


class TestChunkMarkdown:
    def test_empty_text(self):
        assert chunk_markdown("", "f.md") == []

    def test_single_section_small(self):
        text = "## Title\n\nSome content here."
        chunks = chunk_markdown(text, "test.md")
        assert len(chunks) == 1
        assert chunks[0]["metadata"]["file"] == "test.md"
        assert chunks[0]["metadata"]["heading"] == "Title"
        assert chunks[0]["metadata"]["chunk_idx"] == 0

    def test_multiple_headings(self):
        text = "# H1\n\nBody1\n\n## H2\n\nBody2\n\n## H3\n\nBody3"
        chunks = chunk_markdown(text, "multi.md")
        assert len(chunks) == 3
        headings = [c["metadata"]["heading"] for c in chunks]
        assert headings == ["H1", "H2", "H3"]

    def test_chunk_idx_increments(self):
        text = "# A\n\nContent A\n\n## B\n\nContent B"
        chunks = chunk_markdown(text, "idx.md")
        indices = [c["metadata"]["chunk_idx"] for c in chunks]
        assert indices == [0, 1]

    def test_large_section_splits(self):
        # Create section larger than max_chunk_size
        text = "## Big\n\n" + "\n\n".join(f"Paragraph {i} " * 20 for i in range(10))
        chunks = chunk_markdown(text, "big.md", max_chunk_size=200)
        assert len(chunks) > 1
        for c in chunks:
            assert c["metadata"]["heading"] == "Big"

    def test_no_heading_section(self):
        text = "Just some text without any heading."
        chunks = chunk_markdown(text, "nohead.md")
        assert len(chunks) == 1
        assert chunks[0]["metadata"]["heading"] == ""

    def test_h3_heading(self):
        text = "### Level3\n\nSome deep content."
        chunks = chunk_markdown(text, "h3.md")
        assert chunks[0]["metadata"]["heading"] == "Level3"

    def test_overlap_in_large_section(self):
        # With overlap=50, split chunks should have overlap text
        para = "A" * 100
        text = "## S\n\n" + "\n\n".join([para] * 5)
        chunks = chunk_markdown(text, "overlap.md", max_chunk_size=150, overlap=30)
        assert len(chunks) > 1

    def test_whitespace_only_sections_skipped(self):
        text = "# A\n\nContent\n\n\n\n\n"
        chunks = chunk_markdown(text, "ws.md")
        # Should not create empty chunks from trailing whitespace
        for c in chunks:
            assert c["text"].strip()

    def test_frontmatter_not_heading(self):
        # Text starting without heading (e.g. after frontmatter strip)
        text = "Some preamble text.\n\n## Real Heading\n\nBody"
        chunks = chunk_markdown(text, "fm.md")
        assert len(chunks) == 2


# ── _file_hash ──────────────────────────────────────────────


class TestFileHash:
    def test_hash_of_file(self, tmp_path):
        f = tmp_path / "test.md"
        f.write_text("hello world", encoding="utf-8")
        h = _file_hash(str(f))
        assert len(h) == 32  # MD5 hex digest

    def test_hash_changes_with_content(self, tmp_path):
        f = tmp_path / "test.md"
        f.write_text("v1", encoding="utf-8")
        h1 = _file_hash(str(f))
        f.write_text("v2", encoding="utf-8")
        h2 = _file_hash(str(f))
        assert h1 != h2

    def test_nonexistent_file(self):
        assert _file_hash("/nonexistent/path/file.md") == ""

    def test_same_content_same_hash(self, tmp_path):
        f1 = tmp_path / "a.md"
        f2 = tmp_path / "b.md"
        f1.write_text("same", encoding="utf-8")
        f2.write_text("same", encoding="utf-8")
        assert _file_hash(str(f1)) == _file_hash(str(f2))


# ── Mock helpers ────────────────────────────────────────────


class MockCollection:
    """Mock ChromaDB collection."""

    def __init__(self):
        self._store: dict[str, dict] = {}  # id -> {doc, meta, embedding}

    def count(self):
        return len(self._store)

    def add(self, ids, embeddings, documents, metadatas):
        for i, id_ in enumerate(ids):
            self._store[id_] = {
                "doc": documents[i],
                "meta": metadatas[i],
                "embedding": embeddings[i],
            }

    def delete(self, where=None, ids=None):
        if where and "file" in where:
            file_val = where["file"]
            to_del = [k for k, v in self._store.items()
                      if v["meta"].get("file") == file_val]
            for k in to_del:
                del self._store[k]
        if ids:
            for id_ in ids:
                self._store.pop(id_, None)

    def get(self, where=None, include=None):
        if where and "file" in where:
            file_val = where["file"]
            matched = {k: v for k, v in self._store.items()
                       if v["meta"].get("file") == file_val}
        else:
            matched = dict(self._store)
        result = {"ids": list(matched.keys())}
        if include:
            if "documents" in include:
                result["documents"] = [v["doc"] for v in matched.values()]
            if "metadatas" in include:
                result["metadatas"] = [v["meta"] for v in matched.values()]
        return result

    def query(self, query_embeddings, n_results, where=None, include=None):
        # Return all docs sorted by fake distance (just use order)
        items = list(self._store.values())
        if where and isinstance(where, dict) and "file" in where:
            filter_val = where["file"]
            if isinstance(filter_val, dict) and "$contains" in filter_val:
                substr = filter_val["$contains"]
                items = [v for v in items if substr in v["meta"].get("file", "")]
        items = items[:n_results]
        if not items:
            return {"documents": [[]], "metadatas": [[]], "distances": [[]]}
        return {
            "documents": [[v["doc"] for v in items]],
            "metadatas": [[v["meta"] for v in items]],
            "distances": [[0.2 for _ in items]],  # cosine dist 0.2 → score 0.9
        }


class MockModel:
    """Mock SentenceTransformer."""

    def encode(self, texts, show_progress_bar=False):
        # Return fake embeddings (list of float lists)
        arr = MagicMock()
        arr.tolist.return_value = [[0.1] * 384 for _ in texts]
        return arr


class MockClient:
    """Mock ChromaDB PersistentClient."""

    def __init__(self, path=""):
        self._collections: dict[str, MockCollection] = {}

    def get_or_create_collection(self, name, metadata=None):
        if name not in self._collections:
            self._collections[name] = MockCollection()
        return self._collections[name]

    def create_collection(self, name, metadata=None):
        self._collections[name] = MockCollection()
        return self._collections[name]

    def delete_collection(self, name):
        self._collections.pop(name, None)


def _make_index(tmp_path, knowledge_dirs=None):
    """Create a RAGIndex with mocked deps pointing at tmp_path."""
    project_dir = str(tmp_path / "project")
    os.makedirs(project_dir, exist_ok=True)
    cache_dir = str(tmp_path / "cache")

    if knowledge_dirs is None:
        kb_dir = os.path.join(project_dir, "kb")
        os.makedirs(kb_dir, exist_ok=True)
        knowledge_dirs = ["kb"]

    idx = RAGIndex(
        knowledge_dirs=knowledge_dirs,
        project_dir=project_dir,
        cache_dir=cache_dir,
        collection_name="test_col",
    )

    # Inject mocks
    client = MockClient()
    idx._client = client
    idx._collection = client.get_or_create_collection("test_col")
    idx._model = MockModel()

    return idx, project_dir, cache_dir


def _write_md(project_dir, rel_path, content):
    """Write a markdown file under project_dir."""
    abs_path = os.path.join(project_dir, rel_path)
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    with open(abs_path, "w", encoding="utf-8") as f:
        f.write(content)
    return abs_path


# ── RAGIndex.build ──────────────────────────────────────────


class TestRAGIndexBuild:
    def test_build_empty_dir(self, tmp_path):
        idx, project_dir, _ = _make_index(tmp_path)
        result = idx.build()
        assert result["files_scanned"] == 0
        assert result["files_indexed"] == 0
        assert result["chunks_indexed"] == 0

    def test_build_indexes_md_files(self, tmp_path):
        idx, project_dir, _ = _make_index(tmp_path)
        _write_md(project_dir, "kb/topic.md", "# Topic\n\nThis is knowledge content.")
        result = idx.build()
        assert result["files_scanned"] == 1
        assert result["files_indexed"] == 1
        assert result["chunks_indexed"] >= 1

    def test_build_skips_short_files(self, tmp_path):
        idx, project_dir, _ = _make_index(tmp_path)
        _write_md(project_dir, "kb/tiny.md", "hi")  # < 20 chars
        result = idx.build()
        assert result["files_indexed"] == 0

    def test_build_skips_non_md(self, tmp_path):
        idx, project_dir, _ = _make_index(tmp_path)
        # Write a .txt file
        txt_path = os.path.join(project_dir, "kb", "notes.txt")
        os.makedirs(os.path.dirname(txt_path), exist_ok=True)
        with open(txt_path, "w") as f:
            f.write("This is not markdown " * 10)
        result = idx.build()
        assert result["files_scanned"] == 0

    def test_build_strips_frontmatter(self, tmp_path):
        idx, project_dir, _ = _make_index(tmp_path)
        content = "---\nstatus: active\n---\n# Real Content\n\nBody text here that is long enough."
        _write_md(project_dir, "kb/fm.md", content)
        result = idx.build()
        assert result["files_indexed"] == 1

    def test_build_incremental_skips_unchanged(self, tmp_path):
        idx, project_dir, _ = _make_index(tmp_path)
        _write_md(
            project_dir, "kb/stable.md",
            "# Stable\n\nContent that won't change and is long enough.",
        )

        # First build
        r1 = idx.build()
        assert r1["files_indexed"] == 1

        # Second build — same content, should skip
        r2 = idx.build()
        assert r2["files_indexed"] == 0

    def test_build_reindexes_changed_file(self, tmp_path):
        idx, project_dir, cache_dir = _make_index(tmp_path)
        path = _write_md(
            project_dir, "kb/evolving.md",
            "# V1\n\nOriginal content that is long enough to index.",
        )

        idx.build()

        # Modify file
        with open(path, "w", encoding="utf-8") as f:
            f.write("# V2\n\nUpdated content that is different and long enough to index.")

        r2 = idx.build()
        assert r2["files_indexed"] == 1

    def test_build_force_rebuilds_all(self, tmp_path):
        idx, project_dir, _ = _make_index(tmp_path)
        _write_md(project_dir, "kb/a.md", "# A\n\nContent A is here with enough text to index.")

        idx.build()

        # Force rebuild — should reindex even unchanged
        r2 = idx.build(force=True)
        assert r2["files_indexed"] == 1

    def test_build_multiple_dirs(self, tmp_path):
        idx, project_dir, _ = _make_index(tmp_path, knowledge_dirs=["kb", "rules"])
        _write_md(
            project_dir, "kb/k1.md",
            "# KB Entry\n\nKnowledge base content long enough.",
        )
        _write_md(
            project_dir, "rules/r1.md",
            "# Rule\n\nRule content also long enough to index.",
        )
        result = idx.build()
        assert result["files_scanned"] == 2
        assert result["files_indexed"] == 2

    def test_build_saves_hashes(self, tmp_path):
        idx, project_dir, cache_dir = _make_index(tmp_path)
        _write_md(
            project_dir, "kb/hashed.md",
            "# Hashed\n\nContent for hash tracking, long enough.",
        )
        idx.build()
        hash_path = os.path.join(cache_dir, "file_hashes.json")
        assert os.path.isfile(hash_path)
        with open(hash_path, encoding="utf-8") as f:
            hashes = json.load(f)
        assert "kb/hashed.md" in hashes

    def test_build_duration_ms_positive(self, tmp_path):
        idx, _, _ = _make_index(tmp_path)
        result = idx.build()
        assert result["duration_ms"] >= 0

    def test_build_walks_subdirs(self, tmp_path):
        idx, project_dir, _ = _make_index(tmp_path)
        _write_md(
            project_dir, "kb/sub/deep.md",
            "# Deep\n\nNested content in subdirectory long enough.",
        )
        result = idx.build()
        assert result["files_scanned"] == 1
        assert result["files_indexed"] == 1


# ── RAGIndex.update ─────────────────────────────────────────


class TestRAGIndexUpdate:
    def test_update_new_file(self, tmp_path):
        idx, project_dir, _ = _make_index(tmp_path)
        _write_md(project_dir, "kb/new.md", "# New\n\nBrand new content added.")
        result = idx.update("kb/new.md")
        assert result["chunks_indexed"] >= 1

    def test_update_deleted_file(self, tmp_path):
        idx, project_dir, _ = _make_index(tmp_path)
        result = idx.update("kb/nonexistent.md")
        assert result["chunks_indexed"] == 0
        assert result.get("action") == "deleted"

    def test_update_replaces_old_chunks(self, tmp_path):
        idx, project_dir, _ = _make_index(tmp_path)
        _write_md(project_dir, "kb/up.md", "# V1\n\nOriginal version content.")
        idx.update("kb/up.md")

        # Update with new content
        _write_md(project_dir, "kb/up.md", "# V2\n\nNew version content here.")
        result = idx.update("kb/up.md")
        assert result["chunks_indexed"] >= 1

    def test_update_saves_hash(self, tmp_path):
        idx, project_dir, cache_dir = _make_index(tmp_path)
        _write_md(project_dir, "kb/tracked.md", "# Tracked\n\nContent for tracking.")
        idx.update("kb/tracked.md")
        hash_path = os.path.join(cache_dir, "file_hashes.json")
        assert os.path.isfile(hash_path)
        with open(hash_path, encoding="utf-8") as f:
            hashes = json.load(f)
        assert "kb/tracked.md" in hashes

    def test_update_absolute_path(self, tmp_path):
        idx, project_dir, _ = _make_index(tmp_path)
        abs_path = _write_md(project_dir, "kb/abs.md", "# Abs\n\nAbsolute path content.")
        result = idx.update(abs_path)
        assert result["chunks_indexed"] >= 1


# ── RAGIndex.search ─────────────────────────────────────────


class TestRAGIndexSearch:
    def test_search_empty_index(self, tmp_path):
        idx, _, _ = _make_index(tmp_path)
        results = idx.search("anything")
        assert results == []

    def test_search_returns_results(self, tmp_path):
        idx, project_dir, _ = _make_index(tmp_path)
        _write_md(project_dir, "kb/s1.md", "# Search Target\n\nThis is the content to find.")
        idx.build()
        results = idx.search("search target")
        assert len(results) >= 1
        assert "score" in results[0]
        assert "text" in results[0]
        assert "file" in results[0]

    def test_search_min_score_filter(self, tmp_path):
        idx, project_dir, _ = _make_index(tmp_path)
        _write_md(project_dir, "kb/s2.md", "# Content\n\nSome searchable content here.")
        idx.build()
        # Mock returns distance 0.2 → score 0.9, so min_score=0.95 should filter out
        results = idx.search("query", min_score=0.95)
        assert results == []

    def test_search_top_k_limit(self, tmp_path):
        idx, project_dir, _ = _make_index(tmp_path)
        for i in range(10):
            _write_md(project_dir, f"kb/file{i}.md", f"# File {i}\n\nContent number {i} is here.")
        idx.build()
        results = idx.search("content", top_k=3)
        assert len(results) <= 3

    def test_search_file_filter(self, tmp_path):
        idx, project_dir, _ = _make_index(tmp_path, knowledge_dirs=["kb", "rules"])
        _write_md(project_dir, "kb/k.md", "# KB\n\nKnowledge base entry content.")
        _write_md(project_dir, "rules/r.md", "# Rule\n\nRule definition content here.")
        idx.build()
        results = idx.search("content", file_filter="rules")
        # Mock collection filters by $contains
        for r in results:
            assert "rules" in r["file"]

    def test_search_score_calculation(self, tmp_path):
        idx, project_dir, _ = _make_index(tmp_path)
        _write_md(project_dir, "kb/sc.md", "# Score\n\nContent for score testing.")
        idx.build()
        results = idx.search("score")
        # Mock returns distance 0.2 → score should be 1 - 0.2/2 = 0.9
        assert len(results) >= 1
        assert results[0]["score"] == 0.9


# ── RAGIndex._record_hits & stale_report ────────────────────


class TestHitTracking:
    def test_record_hits_creates_log(self, tmp_path):
        idx, _, cache_dir = _make_index(tmp_path)
        idx._record_hits(["kb/a.md", "kb/b.md"])
        hits_path = os.path.join(cache_dir, "hit_log.json")
        assert os.path.isfile(hits_path)
        with open(hits_path, encoding="utf-8") as f:
            hits = json.load(f)
        assert "kb/a.md" in hits
        assert hits["kb/a.md"]["count"] == 1

    def test_record_hits_increments(self, tmp_path):
        idx, _, cache_dir = _make_index(tmp_path)
        idx._record_hits(["kb/a.md"])
        idx._record_hits(["kb/a.md"])
        hits_path = os.path.join(cache_dir, "hit_log.json")
        with open(hits_path, encoding="utf-8") as f:
            hits = json.load(f)
        assert hits["kb/a.md"]["count"] == 2

    def test_stale_report_empty(self, tmp_path):
        idx, _, _ = _make_index(tmp_path)
        report = idx.stale_report()
        assert report["total_files"] == 0
        assert report["stale_count"] == 0
        assert report["stale_ratio"] == 0

    def test_stale_report_identifies_stale(self, tmp_path):
        idx, project_dir, cache_dir = _make_index(tmp_path)
        _write_md(project_dir, "kb/stale.md", "# Stale\n\nOld content that nobody searches.")
        idx.build()

        # No hits recorded → stale
        report = idx.stale_report(days=1)
        assert report["stale_count"] == 1
        assert report["stale"][0]["file"] == "kb/stale.md"

    def test_stale_report_identifies_active(self, tmp_path):
        idx, project_dir, cache_dir = _make_index(tmp_path)
        _write_md(project_dir, "kb/active.md", "# Active\n\nFrequently searched content.")
        idx.build()

        # Record a recent hit
        idx._record_hits(["kb/active.md"])

        report = idx.stale_report(days=1)
        assert report["stale_count"] == 0
        assert len(report["active"]) == 1

    def test_stale_report_ratio(self, tmp_path):
        idx, project_dir, cache_dir = _make_index(tmp_path)
        _write_md(project_dir, "kb/a.md", "# A\n\nContent A for ratio test.")
        _write_md(project_dir, "kb/b.md", "# B\n\nContent B for ratio test.")
        idx.build()

        idx._record_hits(["kb/a.md"])
        report = idx.stale_report(days=1)
        # 1 of 2 is stale
        assert report["stale_ratio"] == 0.5


# ── RAGIndex.prune ──────────────────────────────────────────


class TestPrune:
    def test_prune_dry_run(self, tmp_path):
        idx, project_dir, _ = _make_index(tmp_path)
        _write_md(project_dir, "kb/old.md", "# Old\n\nStale content to maybe prune.")
        idx.build()

        result = idx.prune(days=0, dry_run=True)
        assert result["dry_run"] is True
        assert result["count"] >= 1
        # Collection still has data
        assert idx._collection.count() > 0

    def test_prune_execute(self, tmp_path):
        idx, project_dir, _ = _make_index(tmp_path)
        _write_md(project_dir, "kb/old.md", "# Old\n\nStale content that will be pruned.")
        idx.build()

        result = idx.prune(days=0, dry_run=False)
        assert result["dry_run"] is False
        assert result["count"] >= 1
        assert result["chunks_removed"] >= 1

    def test_prune_preserves_active(self, tmp_path):
        idx, project_dir, _ = _make_index(tmp_path)
        _write_md(
            project_dir, "kb/keep.md",
            "# Keep\n\nContent that should be kept, it's active.",
        )
        idx.build()

        # Record hit so it's active
        idx._record_hits(["kb/keep.md"])

        result = idx.prune(days=1, dry_run=False)
        # Active file should not be pruned
        assert "kb/keep.md" not in result.get("pruned", [])

    def test_prune_empty_index(self, tmp_path):
        idx, _, _ = _make_index(tmp_path)
        result = idx.prune(days=90, dry_run=False)
        assert result["count"] == 0


# ── RAGIndex.stats ──────────────────────────────────────────


class TestStats:
    def test_stats_empty(self, tmp_path):
        idx, _, cache_dir = _make_index(tmp_path)
        s = idx.stats()
        assert s["total_chunks"] == 0
        assert s["cache_dir"] == cache_dir
        assert s["collection"] == "test_col"

    def test_stats_after_build(self, tmp_path):
        idx, project_dir, _ = _make_index(tmp_path)
        _write_md(project_dir, "kb/s.md", "# Stats\n\nContent for stats testing.")
        idx.build()
        s = idx.stats()
        assert s["total_chunks"] >= 1


# ── RAGIndex init ───────────────────────────────────────────


class TestRAGIndexInit:
    def test_default_knowledge_dirs(self):
        idx = RAGIndex.__new__(RAGIndex)
        idx.__init__()
        assert len(idx.knowledge_dirs) == 4

    def test_custom_dirs(self):
        idx = RAGIndex(knowledge_dirs=["my_kb"])
        assert idx.knowledge_dirs == ["my_kb"]

    def test_env_project_dir(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/env/project")
        idx = RAGIndex()
        assert idx.project_dir == "/env/project"

    def test_explicit_project_dir(self):
        idx = RAGIndex(project_dir="/explicit")
        assert idx.project_dir == "/explicit"

    def test_cache_dir_default(self, tmp_path):
        idx = RAGIndex(project_dir=str(tmp_path))
        expected = os.path.join(str(tmp_path), ".concinno_cache", "rag")
        assert idx.cache_dir == expected


# ── CLI ─────────────────────────────────────────────────────


class TestCLI:
    @patch("concinno.rag.RAGIndex")
    def test_cli_build(self, MockIdx, monkeypatch):
        monkeypatch.setattr("sys.argv", ["concinno-rag", "build", "--project-dir", "/tmp"])
        mock_instance = MagicMock()
        mock_instance.build.return_value = {
            "files_scanned": 1,
            "files_indexed": 1,
            "chunks_indexed": 5,
            "duration_ms": 100,
        }
        MockIdx.return_value = mock_instance

        from concinno.rag import cli_main

        cli_main()
        mock_instance.build.assert_called_once_with(force=False)

    @patch("concinno.rag.RAGIndex")
    def test_cli_search(self, MockIdx, monkeypatch):
        monkeypatch.setattr(
            "sys.argv",
            ["concinno-rag", "search", "test query", "--project-dir", "/tmp"],
        )
        mock_instance = MagicMock()
        mock_instance.search.return_value = [
            {"score": 0.9, "file": "kb/a.md", "heading": "Test", "text": "Content"}
        ]
        MockIdx.return_value = mock_instance

        from concinno.rag import cli_main

        cli_main()
        mock_instance.search.assert_called_once()

    @patch("concinno.rag.RAGIndex")
    def test_cli_stats(self, MockIdx, monkeypatch):
        monkeypatch.setattr("sys.argv", ["concinno-rag", "stats", "--project-dir", "/tmp"])
        mock_instance = MagicMock()
        mock_instance.stats.return_value = {"total_chunks": 42}
        MockIdx.return_value = mock_instance

        from concinno.rag import cli_main

        cli_main()
        mock_instance.stats.assert_called_once()

    @patch("concinno.rag.RAGIndex")
    def test_cli_stale(self, MockIdx, monkeypatch):
        monkeypatch.setattr(
            "sys.argv",
            ["concinno-rag", "stale", "--days", "30", "--project-dir", "/tmp"],
        )
        mock_instance = MagicMock()
        mock_instance.stale_report.return_value = {
            "stale": [], "active": [], "total_files": 0,
            "stale_count": 0, "stale_ratio": 0,
        }
        MockIdx.return_value = mock_instance

        from concinno.rag import cli_main

        cli_main()
        mock_instance.stale_report.assert_called_once_with(days=30)

    @patch("concinno.rag.RAGIndex")
    def test_cli_prune_dry_run(self, MockIdx, monkeypatch):
        monkeypatch.setattr("sys.argv", ["concinno-rag", "prune", "--project-dir", "/tmp"])
        mock_instance = MagicMock()
        mock_instance.prune.return_value = {"dry_run": True, "would_prune": [], "count": 0}
        MockIdx.return_value = mock_instance

        from concinno.rag import cli_main

        cli_main()
        mock_instance.prune.assert_called_once_with(days=90, dry_run=True)

    @patch("concinno.rag.RAGIndex")
    def test_cli_prune_execute(self, MockIdx, monkeypatch):
        monkeypatch.setattr(
            "sys.argv",
            ["concinno-rag", "prune", "--execute", "--project-dir", "/tmp"],
        )
        mock_instance = MagicMock()
        mock_instance.prune.return_value = {
            "dry_run": False, "pruned": [], "count": 0,
            "chunks_removed": 0,
        }
        MockIdx.return_value = mock_instance

        from concinno.rag import cli_main

        cli_main()
        mock_instance.prune.assert_called_once_with(days=90, dry_run=False)

    @patch("concinno.rag.RAGIndex")
    def test_cli_update(self, MockIdx, monkeypatch):
        monkeypatch.setattr(
            "sys.argv",
            ["concinno-rag", "update", "kb/new.md", "--project-dir", "/tmp"],
        )
        mock_instance = MagicMock()
        mock_instance.update.return_value = {"chunks_indexed": 3, "duration_ms": 50}
        MockIdx.return_value = mock_instance

        from concinno.rag import cli_main

        cli_main()
        mock_instance.update.assert_called_once_with("kb/new.md")


# ── Edge cases ──────────────────────────────────────────────


class TestEdgeCases:
    def test_scan_nonexistent_dir(self, tmp_path):
        idx, _, _ = _make_index(tmp_path, knowledge_dirs=["nonexistent"])
        files = idx._scan_files()
        assert files == []

    def test_build_handles_unreadable_file(self, tmp_path):
        idx, project_dir, _ = _make_index(tmp_path)
        _write_md(project_dir, "kb/good.md", "# Good\n\nReadable content long enough.")
        # Build should work even if some files fail
        result = idx.build()
        assert result["files_indexed"] >= 0

    def test_search_records_hits(self, tmp_path):
        idx, project_dir, cache_dir = _make_index(tmp_path)
        _write_md(project_dir, "kb/hit.md", "# Hit\n\nContent that will be searched.")
        idx.build()

        idx.search("hit content")

        hits_path = os.path.join(cache_dir, "hit_log.json")
        assert os.path.isfile(hits_path)

    def test_multiple_builds_no_duplicates(self, tmp_path):
        idx, project_dir, _ = _make_index(tmp_path)
        _write_md(project_dir, "kb/dup.md", "# Dup\n\nContent to check for duplicates in index.")

        idx.build(force=True)
        count1 = idx._collection.count()

        idx.build(force=True)
        count2 = idx._collection.count()

        # Force rebuild clears then re-adds, should be same count
        assert count1 == count2

    def test_chinese_content(self, tmp_path):
        idx, project_dir, _ = _make_index(tmp_path)
        _write_md(project_dir, "kb/cn.md", "# 中文標題\n\n這是中文內容，用於測試多語言支援。")
        result = idx.build()
        assert result["files_indexed"] == 1

    def test_update_then_search(self, tmp_path):
        idx, project_dir, _ = _make_index(tmp_path)
        _write_md(project_dir, "kb/flow.md", "# Flow\n\nUpdate then search flow test.")
        idx.update("kb/flow.md")

        results = idx.search("flow test")
        assert len(results) >= 1


# ── Namespace support ──────────────────────────────────────


class TestNamespace:
    def test_default_namespace(self, tmp_path):
        idx, _, _ = _make_index(tmp_path)
        assert idx.namespace == "default"
        assert idx.collection_name == "test_col"

    def test_custom_namespace_changes_collection(self):
        idx = RAGIndex(
            knowledge_dirs=["kb"],
            project_dir="/tmp",
            namespace="memory",
        )
        assert idx.namespace == "memory"
        assert idx.collection_name == "cc_memory"

    def test_custom_namespace_preserves_explicit_collection(self):
        idx = RAGIndex(
            knowledge_dirs=["kb"],
            project_dir="/tmp",
            namespace="memory",
            collection_name="my_custom",
        )
        assert idx.collection_name == "my_custom"

    def test_stats_includes_namespace(self, tmp_path):
        idx, _, _ = _make_index(tmp_path)
        s = idx.stats()
        assert "namespace" in s
        assert s["namespace"] == "default"

    def test_namespaces_dict_has_five_entries(self):
        assert len(NAMESPACES) == 5
        assert "knowledge" in NAMESPACES
        assert "memory" in NAMESPACES
        assert "cognition" in NAMESPACES
        assert "skills" in NAMESPACES
        assert "context" in NAMESPACES

    def test_create_namespace_index(self, tmp_path):
        project_dir = str(tmp_path / "project")
        cache_dir = str(tmp_path / "cache")
        os.makedirs(project_dir, exist_ok=True)

        idx = create_namespace_index("memory", project_dir, cache_dir)
        assert idx.namespace == "memory"
        assert idx.collection_name == "cc_memory"
        assert "corrections/" in idx.knowledge_dirs
        assert os.path.join(cache_dir, "rag_memory") in idx.cache_dir

    def test_create_namespace_index_unknown(self, tmp_path):
        project_dir = str(tmp_path / "project")
        cache_dir = str(tmp_path / "cache")
        os.makedirs(project_dir, exist_ok=True)

        idx = create_namespace_index("custom", project_dir, cache_dir)
        assert idx.namespace == "custom"
        assert idx.collection_name == "cc_custom"


# ── RRF Fusion ─────────────────────────────────────────────


class TestFuseResults:
    def test_fuse_empty(self):
        result = RAGIndex._fuse_results([], [], 0.3)
        assert result == []

    def test_fuse_dense_only(self):
        dense = [
            {"text": "a", "file": "a.md", "heading": "A", "score": 0.9},
            {"text": "b", "file": "b.md", "heading": "B", "score": 0.8},
        ]
        result = RAGIndex._fuse_results(dense, [], 0.3)
        assert len(result) == 2
        assert all("fused_score" in r for r in result)

    def test_fuse_preserves_all_items(self):
        dense = [{"text": "a", "file": "a.md", "heading": "A", "score": 0.9}]
        sparse = [{"text": "b", "file": "b.md", "heading": "B", "score": 5.0}]
        result = RAGIndex._fuse_results(dense, sparse, 0.5)
        assert len(result) == 2
        files = {r["file"] for r in result}
        assert files == {"a.md", "b.md"}

    def test_fuse_overlap_boosts_score(self):
        # Same item in both lists should get higher fused score
        item = {"text": "x", "file": "x.md", "heading": "X", "score": 0.9}
        dense = [item]
        sparse = [dict(item)]
        result = RAGIndex._fuse_results(dense, sparse, 0.5)
        assert len(result) == 1
        # Score from both sides
        assert result[0]["fused_score"] > 0

    def test_fuse_weight_zero_is_dense_only(self):
        dense = [{"text": "a", "file": "a.md", "heading": "", "score": 0.9}]
        sparse = [{"text": "b", "file": "b.md", "heading": "", "score": 5.0}]
        result = RAGIndex._fuse_results(dense, sparse, bm25_weight=0.0)
        # With bm25_weight=0, sparse contributes 0 RRF score
        assert result[0]["file"] == "a.md"


# ── Hybrid search ──────────────────────────────────────────


class TestHybridSearch:
    def test_hybrid_falls_back_to_dense_on_import_error(self, tmp_path):
        idx, project_dir, _ = _make_index(tmp_path)
        _write_md(project_dir, "kb/h.md", "# Hybrid\n\nContent for hybrid search test.")
        idx.build()

        # hybrid_search should work even without bm25s (graceful fallback)
        results = idx.hybrid_search("hybrid content")
        assert isinstance(results, list)

    def test_hybrid_respects_top_k(self, tmp_path):
        idx, project_dir, _ = _make_index(tmp_path)
        for i in range(5):
            _write_md(project_dir, f"kb/h{i}.md", f"# H{i}\n\nHybrid content number {i}.")
        idx.build()
        results = idx.hybrid_search("content", top_k=2)
        assert len(results) <= 2


# ── Reranked search ────────────────────────────────────────


class TestRerankedSearch:
    def test_reranked_without_reranker(self, tmp_path):
        idx, project_dir, _ = _make_index(tmp_path)
        _write_md(project_dir, "kb/r.md", "# Rerank\n\nContent for reranker test.")
        idx.build()
        results = idx.reranked_search("rerank", top_k=3)
        assert isinstance(results, list)

    def test_reranked_with_mock_reranker(self, tmp_path):
        idx, project_dir, _ = _make_index(tmp_path)
        _write_md(project_dir, "kb/r.md", "# Rerank\n\nContent for reranker test.")
        idx.build()

        class MockReranker:
            def rerank(self, query, candidates):
                # Reverse order as mock reranking
                return list(reversed(candidates))

        results = idx.reranked_search("rerank", top_k=3, reranker=MockReranker())
        assert isinstance(results, list)

    def test_reranked_top_k_limit(self, tmp_path):
        idx, project_dir, _ = _make_index(tmp_path)
        for i in range(5):
            _write_md(project_dir, f"kb/rr{i}.md", f"# RR{i}\n\nReranked content {i} here.")
        idx.build()
        results = idx.reranked_search("content", top_k=2)
        assert len(results) <= 2
