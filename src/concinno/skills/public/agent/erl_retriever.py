"""ERL (Experience Retrieval Learning) retriever for GAIA.

Inspired by Agent KB (arXiv 2507.06229): index KB sediment (feedback_*.md,
kb_benchmark topics, skills) into ChromaDB; at task-solve time retrieve the
top-k most relevant experience hints and prepend them to the system prompt.

Design decisions:
- Uses ChromaDB default embedding (all-MiniLM-L6-v2 ONNX) — already cached.
  No additional heavy dep; chroma + sentence-transformers already in pyproject.
- Persisted at ~/.concinno/erl/gaia_kb/ so the index survives code changes.
- feature gate: ``gaia_erl_retrieval`` (default off, opt-in after smoke
  confirms positive delta; flip via CONCINNO_GAIA_ERL_RETRIEVAL_ENABLED=1).
- Thread-safe singleton: index is built once per process.
- Falls through silently on any error (never breaks existing agent paths).

Agent KB terminology mapping:
  "planning seeds"  → retrieved procedures / SOP hints from kb_benchmark
  "feedback fixes"  → retrieved diagnostic patches from feedback_*.md
  "disagreement gate" → min_score threshold (hits below cutoff suppressed)

Usage (internal — called from _get_domain_procedure in gaia_agent.py)::

    from .erl_retriever import retrieve_erl_hints
    hints = retrieve_erl_hints(question)   # list[str], may be empty
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_MEMORY_DIR = Path.home() / ".claude" / "projects" / "e--ai-king" / "memory"
_KB_BENCHMARK_DIR = Path.home() / ".claude" / "skills" / "kb_benchmark"
_ERL_INDEX_DIR = Path.home() / ".concinno" / "erl" / "gaia_kb"

# ---------------------------------------------------------------------------
# Corpus discovery
# ---------------------------------------------------------------------------

_SKIP_FEEDBACK_PATTERNS = [
    # Non-GAIA noise patterns: UI/video/branding/etc.
    "video_pipeline", "ux_branding", "product_design", "image_generation",
    "word_com", "toast_reputation", "pexels", "video_product",
    "audio_params", "book_content", "book_rules",
]


def _should_skip(path: Path) -> bool:
    """Return True for feedback files unlikely to help GAIA."""
    name = path.stem
    return any(pat in name for pat in _SKIP_FEEDBACK_PATTERNS)


def _load_corpus() -> list[dict[str, str]]:
    """Load all KB sediment docs as {id, text, source} dicts."""
    docs: list[dict[str, str]] = []

    # --- Priority 1: GAIA-specific feedback ---
    for fp in sorted(_MEMORY_DIR.glob("feedback_gaia_*.md")):
        text = fp.read_text(encoding="utf-8", errors="replace")
        docs.append({"id": fp.stem, "text": text, "source": "gaia_feedback"})

    # --- Priority 2: GAIA-adjacent benchmark feedback ---
    for fp in sorted(_MEMORY_DIR.glob("feedback_*.md")):
        if "gaia" in fp.stem:
            continue  # already added
        if _should_skip(fp):
            continue
        text = fp.read_text(encoding="utf-8", errors="replace")
        # Trim YAML frontmatter for shorter chunks
        if text.startswith("---"):
            # Keep frontmatter name+description as context, drop body > 2000 chars
            body_start = text.find("---", 3)
            if body_start != -1:
                body = text[body_start + 3:].strip()
                header = text[:body_start + 3]
                text = header + "\n" + body[:2000] if len(body) > 2000 else text
        docs.append({"id": fp.stem, "text": text[:3000], "source": "feedback"})

    # --- Priority 3: kb_benchmark topic files ---
    if _KB_BENCHMARK_DIR.exists():
        for fp in sorted(_KB_BENCHMARK_DIR.glob("*.md")):
            text = fp.read_text(encoding="utf-8", errors="replace")
            docs.append({
                "id": f"kb_benchmark_{fp.stem}",
                "text": text[:3000],
                "source": "kb_benchmark",
            })

    # --- Priority 4: gaia skill SKILL.md ---
    gaia_skill = Path.home() / ".claude" / "skills" / "gaia" / "SKILL.md"
    if gaia_skill.exists():
        docs.append({
            "id": "gaia_skill",
            "text": gaia_skill.read_text(encoding="utf-8", errors="replace")[:3000],
            "source": "gaia_skill",
        })

    return docs


# ---------------------------------------------------------------------------
# ChromaDB index (singleton)
# ---------------------------------------------------------------------------

_collection: Any = None  # chromadb collection
_index_doc_count: int = 0


def _get_collection() -> Any:
    """Return (or build) the ChromaDB collection singleton."""
    global _collection, _index_doc_count
    if _collection is not None:
        return _collection

    try:
        import chromadb  # type: ignore

        _ERL_INDEX_DIR.mkdir(parents=True, exist_ok=True)
        client = chromadb.PersistentClient(path=str(_ERL_INDEX_DIR))
        col = client.get_or_create_collection(
            name="gaia_erl",
            metadata={"hnsw:space": "cosine"},
        )

        # Check if we need to populate
        existing_count = col.count()
        docs = _load_corpus()

        if existing_count < len(docs) * 0.5:
            # Index is empty or significantly stale — rebuild
            logger.info(
                "erl_retriever: rebuilding index (%d docs, %d existing)",
                len(docs),
                existing_count,
            )
            if existing_count > 0:
                # Clear stale entries by deleting all
                all_ids = col.get(limit=existing_count + 1)["ids"]
                if all_ids:
                    col.delete(ids=all_ids)

            # Batch upsert
            ids = [d["id"] for d in docs]
            texts = [d["text"] for d in docs]
            metadatas = [{"source": d["source"]} for d in docs]

            batch_size = 50
            for i in range(0, len(docs), batch_size):
                col.upsert(
                    ids=ids[i:i + batch_size],
                    documents=texts[i:i + batch_size],
                    metadatas=metadatas[i:i + batch_size],
                )
            _index_doc_count = len(docs)
            logger.info("erl_retriever: index built (%d docs)", _index_doc_count)
        else:
            _index_doc_count = existing_count
            logger.debug(
                "erl_retriever: using cached index (%d docs)", _index_doc_count
            )

        _collection = col
        return col

    except Exception as exc:
        logger.warning("erl_retriever: chromadb init failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def retrieve_erl_hints(
    question: str,
    top_k: int = 5,
    min_score: float = 0.25,
) -> list[str]:
    """Retrieve top-k relevant KB experience hints for *question*.

    Returns a list of hint strings (may be empty on failure or no hits).
    Never raises — all errors are swallowed and logged.

    Args:
        question: The GAIA task question text.
        top_k: Maximum number of hints to return.
        min_score: Cosine similarity floor (hits below this are suppressed).
            Acts as Agent KB's "disagreement gate" to avoid injecting
            irrelevant noise.
    """
    try:
        col = _get_collection()
        if col is None:
            return []

        results = col.query(
            query_texts=[question],
            n_results=min(top_k * 2, _index_doc_count or top_k * 2),
            include=["documents", "distances", "metadatas"],
        )

        if not results or not results.get("documents"):
            return []

        docs = results["documents"][0]
        distances = results["distances"][0]
        metadatas = results["metadatas"][0]

        hints: list[str] = []
        for doc, dist, meta in zip(docs, distances, metadatas):
            # ChromaDB hnsw:space=cosine distance = 1 - cosine_similarity,
            # range [0, 2].  Similarity = 1 - dist  (verified 2026-04-27).
            similarity = 1.0 - dist
            if similarity < min_score:
                continue

            # Extract the most useful excerpt: frontmatter name + first body para
            hint = _extract_hint_excerpt(doc, meta.get("source", "kb"))
            if hint:
                hints.append(hint)
            if len(hints) >= top_k:
                break

        return hints

    except Exception as exc:
        logger.warning("erl_retriever: retrieve failed: %s", exc)
        return []


def format_erl_block(hints: list[str]) -> str:
    """Format retrieved hints into a prompt injection block.

    Returns empty string when hints is empty.
    """
    if not hints:
        return ""

    lines = ["[ERL: Relevant experience from KB sediment]"]
    for i, hint in enumerate(hints, 1):
        lines.append(f"• ERL-{i}: {hint}")
    lines.append("[End ERL]")
    return "\n".join(lines)


def rebuild_index() -> int:
    """Force a full index rebuild. Returns number of docs indexed."""
    global _collection, _index_doc_count
    _collection = None  # reset singleton
    _index_doc_count = 0

    # Remove persisted index so _get_collection() rebuilds from scratch
    import shutil
    if _ERL_INDEX_DIR.exists():
        shutil.rmtree(_ERL_INDEX_DIR)

    col = _get_collection()
    return _index_doc_count if col is not None else 0


def index_size() -> int:
    """Return number of docs in the current index (0 if not built)."""
    return _index_doc_count


# ---------------------------------------------------------------------------
# Hint excerpt extraction
# ---------------------------------------------------------------------------

def _extract_hint_excerpt(doc: str, source: str) -> str:
    """Extract a compact hint string from a raw doc."""
    # Try to get frontmatter name + description
    name_match = re.search(r'^name:\s*(.+)$', doc, re.MULTILINE)
    desc_match = re.search(r'^description:\s*(.+)$', doc, re.MULTILINE)

    if name_match and desc_match:
        name = name_match.group(1).strip()[:80]
        desc = desc_match.group(1).strip()[:200]
        return f"[{source}] {name}: {desc}"

    # Fallback: first non-empty non-header line
    for line in doc.splitlines():
        line = line.strip()
        if line and not line.startswith(("#", "---", "|", ">")):
            return f"[{source}] {line[:250]}"

    return ""


# ---------------------------------------------------------------------------
# CLI: build or query index
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="ERL retriever CLI")
    sub = parser.add_subparsers(dest="cmd")

    build_p = sub.add_parser("build", help="Build/rebuild the ERL index")
    build_p.add_argument("--rebuild", action="store_true", help="Force rebuild")

    query_p = sub.add_parser("query", help="Query the index")
    query_p.add_argument("question", help="Question text to retrieve hints for")
    query_p.add_argument("--top-k", type=int, default=5)
    query_p.add_argument("--min-score", type=float, default=0.25)

    args = parser.parse_args()

    if args.cmd == "build":
        if args.rebuild:
            n = rebuild_index()
        else:
            col = _get_collection()
            n = _index_doc_count
        print(f"Index ready: {n} docs at {_ERL_INDEX_DIR}")

    elif args.cmd == "query":
        hints = retrieve_erl_hints(args.question, args.top_k, args.min_score)
        block = format_erl_block(hints)
        print(block if block else "(no hints above threshold)")

    else:
        parser.print_help()
