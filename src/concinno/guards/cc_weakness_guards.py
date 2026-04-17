"""CC weakness mitigation guards — address known Claude Code blindspots.

Five COGNITIVE guards that inject awareness when CC's native limitations
could cause silent failures:

1. TruncationAwareGuard — tool results >50K chars are silently truncated
2. LargeFileReadGuard — files >500 LOC read without offset/limit
3. RenameScopeGuard — rename/refactor without comprehensive search
4. CompactFailureGuard — detect autoCompact failure storm (CC bug: up to 3272 retries)
5. McpCleanupGuard — detect unused MCP servers wasting ~3750 tokens/turn

These ALLOW all tool calls but inject context to prevent blind spots.

@module guards/cc_weakness_guards
@category COGNITIVE
"""

from __future__ import annotations

from concinno.guards.base import (
    BaseGuard,
    GuardCategory,
    GuardContext,
    GuardResult,
)

# CC hard limits (from leaked source: constants/toolLimits.ts)
CC_MAX_RESULT_SIZE_CHARS = 50_000
CC_MAX_LINES_TO_READ = 2000
LARGE_FILE_THRESHOLD = 500  # LOC — below CC's 2000 hard cap but still risky


class TruncationAwareGuard(BaseGuard):
    """PostToolUse: detect when tool results were likely truncated.

    CC silently truncates results >50K chars to a ~2K preview.
    The model doesn't know it missed data. This guard injects a reminder
    when the result looks suspiciously short relative to the query scope.
    """

    name = "truncation_aware"
    category = GuardCategory.COGNITIVE
    priority = 310
    hook_event = "PostToolUse"

    def check(self, ctx: GuardContext) -> GuardResult:
        # Only relevant for search/read tools
        if ctx.tool_name not in ("Grep", "Bash", "Read"):
            return GuardResult.allow()

        result = ctx.tool_result
        if not result:
            return GuardResult.allow()

        # Heuristic: CC's persisted result message contains these markers
        truncation_markers = [
            "Output too large",
            "Full output saved to:",
            "persisted-output",
            "head_limit",
        ]

        if any(marker in result for marker in truncation_markers):
            return GuardResult.allow(
                context=(
                    "⚠ Tool result was truncated (>50K chars). "
                    "Only a preview was returned. Re-run with narrower scope "
                    "(single directory, stricter glob) if results look incomplete."
                ),
            )

        return GuardResult.allow()


class LargeFileReadGuard(BaseGuard):
    """PreToolUse: warn when reading a large file without offset/limit.

    CC caps file reads at 2000 lines / 25K tokens. Content beyond that
    is silently dropped. The model may hallucinate the rest.

    Triggers when: Read tool, no limit specified, file known to be large.
    """

    name = "large_file_read"
    category = GuardCategory.COGNITIVE
    priority = 311
    hook_event = "PreToolUse"

    def check(self, ctx: GuardContext) -> GuardResult:
        if ctx.tool_name != "Read":
            return GuardResult.allow()

        tool_input = ctx.tool_input
        has_limit = tool_input.get("limit") is not None
        has_offset = tool_input.get("offset") is not None

        # If already using offset/limit, user is aware — skip
        if has_limit or has_offset:
            return GuardResult.allow()

        file_path = tool_input.get("file_path", "")
        if not file_path:
            return GuardResult.allow()

        # Check file size (best effort)
        try:
            import os
            if os.path.isfile(file_path):
                with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                    line_count = sum(1 for _ in f)
                if line_count > LARGE_FILE_THRESHOLD:
                    return GuardResult.allow(
                        context=(
                            f"⚠ File has {line_count} lines (CC caps reads at 2000). "
                            "Use offset+limit to read in chunks. "
                            "Content beyond 2000 lines is silently dropped."
                        ),
                    )
        except Exception:
            pass

        return GuardResult.allow()


class RenameScopeGuard(BaseGuard):
    """PreToolUse: inject comprehensive search reminder on rename operations.

    CC's Grep is text-based, not AST-aware. Renames that only grep for
    the function name miss: re-exports, dynamic imports, string literals,
    test mocks, barrel files.

    Triggers when: Edit tool with replace_all, or tool_input contains
    patterns suggesting a rename operation.
    """

    name = "rename_scope"
    category = GuardCategory.COGNITIVE
    priority = 312
    hook_event = "PreToolUse"

    _RENAME_SIGNALS = [
        "rename", "refactor", "replace_all",
    ]

    def check(self, ctx: GuardContext) -> GuardResult:
        if ctx.tool_name != "Edit":
            return GuardResult.allow()

        tool_input = ctx.tool_input
        is_replace_all = tool_input.get("replace_all", False)

        if not is_replace_all:
            return GuardResult.allow()

        old_string = tool_input.get("old_string", "")
        new_string = tool_input.get("new_string", "")

        # Only trigger for identifier-like renames (not content edits)
        if not old_string or not new_string:
            return GuardResult.allow()

        # Heuristic: identifier rename = single word or camelCase/snake_case
        import re
        if not re.match(r'^[a-zA-Z_]\w*$', old_string.strip()):
            return GuardResult.allow()

        return GuardResult.allow(
            context=(
                f"⚠ replace_all '{old_string}' → '{new_string}': "
                "Grep is text-based, not AST. After this edit, verify: "
                "re-exports, dynamic imports, string literals containing "
                "the name, test mocks, barrel files (index.ts)."
            ),
        )


class CompactFailureGuard(BaseGuard):
    """PostToolUse: detect autoCompact failure storm.

    CC bug (fixed in latest): autoCompact retries infinitely on failure,
    burning up to 250K API calls/day (max observed: 3272 consecutive
    failures in one session). CC added MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES=3
    but older versions or edge cases may still hit this.

    Detection: if context usage keeps climbing after multiple compactions
    in the same session, inject a warning to start a new session.
    """

    name = "compact_failure"
    category = GuardCategory.COGNITIVE
    priority = 313
    hook_event = "PostToolUse"

    def check(self, ctx: GuardContext) -> GuardResult:
        import json
        import os

        # Read zone file for compact count
        zone_path = os.path.join(
            os.path.expanduser("~"), ".claude", ".token_zone.json",
        )
        try:
            with open(zone_path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return GuardResult.allow()

        compact_count = data.get("compact_count", 0)
        input_tokens = data.get("input_tokens", 0)
        quality_zone = data.get("quality_zone", 200_000)

        # If compacted 3+ times AND still in yellow/red zone → likely failure storm
        if compact_count >= 3 and input_tokens > quality_zone:
            return GuardResult.allow(
                context=(
                    f"⚠ {compact_count} compactions but still at "
                    f"{input_tokens // 1000}K tokens. "
                    "Possible autoCompact failure loop. "
                    "Consider starting a new session (/clear) to reset."
                ),
            )

        return GuardResult.allow()


class McpCleanupGuard(BaseGuard):
    """PreToolUse: remind about unused MCP servers burning tokens.

    Each connected MCP server adds ~500-3750 tokens to the system prompt
    every turn. Unused servers waste context budget silently.

    Detection: if MCP tools haven't been called in 10+ tool calls,
    inject a reminder to check /mcp status.
    """

    name = "mcp_cleanup"
    category = GuardCategory.COGNITIVE
    priority = 314
    hook_event = "PreToolUse"

    # MCP tool name patterns
    _MCP_PREFIXES = ("mcp__",)

    def check(self, ctx: GuardContext) -> GuardResult:
        import json
        import os

        # Only check periodically (every 20 tool calls)
        state_path = os.path.join(ctx.cache_dir, "mcp_cleanup_state.json")
        try:
            with open(state_path, encoding="utf-8") as f:
                state = json.load(f)
        except Exception:
            state = {"tool_count": 0, "mcp_used": False, "last_warned": 0}

        state["tool_count"] = state.get("tool_count", 0) + 1

        # Track if any MCP tool was used
        if any(
            ctx.tool_name.startswith(p) for p in self._MCP_PREFIXES
        ):
            state["mcp_used"] = True

        # Every 20 tool calls, check if MCP was used
        if state["tool_count"] >= 20:
            warned_at = state.get("last_warned", 0)
            should_warn = (
                not state.get("mcp_used", False)
                and state["tool_count"] - warned_at >= 20
            )

            if should_warn:
                state["last_warned"] = state["tool_count"]
                self._save_state(state_path, state)
                return GuardResult.allow(
                    context=(
                        "⚠ 20+ tool calls without any MCP tool usage. "
                        "Each connected MCP server adds ~500-3750 tokens "
                        "to every API call. Run /mcp to check and disable "
                        "unused servers."
                    ),
                )

        self._save_state(state_path, state)
        return GuardResult.allow()

    @staticmethod
    def _save_state(path: str, state: dict) -> None:
        import json
        import os
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(state, f)
        except Exception:
            pass
