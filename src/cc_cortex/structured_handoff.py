"""cc_cortex.structured_handoff — Fixed-field handoff template for agent transfers.

@module structured_handoff
@responsibility Replace AI-generated summaries with deterministic, parseable
    handoff records. Each field is required — omissions are compile errors,
    not forgotten context.
@dependencies cc_cortex.guards.base
@exports HandoffRecord, HandoffTemplate, StructuredHandoffGuard

Design rationale (LLM-Proof Architecture):
    AI summaries drift — fields don't. A missing ``pending`` list is a parse
    error caught by any downstream agent. A missing paragraph in a free-text
    summary is invisible.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Literal

from cc_cortex.guards.base import (
    BaseGuard,
    GuardCategory,
    GuardContext,
    GuardResult,
)

# ── Constants ────────────────────────────────────────────

STATUS_VALUES = ("completed", "partial", "blocked")
CONTEXT_SUMMARY_MAX = 120  # chars
NEXT_STEP_MAX = 200  # chars


# ── Data ─────────────────────────────────────────────────


@dataclass
class HandoffRecord:
    """Fixed-field handoff for agent-to-agent transfers.

    Every field is required at build time.  Validators reject empty
    ``completed`` + empty ``pending`` (nothing done, nothing to do = bug).
    """

    session_id: str
    task_name: str
    status: Literal["completed", "partial", "blocked"]
    completed: list[str]
    pending: list[str]
    blockers: list[str]
    files_touched: list[str]
    context_summary: str  # ≤120 chars — one-line elevator pitch
    next_step: str  # exactly one actionable sentence
    token_used: int = 0
    duration_s: int = 0
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )


# ── Template ─────────────────────────────────────────────


class HandoffTemplate:
    """Build, parse, and validate structured handoff records."""

    # ── Build ────────────────────────────────────────────

    @staticmethod
    def build(record: HandoffRecord) -> str:
        """Render a HandoffRecord as parseable markdown.

        Format is machine-readable (regex-parseable) AND human-readable.
        """
        lines: list[str] = []
        lines.append("---")
        lines.append(f"session_id: {record.session_id}")
        lines.append(f"task_name: {record.task_name}")
        lines.append(f"status: {record.status}")
        lines.append(f"token_used: {record.token_used}")
        lines.append(f"duration_s: {record.duration_s}")
        lines.append(f"created_at: {record.created_at}")
        lines.append("---")
        lines.append("")

        lines.append(f"# Handoff: {record.task_name}")
        lines.append("")

        # Context summary — the one-line pitch
        lines.append(f"**Context**: {record.context_summary}")
        lines.append("")

        # Completed
        lines.append("## Completed")
        lines.append("")
        if record.completed:
            for item in record.completed:
                lines.append(f"- ✅ {item}")
        else:
            lines.append("- (none)")
        lines.append("")

        # Pending
        lines.append("## Pending")
        lines.append("")
        if record.pending:
            for item in record.pending:
                lines.append(f"- ⬜ {item}")
        else:
            lines.append("- (none)")
        lines.append("")

        # Blockers
        lines.append("## Blockers")
        lines.append("")
        if record.blockers:
            for item in record.blockers:
                lines.append(f"- 🚧 {item}")
        else:
            lines.append("- (none)")
        lines.append("")

        # Files touched
        lines.append("## Files")
        lines.append("")
        if record.files_touched:
            for f in record.files_touched:
                lines.append(f"- `{f}`")
        else:
            lines.append("- (none)")
        lines.append("")

        # Next step — exactly one
        lines.append(f"**Next step**: {record.next_step}")
        lines.append("")

        return "\n".join(lines)

    # ── Parse ────────────────────────────────────────────

    @staticmethod
    def parse(content: str) -> HandoffRecord | None:
        """Parse structured handoff markdown back into a HandoffRecord.

        Returns None if the content doesn't match the expected format.
        """
        # Parse frontmatter
        fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
        if not fm_match:
            return None

        fm_fields: dict[str, str] = {}
        for line in fm_match.group(1).strip().split("\n"):
            if ":" in line:
                key, val = line.split(":", 1)
                fm_fields[key.strip()] = val.strip()

        session_id = fm_fields.get("session_id", "")
        task_name = fm_fields.get("task_name", "")
        status_raw = fm_fields.get("status", "partial")
        if status_raw not in STATUS_VALUES:
            status_raw = "partial"

        token_used = int(fm_fields.get("token_used", "0"))
        duration_s = int(fm_fields.get("duration_s", "0"))
        created_at = fm_fields.get("created_at", "")

        body = content[fm_match.end():]

        def _extract_list(section_name: str, marker: str) -> list[str]:
            pattern = rf"## {section_name}\s*\n(.*?)(?=\n## |\n\*\*Next step\*\*|\Z)"
            m = re.search(pattern, body, re.DOTALL)
            if not m:
                return []
            items = []
            for line in m.group(1).strip().split("\n"):
                line = line.strip()
                if line.startswith(f"- {marker} "):
                    items.append(line[len(f"- {marker} "):])
                elif line.startswith("- `") and line.endswith("`"):
                    items.append(line[3:-1])
                elif line == "- (none)":
                    continue
            return items

        completed = _extract_list("Completed", "✅")
        pending = _extract_list("Pending", "⬜")
        blockers = _extract_list("Blockers", "🚧")
        files_touched = _extract_list("Files", "")

        # Files section uses backtick format
        if not files_touched:
            pat = r"## Files\s*\n(.*?)(?=\n## |\n\*\*Next step\*\*|\Z)"
            files_m = re.search(pat, body, re.DOTALL)
            if files_m:
                for line in files_m.group(1).strip().split("\n"):
                    line = line.strip()
                    tick_m = re.match(r"^- `(.+)`$", line)
                    if tick_m:
                        files_touched.append(tick_m.group(1))

        # Context summary
        context_summary = ""
        ctx_m = re.search(r"\*\*Context\*\*:\s*(.+)", body)
        if ctx_m:
            context_summary = ctx_m.group(1).strip()

        # Next step
        next_step = ""
        ns_m = re.search(r"\*\*Next step\*\*:\s*(.+)", body)
        if ns_m:
            next_step = ns_m.group(1).strip()

        return HandoffRecord(
            session_id=session_id,
            task_name=task_name,
            status=status_raw,  # type: ignore[arg-type]
            completed=completed,
            pending=pending,
            blockers=blockers,
            files_touched=files_touched,
            context_summary=context_summary,
            next_step=next_step,
            token_used=token_used,
            duration_s=duration_s,
            created_at=created_at,
        )

    # ── Validate ─────────────────────────────────────────

    @staticmethod
    def validate(record: HandoffRecord) -> list[str]:
        """Validate record completeness. Returns list of error strings.

        Empty list = valid.
        """
        errors: list[str] = []

        if not record.session_id:
            errors.append("session_id is required")
        if not record.task_name:
            errors.append("task_name is required")
        if record.status not in STATUS_VALUES:
            errors.append(f"status must be one of {STATUS_VALUES}, got '{record.status}'")

        # Nothing done AND nothing to do = nonsensical handoff
        if not record.completed and not record.pending:
            errors.append("at least one of completed/pending must be non-empty")

        # blocked status requires blockers
        if record.status == "blocked" and not record.blockers:
            errors.append("status is 'blocked' but blockers list is empty")

        # completed status should have no pending
        if record.status == "completed" and record.pending:
            errors.append("status is 'completed' but pending list is non-empty (use 'partial')")

        # Context summary length
        if len(record.context_summary) > CONTEXT_SUMMARY_MAX:
            errors.append(
                f"context_summary exceeds {CONTEXT_SUMMARY_MAX} chars "
                f"(got {len(record.context_summary)})"
            )

        # Next step length
        if len(record.next_step) > NEXT_STEP_MAX:
            errors.append(
                f"next_step exceeds {NEXT_STEP_MAX} chars "
                f"(got {len(record.next_step)})"
            )

        # Next step is required for non-completed
        if record.status != "completed" and not record.next_step:
            errors.append("next_step is required when status is not 'completed'")

        return errors

    # ── JSON ─────────────────────────────────────────────

    @staticmethod
    def to_json(record: HandoffRecord) -> str:
        """Serialize to JSON (for inter-process / MCP transfer)."""
        return json.dumps(asdict(record), ensure_ascii=False, indent=2)

    @staticmethod
    def from_json(data: str) -> HandoffRecord | None:
        """Deserialize from JSON."""
        try:
            d = json.loads(data)
            return HandoffRecord(**d)
        except Exception:
            return None


# ── Guard ────────────────────────────────────────────────


class StructuredHandoffGuard(BaseGuard):
    """PostToolUse: validate structured handoff files on Write.

    Checks that handoff files written by agents follow the fixed-field
    template. Fires only for files matching the structured handoff pattern
    (frontmatter with session_id + task_name + status).
    """

    name = "structured_handoff_guard"
    category = GuardCategory.QUALITY

    def check(self, ctx: GuardContext) -> GuardResult | None:
        """No-op for PreToolUse."""
        return None

    def on_post_tool(self, ctx: GuardContext) -> GuardResult | None:
        """Validate structured handoff after Write."""
        if ctx.tool_name != "Write":
            return None

        file_path = ctx.tool_input.get("file_path", "")
        if not file_path or not file_path.endswith(".md"):
            return None

        # Only fire for files that look like structured handoffs
        content = ctx.tool_input.get("content", "")
        if not content:
            return None

        # Quick check: does it have our frontmatter signature?
        if "session_id:" not in content or "task_name:" not in content:
            return None

        record = HandoffTemplate.parse(content)
        if record is None:
            return None  # Not our format

        errors = HandoffTemplate.validate(record)
        if not errors:
            return None  # Clean

        from cc_cortex.i18n import msg

        error_text = msg(
            "structured_handoff.validation_errors",
            count=len(errors),
            errors="; ".join(errors),
        )
        return GuardResult.allow(context=error_text)
