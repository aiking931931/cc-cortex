"""Example: writing your own rewrite guard.

Shows how to subclass `BaseGuard` and return `GuardResult.rewrite(...)`
instead of `.allow()` / `.deny()`. The example guard canonicalises
`git commit` calls that forgot the `-m` flag: instead of denying,
it rewrites the command to include a reminder comment.

Run::

    python examples/custom_rewrite_guard_example.py
"""

from __future__ import annotations

import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from cc_cortex import (
    BaseGuard,
    GuardCategory,
    GuardContext,
    GuardPipeline,
    GuardResult,
)


class GitCommitMessageRewriter(BaseGuard):
    """Rewrite `git commit` without -m to a stub-message form.

    `git commit` with no `-m` opens an interactive editor, which
    hangs Claude Code forever. This guard rewrites it to a
    placeholder form so the call returns quickly and the user can
    retry with a real message.
    """

    name = "git_commit_message_rewriter"
    category = GuardCategory.QUALITY
    step_back_reason = ""
    path_scope: list[str] = []

    def check(self, ctx: GuardContext) -> GuardResult | None:
        if ctx.tool_name != "Bash":
            return None
        cmd = ctx.tool_input.get("command", "")
        if not isinstance(cmd, str):
            return None

        stripped = cmd.strip()
        if not stripped.startswith("git commit"):
            return None
        # Already has -m / --message / --file / --no-edit / --amend?
        if any(
            flag in stripped
            for flag in (" -m", " --message", " --file", " --no-edit", " --amend")
        ):
            return None

        new_cmd = (
            'git commit -m "TODO: write a real commit message before pushing" '
            "# cc-cortex GitCommitMessageRewriter"
        )
        new_input = dict(ctx.tool_input)
        new_input["command"] = new_cmd
        return GuardResult.rewrite(
            updated_input=new_input,
            reason=(
                "git commit without -m would open an interactive editor; "
                "rewrote to a TODO stub. Amend with a real message before "
                "pushing."
            ),
        )


def _run_pipeline(tool_input: dict) -> dict:
    pipe = GuardPipeline()
    pipe.register(GitCommitMessageRewriter())
    ctx = GuardContext(
        tool_name="Bash",
        tool_input=tool_input,
        session_id="example-custom",
        cache_dir="",
        hook_event="PreToolUse",
    )
    return pipe.run_pre_tool(ctx)


def main() -> None:
    print("cc-cortex 1.4.0 — custom rewrite guard example")

    for label, cmd in [
        ("missing -m (should rewrite)", "git commit"),
        ("has -m (passthrough)", 'git commit -m "fix: typo"'),
        ("has --amend (passthrough)", "git commit --amend --no-edit"),
        ("not a commit (passthrough)", "git status"),
    ]:
        print(f"\n── {label} ──")
        print(f"  input : {cmd}")
        result = _run_pipeline({"command": cmd})
        hso = result.get("hookSpecificOutput")
        if hso and "updatedInput" in hso:
            print(f"  out   : {hso['updatedInput']['command']}")
        else:
            print("  out   : (unchanged)")


if __name__ == "__main__":
    main()
