"""concinno.guards.benchmark_setup_guard — inject per-harness SOP on
first Write / Edit that touches a known benchmark harness.

@module benchmark_setup_guard
@responsibility Detect benchmark / eval-harness keywords (GAIA,
    AgentBench, OSWorld, WebArena, HumanEval, MMLU, BEIR, locomo,
    ImpliRet, E2Rank) in file paths or content. First hit per harness
    per session injects a short SOP reminder (token budget, official
    metric, screenshot requirement, etc). Subsequent hits silent —
    state persisted via StateStore.
@dependencies concinno.guards.base, concinno.core.state_store
@exports BenchmarkSetupGuard, HARNESS_SOP
"""

from __future__ import annotations

import os
import re

from concinno.core.state_store import StateStore
from concinno.guards.base import BaseGuard, GuardCategory, GuardContext, GuardResult

# SOP text per harness. Keep concise — these get injected into LLM
# context, so every line should be load-bearing.
HARNESS_SOP: dict[str, str] = {
    "GAIA": (
        "GAIA harness detected. Rules: ① official metric = weighted score "
        "across level 1-3, NOT raw accuracy ② log token cost per task (cost/score "
        "ratio matters) ③ keep answers <30 words, exact-match grader ④ submit via "
        "HuggingFace leaderboard API not local CSV."
    ),
    "AgentBench": (
        "AgentBench detected. Rules: ① 8 task families × per-family metric, DO "
        "NOT average naively ② Docker environments — pin image hash ③ log trace "
        "JSON per task (replay requirement) ④ scoring script lives in upstream repo."
    ),
    "OSWorld": (
        "OSWorld detected. Rules: ① screenshot required per action for audit "
        "② use provided VM snapshot, do NOT run in host OS ③ official metric = "
        "task success rate after 15-step budget ④ action-trace JSON must be "
        "archived for reproducibility."
    ),
    "WebArena": (
        "WebArena detected. Rules: ① Playwright + headless Chrome, pin browser "
        "version ② reset DB snapshot between tasks (contamination kills score) "
        "③ official metric = task completion with string-match check ④ budget "
        "15-30 steps per task depending on domain."
    ),
    "HumanEval": (
        "HumanEval detected. Rules: ① pass@1 is default, pass@10 / pass@100 "
        "require temperature ≥0.2 ② sandbox execution required — never eval "
        "untrusted code in-process ③ use official grading script, custom "
        "regex WILL drift from canonical."
    ),
    "MMLU": (
        "MMLU detected. Rules: ① 57 subjects × accuracy, report both macro "
        "and micro average ② MCQ A/B/C/D answer extraction — many papers "
        "differ on tie-break, pin your parser ③ 5-shot is default, zero-shot "
        "numbers are NOT comparable to 5-shot leaderboard."
    ),
    "BEIR": (
        "BEIR detected. Rules: ① nDCG@10 is primary metric (not top-1, not "
        "MRR) ② 18 datasets — always report per-dataset AND average ③ use "
        "pytrec_eval official grading script ④ retrieval + rerank costs "
        "must be logged separately."
    ),
    "locomo": (
        "LoCoMo (long-context memory) detected. Rules: ① 10 conversations × "
        "~300 turns each — memory span matters ② 5 task categories (single-hop, "
        "multi-hop, temporal, open-ended, adversarial) report per-category "
        "③ official metric varies by task — read the harness README before "
        "reporting a single number."
    ),
    "ImpliRet": (
        "ImpliRet detected. Rules: ① official metric = nDCG@10 (NOT top-1 — "
        "translating top-1 gains to nDCG@10 is NOT linear, per MEMORY #17) "
        "② 4 sub-tasks × nDCG — report all four ③ SPS router wins over naive "
        "fusion when per-query peakedness detected."
    ),
    "E2Rank": (
        "E2Rank detected. Rules: ① nDCG@k evaluation at multiple k ② pairwise "
        "or pointwise scoring — different leaderboards ③ calibration matters "
        "when fusing across rerankers (see MEMORY #28 score_margin trap)."
    ),
}

# Match basis — case-sensitive markers and case-insensitive fallbacks.
# `(?:^|[^A-Za-z0-9])` as left anchor accepts start-of-string, whitespace,
# punctuation, AND underscore — `\b` alone misses `_` as a boundary and
# would fail on filenames like `GAIA_run.py`. `(?:[^A-Za-z0-9]|$)` is the
# mirrored right anchor. Patterns below use this idiom where we want to
# tolerate underscores as separators.
_HARNESS_PATTERNS: dict[str, re.Pattern[str]] = {
    # Exact-case identifiers first (reduce false-positive on prose).
    "GAIA": re.compile(
        r"(?:^|[^A-Za-z0-9])GAIA(?:[^A-Za-z0-9]|$)|\bgaia[-_]benchmark\b|\bgaia_eval\b",
    ),
    "AgentBench": re.compile(
        r"(?:^|[^A-Za-z0-9])AgentBench(?:[^A-Za-z0-9]|$)|\bagent[-_]bench\b",
        re.IGNORECASE,
    ),
    "OSWorld": re.compile(
        r"(?:^|[^A-Za-z0-9])OSWorld(?:[^A-Za-z0-9]|$)|\bos[-_]world[-_]benchmark\b|\bosworld[-_]",
        re.IGNORECASE,
    ),
    "WebArena": re.compile(
        r"(?:^|[^A-Za-z0-9])WebArena(?:[^A-Za-z0-9]|$)|\bweb[-_]arena[-_]benchmark\b|\bwebarena[-_.]",
        re.IGNORECASE,
    ),
    "HumanEval": re.compile(
        r"(?:^|[^A-Za-z0-9])HumanEval(?:[^A-Za-z0-9]|$)|\bhuman[-_]eval\b|\bhumaneval[-_.]",
        re.IGNORECASE,
    ),
    "MMLU": re.compile(r"(?:^|[^A-Za-z0-9])MMLU(?:[^A-Za-z0-9]|$)"),
    "BEIR": re.compile(
        r"(?:^|[^A-Za-z0-9])BEIR(?:[^A-Za-z0-9]|$)|\bbeir[-_](?:data|corpus|bench)\b",
        re.IGNORECASE,
    ),
    "locomo": re.compile(r"\blocomo\b|\bLoCoMo\b"),
    "ImpliRet": re.compile(
        r"(?:^|[^A-Za-z0-9])ImpliRet(?:[^A-Za-z0-9]|$)|\bimplicit[-_]retrieval\b",
        re.IGNORECASE,
    ),
    "E2Rank": re.compile(
        r"(?:^|[^A-Za-z0-9])E2Rank(?:[^A-Za-z0-9]|$)|\be2[-_]rank\b",
        re.IGNORECASE,
    ),
}

# State store namespace / key.
_STATE_NS = "benchmark_setup"
_KEY_INJECTED = "injected"  # list of harness names already announced


def detect_harnesses(path: str, content: str) -> list[str]:
    """Return list of harness names matched in *path* or *content*.

    Pure function — deterministic + testable. Order stable (dict iter
    order matches insertion order in CPython 3.7+).
    """
    hits: list[str] = []
    for harness, pattern in _HARNESS_PATTERNS.items():
        # path scan uses lowered basename plus full path
        p_hit = bool(path and pattern.search(path))
        c_hit = bool(content and pattern.search(content))
        if p_hit or c_hit:
            hits.append(harness)
    return hits


class BenchmarkSetupGuard(BaseGuard):
    """Inject SOP reminder on first detection per harness per session.

    COGNITIVE layer — advisory, ALLOW with context. Uses StateStore
    to remember which harnesses have been announced so SOP text
    doesn't spam on every edit.
    """

    name = "benchmark_setup"
    category = GuardCategory.COGNITIVE
    feature_name = "benchmark_setup"

    def __init__(self, state_store: StateStore | None = None) -> None:
        self._store: StateStore | None = state_store

    def _get_store(self, ctx: GuardContext) -> StateStore:
        if self._store is not None:
            return self._store
        cache = ctx.cache_dir or os.path.join(
            os.environ.get("CLAUDE_PROJECT_DIR", "."), ".concinno_cache",
        )
        return StateStore(cache)

    def check(self, ctx: GuardContext) -> GuardResult | None:
        if ctx.tool_name not in ("Write", "Edit"):
            return None

        path = ctx.tool_input.get("file_path", "") or ""
        content = (
            ctx.tool_input.get("content", "")
            or ctx.tool_input.get("new_string", "")
            or ""
        )
        if not (path or content):
            return None

        hits = detect_harnesses(path, content)
        if not hits:
            return None

        store = self._get_store(ctx)
        session_id = ctx.session_id or "default"
        try:
            state = store.read(_STATE_NS, session_id, default={})
        except Exception:
            state = {}
        injected: list[str] = list(state.get(_KEY_INJECTED, []))

        new_hits = [h for h in hits if h not in injected]
        if not new_hits:
            return None

        # Persist injection.
        injected.extend(new_hits)
        try:
            store.write(
                _STATE_NS,
                session_id,
                {**state, _KEY_INJECTED: injected},
            )
        except Exception:
            # Persistence failure is non-fatal — we'll just re-announce
            # next time. Don't abort the guard.
            pass

        sections: list[str] = []
        for harness in new_hits:
            sections.append(f"### {harness}\n{HARNESS_SOP[harness]}")
        body = "\n\n".join(sections)
        msg = f"[benchmark-setup] harness SOP — shown once per session:\n\n{body}"
        return GuardResult.allow(context=msg)
