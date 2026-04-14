"""Anti-bloat module — hit tracking, stale detection, and pruning.

Extracted from rag.py for cross-project import.
Follows RAGSpec anti-bloat standard.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta
from typing import Callable


# Protection levels for GDPR compliance
class ProtectionLevel:
    SYSTEM = "system"  # System infrastructure, never delete
    USER_DATA = "user"  # User data, GDPR deletion can override
    AUDIT = "audit"  # Audit data, legal retention period applies


class HitTracker:
    """Track which files/documents are being accessed."""

    def __init__(self, cache_dir: str):
        self.cache_dir = cache_dir
        self.hits_path = os.path.join(cache_dir, "hit_log.json")

    def record_hits(self, files: list[str]) -> None:
        hits = self._load_hits()
        now = time.strftime("%Y-%m-%d")
        for file in files:
            entry = hits.get(file, {"count": 0, "first_hit": now, "last_hit": now})
            entry["count"] = entry.get("count", 0) + 1
            entry["last_hit"] = now
            hits[file] = entry
        self._save_hits(hits)

    def _load_hits(self) -> dict:
        if os.path.isfile(self.hits_path):
            try:
                with open(self.hits_path, encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _save_hits(self, hits: dict) -> None:
        try:
            os.makedirs(self.cache_dir, exist_ok=True)
            with open(self.hits_path, "w", encoding="utf-8") as f:
                json.dump(hits, f, indent=2, ensure_ascii=False)
        except Exception:
            pass


class StaleDetector:
    """Detect stale documents that haven't been accessed."""

    def __init__(self, cache_dir: str, stale_days: int = 90):
        self.cache_dir = cache_dir
        self.stale_days = stale_days
        self.hit_tracker = HitTracker(cache_dir)

    def report(self, days: int | None = None, hash_path: str | None = None) -> dict:
        days = days or self.stale_days
        hits = self.hit_tracker._load_hits()

        # Get all indexed files from hash log
        hp = hash_path or os.path.join(self.cache_dir, "file_hashes.json")
        all_files: dict = {}
        if os.path.isfile(hp):
            try:
                with open(hp, encoding="utf-8") as f:
                    all_files = json.load(f)
            except Exception:
                pass

        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        stale = []
        active = []

        for file in all_files:
            entry = hits.get(file)
            if not entry or entry.get("last_hit", "1970-01-01") < cutoff:
                stale.append(
                    {"file": file, "last_hit": entry.get("last_hit") if entry else None}
                )
            else:
                active.append(
                    {"file": file, "hits": entry["count"], "last_hit": entry["last_hit"]}
                )

        total = len(all_files)
        return {
            "stale": sorted(stale, key=lambda x: x.get("last_hit") or ""),
            "active": sorted(active, key=lambda x: -x["hits"]),
            "total_files": total,
            "stale_count": len(stale),
            "stale_ratio": round(len(stale) / total, 2) if total > 0 else 0,
        }


# Default protection callback
def default_protection(metadata: dict) -> bool:
    """Check if a document is protected from pruning."""
    if metadata.get("protected") is True:
        return True
    if metadata.get("type") in ("foundation", "persona_definition"):
        return True
    if metadata.get("importance") == "P0":
        return True
    if metadata.get("protection_level") == ProtectionLevel.SYSTEM:
        return True
    return False


class Pruner:
    """Prune stale documents respecting protection rules."""

    def __init__(
        self,
        cache_dir: str,
        stale_days: int = 90,
        protected_callback: Callable[[dict], bool] | None = None,
    ):
        self.detector = StaleDetector(cache_dir, stale_days)
        self.protected_callback = protected_callback or default_protection

    def prune(
        self,
        collection,  # ChromaDB collection or compatible
        days: int | None = None,
        dry_run: bool = True,
    ) -> dict:
        report = self.detector.report(days=days)
        stale_files = [s["file"] for s in report["stale"]]

        if dry_run or not stale_files:
            return {
                "dry_run": dry_run,
                "would_prune": stale_files,
                "count": len(stale_files),
            }

        pruned = []
        skipped = []
        chunks_removed = 0

        for file in stale_files:
            try:
                existing = collection.get(where={"file": file})
                if not existing or not existing.get("ids"):
                    continue

                # Check protection for each document
                metadatas = existing.get("metadatas", [])
                if metadatas and any(self.protected_callback(m) for m in metadatas):
                    skipped.append(file)
                    continue

                count = len(existing["ids"])
                collection.delete(where={"file": file})
                chunks_removed += count
                pruned.append(file)
            except Exception:
                pass

        return {
            "dry_run": False,
            "pruned": pruned,
            "skipped_protected": skipped,
            "count": len(pruned),
            "chunks_removed": chunks_removed,
        }
