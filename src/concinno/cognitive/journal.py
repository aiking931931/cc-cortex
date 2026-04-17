"""Decision journal — record AI decisions and their outcomes.

@module cognitive.journal
@responsibility Track decisions, quality scores, weak spots
@dependencies cognitive._base
"""

from __future__ import annotations

import hashlib
import os
import time
from datetime import datetime, timezone
from typing import Optional

from ._base import JOURNAL_FILE, MAX_JOURNAL_ENTRIES, cognitive_dir, read_json, write_json


class DecisionJournal:
    """Records AI decisions and their outcomes for self-improvement.

    Tracks what the AI decided to do, why, and whether the outcome was
    positive (user accepted) or negative (user corrected). Over time,
    builds a decision quality profile that can inform future behavior.
    """

    def __init__(self, base_dir: Optional[str] = None):
        self._dir = cognitive_dir(base_dir)
        self._path = os.path.join(self._dir, JOURNAL_FILE)

    def record(
        self,
        session_id: str,
        decision_type: str,
        context: str,
        action: str,
        confidence: float = 0.5,
        tags: Optional[list[str]] = None,
    ) -> str:
        """Record a decision.

        Args:
            session_id: Current session ID.
            decision_type: Category (e.g., "tool_choice", "file_edit", "approach").
            context: What prompted the decision.
            action: What was decided.
            confidence: AI's confidence in the decision (0.0-1.0).
            tags: Optional tags for categorization.

        Returns:
            Decision ID (8-char hash).
        """
        decision_id = hashlib.sha256(
            f"{session_id}:{decision_type}:{action}:{time.time()}".encode()
        ).hexdigest()[:8]

        entry = {
            "id": decision_id,
            "session_id": session_id[:16] if session_id else "",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "decision_type": decision_type,
            "context": context[:300],
            "action": action[:300],
            "confidence": round(confidence, 2),
            "outcome": None,  # Filled later by record_outcome
            "tags": tags or [],
        }

        data = read_json(self._path)
        entries = data.get("entries", [])
        entries.append(entry)

        # Trim
        if len(entries) > MAX_JOURNAL_ENTRIES:
            entries = entries[-MAX_JOURNAL_ENTRIES:]

        data["entries"] = entries
        data["last_updated"] = entry["timestamp"]
        write_json(self._path, data)
        return decision_id

    def record_outcome(
        self,
        decision_id: str,
        outcome: str,
        user_feedback: Optional[str] = None,
    ) -> bool:
        """Record the outcome of a previous decision.

        Args:
            decision_id: The ID returned by record().
            outcome: "accepted", "corrected", "reverted", or "ignored".
            user_feedback: Optional user feedback text.

        Returns:
            True if decision was found and updated.
        """
        data = read_json(self._path)
        entries = data.get("entries", [])

        for entry in reversed(entries):  # Search from newest
            if entry.get("id") == decision_id:
                entry["outcome"] = outcome
                if user_feedback:
                    entry["user_feedback"] = user_feedback[:200]
                entry["outcome_time"] = datetime.now(timezone.utc).isoformat()
                write_json(self._path, data)
                return True
        return False

    def get_quality_score(self, decision_type: Optional[str] = None, limit: int = 50) -> float:
        """Calculate decision quality score (0.0-1.0).

        Args:
            decision_type: Filter by type (None = all).
            limit: Number of recent decisions to consider.

        Returns:
            Quality score where 1.0 = all accepted, 0.0 = all corrected.
        """
        data = read_json(self._path)
        entries = data.get("entries", [])

        if decision_type:
            entries = [e for e in entries if e.get("decision_type") == decision_type]

        # Only consider entries with outcomes
        scored = [e for e in entries[-limit:] if e.get("outcome")]
        if not scored:
            return 0.5  # No data = neutral

        weights = {"accepted": 1.0, "ignored": 0.7, "corrected": 0.0, "reverted": 0.0}
        total = sum(weights.get(e["outcome"], 0.5) for e in scored)
        return round(total / len(scored), 3)

    def get_weak_spots(self, threshold: float = 0.4, min_entries: int = 3) -> list[dict]:
        """Find decision types where quality is consistently low.

        Returns:
            List of {decision_type, quality, count} for weak areas.
        """
        data = read_json(self._path)
        entries = data.get("entries", [])

        # Group by decision_type
        by_type: dict[str, list] = {}
        for e in entries:
            dt = e.get("decision_type", "unknown")
            by_type.setdefault(dt, []).append(e)

        weak: list[dict] = []
        for dt, dt_entries in by_type.items():
            scored = [e for e in dt_entries if e.get("outcome")]
            if len(scored) < min_entries:
                continue
            weights = {"accepted": 1.0, "ignored": 0.7, "corrected": 0.0, "reverted": 0.0}
            quality = sum(weights.get(e["outcome"], 0.5) for e in scored) / len(scored)
            if quality < threshold:
                weak.append(
                    {
                        "decision_type": dt,
                        "quality": round(quality, 3),
                        "count": len(scored),
                    }
                )

        weak.sort(key=lambda x: x["quality"])
        return weak

    def get_recent(self, limit: int = 10) -> list[dict]:
        """Get recent journal entries."""
        data = read_json(self._path)
        entries = data.get("entries", [])
        return entries[-limit:]

    def stats(self) -> dict:
        """Get journal statistics."""
        data = read_json(self._path)
        entries = data.get("entries", [])
        total = len(entries)
        scored = [e for e in entries if e.get("outcome")]
        outcomes: dict[str, int] = {}
        for e in scored:
            o = e.get("outcome", "unknown")
            outcomes[o] = outcomes.get(o, 0) + 1
        return {
            "total_decisions": total,
            "scored_decisions": len(scored),
            "outcomes": outcomes,
            "quality_score": self.get_quality_score(),
            "weak_spots": self.get_weak_spots(),
        }
