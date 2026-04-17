"""concinno SubagentStart hook — inject shared cognition into subagents.

Problem: Subagents inherit rules but attention is diluted by task prompt.
Rules tell WHAT to do; cognition tells HOW TO THINK.
Without cognition, even simple tasks produce orphan code.

Solution: On SubagentStart, inject additionalContext with:
  1. Workspace context (paths, existing files — prevent collisions)
  2. Shared cognition (thinking OS, memory RAG, delivery standards)

Parent and subagent share the SAME cognitive layer via cognitive_inject.
Not "give subagent a copy" — "let subagent access the same brain."
"""

from __future__ import annotations

import json
import os
import sys


def _list_existing_files(workspace: str, dirs: list[str], max_per_dir: int = 20) -> str:
    """List existing files in key directories so subagent avoids collisions."""
    lines: list[str] = []
    for d in dirs:
        full = os.path.join(workspace, d)
        if not os.path.isdir(full):
            continue
        try:
            entries = sorted(os.listdir(full))[:max_per_dir]
            if entries:
                lines.append(f"  {d}/: {', '.join(entries)}")
        except OSError:
            continue
    return "\n".join(lines)


def _build_context(workspace: str) -> str:
    """Build workspace context for subagent injection."""
    parts: list[str] = []

    parts.append(f"🔧 Workspace: {workspace}")
    parts.append("All file writes MUST use absolute paths under this workspace.")

    # Key directories to scan
    scan_dirs = [
        "src", "packages", "projects",
        "src/concinno", "src/concinno/hooks",
        "src/concinno/delivery", "src/concinno/guards",
    ]
    existing = _list_existing_files(workspace, scan_dirs)
    if existing:
        parts.append(f"Existing files (avoid duplicates):\n{existing}")

    parts.append(
        "Path rules: Use forward slashes. "
        "Windows paths must be absolute (e.g., C:/project/...)."
    )

    return "\n".join(parts)


def main(hook_data: dict | None = None) -> None:
    """SubagentStart entry point — inject workspace + shared cognition."""
    try:
        if hook_data is None:
            hook_data = json.loads(sys.stdin.read())
    except Exception:
        return

    workspace = os.environ.get("CLAUDE_PROJECT_DIR", "")
    if not workspace:
        return

    # Layer 1: workspace context (paths, existing files)
    workspace_ctx = _build_context(workspace)

    # Extract agent metadata
    # SubagentStart hook only provides: session_id, agent_type, agent_id, cwd
    # No prompt/description — keyword matching only activates if Claude Code
    # adds these fields in future versions.
    agent_type = ""
    task_prompt = ""
    if isinstance(hook_data, dict):
        agent_type = hook_data.get("agent_type", "") or ""
        task_prompt = hook_data.get("prompt", "") or ""

    # Layer 2: subagent identity (task-driven assignment, ~60t)
    identity_ctx = ""
    cognition_depth = ""
    try:
        from concinno.subagent_identity import (
            assign_identity,
            build_identity_context,
        )

        profile = assign_identity(agent_type=agent_type, task_prompt=task_prompt)
        identity_ctx = build_identity_context(profile)
        cognition_depth = profile.cognition_depth
    except Exception:
        pass  # Fail-safe: identity not critical

    # Layer 3: shared cognition (thinking + memory + delivery)
    cognitive_ctx = ""
    try:
        from concinno.cognitive_inject import build_cognitive_context

        cognitive_ctx = build_cognitive_context(
            workspace=workspace,
            task_prompt=task_prompt,
            agent_type=agent_type,
            cognition_depth=cognition_depth,
        )
    except Exception:
        pass  # Fail-safe: workspace context still injected

    parts = [workspace_ctx]
    if identity_ctx:
        parts.append(identity_ctx)
    if cognitive_ctx:
        parts.append(cognitive_ctx)
    context = "\n\n".join(parts)

    output = json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SubagentStart",
            "additionalContext": context,
        }
    }, ensure_ascii=False)

    if hasattr(sys.stdout, "buffer"):
        sys.stdout.buffer.write(output.encode("utf-8"))
        sys.stdout.buffer.flush()
    else:
        sys.stdout.write(output)
        sys.stdout.flush()


if __name__ == "__main__":
    main()
