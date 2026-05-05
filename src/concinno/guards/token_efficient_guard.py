"""concinno.guards.token_efficient_guard — warn when subagent briefs
or one-shot Bash commands burn oversized token budgets.

@module token_efficient_guard
@responsibility Signal (never block) when an Agent spawn ``prompt``
    exceeds 20k characters, or a Bash command exceeds 50k characters.
    Competition / benchmark sessions often have fixed token budgets —
    spotting oversized briefs early saves avoidable spend.
@dependencies concinno.guards.base
@exports TokenEfficientGuard, AGENT_BRIEF_WARN_CHARS, BASH_CMD_WARN_CHARS
"""

from __future__ import annotations

from concinno.guards.base import BaseGuard, GuardCategory, GuardContext, GuardResult

# Thresholds — conservative: 20k chars ~ 5k tokens (prompt), 50k chars
# ~ 12.5k tokens (Bash payload). Tuned to signal only on clearly
# oversized payloads so false-positive rate stays low.
AGENT_BRIEF_WARN_CHARS: int = 20_000
BASH_CMD_WARN_CHARS: int = 50_000


def _chars_to_tokens(n_chars: int) -> int:
    """Rough tokens estimate (4 chars ≈ 1 token for English)."""
    return n_chars // 4


class TokenEfficientGuard(BaseGuard):
    """Advise when Agent brief or Bash command looks oversized.

    Signal-only by construction: never DENY. Single-turn benchmarks
    with fixed budgets (e.g. GAIA) break cleanly when a brief eats
    5k+ tokens; this gives the author a chance to trim before spend.
    """

    name = "token_efficient"
    category = GuardCategory.QUALITY
    feature_name = "token_efficient"

    def check(self, ctx: GuardContext) -> GuardResult | None:
        if ctx.tool_name == "Agent":
            return self._check_agent(ctx)
        if ctx.tool_name == "Bash":
            return self._check_bash(ctx)
        return None

    def _check_agent(self, ctx: GuardContext) -> GuardResult | None:
        prompt = ctx.tool_input.get("prompt") or ""
        if not isinstance(prompt, str):
            return None
        n = len(prompt)
        if n <= AGENT_BRIEF_WARN_CHARS:
            return None
        tokens = _chars_to_tokens(n)
        msg = (
            f"[token-efficient] subagent brief is {n:,} chars (~{tokens:,} tokens) — "
            f"exceeds soft limit {AGENT_BRIEF_WARN_CHARS:,} chars. "
            f"Consider trimming: ① drop exposition ② link planning docs "
            f"instead of inlining ③ collapse duplicated rules. "
            f"Signal only — spawn proceeds."
        )
        return GuardResult.allow_advisory(context=msg)

    def _check_bash(self, ctx: GuardContext) -> GuardResult | None:
        command = ctx.tool_input.get("command") or ""
        if not isinstance(command, str):
            return None
        n = len(command)
        if n <= BASH_CMD_WARN_CHARS:
            return None
        tokens = _chars_to_tokens(n)
        msg = (
            f"[token-efficient] Bash command is {n:,} chars (~{tokens:,} tokens) — "
            f"exceeds soft limit {BASH_CMD_WARN_CHARS:,} chars. "
            f"Consider: write a script file and run it, or pipe from stdin. "
            f"Signal only — command proceeds."
        )
        return GuardResult.allow_advisory(context=msg)
