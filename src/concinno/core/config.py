"""
concinno.core.config — Central configuration loader.

Reads cc_config.json (or uses defaults) and provides typed access to all settings.
All modules import from here instead of hardcoding paths/thresholds.

Usage:
    from concinno.core import get_config
    cfg = get_config("/path/to/hooks")
    workspace = cfg.workspace
    lock_path = cfg.path("instance_lock")
    ttl = cfg.threshold("zombie_seconds")
"""

import json
import logging
import os
from datetime import timedelta, timezone
from typing import Any, Optional

_DEFAULTS = {
    "workspace": "",
    "ai_brain_dir": ".claude-forge/brain",
    "timezone_offset_hours": 8,
    "paths": {
        "instance_lock": ".claude-forge/state/instance_lock.json",
        "learnings": ".claude-forge/memory/evolution/learnings.json",
        "evolution_dir": ".claude-forge/memory/evolution",
        "handoffs_dir": ".claude-forge/handoffs",
        "compact_state": ".claude-forge/memory/compact_state.json",
        "active_tasks": ".claude-forge/memory/active_tasks.md",
    },
    "identity": {
        "enabled": True,
        "base_narrative": "",  # empty = built-in default; override for custom identity
    },
    "ts_projects": [],
    "thresholds": {
        "zombie_seconds": 1800,
        "max_tracked_files": 30,
        "tidy_md_lines": 8,
        "tidy_code_lines": 40,
        "sentinel_repeat": 3,
        "sentinel_error": 2,
        "sentinel_stale": 2,
        "sentinel_paralysis": 7,
        "sentinel_scope": 10,
        "token_bytes_per_token": 5,
        "system_prompt_est_tokens": 8000,
        "tsc_timeout": 15,
        "max_transcript_size_mb": 10,
        "max_corrections_per_session": 10,
        "max_learnings_stored": 50,
        "learnings_promote_threshold": 3,
        "default_token_budget": 40000,
        "scores_max_entries": 50,
    },
    "stop_guard": {
        "enabled": True,
        "completion_keywords": [],
        "question_keywords": [],
        "continuation_patterns": [],
        "pending_patterns": [],
    },
    "token_warnings": [
        {"threshold": 180000, "icon": "🆘", "message": "Write handoff immediately"},
        {"threshold": 140000, "icon": "🔴", "message": "Finish current task, then handoff"},
        {"threshold": 100000, "icon": "ℹ️", "message": "Informational, no action needed"},
        {"threshold": 60000, "icon": "ℹ️", "message": "Informational, no action needed"},
    ],
    "streak_ux": {
        "streak_fmt": "\U0001f525x{streak} clean edits",
        "fix_fmt": "\u2705 {fname} fixed {fixed}/{total} | \U0001f525x{streak} clean edits",
    },
    # ── Feature toggles (all default ON, all configurable) ──
    "features": {
        # Hard gates (PreToolUse deny)
        "token_gate": {
            "enabled": True,
            "mode": "step_back_first",
            "agent_threshold": 140000,
            "critical_threshold": 160000,
        },
        "read_first_gate": {
            "enabled": True,
            "mode": "step_back_first",
            "min_lines": 50,
        },
        "agent_cap": {
            "enabled": True,
            "mode": "step_back_first",
            "max_spawns": 5,
        },
        "sentinel_gate": {
            "enabled": True,
            "mode": "step_back_first",
            "max_repeats": 5,
            "lint_exception": True,
        },
        "consecutive_fail_gate": {
            "enabled": True,
            "mode": "step_back_first",
            "max_fails": 3,
        },
        "hijack_gate": {
            "enabled": True,
            "mode": "hard_deny",
            "l2_threshold": 0.3,
            "l3_threshold": 0.6,
            "l4_threshold": 0.8,
        },
        "identity_guard": {"enabled": True, "mode": "hard_deny"},
        "publish_scan": {"enabled": True, "mode": "hard_deny"},
        "delivery_gate": {"enabled": True, "mode": "hard_deny", "max_iterations": 5},
        "bash_background_gate": {"enabled": True, "mode": "step_back_first"},
        "python_c_gate": {"enabled": True, "mode": "step_back_first"},
        "whitepaper_guard": {"enabled": True, "mode": "hard_deny"},
        "clarity_gate": {"enabled": True, "mode": "step_back_first", "min_clarity": 0.4},
        # Hard quality (PostToolUse lint-level)
        "code_guard": {"enabled": True},
        "typescript": {"enabled": True},
        "linting": {"enabled": True},
        "handoff_format": {"enabled": True},
        # UX (user-visible)
        "streak_ux": {
            "enabled": True,
            "milestone_interval": 5,
        },
        "session_summary": {"enabled": True},
        "deny_marker": {"enabled": True},
        # Boundary guard (hard gate)
        "boundary_guard": {"enabled": True},
        # Language enforcement
        "language_enforce": {
            "enabled": True,
            "language": "English",
        },
    },
    "deny_stderr": True,
    "lint_rules": [],
    "shared_basenames": ["MEMORY.md", "knowledge_base.md"],
    "shared_paths": [],
    "handoff_prefixes": [],
    "notification": {
        "sound_file": "notify.mp3",
        "toast_app_id": "Microsoft.VisualStudioCode",
        "toast_title": "Claude Code",
        "toast_enabled": False,
        "toast_tag": "concinno",
        "toast_group": "concinno",
    },
}


class Config:
    """Lazy-loaded, cached configuration from cc_config.json."""

    def __init__(self, hooks_dir: Optional[str] = None, config_path: Optional[str] = None):
        self._data: Optional[dict] = None
        self._hooks_dir = hooks_dir or ""
        self._config_path = config_path

    def _load(self):
        if self._data is not None:
            return
        self._data = dict(_DEFAULTS)
        config_file = self._config_path or (
            os.path.join(self._hooks_dir, "cc_config.json") if self._hooks_dir else ""
        )
        if config_file and os.path.isfile(config_file):
            try:
                with open(config_file, "r", encoding="utf-8") as f:
                    user = json.load(f)
                for k, v in user.items():
                    if isinstance(v, dict) and isinstance(self._data.get(k), dict):
                        self._data[k] = {**self._data[k], **v}
                    else:
                        self._data[k] = v
            except Exception:
                pass

    @property
    def workspace(self) -> str:
        self._load()
        return os.path.normpath(self._data["workspace"])

    @property
    def brain_dir(self) -> str:
        """The AI brain directory name (default: '_AI_BRAIN', configurable via ai_brain_dir)."""
        self._load()
        return self._data.get("ai_brain_dir", ".claude-forge/brain")

    @property
    def tz(self) -> timezone:
        self._load()
        return timezone(timedelta(hours=self._data["timezone_offset_hours"]))

    @property
    def hooks_dir(self) -> str:
        return self._hooks_dir

    def path(self, key: str) -> str:
        """Resolve a relative path key to absolute path under workspace."""
        self._load()
        rel = self._data.get("paths", {}).get(key, "")
        if not rel:
            return ""
        return os.path.normpath(os.path.join(self.workspace, rel))

    def abs_path(self, relative: str) -> str:
        """Resolve any relative path against workspace."""
        return os.path.normpath(os.path.join(self.workspace, relative))

    def threshold(self, key: str, default: Any = None) -> Any:
        """Get a threshold value by key."""
        self._load()
        return self._data.get("thresholds", {}).get(key, default)

    @property
    def shared_basenames(self) -> frozenset:
        self._load()
        return frozenset(self._data.get("shared_basenames", []))

    @property
    def shared_paths(self) -> frozenset:
        self._load()
        return frozenset(self._data.get("shared_paths", []))

    @property
    def handoff_prefixes(self) -> tuple:
        self._load()
        return tuple(self._data.get("handoff_prefixes", []))

    @property
    def ts_projects(self) -> list:
        """Returns list of (abs_path, name) tuples, sorted longest path first."""
        self._load()
        result = []
        for p in self._data.get("ts_projects", []):
            abs_p = os.path.normpath(os.path.join(self.workspace, p["path"]))
            result.append((abs_p, p["name"]))
        return sorted(result, key=lambda x: len(x[0]), reverse=True)

    @property
    def token_warnings(self) -> list:
        """Returns list of (threshold, icon, message) tuples, sorted descending."""
        self._load()
        result = []
        for w in self._data.get("token_warnings", []):
            result.append((w["threshold"], w["icon"], w["message"]))
        return sorted(result, key=lambda x: x[0], reverse=True)

    @property
    def sound_file(self) -> str:
        self._load()
        fname = self._data.get("notification", {}).get("sound_file", "notify.mp3")
        return os.path.join(self._hooks_dir, fname)

    @property
    def toast_app_id(self) -> str:
        self._load()
        return self._data.get("notification", {}).get("toast_app_id", "Microsoft.VisualStudioCode")

    @property
    def toast_title(self) -> str:
        self._load()
        return self._data.get("notification", {}).get("toast_title", "Claude Code")

    @property
    def toast_enabled(self) -> bool:
        self._load()
        return self._data.get("notification", {}).get("toast_enabled", False)

    @property
    def toast_tag(self) -> str:
        self._load()
        return self._data.get("notification", {}).get("toast_tag", "concinno")

    @property
    def toast_group(self) -> str:
        self._load()
        return self._data.get("notification", {}).get("toast_group", "concinno")

    @property
    def config_file_path(self) -> str:
        """Resolved path to the cc_config.json file."""
        return self._config_path or (
            os.path.join(self._hooks_dir, "cc_config.json") if self._hooks_dir else ""
        )

    def feature(self, name: str, key: str = "enabled") -> Any:
        """Get a feature config value. e.g. feature("agent_cap", "max_spawns")."""
        self._load()
        feat = self._data.get("features", {}).get(name, {})
        if key == "enabled":
            return feat.get("enabled", True)
        return feat.get(key)

    def feature_all(self, name: str) -> dict:
        """Get full feature config dict."""
        self._load()
        return dict(self._data.get("features", {}).get(name, {}))

    def set_feature(self, name: str, key: str, value: Any) -> None:
        """Update a feature config value and persist to disk.

        Use feature_config.validate() first for risk warnings.
        """
        self._load()
        features = self._data.setdefault("features", {})
        feat = features.setdefault(name, {})
        feat[key] = value
        self.update_file("features", features)

    def raw(self, key: str, default: Any = None) -> Any:
        """Get any top-level config value."""
        self._load()
        return self._data.get(key, default)

    def update_file(self, key: str, value: Any) -> None:
        """Merge *key* into the on-disk cc_config.json and refresh cache.

        Reads the existing JSON (if any), sets ``data[key] = value``,
        writes back, and invalidates the in-memory cache so the next
        property access picks up the change.
        """
        path = self.config_file_path
        if not path:
            return
        data: dict = {}
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                pass
        data[key] = value
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        # Invalidate cache so next access re-reads
        self._data = None


# Convenience constant — default local timezone (UTC+8)
LOCAL_TZ = timezone(timedelta(hours=_DEFAULTS["timezone_offset_hours"]))

# Module-level factory
_instance: Optional[Config] = None


def get_config(hooks_dir: Optional[str] = None, config_path: Optional[str] = None) -> Config:
    """Get or create the singleton Config instance.

    If called with different arguments after initial creation, logs a warning
    and returns the existing instance (singleton contract).
    """
    global _instance
    if _instance is None:
        _instance = Config(hooks_dir=hooks_dir, config_path=config_path)
    else:
        # Warn if caller passes different params than the existing singleton
        if hooks_dir and hooks_dir != _instance._hooks_dir:
            logging.getLogger("concinno").warning(
                "get_config() called with hooks_dir=%r but singleton already uses %r",
                hooks_dir,
                _instance._hooks_dir,
            )
        if config_path and config_path != _instance._config_path:
            logging.getLogger("concinno").warning(
                "get_config() called with config_path=%r but singleton already uses %r",
                config_path,
                _instance._config_path,
            )
    return _instance


def reset_config() -> None:
    """Reset the singleton instance. Primarily for testing."""
    global _instance
    _instance = None
