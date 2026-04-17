"""Shared constants and helpers for the cognitive package.

@module cognitive._base
@responsibility Constants, file paths, JSON I/O for cognitive subsystem
@dependencies None (stdlib only)
"""

from __future__ import annotations

import json
import os
from typing import Optional

# ── Constants ──────────────────────────────────────────────

COGNITIVE_DIR_NAME = "cognitive"
PROFILE_FILE = "session_profiles.json"
JOURNAL_FILE = "decision_journal.json"
THRESHOLDS_FILE = "adaptive_thresholds.json"

# Session type classification
SESSION_TYPES = {
    "bugfix": {"signals": ["fix", "bug", "error", "broken", "fail", "issue", "debug"]},
    "feature": {"signals": ["add", "new", "create", "implement", "build", "feature"]},
    "refactor": {"signals": ["refactor", "clean", "rename", "move", "extract", "simplify"]},
    "review": {"signals": ["review", "check", "audit", "inspect", "look", "read"]},
    "config": {"signals": ["config", "setup", "install", "deploy", "ci", "env"]},
    "docs": {"signals": ["doc", "readme", "comment", "explain", "describe"]},
    "test": {"signals": ["test", "spec", "coverage", "assert", "mock"]},
}

# File domain classification (first path component → domain)
FILE_DOMAINS = {
    "src": "source",
    "lib": "source",
    "app": "source",
    "test": "test",
    "tests": "test",
    "spec": "test",
    "docs": "docs",
    "config": "config",
    ".github": "ci",
    ".claude": "ai-config",
    "scripts": "tooling",
}

# Default adaptive thresholds (sentinel/guard baselines)
DEFAULT_THRESHOLDS = {
    "sentinel_repeat": 3,
    "sentinel_paralysis": 7,
    "sentinel_scope": 10,
    "sentinel_drift": 4,
    "sentinel_diminish": 3,
    "tidy_md_lines": 8,
    "tidy_code_lines": 40,
    "destruction_confirm_level": 2,  # R2+ requires confirmation
}

# Threshold adjustment bounds (min, max) to prevent runaway adaptation
THRESHOLD_BOUNDS = {
    "sentinel_repeat": (2, 8),
    "sentinel_paralysis": (4, 15),
    "sentinel_scope": (5, 25),
    "sentinel_drift": (2, 8),
    "sentinel_diminish": (2, 6),
    "tidy_md_lines": (4, 20),
    "tidy_code_lines": (15, 80),
    "destruction_confirm_level": (1, 4),
}

MAX_PROFILES = 200
MAX_JOURNAL_ENTRIES = 500


# ── Helpers ────────────────────────────────────────────────


def cognitive_dir(base_dir: Optional[str] = None) -> str:
    """Get cognitive data directory."""
    if base_dir:
        return base_dir
    return os.path.join(os.path.expanduser("~"), ".claude", COGNITIVE_DIR_NAME)


def read_json(path: str) -> dict:
    """Read JSON file, return empty dict on failure."""
    try:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                data: dict = json.load(f)
                return data
    except Exception:
        pass
    return {}


def write_json(path: str, data: dict) -> bool:
    """Write JSON file atomically."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
        return True
    except Exception:
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            return True
        except Exception:
            return False


def classify_file_domain(file_path: str) -> str:
    """Classify a file path into a domain category."""
    if not file_path:
        return "unknown"
    normalized = file_path.replace("\\", "/").lstrip("/")
    parts = normalized.split("/")
    for part in parts[:3]:  # Check first 3 path components
        low = part.lower()
        if low in FILE_DOMAINS:
            return FILE_DOMAINS[low]
    # Fallback: extension-based
    low_path = file_path.lower()
    ext_map = {
        ".md": "docs",
        ".json": "config",
        ".yaml": "config",
        ".yml": "config",
        ".toml": "config",
        ".ini": "config",
        ".env": "config",
        ".test.ts": "test",
        ".test.js": "test",
        ".spec.ts": "test",
    }
    for suffix, domain in ext_map.items():
        if low_path.endswith(suffix):
            return domain
    return "source"
