"""concinno.rag — Cognitive RAG: AI self-evolution memory, not Q&A.

@module rag
@responsibility Cross-session cognitive memory via local embeddings + ChromaDB.
    Indexes corrections, learnings, rules, and handoffs for semantic retrieval.
@dependencies none (optional: sentence-transformers, chromadb)
@exports RAGIndex, NAMESPACES, create_namespace_index, cli_main

Unlike typical RAG (index company docs → answer questions), Cognitive RAG
is a **cognitive prosthesis** for AI agents:

- **What it indexes**: corrections, learnings, rules, handoffs — the agent's
  own mistakes, decisions, and growth artifacts
- **When it triggers**: automatically on every user prompt (via hook), not
  on-demand Q&A
- **Why it exists**: cross-session amnesia is the #1 cause of AI regression.
  Each new session loses all corrections from previous sessions. Cognitive RAG
  makes past learnings retrievable by semantic similarity, so the agent
  doesn't repeat the same mistake twice ("cold stakes" problem)

This is NOT a knowledge base search. This is a **stake** (樁) — an anchor
that prevents the AI from drifting back to broken patterns.

Architecture:
- Engine (this module): chunker + embedder + vector search → open source (CCC)
- Fuel (indexed content): project-specific corrections/rules → private, never shared
- Trigger (hook): on-prompt-submit.py injects top matches → per-project config

Uses local embeddings (sentence-transformers, default BAAI/bge-m3) + ChromaDB.
No API calls, no data leaves the machine. bge-m3 is the strongest multilingual
embedding model (100+ languages, 8192 token context, MTEB top-tier).

Usage::

    from concinno.rag import RAGIndex

    idx = RAGIndex(knowledge_dirs=["corrections/", "rules/", "handoffs/"])
    idx.build()                         # Index all markdown files
    results = idx.search("冷樁根治")     # Semantic search
    idx.update("skills/kb_topic/SKILL.md")  # Incremental update after learning
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from typing import Optional

from concinno.anti_bloat import HitTracker, Pruner, StaleDetector

# Lazy imports — heavy deps loaded only when needed
_chromadb = None
_SentenceTransformer = None
_bm25s = None


def _ensure_deps():
    """Lazy-import heavy dependencies."""
    global _chromadb, _SentenceTransformer
    if _chromadb is None:
        import chromadb

        _chromadb = chromadb
    if _SentenceTransformer is None:
        from sentence_transformers import SentenceTransformer

        _SentenceTransformer = SentenceTransformer


def _ensure_bm25():
    """Lazy-import bm25s (optional sparse retrieval)."""
    global _bm25s
    if _bm25s is None:
        import bm25s  # type: ignore[import-untyped]

        _bm25s = bm25s
    return _bm25s


# ── Namespace definitions ────────────────────────────────────

NAMESPACES: dict[str, dict] = {
    "knowledge": {"dirs": [], "description": "External docs, API specs"},
    "memory": {
        "dirs": ["corrections/", "06_Handoffs/"],
        "description": "Feedback, learnings, handoffs",
    },
    "cognition": {
        "dirs": [".claude/rules/", "05_Planning/"],
        "description": "Rules, CBUA, planning",
    },
    "skills": {"dirs": [".claude/skills/"], "description": "KB skills, SOP"},
    "context": {"dirs": [], "description": "Session-scoped read cache"},
}


def create_namespace_index(
    namespace: str,
    project_dir: str,
    cache_dir: str,
) -> "RAGIndex":
    """Create a RAGIndex for a specific namespace.

    Uses predefined dirs from NAMESPACES if available.

    Args:
        namespace: Namespace key (e.g. "memory", "cognition").
        project_dir: Project root directory.
        cache_dir: Base cache directory (namespace subdir added automatically).

    Returns:
        RAGIndex configured for the namespace.
    """
    ns_config = NAMESPACES.get(namespace, {})
    knowledge_dirs = ns_config.get("dirs", []) or []
    ns_cache = os.path.join(cache_dir, f"rag_{namespace}")
    return RAGIndex(
        knowledge_dirs=knowledge_dirs if knowledge_dirs else None,
        project_dir=project_dir,
        cache_dir=ns_cache,
        namespace=namespace,
    )


# ── Markdown Chunker ──────────────────────────────────────


def chunk_markdown(
    text: str,
    file_path: str,
    max_chunk_size: int = 512,
    overlap: int = 50,
) -> list[dict]:
    """Split markdown into semantic chunks by headings, then by size.

    Returns list of dicts with keys: text, metadata (file, heading, chunk_idx).
    """
    chunks: list[dict] = []

    # Split by headings (## or #)
    sections = re.split(r"(?=^#{1,3}\s)", text, flags=re.MULTILINE)

    chunk_idx = 0
    for section in sections:
        section = section.strip()
        if not section:
            continue

        # Extract heading
        heading_match = re.match(r"^(#{1,3})\s+(.+?)$", section, re.MULTILINE)
        heading = heading_match.group(2).strip() if heading_match else ""

        # If section is small enough, keep as one chunk
        if len(section) <= max_chunk_size:
            chunks.append(
                {
                    "text": section,
                    "metadata": {
                        "file": file_path,
                        "heading": heading,
                        "chunk_idx": chunk_idx,
                    },
                }
            )
            chunk_idx += 1
        else:
            # Split large sections by paragraphs, then merge up to max_chunk_size
            paragraphs = re.split(r"\n\n+", section)
            current = ""
            for para in paragraphs:
                if len(current) + len(para) + 2 <= max_chunk_size:
                    current = (current + "\n\n" + para).strip()
                else:
                    if current:
                        chunks.append(
                            {
                                "text": current,
                                "metadata": {
                                    "file": file_path,
                                    "heading": heading,
                                    "chunk_idx": chunk_idx,
                                },
                            }
                        )
                        chunk_idx += 1
                        # Overlap: keep last N chars
                        current = current[-overlap:] + "\n\n" + para
                    else:
                        current = para
            if current.strip():
                chunks.append(
                    {
                        "text": current.strip(),
                        "metadata": {
                            "file": file_path,
                            "heading": heading,
                            "chunk_idx": chunk_idx,
                        },
                    }
                )
                chunk_idx += 1

    return chunks


def _file_hash(path: str) -> str:
    """MD5 hash of file content for change detection."""
    try:
        with open(path, "rb") as f:
            return hashlib.md5(f.read()).hexdigest()
    except Exception:
        return ""


# ── RAG Index ─────────────────────────────────────────────


class RAGIndex:
    """Cognitive RAG index — semantic search over AI agent's own learnings.

    Indexes corrections, rules, handoffs, and skill knowledge entries so
    the agent can recall past decisions and mistakes by semantic similarity.

    Args:
        knowledge_dirs: Directories to index (relative to project_dir).
            Default: corrections, rules, handoffs, skills — the agent's growth artifacts.
        project_dir: Project root directory.
        cache_dir: Where to store the vector DB. Default: .concinno_cache/rag
        model_name: Sentence-transformers model for embeddings.
        collection_name: ChromaDB collection name.
        include_bundled: Whether to include concinno's bundled knowledge/
            directory (cognitive frameworks shipped with the package).
    """

    # Bundled cognitive frameworks shipped with concinno
    _BUNDLED_KNOWLEDGE_DIR: str = ""

    @classmethod
    def _bundled_knowledge_dir(cls) -> str:
        """Return absolute path to concinno's bundled knowledge/ directory."""
        if not cls._BUNDLED_KNOWLEDGE_DIR:
            # knowledge/ lives at package root (sibling of src/)
            pkg_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            # Go up from concinno/ → src/ → concinno/ → knowledge/
            candidate = os.path.join(
                os.path.dirname(os.path.dirname(pkg_dir)), "knowledge"
            )
            if os.path.isdir(candidate):
                cls._BUNDLED_KNOWLEDGE_DIR = candidate
            else:
                # Fallback: installed package, knowledge/ beside the package
                alt = os.path.join(pkg_dir, "knowledge")
                if os.path.isdir(alt):
                    cls._BUNDLED_KNOWLEDGE_DIR = alt
        return cls._BUNDLED_KNOWLEDGE_DIR

    def __init__(
        self,
        knowledge_dirs: list[str] | None = None,
        project_dir: str = "",
        cache_dir: str = "",
        model_name: str = "BAAI/bge-m3",
        collection_name: str = "cc_knowledge",
        include_bundled: bool = True,
        namespace: str = "default",
    ):
        self.project_dir = project_dir or os.environ.get("CLAUDE_PROJECT_DIR", ".")
        self.namespace = namespace
        from concinno.core.config import get_config
        brain_dir = get_config().brain_dir
        self.knowledge_dirs = knowledge_dirs or [
            f"{brain_dir}/01_Memory/kb",
            f"{brain_dir}/00_System/rules",
            ".claude/rules",
            f"{brain_dir}/06_Handoffs",
        ]
        self._include_bundled = include_bundled
        self.cache_dir = cache_dir or os.path.join(
            self.project_dir, ".concinno_cache", "rag"
        )
        self.model_name = model_name
        # Map namespace to collection name for multi-namespace isolation
        if namespace != "default" and collection_name == "cc_knowledge":
            self.collection_name = f"cc_{namespace}"
        else:
            self.collection_name = collection_name
        self._model = None
        self._hit_tracker = HitTracker(self.cache_dir)
        self._stale_detector = StaleDetector(self.cache_dir)
        self._pruner = Pruner(self.cache_dir)
        self._client = None
        self._collection = None
        # BM25 sparse index (lazy-built)
        self._bm25_index = None
        self._bm25_corpus_ids: list[str] = []
        self._bm25_corpus_meta: list[dict] = []

    def _get_model(self):
        if self._model is None:
            _ensure_deps()
            self._model = _SentenceTransformer(self.model_name)
        return self._model

    def _get_collection(self):
        if self._collection is None:
            _ensure_deps()
            os.makedirs(self.cache_dir, exist_ok=True)
            self._client = _chromadb.PersistentClient(path=self.cache_dir)
            self._collection = self._client.get_or_create_collection(
                name=self.collection_name,
                metadata={"hnsw:space": "cosine"},
            )
        return self._collection

    def _scan_files(self) -> list[str]:
        """Find all .md files in knowledge_dirs + bundled knowledge."""
        files = []
        # Project-relative dirs
        for rel_dir in self.knowledge_dirs:
            abs_dir = os.path.join(self.project_dir, rel_dir)
            if not os.path.isdir(abs_dir):
                continue
            for root, _, filenames in os.walk(abs_dir):
                for fn in filenames:
                    if fn.endswith(".md"):
                        files.append(os.path.join(root, fn))
        # Bundled cognitive frameworks (shipped with concinno)
        if self._include_bundled:
            bundled = self._bundled_knowledge_dir()
            if bundled and os.path.isdir(bundled):
                for root, _, filenames in os.walk(bundled):
                    for fn in filenames:
                        if fn.endswith(".md"):
                            files.append(os.path.join(root, fn))
        return files

    def build(self, force: bool = False) -> dict:
        """Build or rebuild the full index.

        Args:
            force: If True, delete existing collection and rebuild from scratch.

        Returns:
            Stats dict: files_indexed, chunks_indexed, duration_ms.
        """
        start = time.time()
        collection = self._get_collection()

        if force:
            # Delete and recreate
            self._client.delete_collection(self.collection_name)
            self._collection = self._client.create_collection(
                name=self.collection_name,
                metadata={"hnsw:space": "cosine"},
            )
            collection = self._collection

        model = self._get_model()
        files = self._scan_files()

        # Load existing file hashes to skip unchanged files
        hash_path = os.path.join(self.cache_dir, "file_hashes.json")
        existing_hashes = {}
        if not force and os.path.isfile(hash_path):
            import json

            try:
                with open(hash_path, encoding="utf-8") as f:
                    existing_hashes = json.load(f)
            except Exception:
                pass

        files_indexed = 0
        chunks_indexed = 0
        new_hashes = {}

        for file_path in files:
            fhash = _file_hash(file_path)
            rel_path = os.path.relpath(file_path, self.project_dir).replace("\\", "/")
            new_hashes[rel_path] = fhash

            # Skip if unchanged
            if not force and existing_hashes.get(rel_path) == fhash:
                continue

            # Read and chunk
            try:
                with open(file_path, encoding="utf-8") as f:
                    text = f.read()
            except Exception:
                continue

            if len(text.strip()) < 20:
                continue

            # Remove frontmatter
            text = re.sub(r"^---\n.*?---\n", "", text, flags=re.DOTALL)

            chunks = chunk_markdown(text, rel_path)
            if not chunks:
                continue

            # Delete old chunks for this file
            try:
                collection.delete(where={"file": rel_path})
            except Exception:
                pass

            # Embed and add
            texts = [c["text"] for c in chunks]
            embeddings = model.encode(texts, show_progress_bar=False).tolist()
            ids = [f"{rel_path}::{c['metadata']['chunk_idx']}" for c in chunks]
            metadatas = [c["metadata"] for c in chunks]

            # ChromaDB batch limit
            batch_size = 100
            for i in range(0, len(ids), batch_size):
                collection.add(
                    ids=ids[i : i + batch_size],
                    embeddings=embeddings[i : i + batch_size],
                    documents=texts[i : i + batch_size],
                    metadatas=metadatas[i : i + batch_size],
                )

            files_indexed += 1
            chunks_indexed += len(chunks)

        # Save hashes
        import json

        os.makedirs(self.cache_dir, exist_ok=True)
        with open(hash_path, "w", encoding="utf-8") as f:
            json.dump(new_hashes, f, indent=2, ensure_ascii=False)

        duration_ms = int((time.time() - start) * 1000)
        return {
            "files_scanned": len(files),
            "files_indexed": files_indexed,
            "chunks_indexed": chunks_indexed,
            "duration_ms": duration_ms,
        }

    def update(self, file_path: str) -> dict:
        """Incrementally update index for a single file.

        Args:
            file_path: Absolute or relative path to the changed file.

        Returns:
            Stats dict: chunks_indexed, duration_ms.
        """
        start = time.time()
        abs_path = (
            file_path
            if os.path.isabs(file_path)
            else os.path.join(self.project_dir, file_path)
        )
        rel_path = os.path.relpath(abs_path, self.project_dir).replace("\\", "/")

        collection = self._get_collection()
        model = self._get_model()

        # Delete old chunks
        try:
            collection.delete(where={"file": rel_path})
        except Exception:
            pass

        if not os.path.isfile(abs_path):
            return {"chunks_indexed": 0, "duration_ms": 0, "action": "deleted"}

        try:
            with open(abs_path, encoding="utf-8") as f:
                text = f.read()
        except Exception:
            return {"chunks_indexed": 0, "duration_ms": 0, "error": "read_failed"}

        text = re.sub(r"^---\n.*?---\n", "", text, flags=re.DOTALL)
        chunks = chunk_markdown(text, rel_path)
        if not chunks:
            return {"chunks_indexed": 0, "duration_ms": 0}

        texts = [c["text"] for c in chunks]
        embeddings = model.encode(texts, show_progress_bar=False).tolist()
        ids = [f"{rel_path}::{c['metadata']['chunk_idx']}" for c in chunks]
        metadatas = [c["metadata"] for c in chunks]

        collection.add(
            ids=ids,
            embeddings=embeddings,
            documents=texts,
            metadatas=metadatas,
        )

        # Update hash
        import json

        hash_path = os.path.join(self.cache_dir, "file_hashes.json")
        hashes = {}
        if os.path.isfile(hash_path):
            try:
                with open(hash_path, encoding="utf-8") as f:
                    hashes = json.load(f)
            except Exception:
                pass
        hashes[rel_path] = _file_hash(abs_path)
        os.makedirs(os.path.dirname(hash_path), exist_ok=True)
        with open(hash_path, "w", encoding="utf-8") as f:
            json.dump(hashes, f, indent=2, ensure_ascii=False)

        duration_ms = int((time.time() - start) * 1000)
        return {"chunks_indexed": len(chunks), "duration_ms": duration_ms}

    def search(
        self,
        query: str,
        top_k: int = 5,
        min_score: float = 0.3,
        file_filter: Optional[str] = None,
    ) -> list[dict]:
        """Semantic search over indexed knowledge.

        Args:
            query: Search query (natural language).
            top_k: Max results to return.
            min_score: Minimum similarity score (0-1, higher = more similar).
            file_filter: Optional glob pattern to filter files (e.g. "skills/kb_*/*").

        Returns:
            List of result dicts with keys: text, file, heading, score.
        """
        collection = self._get_collection()
        model = self._get_model()

        if collection.count() == 0:
            return []

        query_embedding = model.encode([query], show_progress_bar=False).tolist()

        where_filter = None
        if file_filter:
            # ChromaDB doesn't support glob, use $contains
            where_filter = {"file": {"$contains": file_filter}}

        try:
            results = collection.query(
                query_embeddings=query_embedding,
                n_results=min(top_k, collection.count()),
                where=where_filter,
                include=["documents", "metadatas", "distances"],
            )
        except Exception:
            # Retry without filter
            results = collection.query(
                query_embeddings=query_embedding,
                n_results=min(top_k, collection.count()),
                include=["documents", "metadatas", "distances"],
            )

        if not results or not results.get("documents"):
            return []

        output = []
        docs = results["documents"][0]
        metas = results["metadatas"][0]
        distances = results["distances"][0]

        for doc, meta, dist in zip(docs, metas, distances):
            # ChromaDB cosine distance: 0 = identical, 2 = opposite
            # Convert to similarity score: 1 - dist/2
            score = 1 - dist / 2
            if score < min_score:
                continue
            output.append(
                {
                    "text": doc,
                    "file": meta.get("file", ""),
                    "heading": meta.get("heading", ""),
                    "score": round(score, 3),
                }
            )

        # Record hits for anti-bloat tracking
        if output:
            self._record_hits([r["file"] for r in output])

        return output

    # ── BM25 sparse retrieval ────────────────────────────────

    def _build_bm25_index(self) -> None:
        """Build in-memory BM25 index from ChromaDB collection documents."""
        collection = self._get_collection()
        if collection.count() == 0:
            self._bm25_index = None
            return

        # Fetch all documents from the collection
        all_data = collection.get(include=["documents", "metadatas"])
        docs = all_data.get("documents", [])
        metas = all_data.get("metadatas", [])
        ids = all_data.get("ids", [])

        if not docs:
            self._bm25_index = None
            return

        bm25 = _ensure_bm25()
        # bm25s tokenizes internally
        corpus_tokens = bm25.tokenize(docs)
        retriever = bm25.BM25()
        retriever.index(corpus_tokens)

        self._bm25_index = retriever
        self._bm25_corpus_ids = list(ids)
        self._bm25_corpus_meta = [
            {"doc": d, "meta": m} for d, m in zip(docs, metas)
        ]

    def _bm25_search(self, query: str, top_k: int = 10) -> list[dict]:
        """Sparse BM25 search over indexed documents.

        Args:
            query: Search query text.
            top_k: Max results.

        Returns:
            List of result dicts with keys: text, file, heading, score.
        """
        if self._bm25_index is None:
            try:
                self._build_bm25_index()
            except ImportError:
                return []
        if self._bm25_index is None:
            return []

        bm25 = _ensure_bm25()
        query_tokens = bm25.tokenize([query])
        results, scores = self._bm25_index.retrieve(
            query_tokens, k=min(top_k, len(self._bm25_corpus_ids)),
        )

        output = []
        for idx_arr, score_arr in zip(results[0], scores[0]):
            idx = int(idx_arr)
            score = float(score_arr)
            if idx < 0 or idx >= len(self._bm25_corpus_meta):
                continue
            entry = self._bm25_corpus_meta[idx]
            meta = entry["meta"] or {}
            output.append({
                "text": entry["doc"],
                "file": meta.get("file", ""),
                "heading": meta.get("heading", ""),
                "score": round(score, 4),
            })
        return output

    @staticmethod
    def _fuse_results(
        dense: list[dict],
        sparse: list[dict],
        bm25_weight: float = 0.3,
    ) -> list[dict]:
        """Reciprocal Rank Fusion (RRF) of dense + sparse results.

        Args:
            dense: Dense (embedding) search results.
            sparse: BM25 sparse search results.
            bm25_weight: Weight for BM25 scores in fusion (0-1).

        Returns:
            Fused and re-sorted result list.
        """
        k = 60  # RRF constant
        scores: dict[str, float] = {}
        items: dict[str, dict] = {}

        dense_weight = 1.0 - bm25_weight
        for rank, r in enumerate(dense):
            key = f"{r['file']}::{r.get('heading', '')}"
            rrf = dense_weight / (k + rank + 1)
            scores[key] = scores.get(key, 0.0) + rrf
            items[key] = r

        for rank, r in enumerate(sparse):
            key = f"{r['file']}::{r.get('heading', '')}"
            rrf = bm25_weight / (k + rank + 1)
            scores[key] = scores.get(key, 0.0) + rrf
            if key not in items:
                items[key] = r

        # Sort by fused score descending
        sorted_keys = sorted(scores, key=lambda x: scores[x], reverse=True)
        output = []
        for key in sorted_keys:
            result = dict(items[key])
            result["fused_score"] = round(scores[key], 6)
            output.append(result)
        return output

    def hybrid_search(
        self,
        query: str,
        top_k: int = 5,
        bm25_weight: float = 0.3,
        min_score: float = 0.3,
    ) -> list[dict]:
        """BM25 + dense hybrid search with RRF score fusion.

        Falls back to dense-only if bm25s is not installed.

        Args:
            query: Search query.
            top_k: Max results to return.
            bm25_weight: Weight for BM25 in fusion (0=dense only, 1=BM25 only).
            min_score: Minimum dense score filter.

        Returns:
            Fused result list sorted by combined score.
        """
        dense_results = self.search(query, top_k=top_k * 2, min_score=min_score)

        try:
            bm25_results = self._bm25_search(query, top_k=top_k * 2)
        except ImportError:
            # bm25s not installed — graceful fallback to dense only
            return dense_results[:top_k]

        if not bm25_results:
            return dense_results[:top_k]

        fused = self._fuse_results(dense_results, bm25_results, bm25_weight)
        return fused[:top_k]

    def reranked_search(
        self,
        query: str,
        top_k: int = 5,
        reranker=None,
    ) -> list[dict]:
        """Search with optional reranker (e.g. GTE-ModernBERT).

        Fetches extra candidates via hybrid_search, then applies reranker.

        Args:
            query: Search query.
            top_k: Final number of results.
            reranker: Object with a ``rerank(query, candidates) -> list`` method.
                If None, returns hybrid results directly.

        Returns:
            Top-k results after optional reranking.
        """
        candidates = self.hybrid_search(query, top_k=top_k * 3)
        if reranker is not None:
            candidates = reranker.rerank(query, candidates)
        return candidates[:top_k]

    def _record_hits(self, files: list[str]) -> None:
        """Record which files were hit by search (for staleness tracking)."""
        self._hit_tracker.record_hits(files)

    def stale_report(self, days: int = 90) -> dict:
        """Find files that haven't been hit by any search in N days.

        Returns:
            Dict with 'stale' (list of never/old-hit files),
            'active' (recently hit), 'total_files', 'stale_ratio'.
        """
        return self._stale_detector.report(days=days)

    def prune(self, days: int = 90, dry_run: bool = True) -> dict:
        """Remove stale chunks from index (files not hit in N days).

        Args:
            days: Files not hit in this many days are candidates.
            dry_run: If True, only report what would be removed.

        Returns:
            Dict with 'pruned' files list and 'chunks_removed' count.
        """
        collection = self._get_collection()
        return self._pruner.prune(collection, days=days, dry_run=dry_run)

    def stats(self) -> dict:
        """Get index statistics."""
        collection = self._get_collection()
        return {
            "total_chunks": collection.count(),
            "cache_dir": self.cache_dir,
            "model": self.model_name,
            "collection": self.collection_name,
            "namespace": self.namespace,
        }


# ── CLI Entry Point ───────────────────────────────────────


def cli_main():
    """CLI for RAG index management."""
    import argparse
    import json

    parser = argparse.ArgumentParser(description="CC Cortex RAG Index")
    sub = parser.add_subparsers(dest="command")

    build_p = sub.add_parser("build", help="Build/rebuild index")
    build_p.add_argument("--force", action="store_true", help="Force full rebuild")
    build_p.add_argument("--project-dir", default="", help="Project root")

    search_p = sub.add_parser("search", help="Search knowledge")
    search_p.add_argument("query", help="Search query")
    search_p.add_argument("--top-k", type=int, default=5)
    search_p.add_argument("--project-dir", default="")

    stats_p = sub.add_parser("stats", help="Index statistics")
    stats_p.add_argument("--project-dir", default="")

    update_p = sub.add_parser("update", help="Update single file")
    update_p.add_argument("file", help="File path to update")
    update_p.add_argument("--project-dir", default="")

    stale_p = sub.add_parser("stale", help="Show stale (never-hit) files")
    stale_p.add_argument("--days", type=int, default=90, help="Staleness threshold")
    stale_p.add_argument("--project-dir", default="")

    prune_p = sub.add_parser("prune", help="Remove stale chunks from index")
    prune_p.add_argument("--days", type=int, default=90, help="Staleness threshold")
    prune_p.add_argument("--execute", action="store_true", help="Actually prune (default: dry-run)")
    prune_p.add_argument("--project-dir", default="")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    idx = RAGIndex(project_dir=args.project_dir or os.environ.get("CLAUDE_PROJECT_DIR", "."))

    if args.command == "build":
        result = idx.build(force=args.force)
        print(json.dumps(result, indent=2, ensure_ascii=False))

    elif args.command == "search":
        results = idx.search(args.query, top_k=args.top_k)
        for r in results:
            print(f"\n[{r['score']:.3f}] {r['file']} — {r['heading']}")
            print(f"  {r['text'][:200]}...")

    elif args.command == "stats":
        print(json.dumps(idx.stats(), indent=2, ensure_ascii=False))

    elif args.command == "update":
        result = idx.update(args.file)
        print(json.dumps(result, indent=2, ensure_ascii=False))

    elif args.command == "stale":
        report = idx.stale_report(days=args.days)
        total = report["total_files"]
        sc = report["stale_count"]
        ratio = report["stale_ratio"]
        print(f"Total: {total} | Stale: {sc} ({ratio:.0%})")
        if report["stale"]:
            print(f"\nNot hit in {args.days} days:")
            for s in report["stale"]:
                lh = s["last_hit"] or "never"
                print(f"  {s['file']}  (last: {lh})")
        if report["active"]:
            print("\nTop active:")
            for a in report["active"][:10]:
                print(
                    f"  [{a['hits']}x] {a['file']}"
                    f"  (last: {a['last_hit']})"
                )

    elif args.command == "prune":
        result = idx.prune(
            days=args.days, dry_run=not args.execute
        )
        print(json.dumps(result, indent=2, ensure_ascii=False))
        if result.get("dry_run"):
            print("\n(Dry run. --execute to prune.)")


if __name__ == "__main__":
    cli_main()
