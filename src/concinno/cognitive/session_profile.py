"""Session profiling — track and classify session work patterns.

@module cognitive.session_profile
@responsibility Classify session type, track work patterns, file domains
@dependencies concinno.constants, cognitive._base
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Optional

from concinno.constants import READ_TOOLS, WRITE_TOOLS

from ._base import (
    MAX_PROFILES,
    PROFILE_FILE,
    SESSION_TYPES,
    classify_file_domain,
    cognitive_dir,
    read_json,
    write_json,
)


class SessionProfile:
    """Tracks and classifies session work patterns.

    Builds a profile of each session: what type of work, which file domains,
    read/write ratio, tool distribution. Used to adapt thresholds and
    detect anomalies.
    """

    def __init__(self, session_id: str, base_dir: Optional[str] = None):
        self.session_id = session_id
        self.short_id = session_id[:8] if session_id else "unknown"
        self._dir = cognitive_dir(base_dir)
        self.start_time = time.time()
        self.tool_counts: dict[str, int] = {}
        self.file_domains: dict[str, int] = {}
        self.files_touched: set[str] = set()
        self.session_type: str = "unknown"
        self.user_messages: list[str] = []

    def record_tool(self, tool_name: str, tool_input: dict) -> None:
        """Record a tool invocation."""
        self.tool_counts[tool_name] = self.tool_counts.get(tool_name, 0) + 1

        file_path = ""
        if isinstance(tool_input, dict):
            file_path = (
                tool_input.get("file_path")
                or tool_input.get("path")
                or tool_input.get("notebook_path")
                or ""
            )

        if file_path:
            self.files_touched.add(file_path)
            domain = classify_file_domain(file_path)
            self.file_domains[domain] = self.file_domains.get(domain, 0) + 1

    def record_user_message(self, text: str) -> None:
        """Record a user message for session type classification."""
        if text and len(text) < 1000:
            self.user_messages.append(text[:200])

    def classify(self) -> str:
        """Classify the session type based on accumulated signals."""
        if not self.user_messages:
            self.session_type = "unknown"
            return self.session_type

        combined = " ".join(self.user_messages).lower()
        scores: dict[str, int] = {}

        for stype, info in SESSION_TYPES.items():
            score = sum(1 for sig in info["signals"] if sig in combined)
            if score > 0:
                scores[stype] = score

        if scores:
            self.session_type = max(scores, key=scores.get)  # type: ignore[arg-type]
        else:
            self.session_type = "general"
        return self.session_type

    @property
    def read_write_ratio(self) -> float:
        """Calculate read/write tool ratio. >1 means more reading."""
        reads = sum(self.tool_counts.get(t, 0) for t in READ_TOOLS)
        writes = sum(self.tool_counts.get(t, 0) for t in WRITE_TOOLS)
        if writes == 0:
            return float(reads) if reads > 0 else 0.0
        return reads / writes

    @property
    def duration_seconds(self) -> float:
        """Session duration in seconds."""
        return time.time() - self.start_time

    def to_dict(self) -> dict:
        """Serialize profile for storage."""
        return {
            "session_id": self.session_id,
            "short_id": self.short_id,
            "session_type": self.session_type or self.classify(),
            "start_time": self.start_time,
            "duration_seconds": round(self.duration_seconds, 1),
            "tool_counts": dict(self.tool_counts),
            "file_domains": dict(self.file_domains),
            "files_touched_count": len(self.files_touched),
            "read_write_ratio": round(self.read_write_ratio, 2),
            "user_message_count": len(self.user_messages),
        }

    def save(self) -> bool:
        """Save profile to persistent storage."""
        path = os.path.join(self._dir, PROFILE_FILE)
        data = read_json(path)
        profiles = data.get("profiles", [])

        # Update or append
        existing = next((p for p in profiles if p.get("session_id") == self.session_id), None)
        profile_dict = self.to_dict()
        if existing:
            profiles[profiles.index(existing)] = profile_dict
        else:
            profiles.append(profile_dict)

        # Trim to max
        if len(profiles) > MAX_PROFILES:
            profiles = profiles[-MAX_PROFILES:]

        data["profiles"] = profiles
        data["last_updated"] = datetime.now(timezone.utc).isoformat()
        return write_json(path, data)

    @staticmethod
    def load_history(base_dir: Optional[str] = None, limit: int = 50) -> list[dict]:
        """Load recent session profiles."""
        path = os.path.join(cognitive_dir(base_dir), PROFILE_FILE)
        data = read_json(path)
        profiles = data.get("profiles", [])
        return profiles[-limit:]

    @staticmethod
    def get_type_distribution(base_dir: Optional[str] = None, limit: int = 50) -> dict[str, int]:
        """Get distribution of session types from recent history."""
        profiles = SessionProfile.load_history(base_dir, limit)
        dist: dict[str, int] = {}
        for p in profiles:
            stype = p.get("session_type", "unknown")
            dist[stype] = dist.get(stype, 0) + 1
        return dist
