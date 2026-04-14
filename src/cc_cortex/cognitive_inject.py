"""cc_cortex.cognitive_inject — Three-layer knowledge router.

@module cognitive_inject
@responsibility Route knowledge across three layers: rules (index) → RAG
    (summary) → Skill (full text pointer). Condition-triggered, not blind.
@dependencies cc_cortex.hooks.io_utils
@exports build_cognitive_context, build_thinking_directives,
    build_rag_context (alias: route_knowledge), build_delivery_standards

Architecture — where each type of knowledge lives:

  rules/*.md = Cognitive framework (anti-bias, three-layer thinking)
    → Re-injected every tool call by Claude Code. Survives compact.
    → This is the PRIMARY cognitive layer. Not this module.

  CLAUDE.md = Core identity + project context
    → Re-injected every user message. Survives compact.

  This module = Knowledge ROUTER for subagents + dynamic RAG
    → SessionStart: diluted by context growth, eaten by compact.
    → SubagentStart: fresh context, primacy effect — PRIMARY beneficiary.
    → PostToolUse: self-renewing, recency position.

Three-layer routing (condition-triggered):
  Layer 1 — Index: correction counts (always, ~50t)
  Layer 2 — Summary: matching corrections (on keyword hit, ~150t)
  Layer 3 — Pointer: Skill path (on match, ~30t, agent reads on-demand)

  rules→RAG→Skill linkage: RAG matches task keywords against learnings
  AND skill descriptions. Matched skills appear as pointers, not full text.
  Research shows hierarchical RAG only wins when condition-triggered
  (RAPTOR ICLR 2024, GraphRAG-Bench ICLR 2026: complex RAG often ≤ flat).

Unhardnable cognition (kept in soft layer for subagents):
  L0 (~50t): Process rules with no gate/hook equivalent
  L1 (~70t): Anti-bias (pure cognition, cannot be structurally detected)
  L2 (~200t): Deep cognition framework (reasoning patterns)

Already hardened (removed — Attention Budget conservation):
  - "Read before Edit" → think_inject.py blind_edit gate
  - "New module must be imported" → on_subagent_stop.py import verification
  - "Consecutive failures → switch strategy" → think_inject.py failure_trigger
  - "Absolute paths" → pre_tool_guards.py path check
"""

from __future__ import annotations

import json
import os
import re

# ── 1. Thinking Directives — Layered ──────────────────────

# ── Prompt engineering constraints ────────────────────────
# All injected text MUST follow:
#   1. Token cap: L0 ≤60t | L1 ≤80t | L2 ≤200t | Delivery ≤80t
#   2. U-shape attention: most important at FIRST and LAST line
#   3. Gas-state → cognitive_anchor.py ONLY. Here = pure imperative.
#   4. Line cap: L0 ≤5 | L1 ≤7 | L2 ≤10 | Delivery ≤7 (hard max)

# L0: Unhardnable process rules (~40t, ≤4 lines). Pure imperative.
# Gas-state lives in cognitive_anchor.py (identity). Here = operations.
_L0_HARD_RULES = """\
- Fix all errors you see now. ✅done ⏸half(where+why).
- Unsure → look it up. Don't guess.
- Rank by CP: ①likelihood ②ease → highest first."""

# L1: Anti-bias (~60t, ≤6 lines). Imperative counter-bias checklist.
_L1_ANTI_BIAS = """\
- First instinct ≠ best. List 3+ options before choosing.
- Find evidence against yourself. Can you disprove your answer?
- Time spent ≠ reason to continue. Wrong direction → turn.
- What should be here but isn't? What should happen but didn't?
- A changed, B improved ≠ A caused B. Without A, would B self-heal?"""

# L2: Deep cognition (~120t, ≤8 lines). Imperative method steps.
_L2_COGNITION = """\
- Root cause: diverge → CP(likelihood×ease) → converge → test highest.
- User's framing may mislead. Low confidence → say so, then verify.
- Sweet spot: simplest + fewest side effects. Stuck ≥2 rounds → escalate.
- Counterfactual: A fixed B ≠ root cause. Without A, would B self-heal?
- Inversion: how would this fail? Avoid those paths.
- Every 3-5 steps: drifting? stuck? repeating? \
Remove it — what breaks? Nothing = don't build."""


def build_thinking_directives(complexity: str = "full") -> str:
    """Return layered thinking directives (only unhardnable cognition).

    Args:
        complexity: "minimal" for L0 only (~50 tokens),
                   "standard" for L0+L1 (~120 tokens),
                   "full" for L0+L1+L2 (~320 tokens).
    """
    if complexity == "minimal":
        return _L0_HARD_RULES
    if complexity == "standard":
        return _L0_HARD_RULES + "\n\n" + _L1_ANTI_BIAS
    return _L0_HARD_RULES + "\n\n" + _L1_ANTI_BIAS + "\n\n" + _L2_COGNITION


# ── 2. Memory Context (記憶認知) ─────────────────────────

# Three-layer architecture to save tokens:
#   Layer 1: Index (always inject, ~50 tokens) — titles only
#   Layer 2: Summary (on hit, ~150 tokens) — one-line per item
#   Layer 3: Pointer (path only, ~30 tokens) — subagent reads if needed


def _depth_from_c0(workspace: str) -> str:
    """Map C0Router complexity to inject depth. Fail-safe to 'full'."""
    try:
        from cc_cortex.core.state_store import StateStore
        store = StateStore(
            os.path.join(workspace, ".cc_cortex_cache") if workspace else "",
        )
        session_id = os.environ.get("CLAUDE_SESSION_ID", "")
        if not session_id or not workspace:
            return "full"
        state = store.read("c0_route", session_id, default={})
        complexity = state.get("complexity", "complicated")
        return {"simple": "minimal", "complicated": "standard"}.get(
            complexity, "full",
        )
    except Exception:
        return "full"


def _load_learnings(workspace: str) -> list[dict]:
    """Load learnings.json items. Returns [] on any failure."""
    try:
        from cc_cortex.hooks.io_utils import learnings_path
        path = learnings_path()
    except ImportError:
        from cc_cortex.core.config import get_config
        brain_dir = get_config().brain_dir
        path = os.path.join(
            workspace, brain_dir, "01_Memory", "evolution", "learnings.json",
        )
    if not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("learnings", [])
    except Exception:
        return []


def _match_keywords(text: str, keywords: list[str]) -> bool:
    """Check if any keyword appears in text (case-insensitive)."""
    text_lower = text.lower()
    return any(kw.lower() in text_lower for kw in keywords)


def _extract_task_keywords(task_prompt: str) -> list[str]:
    """Extract meaningful keywords from task prompt for RAG matching."""
    if not task_prompt:
        return []
    # Remove common stop words, keep meaningful terms
    words = re.findall(r'[a-zA-Z_\u4e00-\u9fff]{2,}', task_prompt[:500])
    stop = {
        "the", "this", "that", "with", "from", "into", "for", "and",
        "you", "your", "should", "must", "please", "make", "use",
        "implement", "create", "write", "add", "new", "file",
    }
    return [w for w in words if w.lower() not in stop][:20]


def _build_index(learnings: list[dict], task_keywords: list[str]) -> str:
    """Layer 1: Index of corrections (titles only, ~50 tokens)."""
    if not learnings:
        return ""

    # Filter to count≥2 unpromoted
    relevant = [
        it for it in learnings
        if not it.get("promoted") and it.get("count", 0) >= 2
    ]
    if not relevant:
        return ""

    relevant.sort(key=lambda x: x.get("count", 0), reverse=True)
    lines = ["📋 記憶索引（corrections）："]
    for it in relevant[:8]:
        key = it.get("pattern_key", it.get("domain", "?"))
        count = it.get("count", 0)
        lines.append(f"  [{count}x] {key}")
    return "\n".join(lines)


def _build_summaries(
    learnings: list[dict], task_keywords: list[str],
) -> str:
    """Layer 2: Summaries of relevant corrections (~150 tokens)."""
    if not learnings or not task_keywords:
        return ""

    hits: list[dict] = []
    for it in learnings:
        if it.get("promoted"):
            continue
        text = it.get("correction_text", "") + " " + it.get("pattern_key", "")
        if _match_keywords(text, task_keywords):
            hits.append(it)

    if not hits:
        return ""

    hits.sort(key=lambda x: x.get("count", 0), reverse=True)
    lines = ["⚠ 相關糾正（摘要）："]
    for it in hits[:5]:
        key = it.get("pattern_key", "?")
        count = it.get("count", 0)
        text = it.get("correction_text", "")[:80]
        lines.append(f"  - [{count}x|{key}] {text}")
    return "\n".join(lines)


def _load_skill_index(workspace: str) -> list[dict]:
    """Load skill metadata from SKILL.md frontmatter (name + description).

    Cached per call (no disk cache — skills change rarely, cost is low).
    Returns [{"name": "kb_audio", "desc": "音訊...", "path": "..."}].
    """
    kb_dir = os.path.join(workspace, ".claude", "skills")
    if not os.path.isdir(kb_dir):
        return []

    skills: list[dict] = []
    try:
        for entry in os.listdir(kb_dir):
            skill_dir = os.path.join(kb_dir, entry)
            if not os.path.isdir(skill_dir):
                continue
            skill_file = os.path.join(skill_dir, "SKILL.md")
            if not os.path.isfile(skill_file):
                continue
            # Read first 500 bytes for frontmatter description
            try:
                with open(skill_file, encoding="utf-8") as f:
                    head = f.read(500)
            except OSError:
                continue
            desc = ""
            for line in head.splitlines():
                if line.startswith("description:"):
                    desc = line[len("description:"):].strip()
                    break
            skills.append({
                "name": entry,
                "desc": desc,
                "path": f".claude/skills/{entry}/SKILL.md",
            })
    except OSError:
        pass
    return skills


def _build_pointers(
    workspace: str,
    task_keywords: list[str],
    rag_domains: list[str] | None = None,
) -> str:
    """Layer 3: Route to relevant Skills (condition-triggered).

    Matches against BOTH task keywords AND RAG hit domains.
    This is the linkage: RAG finds a correction → pointer routes to
    the Skill that covers that domain → agent reads on-demand.

    Args:
        workspace: Project root.
        task_keywords: Keywords from task prompt.
        rag_domains: Pattern keys from RAG hits (correction domains).
    """
    if not task_keywords and not rag_domains:
        return ""

    skills = _load_skill_index(workspace)
    if not skills:
        return ""

    # Combine task keywords + RAG domain words for broader matching
    # Split RAG domains like "audio-fix" into ["audio", "fix"]
    all_keywords = list(task_keywords)
    if rag_domains:
        for domain in rag_domains:
            all_keywords.extend(re.split(r'[-_\s]+', domain))

    hits: list[str] = []
    for sk in skills:
        # Match against skill name AND description (trigger keywords)
        matchable = sk["name"] + " " + sk["desc"]
        if _match_keywords(matchable, all_keywords):
            hits.append(sk["path"])

    if not hits:
        return ""

    lines = ["📖 相關 Skill（按需 Read）："]
    for h in hits[:3]:
        lines.append(f"  → {h}")
    return "\n".join(lines)


def build_rag_context(task_prompt: str, workspace: str) -> str:
    """Three-layer knowledge router: index → summary → Skill pointer.

    Condition-triggered routing (not blind injection):
      - Layer 1 (index): Always if corrections exist (~50t)
      - Layer 2 (summary): Only if task keywords match corrections (~150t)
      - Layer 3 (pointer): Only if RAG hits or keywords match Skills (~30t)

    The linkage: RAG Layer 2 finds matching corrections → their domains
    feed into Layer 3 → Layer 3 finds the Skill covering that domain.
    Agent reads the Skill on-demand (never injected as full text).

    Token budget: 0t (no hits) → ~50t (index only) → ~230t (full route).
    """
    learnings = _load_learnings(workspace)
    keywords = _extract_task_keywords(task_prompt)

    parts: list[str] = []

    index = _build_index(learnings, keywords)
    if index:
        parts.append(index)

    # Layer 2: summaries (condition: keyword match)
    summaries = _build_summaries(learnings, keywords)
    rag_domains: list[str] = []
    if summaries:
        parts.append(summaries)
        # Extract domains from RAG hits to feed into Layer 3
        for it in learnings:
            if it.get("promoted"):
                continue
            text = (
                it.get("correction_text", "")
                + " " + it.get("pattern_key", "")
            )
            if _match_keywords(text, keywords):
                pk = it.get("pattern_key", "")
                if pk:
                    rag_domains.append(pk)

    # Layer 3: Skill pointers (condition: task keywords OR RAG domains)
    pointers = _build_pointers(workspace, keywords, rag_domains)
    if pointers:
        parts.append(pointers)

    return "\n".join(parts)


# ── 3. Delivery Standards (交付標準) ─────────────────────

_CODE_KEYWORDS = re.compile(
    r'(?:implement|create|write|build|refactor|fix|module|class|function|'
    r'component|實作|建立|寫|模組|重構|修復|新增)',
    re.IGNORECASE,
)

# Delivery standards (~70t, ≤7 lines). Pure imperative.
_CODE_DELIVERY = """\
- No callers = dead code. Don't write it.
- W: grep confirms callers. Delete it → something breaks = wired.
- I: unified template, correct location, exported to index.
- D: tests + lint zero + Zod schema where applicable.
- Errors you see now → fix now, or document why you can't."""


def build_delivery_standards(task_prompt: str) -> str:
    """Return delivery standards for code tasks. Empty for non-code."""
    if _CODE_KEYWORDS.search(task_prompt):
        return _CODE_DELIVERY
    return ""


# ── Unified Entry Point ──────────────────────────────────


def build_cognitive_context(
    task_prompt: str = "",
    workspace: str = "",
    *,
    agent_type: str = "",
    cognition_depth: str = "",
) -> str:
    """Knowledge router + unhardnable cognition for subagents.

    Primary value: SubagentStart (fresh context, primacy effect).
    Parent session: rules/*.md + CLAUDE.md are the real cognitive layer
    (re-injected every message/tool call, survive compact).

    Cognition depth (identity-driven or fallback):
      - "minimal": L0 only (~50 tokens) — Recorder
      - "standard": L0 + L1 anti-bias (~120t) — Surgeon, Engineer
      - "full": L0 + L1 + L2 (~320t) — Craftsman, Architect, Inquirer

    Args:
        task_prompt: Task description (empty for subagents).
        workspace: Absolute workspace path.
        agent_type: Subagent type (empty for parent session).
        cognition_depth: Override from subagent identity assignment.
    """
    sections: list[str] = []

    # Determine complexity: identity-driven > agent_type > C0Router > parent
    is_subagent = bool(agent_type)
    if cognition_depth:
        depth = cognition_depth
    elif agent_type in {"Explore", "Plan", "claude-code-guide"}:
        depth = "minimal"
    elif is_subagent:
        depth = "standard"
    else:
        # Parent session: use C0Router complexity to save tokens
        # Simple → minimal (~50t), Complicated → standard (~120t),
        # Complex/Chaotic → full (~320t)
        depth = _depth_from_c0(workspace)


    sections.append(build_thinking_directives(depth))

    # Memory RAG: index always (50 tokens), summary only if keywords
    if workspace:
        rag = build_rag_context(task_prompt, workspace)
        if rag:
            sections.append(rag)

    # Delivery: code tasks or execution subagents
    if task_prompt:
        delivery = build_delivery_standards(task_prompt)
        if delivery:
            sections.append(delivery)
    elif is_subagent and depth != "minimal":
        # No task_prompt for subagents, but execution agents
        # get delivery basics unconditionally
        sections.append(_CODE_DELIVERY)

    # Cross-session cognitive pool (1.16 cache module). Closes the
    # islanded-module gap: pool sections were being written by
    # microcompact + l2_distill but never read into a subagent's
    # primacy slot. Fail-safe — pool inject is supplementary.
    try:
        from cc_cortex.cognitive_pool_inject import build_pool_context
        pool_ctx = build_pool_context(task_prompt=task_prompt)
        if pool_ctx:
            sections.append(pool_ctx)
    except Exception:  # noqa: BLE001
        pass

    return "\n\n".join(sections)


# Semantic alias — this module IS the knowledge router
route_knowledge = build_rag_context
