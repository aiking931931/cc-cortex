"""cc_cortex.core.compact — PreCompact state preservation.

@module compact
@responsibility Save session context before conversation compaction
@dependencies (none — stdlib only)
@exports save_compact_state, extract_recent_user_messages
"""

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Optional


def save_compact_state(
    state: dict,
    output_path: Optional[str] = None,
    tz_offset_hours: int = 8,
) -> str:
    """Save session state before compaction.

    Args:
        state: Dict with keys like session_name, current_task, files, etc.
        output_path: Where to save (default: CLAUDE_PROJECT_DIR/.cc_cortex_cache/compact_state.json)
        tz_offset_hours: Timezone offset.

    Returns:
        Path where state was saved.
    """
    if not output_path:
        base = os.environ.get("CLAUDE_PROJECT_DIR", ".")
        output_path = os.path.join(base, ".cc_cortex_cache", "compact_state.json")

    tz = timezone(timedelta(hours=tz_offset_hours))
    state["saved_at"] = datetime.now(tz).isoformat()

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    tmp = output_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    os.replace(tmp, output_path)
    return output_path


def extract_recent_user_messages(
    transcript: list[dict],
    max_messages: int = 3,
) -> list[str]:
    """Extract recent user messages from transcript for compact state.

    Args:
        transcript: List of message dicts with 'role' and 'content'.
        max_messages: Max number of recent user messages to extract.

    Returns:
        List of user message strings (most recent first).
    """
    messages = []
    for msg in reversed(transcript):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, str) and content.strip():
                # Skip system tags
                text = content.strip()
                if not text.startswith("<"):
                    messages.append(text[:200])
                    if len(messages) >= max_messages:
                        break
    return messages
