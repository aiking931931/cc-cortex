"""Curated fallback descriptions + examples for bare skill directories.

User directive 2026-04-24: many entries under ``~/.claude/skills/`` are
empty directories (no ``SKILL.md``) because they were created as
placeholders for guard / hook shims long before the SKILL format
stabilised. The GUI Skills tab otherwise shows them as "(no
description)", which is unhelpful.

This module ships hand-written descriptions + concrete examples for
those bare-dir skills, grounded in the actual Concinno source that
each directory corresponds to. The GUI merges this fallback when
``SKILL.md`` is missing or silent.

Format is the same as ``feature_examples``: plain-English summary,
one concrete example, prerequisites when they exist.
"""

from __future__ import annotations

__all__ = ["DESCRIPTIONS", "EXAMPLES", "describe"]


# Keyed by directory name (lowercase). Values are plain text.
DESCRIPTIONS: dict[str, str] = {
    "bash-dry-run": (
        "Gate that refuses destructive Bash commands until the user "
        "confirms — legacy shim for destruction_guard."
    ),
    "butterfly": (
        "Iron-rule #1: discovered issues (including pre-existing) "
        "must be handled in the current session, not deferred."
    ),
    "consecutive-fail": (
        "After N consecutive tool failures, force the agent into RAG "
        "/ doc-lookup instead of hammering the same path."
    ),
    "cortex-guard": (
        "Legacy alias for destruction_guard + boundary_guard bundle."
    ),
    "cortex-hooks": (
        "Legacy alias for the original Concinno hook pipeline "
        "(superseded by concinno.guards.pipeline in 2.x)."
    ),
    "cortex-schedule": (
        "Legacy alias for the scheduler skill (scheduled reflection / "
        "scavenger / weekly-research tasks)."
    ),
    "destruction-guard": (
        "R0–R4 classifier for destructive CLI commands (rm -rf, "
        "DROP TABLE, git push --force, etc.) with auto-backup + "
        "#DESTROY_CONFIRMED escape."
    ),
    "hallucination": (
        "LLM-as-judge detector for unsourced factual claims in "
        "written content (blog posts, PR descriptions, etc.)."
    ),
    "handoff-required": (
        "Blocks Stop event when the session produced ≥N touched "
        "files without a handoff update."
    ),
    "premise-gate": (
        "Blocks execution until external constraints (CC platform "
        "limits, vendor docs, benchmark rules) have been verified."
    ),
    "secret-scan": (
        "Pre-commit + pre-tool scan for hardcoded API keys, tokens, "
        "and passwords."
    ),
    "verify-before-write": (
        "Forces a Read before Write/Edit on files the agent has not "
        "inspected this session — prevents blind edits."
    ),
    "wiredo": (
        "Six-dimension delivery checklist (Wired / Inherited / "
        "Responsive / Extensible / Defended / Observable) enforced "
        "before a task is marked done."
    ),
    "general-mode": (
        "Default agent behaviour preset — context runs to the end, "
        "auto-compact at threshold, memory file used, fresh session "
        "when full. Suits most users other than the author."
    ),
    "competition-mode": (
        "DEPRECATED — renamed to general-mode in Concinno 2.6.0. "
        "Redirect removed three months from 2026-04-18."
    ),
}


EXAMPLES: dict[str, str] = {
    "bash-dry-run": (
        "Example: `rm -rf build/` with destruction_guard in "
        "step_back_first mode → bash-dry-run shim prints the "
        "command + risk level, then hands control to the confirm "
        "flow.\n\nRequires: destruction_guard enabled (the real "
        "logic lives there)."
    ),
    "butterfly": (
        "Example: you edit `foo.py` and notice a stale comment on an "
        "unrelated function → butterfly rule refuses to close the "
        "session until you fix the comment or log it in the "
        "handoff's unresolved section.\n\nRequires: L0 rule file "
        "present in the project."
    ),
    "consecutive-fail": (
        "Example: `pytest -q` fails three times in a row → guard "
        "blocks further test invocations until the agent performs "
        "a `/kb_` lookup or WebSearch.\n\nRequires: sentinel "
        "ConsecutiveFailGuard enabled (default in 2.x)."
    ),
    "destruction-guard": (
        "Example: agent runs `git push --force main` → guard "
        "classifies R4 (irreversible, shared-state) → deny unless "
        "the message contains `#DESTROY_CONFIRMED:<reason>`.\n\n"
        "Requires: always on (hardcoded)."
    ),
    "hallucination": (
        "Example: agent writes \"Concinno ships 200 guards\" in a "
        "blog post → Haiku judge flags the claim as unsupported → "
        "agent cites the real count before publishing."
    ),
    "handoff-required": (
        "Example: session touched 5 files; operator tries to close → "
        "guard refuses until the handoff file shows a matching "
        "update.\n\nRequires: a project handoff location configured."
    ),
    "premise-gate": (
        "Example: agent about to publish → premise_gate runs "
        "`describe_current_config()` + inspects harness allow rules "
        "(two-layer gate check SOP from switches.md)."
    ),
    "secret-scan": (
        "Example: agent about to write `openai_key = \"sk-\"...` → "
        "guard flags the pattern + offers replacement with env "
        "var / credentials vault lookup."
    ),
    "verify-before-write": (
        "Example: tries `Edit(src/foo.py, …)` without prior `Read` → "
        "guard refuses. Skip for tiny files via "
        "`read_first_gate.min_lines`."
    ),
    "wiredo": (
        "Example: task marked done but no screenshot / no smoke run "
        "/ no observability hook → wiredo checklist refuses "
        "delivery until all six dimensions are ticked."
    ),
    "general-mode": (
        "Example: a user unfamiliar with the author's handoff "
        "workflow installs Concinno → general-mode keeps the LLM "
        "in the baseline behaviour so it does not assume handoff "
        "files / task decomposition discipline."
    ),
    "cortex-guard": (
        "Historical name; actual logic lives in "
        "`concinno.destruction_guard` + `concinno.boundary_guard`."
    ),
    "cortex-hooks": (
        "Historical name; actual logic lives in "
        "`concinno.guards.pipeline`. Kept as a redirect so older "
        "CLAUDE.md references still resolve."
    ),
    "cortex-schedule": (
        "Historical name; actual logic lives in "
        "`concinno.scheduler` + `.claude/hooks/schedule_config.json`."
    ),
    "competition-mode": (
        "DEPRECATED — use `general-mode` instead. Removed "
        "three months after 2026-04-18."
    ),
}


def describe(name: str) -> tuple[str | None, str | None]:
    """Return ``(description, example)`` for ``name`` — either element
    may be ``None`` if no fallback exists."""
    key = name.lower()
    return DESCRIPTIONS.get(key), EXAMPLES.get(key)
