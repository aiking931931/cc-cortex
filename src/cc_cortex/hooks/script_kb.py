#!/usr/bin/env python3
"""cc-cortex Script Skill Check — remind to consult kb_* Skills when writing scripts.

Triggers only when ALL conditions are met:
  1. Tool is a write/edit operation
  2. Target file is under a scripts/ directory
  3. File content contains specific API/library keywords

Each keyword group maps to a specific kb_* Skill, so only the relevant
hint is shown — no blanket reminders.
"""

from __future__ import annotations

from typing import Any

from cc_cortex.constants import WRITE_TOOLS_EXT as _WRITE_TOOLS
from cc_cortex.core.path_utils import extract_file_path as _extract_file_path

# ── Skill Section Registry ──────────────────────────────────────

KB_SECTIONS: dict[str, dict[str, Any]] = {
    "book": {
        "keywords": [
            "python-docx",
            "Document(",
            "OxmlElement",
            "qn(",
            "add_paragraph",
        ],
        "kb_hint": "/kb_word",
    },
    "image": {
        "keywords": [
            "fal.ai",
            "fal-ai",
            "flux",
            "pulid",
            "FalGuard",
            "fal_client",
            "kontext",
        ],
        "kb_hint": "/kb_image",
    },
    "face": {
        "keywords": [
            "DeepFace",
            "faiss",
            "face_index",
            "Facenet",
            "embedding",
        ],
        "kb_hint": "/kb_image",
    },
    "deploy": {
        "keywords": [
            "paramiko",
            "SSHClient",
            "sftp",
            "docker",
        ],
        "kb_hint": "/kb_deploy",
    },
    "translate": {
        "keywords": [
            "genai",
            "gemini",
            "translate",
            "翻譯",
        ],
        "kb_hint": "/kb_audio",
    },
    "dance": {
        "keywords": [
            "motion-control",
            "kling-video",
            "dance",
            "Motion Control",
        ],
        "kb_hint": "/kb_dance",
    },
}


def _is_scripts_dir(file_path: str) -> bool:
    """Check if file is under a scripts/ directory."""
    # Normalize separators for cross-platform
    normalized = file_path.replace("\\", "/")
    return "/scripts/" in normalized or normalized.startswith("scripts/")


def _extract_content(tool_input: dict) -> str:
    """Extract writable content from tool input for keyword matching."""
    parts: list[str] = []
    # Write tool
    if "content" in tool_input:
        parts.append(tool_input["content"])
    # Edit tool
    if "new_string" in tool_input:
        parts.append(tool_input["new_string"])
    if "old_string" in tool_input:
        parts.append(tool_input["old_string"])
    return "\n".join(parts)


def _match_sections(content: str) -> list[str]:
    """Return Skill hints for all sections whose keywords appear in content."""
    hints: list[str] = []
    for section_name, section in KB_SECTIONS.items():
        for kw in section["keywords"]:
            if kw in content:
                hints.append(section["kb_hint"])
                break  # One match per section is enough
    return hints


def check_script_kb(tool_input: dict, tool_name: str) -> list[str]:
    """Check if a script write/edit should trigger a KB reminder.

    Trigger conditions (ALL must be met):
      1. tool_name is a write/edit tool
      2. file_path is under scripts/
      3. Content contains keywords from at least one KB section

    Args:
        tool_input: The tool's input dictionary.
        tool_name: The Claude Code tool name (Write, Edit, etc.).

    Returns:
        List of warning/reminder messages. Empty list means no trigger.
    """
    if tool_name not in _WRITE_TOOLS:
        return []

    file_path = _extract_file_path(tool_input)
    if not file_path or not _is_scripts_dir(file_path):
        return []

    content = _extract_content(tool_input)
    if not content:
        return []

    matched_hints = _match_sections(content)
    if not matched_hints:
        return []

    # Build concise reminder (one line per matched section)
    warnings: list[str] = []
    hint_list = " | ".join(matched_hints)
    warnings.append(f"[Script Skill] Check before proceeding: {hint_list}")
    return warnings
