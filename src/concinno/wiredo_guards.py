"""concinno.wiredo_guards — WIREDO checklist injection + hard enforcement.

Merged from wiredo_guard + wiredo_enforcement (module consolidation Phase 1).

@module wiredo_guards
@responsibility
    1. Inject asset-type-specific WIREDO checklist on first relevant tool call
       (WiredoGuard — PreToolUse context injection).
    2. Hard deny handoff/report writes without WIREDO table when code was edited
       (WiredoEnforcementGuard — PostToolUse DENY).
@dependencies concinno.guards.base, concinno.asset_validator,
    concinno.delivery.wiredo
@exports WiredoGuard, WiredoEnforcementGuard
"""

from __future__ import annotations

import os
import re
from typing import Optional

from concinno.asset_validator import (
    AssetType,
    detect_asset_type,
    is_asset_type_enabled,
    load_wiredo_config,
)
from concinno.guards.base import BaseGuard, GuardCategory, GuardContext, GuardResult

# ══════════════════════════════════════════════════════════════════
# Part 1: WiredoGuard — Checklist injection (from wiredo_guard.py)
# ══════════════════════════════════════════════════════════════════

# Tools that indicate a code task
_CODE_TOOLS = {"Edit", "Write", "NotebookEdit"}

# Tools that handle media/assets
_MEDIA_TOOLS = {"Bash"}

# API patterns that indicate image generation
_IMAGE_API_PATTERNS = (
    "fal-ai/", "fal.run/", "kontext", "text-to-image",
    "image-to-image", "flux", "stable-diffusion",
)

# API patterns for video generation
_VIDEO_API_PATTERNS = (
    "kling", "runway", "pika", "hedra", "luma",
    "video-generation", "motion-control",
)

# API patterns for audio generation
_AUDIO_API_PATTERNS = (
    "suno", "elevenlabs", "tts", "text-to-speech",
    "bark", "whisper",
)

# Patterns for document operations
_DOC_PATTERNS = ("word_create", "word_append", "word_replace", "python-docx")

# API patterns for protocol layer operations
_PROTOCOL_API_PATTERNS = (
    "grpc", "websocket", "mqtt", "amqp", "redis.*pub",
    "nats", "transport", "registry",
)


def _detect_task_type(ctx: GuardContext) -> AssetType | None:
    """Detect what asset type the current tool call is working with."""
    tool = ctx.tool_name
    tool_input = ctx.tool_input

    # Code tools -> detect from file extension first
    if tool in _CODE_TOOLS:
        file_path = tool_input.get("file_path", "")
        detected = detect_asset_type(file_path)
        if detected:
            return detected
        return AssetType.CODE

    # Bash -> check command content
    if tool == "Bash":
        cmd = tool_input.get("command", "")
        cmd_lower = cmd.lower()

        if any(p in cmd_lower for p in _IMAGE_API_PATTERNS):
            return AssetType.IMAGE
        if any(p in cmd_lower for p in _VIDEO_API_PATTERNS):
            return AssetType.VIDEO
        if any(p in cmd_lower for p in _AUDIO_API_PATTERNS):
            return AssetType.AUDIO
        if any(p in cmd_lower for p in _DOC_PATTERNS):
            return AssetType.DOCUMENT
        if any(p in cmd_lower for p in _PROTOCOL_API_PATTERNS):
            return AssetType.PROTOCOL
        # ffprobe/ffmpeg -> VIDEO or AUDIO
        if "ffprobe" in cmd_lower or "ffmpeg" in cmd_lower:
            return AssetType.VIDEO

    # MCP tools -> DOCUMENT
    if tool.startswith("mcp__word"):
        return AssetType.DOCUMENT

    return None


def _build_checklist(asset_type: AssetType) -> str:
    """Build the appropriate WIREDO checklist for the asset type."""
    _checklists = {
        AssetType.CODE: (
            "📋 **WIREDO Delivery Checklist — CODE**\n"
            "```\n"
            "□ W — Wired: Who imports/calls this? Deleting it causes errors?\n"
            "□ I — Inherited & Aligned: Uses base template? Correct module?\n"
            "□ R — Responsive & Performant: No O(n²)/N+1/unnecessary blocking?\n"
            "□ E — Extensible: Constants at top? Config interface?\n"
            "□ D — Defended: FUNCTIONAL verification (runs, does what it should).\n"
            "       tsc/lint = prerequisite, not D. Visual verify if UI/frontend changed.\n"
            "       Can't verify now? → defer to milestone.\n"
            "□ O — Observable: stats/log/metrics? (non-SaaS → N/A)\n"
            "```"
        ),
        AssetType.IMAGE: (
            "📋 **WIREDO Delivery Checklist — IMAGE**\n"
            "```\n"
            "□ W — Wired: In character library? Not orphaned in tmp/?\n"
            "□ I — Inherited & Aligned: Naming convention? Correct folder?\n"
            "□ R — Responsive & Performant: ≥800px? sRGB? Not black/corrupt?\n"
            "□ E — Extensible: Metadata present? Source params recorded?\n"
            "□ D — Defended: Visual check — looks correct, no artifacts?\n"
            "□ O — Observable: N/A (standalone asset)\n"
            "```"
        ),
        AssetType.VIDEO: (
            "📋 **WIREDO Delivery Checklist — VIDEO**\n"
            "```\n"
            "□ W — Wired: In managed media/? Not in tmp/?\n"
            "□ I — Inherited & Aligned: Naming convention? Correct folder?\n"
            "□ R — Responsive & Performant: H.264/H.265? ≤2Mbps? ≥720p?\n"
            "□ E — Extensible: Container metadata? Compression params recorded?\n"
            "□ D — Defended: Plays correctly? Duration/content matches intent?\n"
            "□ O — Observable: N/A (standalone asset)\n"
            "```"
        ),
        AssetType.AUDIO: (
            "📋 **WIREDO Delivery Checklist — AUDIO**\n"
            "```\n"
            "□ W — Wired: In managed directory? Referenced by system?\n"
            "□ I — Inherited & Aligned: Naming convention? Correct folder?\n"
            "□ R — Responsive & Performant: 44.1/48kHz? -16 LUFS? ≤-1 dBTP?\n"
            "□ E — Extensible: Params as top constants? Not hardcoded?\n"
            "□ D — Defended: Plays correctly? Duration/content as expected?\n"
            "□ O — Observable: N/A (standalone asset)\n"
            "```"
        ),
        AssetType.DOCUMENT: (
            "📋 **WIREDO Delivery Checklist — DOCUMENT**\n"
            "```\n"
            "□ W — Wired: Referenced/linked somewhere? Not orphaned?\n"
            "□ I — Inherited & Aligned: Unified template? Heading structure?\n"
            "□ R — Responsive & Performant: Reasonable size? No broken links?\n"
            "□ E — Extensible: Dates/versions parametric? Not hardcoded?\n"
            "□ D — Defended: Content accurate? Opens correctly in target app?\n"
            "□ O — Observable: N/A (standalone document)\n"
            "```"
        ),
        AssetType.PROTOCOL: (
            "📋 **WIREDO Delivery Checklist — PROTOCOL**\n"
            "```\n"
            "□ W — Wired: Transport connected? Client/server handshake works?\n"
            "□ I — Inherited & Aligned: Follows protocol spec? Version compatible?\n"
            "□ R — Responsive & Performant: Latency/throughput within bounds?\n"
            "□ E — Extensible: Adapter pluggable? Backward-compatible versioning?\n"
            "□ D — Defended: Integration tests pass? Crypto/dedup verified?\n"
            "⚠ O — Observable: Trace/log hooks present? (warn — nice-to-have)\n"
            "```"
        ),
        AssetType.CONFIG: (
            "📋 **WIREDO Delivery Checklist — CONFIG**\n"
            "```\n"
            "□ W — Wired: Loaded by application code? Not orphaned?\n"
            "□ I — Inherited & Aligned: Follows project config conventions?\n"
            "  R — Responsive: N/A (no performance dimension)\n"
            "□ E — Extensible: Schema-validated? Defaults documented?\n"
            "□ D — Defended: Parses without error? Schema validation passes?\n"
            "  O — Observable: N/A (no observability dimension)\n"
            "```"
        ),
    }
    return _checklists.get(asset_type, "")


def _get_cascade_note(workspace: str, project: str) -> str:
    """Check if this project has dependencies -> add cascade note."""
    cfg = load_wiredo_config(workspace)
    stack = cfg.get("project_stack", {})
    deps = stack.get(project, [])
    if not deps:
        return ""
    dep_list = ", ".join(deps)
    return (
        f"\n⚡ **Cascade**: {project} depends on [{dep_list}]. "
        f"Bottom-layer ({dep_list}) validates first → {project} inherits results. "
        f"Only verify {project}'s own-layer assets."
    )


def _detect_project(workspace: str, file_path: str) -> str:
    """Detect which project the file belongs to."""
    norm = file_path.replace("\\", "/").lower()
    if "infinite-agent" in norm or "infinite_agent" in norm:
        return "infinite-agent"
    if "psyche" in norm or "digital_persona" in norm:
        return "psyche"
    if "aegis" in norm:
        return "aegis"
    if "concinno" in norm or "concinno" in norm:
        return "concinno"
    return ""


class WiredoGuard(BaseGuard):
    """Inject asset-type-specific WIREDO checklist on first relevant tool call.

    Generalized: detects whether the task involves code, image, video, audio,
    or document, and injects the appropriate checklist. Per-type toggles in
    cc_config.json under wiredo.asset_types.
    """

    name = "wiredo"
    category = GuardCategory.COGNITIVE

    def __init__(self) -> None:
        # Track injected (session_id, asset_type) pairs — inject once per type
        self._injected: set[tuple[str, str]] = set()

    def check(self, ctx: GuardContext) -> Optional[GuardResult]:
        """Inject WIREDO checklist once per session per asset type."""
        asset_type = _detect_task_type(ctx)
        if asset_type is None:
            return None

        key = (ctx.session_id, asset_type.value)
        if key in self._injected:
            return None

        if not is_asset_type_enabled(ctx.workspace, asset_type):
            return None

        self._injected.add(key)

        # Build checklist
        checklist = _build_checklist(asset_type)
        if not checklist:
            return None

        # Add cascade note if applicable
        file_path = ctx.tool_input.get("file_path", "")
        if not file_path:
            file_path = ctx.tool_input.get("command", "")
        project = _detect_project(ctx.workspace, file_path)
        cascade_note = _get_cascade_note(ctx.workspace, project)

        context = checklist + cascade_note
        return GuardResult.allow_advisory(context=context)


# ══════════════════════════════════════════════════════════════════
# Part 2: WiredoEnforcementGuard — Hard enforcement (from wiredo_enforcement.py)
# ══════════════════════════════════════════════════════════════════

# Files that are considered "delivery documents" requiring WIREDO
_HANDOFF_PATTERNS = (
    "交接",
    "handoff",
    "handover",
    "delivery",
    "report",
)

# WIREDO table markers — at least 4 of 6 dimensions must appear
_WIREDO_MARKERS = [
    re.compile(r"W.*Wired|Wired.*[✅❌]|W\s*—\s*Wired", re.I),
    re.compile(r"I.*Inherited|Inherited.*[✅❌]|I\s*—\s*Inherited", re.I),
    re.compile(r"R.*Responsive|Responsive.*[✅❌]|R\s*—\s*Responsive", re.I),
    re.compile(r"E.*Extensible|Extensible.*[✅❌]|E\s*—\s*Extensible", re.I),
    re.compile(r"D.*Defended|Defended.*[✅❌]|D\s*—\s*Defended", re.I),
    re.compile(r"O.*Observable|Observable.*[✅❌]|O\s*—\s*Observable", re.I),
]

# Minimum dimensions that must be present to count as a WIREDO table
_MIN_DIMENSIONS = 4


def _is_handoff_file(file_path: str) -> bool:
    """Check if the file is a handoff/report that requires WIREDO."""
    basename = os.path.basename(file_path).lower()
    return any(p in basename for p in _HANDOFF_PATTERNS)


def _has_wiredo_table(content: str) -> bool:
    """Check if content contains a WIREDO verification table.

    Requires at least 4 of 6 WIREDO dimension markers to be present.
    This avoids false positives from casual mentions.
    """
    matches = sum(1 for marker in _WIREDO_MARKERS if marker.search(content))
    return matches >= _MIN_DIMENSIONS


def _session_has_code_edits(cache_dir: str, session_id: str) -> bool:
    """Check if this session edited any code files (from sentinel state)."""
    try:
        from concinno.delivery.wiredo import _get_session_code_files
        files = _get_session_code_files(cache_dir, session_id)
        return len(files) > 0
    except Exception:
        return False


def _is_wiredo_enabled(workspace: str) -> bool:
    """Check if WIREDO enforcement is enabled."""
    cfg = load_wiredo_config(workspace)
    return cfg.get("enabled", True)


class WiredoEnforcementGuard(BaseGuard):
    """PostToolUse: Hard deny handoff/report writes without WIREDO table.

    Enterprise enforcement:
    1. Detects when a handoff or report file is being written
    2. Checks if code was edited during this session
    3. Scans the written content for WIREDO table markers
    4. If code was edited but WIREDO table is missing -> DENY

    Respects wiredo.enabled toggle in cc_config.json.
    """

    name = "wiredo_enforcement"
    category = GuardCategory.QUALITY
    step_back_reason = "WIREDO table missing in handoff"

    def check(self, ctx: GuardContext) -> Optional[GuardResult]:
        """PreToolUse: no-op. This guard only acts on PostToolUse."""
        return None

    def on_post_tool(self, ctx: GuardContext) -> Optional[GuardResult]:
        """PostToolUse: verify WIREDO table in handoff/report writes."""
        # Only trigger on Write tool
        if ctx.tool_name != "Write":
            return None

        file_path = ctx.tool_input.get("file_path", "")
        if not file_path:
            return None

        # Only check handoff/report files
        if not _is_handoff_file(file_path):
            return None

        # Check if WIREDO is enabled
        if not _is_wiredo_enabled(ctx.workspace):
            return None

        # Check if this session edited code
        if not _session_has_code_edits(ctx.cache_dir, ctx.session_id):
            return None

        # Check the written content for WIREDO table
        content = ctx.tool_input.get("content", "")
        if not content:
            return None

        if _has_wiredo_table(content):
            return GuardResult.allow_advisory(
                context="✅ WIREDO table verified in handoff file",
            )

        # Hard deny — no WIREDO table found
        return GuardResult.deny(
            reason=(
                "WIREDO table missing in handoff file. "
                "Code was edited during this session — WIREDO verification "
                "is required before handoff.\n"
                "Add a WIREDO table with all 6 dimensions:\n"
                "| 維度 | 狀態 | 證據 |\n"
                "|------|------|------|\n"
                "| W — Wired（接線） | ✅/❌ | ... |\n"
                "| I — Inherited & Aligned（母版+合架構） | ✅/❌ | ... |\n"
                "| R — Responsive & Performant（跨裝置+性能） | ✅/❌ | ... |\n"
                "| E — Extensible（可配置） | ✅/❌ | ... |\n"
                "| D — Defended & Verified（測試+驗證） | ✅/❌ | ... |\n"
                "| O — Observable（可觀測） | ✅/❌ | ... |"
            ),
        )
