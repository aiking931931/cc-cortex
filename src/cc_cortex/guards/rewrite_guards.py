"""cc_cortex.guards.rewrite_guards — PreToolUse input rewriters.

@module rewrite_guards
@responsibility Guards that rewrite ``tool_input`` in place to a safer
    canonical form instead of denying the call outright. Uses the
    ``GuardResult.rewrite()`` output channel wired to Claude Code's
    ``hookSpecificOutput.updatedInput`` field.
@dependencies cc_cortex.guards.base
@exports BashDryRunRewriter, WriteSecretFileRewriter,
    BashPipeToShellRewriter

Rationale (CBUA sweet spot):
  For years CCC could only ALLOW or DENY tool calls. If the user typed
  ``Bash(rm -rf .)`` or ``Write(.env)`` the best the pipeline could do
  was block the call outright and hope the user picked a different
  command. Claude Code 2026-04's ``hookSpecificOutput.updatedInput``
  channel lets a hook emit a replacement ``tool_input`` dict that the
  CC runtime uses instead. This module is the first CCC consumer:
  three guards that catch common footguns and rewrite them to safer
  variants, leaving the user in control.

  Design constraints (per red team review):
  - Rewrites must be *visible*: every rewrite sets ``reason=`` so the
    pipeline surfaces ``↻ rewritten: ...`` in additionalContext. Silent
    rewrites are footguns of their own kind.
  - Rewrites must be *narrow*: guards only rewrite when the original
    input is clearly a footgun, not on speculative improvements. False
    positives here are worse than false negatives — the user typed the
    thing intentionally.
  - Rewrites must be *idempotent*: running a rewritten input through
    the same guard must return ALLOW (no opinion) to avoid infinite
    loops in the pipeline.
  - Rewrites must *compose*: a later guard may still DENY a rewritten
    call. Rewrite is not a trump card.
"""

from __future__ import annotations

import re

from cc_cortex.guards.base import (
    BaseGuard,
    GuardCategory,
    GuardContext,
    GuardResult,
)

# ── BashDryRunRewriter ──────────────────────────────────────

_DESTRUCTIVE_BASH_PATTERNS = (
    # Each entry: (regex, human_name)
    # The regex must match the *original* form — the rewritten form
    # includes --dry-run and must NOT match, otherwise we loop.
    (re.compile(r"^\s*rm\s+(-[a-zA-Z]*[rf][a-zA-Z]*\s+)"), "rm -rf"),
    (re.compile(r"^\s*rm\s+(-[a-zA-Z]*[fr][a-zA-Z]*\s+)"), "rm -fr"),
)


class BashDryRunRewriter(BaseGuard):
    """Rewrite destructive ``rm -rf`` / ``rm -fr`` Bash calls to dry-run.

    Matches bare destructive deletion commands and prepends ``echo`` so
    the user sees the expansion without losing data. The original form
    is preserved as a comment in the echoed output for transparency.

    Idempotent: the rewritten command no longer matches the regex
    (``echo [dry-run] ...`` starts with ``echo``, not ``rm``).
    """

    name = "bash_dry_run_rewriter"
    category = GuardCategory.QUALITY
    step_back_reason = ""
    path_scope: list[str] = []

    def check(self, ctx: GuardContext) -> GuardResult | None:
        if ctx.tool_name != "Bash":
            return None
        cmd = ctx.tool_input.get("command", "")
        if not isinstance(cmd, str) or not cmd.strip():
            return None

        # Look at the first non-pipe segment; `... | rm -rf foo` is
        # less common but we still want to catch an outer `rm -rf`.
        stripped = cmd.strip()
        for pattern, human_name in _DESTRUCTIVE_BASH_PATTERNS:
            if pattern.match(stripped):
                safe_cmd = (
                    f"echo '[dry-run] would have run: {stripped}' "
                    f"# cc-cortex BashDryRunRewriter"
                )
                new_input = dict(ctx.tool_input)
                new_input["command"] = safe_cmd
                return GuardResult.rewrite(
                    updated_input=new_input,
                    reason=(
                        f"{human_name} rewritten to echo. Pass an explicit "
                        "target path, not `.` / `*`, then retry."
                    ),
                )
        return None


# ── WriteSecretFileRewriter ─────────────────────────────────

_SECRET_FILE_PATTERNS = (
    re.compile(r"(^|/)\.env$"),
    re.compile(r"(^|/)\.env\.(?!example$|sample$|template$)[\w.-]+$"),
    re.compile(r"(^|/)credentials(\.json|\.yaml|\.yml|\.toml)?$"),
    re.compile(r"(^|/)secrets(\.json|\.yaml|\.yml|\.toml)?$"),
)

_SECRET_FILE_SUFFIX_MAP = {
    ".env": ".env.example",
    "credentials.json": "credentials.example.json",
    "credentials.yaml": "credentials.example.yaml",
    "credentials.yml": "credentials.example.yml",
    "secrets.json": "secrets.example.json",
    "secrets.yaml": "secrets.example.yaml",
    "secrets.yml": "secrets.example.yml",
}


def _redirect_secret_path(path: str) -> str | None:
    """Return a safe ``<name>.example<ext>`` redirect, or None."""
    if not path:
        return None
    # Use forward slashes for matching regardless of OS
    norm = path.replace("\\", "/")
    base = norm.rsplit("/", 1)[-1]

    # Fast-path: exact filename match
    if base in _SECRET_FILE_SUFFIX_MAP:
        return norm.replace(base, _SECRET_FILE_SUFFIX_MAP[base])

    # Pattern-based fallback: .env.prod → .env.example.prod? No — the
    # signal is too ambiguous. Just match the bare .env + credentials
    # forms and let the caller deny anything else.
    for pat in _SECRET_FILE_PATTERNS:
        if pat.search(norm):
            if base == ".env":
                return norm.replace(".env", ".env.example")
            # For dotted env variants like `.env.prod`, redirect to
            # `.env.example.prod` so the template flavour is preserved.
            if base.startswith(".env."):
                suffix = base[len(".env."):]
                if suffix in {"example", "sample", "template"}:
                    return None  # already safe
                return norm.replace(base, f".env.example.{suffix}")
    return None


class WriteSecretFileRewriter(BaseGuard):
    """Rewrite ``Write(.env | credentials.json | ...)`` to ``.example``.

    If Claude tries to write a file that looks like it holds secrets,
    redirect the write to the canonical ``.example`` template instead.
    This avoids the common mistake of committing real secrets because
    the model helpfully "wrote out" the config it just learned about.

    Pure Write tool only — Edit is intentionally left alone because
    editing an existing ``.env`` usually means rotating a value, not
    materialising secrets from conversation.
    """

    name = "write_secret_file_rewriter"
    category = GuardCategory.QUALITY
    step_back_reason = ""
    path_scope: list[str] = []

    def check(self, ctx: GuardContext) -> GuardResult | None:
        if ctx.tool_name != "Write":
            return None
        path = ctx.tool_input.get("file_path", "")
        if not isinstance(path, str) or not path:
            return None

        redirect = _redirect_secret_path(path)
        if not redirect or redirect == path:
            return None

        new_input = dict(ctx.tool_input)
        new_input["file_path"] = redirect
        return GuardResult.rewrite(
            updated_input=new_input,
            reason=(
                f"Secret-looking path `{path}` redirected to `{redirect}` — "
                "use the template form and ask the user to fill in real "
                "values separately."
            ),
        )


# ── BashPipeToShellRewriter ─────────────────────────────────

_PIPE_TO_SHELL_RE = re.compile(
    r"(curl|wget)\s+[^|;&]*\|\s*(sudo\s+)?(ba)?sh\b",
    re.IGNORECASE,
)


class BashPipeToShellRewriter(BaseGuard):
    """Break ``curl ... | bash`` / ``wget ... | sh`` into a two-step form.

    Unverified remote-execution is a recurring supply-chain footgun.
    Instead of blocking the call, rewrite it so the download and the
    execution are separated — Claude downloads to a named file, the
    user can inspect it, then execute explicitly.

    The rewritten form is ``curl -fsSL URL -o /tmp/cc-cortex-download.sh
    && echo '[cc-cortex] downloaded — inspect before running'``, which
    no longer matches the regex and is therefore idempotent.
    """

    name = "bash_pipe_to_shell_rewriter"
    category = GuardCategory.QUALITY
    step_back_reason = ""
    path_scope: list[str] = []

    def check(self, ctx: GuardContext) -> GuardResult | None:
        if ctx.tool_name != "Bash":
            return None
        cmd = ctx.tool_input.get("command", "")
        if not isinstance(cmd, str) or not cmd.strip():
            return None
        if not _PIPE_TO_SHELL_RE.search(cmd):
            return None

        # Extract the first URL-ish token so we can preserve the intent
        # in the rewritten command.
        url_match = re.search(r"https?://\S+", cmd)
        url = url_match.group(0) if url_match else "<URL>"
        safe_cmd = (
            f"curl -fsSL {url} -o /tmp/cc-cortex-download.sh && "
            "echo '[cc-cortex] downloaded to /tmp/cc-cortex-download.sh — "
            "inspect the file before running it'"
        )
        new_input = dict(ctx.tool_input)
        new_input["command"] = safe_cmd
        return GuardResult.rewrite(
            updated_input=new_input,
            reason=(
                "pipe-to-shell broken into download + inspect. Review the "
                "script at /tmp/cc-cortex-download.sh before executing."
            ),
        )
