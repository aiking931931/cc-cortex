"""concinno.skill_proactive_router — UserPromptSubmit Skill suggestion.

@module skill_proactive_router
@responsibility Watch every UserPromptSubmit and surface "Skill X
    matches your request, consider invoking via /<skill>" advisories
    when the user's prompt semantically maps to a registered Skill.
    Two-stage matcher: (1) cheap inverted-index lookup against the
    sub-agent A index file ``_AI_BRAIN/_triggers.json``, (2) optional
    Haiku judge for paraphrase / multi-language disambiguation when
    the index is ambiguous.
@dependencies stdlib only on the hot path; ``anthropic`` lazy-imported
    inside the Haiku judge fallback (fail-soft when absent).
@exports DEFAULT_TRIGGERS_INDEX_PATH, ProactiveRouterResult,
    SkillCandidate, build_router_context, default_haiku_judge,
    propose_skills

Cost guard:
    Hard per-prompt budget at ``MAX_HAIKU_COST_USD`` (default
    $0.001 USD ≈ Haiku 4.5 ~250 tokens out at 2026-04 list price).
    Exceeded → judge step skipped, fall back to top-N index hits.
    Token-out cap and ``DEFAULT_MAX_TOKENS`` enforce the budget at
    the request layer; the cost calculator is a *defence in depth*
    so a future model price change cannot silently overshoot.

Multilingual policy:
    Per ``rules/L1/multilingual_triggers.md`` we never enumerate non-
    English keyword lists. The index file holds canonical English
    triggers; the Haiku judge handles meaning-based matching across
    languages on the cheap. Tests cover the path with an injected
    judge so the suite stays offline.

Index schema (sub-agent A's file format — read-only consumer):

    {
      "version": 1,
      "skills": {
        "<skill_name>": {
          "triggers": ["english phrase", "another phrase", ...],
          "category": "<optional>",
          "description": "<optional one-liner>"
        },
        ...
      }
    }

Fallback behaviour:
    When the index is missing, malformed, or has version > 1 we use
    an empty ``{}`` and rely entirely on the Haiku judge (or skip
    silently). The router never raises on bad input.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

__all__ = [
    "DEFAULT_COST_OVERSHOOT_PATH",
    "DEFAULT_HAIKU_MODEL",
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_TRIGGERS_INDEX_PATH",
    "MAX_HAIKU_COST_USD",
    "MIN_PROMPT_CHARS",
    "OVERSHOOT_MULTIPLIER_HARD_BLOCK",
    "ProactiveRouterResult",
    "SkillCandidate",
    "build_router_context",
    "default_haiku_judge",
    "estimate_haiku_cost_usd",
    "load_triggers_index",
    "propose_skills",
    "read_cost_overshoot_state",
    "record_cost_overshoot",
]

# ── tunables ───────────────────────────────────────────────

DEFAULT_HAIKU_MODEL = "claude-haiku-4-5-20251001"

# Tight cap so a runaway prompt can't burn dollars. Haiku 4.5 emits
# ≤256 tokens worth of JSON for a "rank these candidates" job.
DEFAULT_MAX_TOKENS = 256

# Per-prompt cost ceiling. Haiku 4.5 at $1.00 / 1M output tokens means
# 256 tokens ≈ $0.000256. We pad to $0.001 for input tokens + safety.
# Exceeding this disables the judge call for this prompt only.
MAX_HAIKU_COST_USD = 0.001

# Safety net multiplier — actual judge spend > ceiling × this triggers
# a *persistent* overshoot record so future prompts can be hard-denied
# without re-discovering the runaway model. 2× covers transient noise
# (token-count rounding) but flags genuine pricing surprises.
OVERSHOOT_MULTIPLIER_HARD_BLOCK = 2.0

# Persistent record of cost-cap overshoots. Future prompt-submits read
# this; if the recorded overshoot count exceeds the configurable
# threshold, the router refuses to call the judge entirely. File lives
# under ``$HOME/.concinno`` so a stranger pip-install starts fresh and
# upgrades preserve operator state (per Concinno hard-rule #7).
def _default_cost_overshoot_path() -> Path:
    return Path.home() / ".concinno" / "skill_proactive_router_overshoot.json"


DEFAULT_COST_OVERSHOOT_PATH = _default_cost_overshoot_path()

# Skip routing for prompts shorter than this (e.g. "ok", "yes"). The
# judge cannot disambiguate intent from a 2-char string and the index
# is unlikely to score anything useful.
MIN_PROMPT_CHARS = 12

# Hot path budget — keep the cheap stage under 50 ms wall-clock so the
# UserPromptSubmit hook does not drag.
DEFAULT_HOT_PATH_BUDGET_MS = 50

# Maximum number of candidates we return / advise on. Beyond 3 the
# advisory becomes noise.
MAX_CANDIDATES = 3

# Default location of sub-agent A's reverse index. Resolved relative
# to ``$HOME`` so the lib has no hard-coded paths (BoundaryGuard) and
# tests can override via the explicit kwarg.
def _default_triggers_index_path() -> Path:
    return Path.home() / "_AI_BRAIN" / "_triggers.json"


DEFAULT_TRIGGERS_INDEX_PATH = _default_triggers_index_path()


# ── data classes ──────────────────────────────────────────


@dataclass
class SkillCandidate:
    """One candidate Skill ranked by the router.

    ``score`` is a normalised float in ``[0, 1]``. The cheap index hit
    contributes the lower band (≤0.6); a positive judge verdict can lift
    the same name into the upper band (>0.6) so the renderer can
    confidently say "matches" rather than "may match".

    ``rationale`` is an opaque short string — the judge supplies one
    when invoked, otherwise we synthesise one from the matched trigger.
    """

    name: str
    score: float
    rationale: str = ""
    matched_triggers: list[str] = field(default_factory=list)


@dataclass
class ProactiveRouterResult:
    """Full router result handed back to the UserPromptSubmit handler."""

    elapsed_ms: float = 0.0
    candidates: list[SkillCandidate] = field(default_factory=list)
    judge_called: bool = False
    judge_cost_usd: float = 0.0
    skipped_reason: Optional[str] = None
    error: Optional[str] = None
    warnings: list[str] = field(default_factory=list)
    additional_context: str = ""


# ── index loader ───────────────────────────────────────────


def load_triggers_index(
    *,
    path: Optional[Path] = None,
) -> dict[str, dict[str, Any]]:
    """Read sub-agent A's ``_triggers.json`` reverse index.

    Returns the ``"skills"`` map (``{skill_name: {triggers, ...}}``).
    Any failure (missing file, malformed JSON, unsupported version,
    wrong shape) yields an empty dict so the router degrades to "no
    cheap matches" and either calls the judge or skips entirely.

    Args:
        path: Override location. Defaults to
            ``~/_AI_BRAIN/_triggers.json``.
    """
    target = path or DEFAULT_TRIGGERS_INDEX_PATH
    if not target.is_file():
        return {}
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    version = raw.get("version", 1)
    if not isinstance(version, int) or version > 1:
        # Forward-compat: refuse to parse newer schemas blindly.
        return {}
    skills = raw.get("skills", {})
    if not isinstance(skills, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for name, entry in skills.items():
        if not isinstance(name, str) or not name:
            continue
        if not isinstance(entry, dict):
            continue
        triggers = entry.get("triggers", [])
        if not isinstance(triggers, list):
            continue
        cleaned_triggers = [t for t in triggers if isinstance(t, str) and t]
        out[name] = {
            "triggers": cleaned_triggers,
            "category": entry.get("category", ""),
            "description": entry.get("description", ""),
        }
    return out


# ── cheap stage: inverted-index lookup ────────────────────


# Pre-compiled split pattern — matches CJK characters individually plus
# whitespace-delimited Latin tokens. Good enough for cheap lookup; the
# judge handles the hard cases.
_TOKEN_RE = re.compile(
    r"[A-Za-z][A-Za-z0-9_\-]*"  # latin word
    r"|[一-鿿]"          # single CJK char
    r"|[가-힯]+"         # hangul block
    r"|[぀-ヿ]+"         # hiragana / katakana
)


def _tokenise(text: str) -> set[str]:
    """Lower-case word tokens + raw CJK/Hangul/Kana chunks."""
    if not text:
        return set()
    return {m.group(0).lower() for m in _TOKEN_RE.finditer(text)}


def _index_match(
    user_prompt: str,
    index: dict[str, dict[str, Any]],
    *,
    max_candidates: int,
) -> list[SkillCandidate]:
    """Score every Skill in the index by overlap with the prompt tokens.

    Each matched English-trigger phrase contributes 1 point; the score
    is normalised by the number of triggers the skill registers so a
    skill with 20 triggers does not dominate one with 2 just because it
    threw more chances at the wall.
    """
    if not index or not user_prompt:
        return []
    prompt_tokens = _tokenise(user_prompt)
    if not prompt_tokens:
        return []
    candidates: list[SkillCandidate] = []
    for name, entry in index.items():
        triggers: list[str] = entry.get("triggers", [])
        if not triggers:
            continue
        matched: list[str] = []
        for trig in triggers:
            trig_tokens = _tokenise(trig)
            if trig_tokens and trig_tokens.issubset(prompt_tokens):
                matched.append(trig)
                continue
            # cheap substring fallback for multi-word phrases that
            # tokenisation may split awkwardly.
            if trig.lower() in user_prompt.lower():
                matched.append(trig)
        if not matched:
            continue
        # Score in the lower band (≤0.6) so a judge boost can lift
        # promising matches into the "high confidence" band.
        raw_score = len(matched) / max(1, len(triggers))
        score = min(0.6, 0.3 + raw_score * 0.3)
        candidates.append(
            SkillCandidate(
                name=name,
                score=score,
                rationale=f"index match on {matched[0]!r}",
                matched_triggers=matched[:3],
            )
        )
    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates[:max_candidates]


# ── Haiku judge (lazy import, mockable) ────────────────────


def read_cost_overshoot_state(
    *,
    path: Optional[Path] = None,
) -> dict[str, Any]:
    """Read the persistent cost-overshoot ledger.

    Returns ``{"count": int, "last_overshoot_usd": float, "last_seen": float}``.
    A missing / unreadable file degrades to a zeroed dict — never raises.
    The pre-flight gate uses ``count`` to decide whether to hard-deny
    further judge calls; the ledger is monotonically increasing until
    operator manually resets via ``cc cortex skill-router reset``.
    """
    target = path or DEFAULT_COST_OVERSHOOT_PATH
    blank = {"count": 0, "last_overshoot_usd": 0.0, "last_seen": 0.0}
    if not target.is_file():
        return blank
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return blank
    if not isinstance(raw, dict):
        return blank
    return {
        "count": int(raw.get("count", 0) or 0),
        "last_overshoot_usd": float(raw.get("last_overshoot_usd", 0.0) or 0.0),
        "last_seen": float(raw.get("last_seen", 0.0) or 0.0),
    }


def record_cost_overshoot(
    actual_cost_usd: float,
    *,
    path: Optional[Path] = None,
) -> dict[str, Any]:
    """Append a cost-overshoot incident to the persistent ledger.

    Increments ``count`` by 1, refreshes ``last_overshoot_usd`` and
    ``last_seen``. Best-effort: a write failure is swallowed so a
    read-only home dir cannot break the hot prompt path.

    Returns the post-write ledger state (or the in-memory state when the
    write failed, so callers can still report the incident).
    """
    target = path or DEFAULT_COST_OVERSHOOT_PATH
    state = read_cost_overshoot_state(path=target)
    state["count"] = int(state.get("count", 0)) + 1
    state["last_overshoot_usd"] = float(actual_cost_usd)
    state["last_seen"] = time.time()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception:
        pass
    return state


def estimate_haiku_cost_usd(
    *,
    input_tokens: int,
    output_tokens: int,
    model: str = DEFAULT_HAIKU_MODEL,
) -> float:
    """Return the USD cost for a Haiku call at 2026-04 list prices.

    Haiku 4.5 list price: $1.00 / 1M input, $5.00 / 1M output. Numbers
    pinned in source so a future SDK upgrade does not silently change
    the budget calculation. If the model id is not Haiku 4.5 we fall
    back to the same conservative ceiling so the cost guard still
    applies.
    """
    del model  # one rate sheet for all current Haiku tiers we use.
    in_cost = input_tokens * (1.00 / 1_000_000)
    out_cost = output_tokens * (5.00 / 1_000_000)
    return round(in_cost + out_cost, 6)


def default_haiku_judge(
    user_prompt: str,
    candidates: list[SkillCandidate],
    *,
    model: str = DEFAULT_HAIKU_MODEL,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> dict[str, Any]:
    """Real Haiku 4.5 call — lazy import; tests inject a mock judge.

    Returns a dict shaped:

        {
          "verdict": [{"name": str, "score": float, "rationale": str}, ...],
          "input_tokens": int,
          "output_tokens": int,
        }

    Empty ``verdict`` means the judge declined to lift any candidate.
    Any exception (no API key, no SDK installed, network failure, JSON
    parse error) yields an empty verdict so the router fails open to
    the cheap-stage candidates.
    """
    out: dict[str, Any] = {
        "verdict": [],
        "input_tokens": 0,
        "output_tokens": 0,
    }
    if not candidates:
        return out
    try:
        import anthropic  # local, lazy
    except ImportError:
        return out
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return out

    short_list = [
        {
            "name": c.name,
            "score": round(c.score, 3),
            "matched_triggers": c.matched_triggers[:3],
        }
        for c in candidates
    ]
    prompt = (
        "You rank Skill candidates against a user message. The cheap "
        "lexical stage already produced a shortlist; your job is to "
        "decide which entries match the user's *intent* (across any "
        "natural language) and which are accidental string overlaps.\n\n"
        f"User message:\n{user_prompt!r}\n\n"
        f"Candidates (JSON):\n{json.dumps(short_list, ensure_ascii=False)}\n\n"
        "Return ONLY a JSON object with this shape, no prose:\n"
        '  {"verdict": [{"name": "<skill>", "score": <0..1>, '
        '"rationale": "<one short clause>"}]}\n'
        "Score 0.7+ means high confidence. Drop a candidate by omitting "
        "it. Empty verdict is allowed when none match."
    )

    try:
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception:
        return out

    text = ""
    try:
        text = resp.content[0].text  # type: ignore[union-attr]
    except Exception:
        return out
    usage = getattr(resp, "usage", None)
    if usage is not None:
        out["input_tokens"] = int(getattr(usage, "input_tokens", 0) or 0)
        out["output_tokens"] = int(getattr(usage, "output_tokens", 0) or 0)
    try:
        start = text.index("{")
        end = text.rindex("}") + 1
        parsed = json.loads(text[start:end])
    except (ValueError, json.JSONDecodeError):
        return out
    verdict = parsed.get("verdict") if isinstance(parsed, dict) else None
    if not isinstance(verdict, list):
        return out
    cleaned: list[dict[str, Any]] = []
    for item in verdict:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        score = item.get("score")
        if not isinstance(name, str) or not name:
            continue
        if not isinstance(score, (int, float)):
            continue
        cleaned.append({
            "name": name,
            "score": float(max(0.0, min(1.0, score))),
            "rationale": str(item.get("rationale", "")),
        })
    out["verdict"] = cleaned
    return out


def _merge_judge_verdict(
    candidates: list[SkillCandidate],
    verdict: list[dict[str, Any]],
) -> list[SkillCandidate]:
    """Lift candidates into the high-confidence band using judge scores.

    A candidate not present in ``verdict`` keeps its cheap score (still
    visible in the advisory if it remains in the top-N). A candidate
    only in ``verdict`` (judge invented it) is dropped — we only trust
    the judge to *re-rank*, not to *introduce*.
    """
    if not verdict:
        return candidates
    by_name = {v["name"]: v for v in verdict}
    out: list[SkillCandidate] = []
    for cand in candidates:
        v = by_name.get(cand.name)
        if v is None:
            out.append(cand)
            continue
        new_score = float(v.get("score", cand.score))
        out.append(SkillCandidate(
            name=cand.name,
            score=max(cand.score, new_score),
            rationale=v.get("rationale") or cand.rationale,
            matched_triggers=cand.matched_triggers,
        ))
    out.sort(key=lambda c: c.score, reverse=True)
    return out


# ── render ────────────────────────────────────────────────


def build_router_context(candidates: list[SkillCandidate]) -> str:
    """Render the additionalContext advisory for the matched skills.

    Returns ``""`` when the list is empty so the caller can do a one-
    line ``if text: emit(text)`` check. The body lists each match on
    one line — the agent decides whether to invoke; we never invoke a
    Skill on the user's behalf here.
    """
    if not candidates:
        return ""
    lines = []
    for cand in candidates:
        # Skip negligible matches. 0.3 is the index-match floor.
        if cand.score < 0.3:
            continue
        lines.append(
            f"- /{cand.name} (score={cand.score:.2f}) — "
            f"{cand.rationale or 'matches your request'}"
        )
    if not lines:
        return ""
    body = "\n".join(lines)
    return (
        "🎯 Skill suggestion (proactive router): the following Skills "
        "match your request, consider invoking them:\n"
        f"{body}\n"
    )


# ── orchestrator ───────────────────────────────────────────


def _short_circuit_reason(
    *,
    enabled: bool,
    user_prompt: str,
    cost_ceiling_usd: float,
    result: ProactiveRouterResult,
) -> Optional[str]:
    """Return the ``skipped_reason`` string when the router should bail.

    Pulled out of :func:`propose_skills` so the hot orchestrator stays
    inside the structural ``func_length`` budget. Side-effects (the env
    opt-out branch and the emergency-deny warning) are written to
    ``result`` directly — return value is the skip reason or ``None``.
    """
    if not enabled:
        return "disabled"
    if os.environ.get("CONCINNO_SKILL_PROACTIVE_ROUTER_DISABLED") in {
        "1", "true", "yes", "on",
    }:
        return "disabled via env"
    if not user_prompt or len(user_prompt.strip()) < MIN_PROMPT_CHARS:
        return "prompt too short"
    if cost_ceiling_usd <= 0:
        result.warnings.append(
            "cost ceiling <= 0 hard-denies the entire router"
        )
        return "cost ceiling zero — emergency deny"
    return None


def _run_judge_stage(
    *,
    user_prompt: str,
    candidates: list[SkillCandidate],
    judge: Optional[Callable[[str, list[SkillCandidate]], dict[str, Any]]],
    cost_ceiling_usd: float,
    overshoot_state_path: Optional[Path],
    result: ProactiveRouterResult,
) -> list[SkillCandidate]:
    """Invoke the judge once cheap stage produced candidates.

    Mutates ``result`` in place (``judge_called``, ``judge_cost_usd``,
    ``warnings``) and returns the merged candidate list. Persistent
    overshoot ledger is incremented when actual spend > safety net.
    """
    j = judge or default_haiku_judge
    try:
        verdict_raw = j(user_prompt, candidates)
    except Exception as exc:  # noqa: BLE001
        verdict_raw = {}
        result.warnings.append(f"judge raised: {exc}")
    verdict = (
        verdict_raw.get("verdict", [])
        if isinstance(verdict_raw, dict) else []
    )
    in_tok = int((verdict_raw or {}).get("input_tokens", 0) or 0)
    out_tok = int((verdict_raw or {}).get("output_tokens", 0) or 0)
    actual_cost = estimate_haiku_cost_usd(
        input_tokens=in_tok, output_tokens=out_tok,
    )
    result.judge_called = True
    result.judge_cost_usd = actual_cost

    safety_threshold = (
        cost_ceiling_usd * OVERSHOOT_MULTIPLIER_HARD_BLOCK
    )
    if actual_cost > safety_threshold:
        new_ledger = record_cost_overshoot(
            actual_cost, path=overshoot_state_path,
        )
        result.warnings.append(
            f"actual judge cost {actual_cost:.6f} > safety threshold "
            f"{safety_threshold:.6f}; future budgets blocked "
            f"(overshoot count={new_ledger['count']})"
        )
    return _merge_judge_verdict(candidates, verdict)


def propose_skills(
    user_prompt: str,
    *,
    index_path: Optional[Path] = None,
    judge: Optional[Callable[[str, list[SkillCandidate]], dict[str, Any]]] = None,
    cost_ceiling_usd: float = MAX_HAIKU_COST_USD,
    max_candidates: int = MAX_CANDIDATES,
    hot_path_budget_ms: int = DEFAULT_HOT_PATH_BUDGET_MS,
    enabled: bool = True,
    overshoot_state_path: Optional[Path] = None,
    max_persistent_overshoots: int = 3,
) -> ProactiveRouterResult:
    """Run the proactive Skill router for a single UserPromptSubmit.

    The function never raises — every failure mode populates ``error``
    or ``warnings`` and returns a valid :class:`ProactiveRouterResult`.

    Pipeline:

    1. Honour ``enabled=False`` and the env opt-out.
    2. Pre-flight cost cap: estimate Haiku spend; if >ceiling **the
       judge stage is hard-denied** (renderer still produces cheap
       index matches, but no Haiku call is made). ``cost_ceiling_usd<=0``
       triggers an emergency hard-deny that also skips the cheap stage.
    3. Persistent overshoot ledger: if ``read_cost_overshoot_state()``
       reports ``count >= max_persistent_overshoots`` the judge is
       hard-denied regardless of the per-prompt estimate (defence in
       depth against a runaway pricing change).
    4. Load the cheap inverted index and run the substring/token match.
    5. Run the judge (real or injected) when permitted; merge verdicts.
    6. Safety net: actual Haiku spend > ``cost_ceiling_usd *
       OVERSHOOT_MULTIPLIER_HARD_BLOCK`` writes a row to the persistent
       overshoot ledger so future prompts pre-flight-deny.
    7. Render the advisory.

    Args:
        user_prompt: The user's submitted text.
        index_path: Override sub-agent A's index file location.
        judge: Inject a deterministic judge for tests; defaults to
            :func:`default_haiku_judge` (lazy real call).
        cost_ceiling_usd: Per-prompt budget. Pre-flight hard-deny when
            estimated > ceiling. ``<= 0`` ⇒ emergency deny (cheap-stage
            also skipped — operator killed the feature for the session).
        max_candidates: Cap on returned candidates.
        hot_path_budget_ms: Soft target for the *cheap* stage; over
            budget is logged but not fatal.
        enabled: Master switch — easier than re-reading FEATURE_META.
        overshoot_state_path: Override the persistent ledger path
            (tests use a tmp dir; production uses
            ``~/.concinno/skill_proactive_router_overshoot.json``).
        max_persistent_overshoots: Persistent overshoot count at or
            beyond which the judge is denied without further attempts.

    Goodhart-fix history (2026-04-28 ship-fix sub-agent O):
        Original code logged ``actual_cost > ceiling`` as a warning but
        still merged the verdict — a "cap" that never denied. Now the
        pre-flight estimate hard-denies the judge call (no API spend)
        and the post-call safety net writes a persistent record so a
        runaway pricing change cannot drain the budget across many
        prompts before the operator notices.
    """
    t0 = time.monotonic()
    result = ProactiveRouterResult()
    deadline = t0 + (hot_path_budget_ms / 1000.0)

    def _elapsed_ms() -> float:
        return (time.monotonic() - t0) * 1000.0

    try:
        skip = _short_circuit_reason(
            enabled=enabled,
            user_prompt=user_prompt,
            cost_ceiling_usd=cost_ceiling_usd,
            result=result,
        )
        if skip is not None:
            result.skipped_reason = skip
            result.elapsed_ms = _elapsed_ms()
            return result

        index = load_triggers_index(path=index_path)
        if not index:
            result.warnings.append("triggers index missing or empty")

        candidates = _index_match(
            user_prompt, index, max_candidates=max_candidates,
        )

        if time.monotonic() > deadline:
            result.warnings.append(
                f"cheap-stage over budget at {_elapsed_ms():.0f}ms"
            )

        if not candidates:
            # Judge is a re-ranker, not a generator — skip without spend.
            result.elapsed_ms = _elapsed_ms()
            return result

        # Pre-flight cost gate (hard deny — Goodhart-2 fix).
        estimated = estimate_haiku_cost_usd(
            input_tokens=200,
            output_tokens=80,
        )
        if estimated > cost_ceiling_usd:
            result.warnings.append(
                f"judge hard-denied: pre-flight estimate "
                f"{estimated:.6f} > ceiling {cost_ceiling_usd:.6f}"
            )
            result.candidates = candidates
            result.additional_context = build_router_context(candidates)
            result.elapsed_ms = _elapsed_ms()
            return result

        # Persistent overshoot ledger gate.
        ledger = read_cost_overshoot_state(path=overshoot_state_path)
        if ledger.get("count", 0) >= max_persistent_overshoots:
            result.warnings.append(
                f"judge hard-denied: persistent overshoot count "
                f"{ledger['count']} >= {max_persistent_overshoots}"
            )
            result.candidates = candidates
            result.additional_context = build_router_context(candidates)
            result.elapsed_ms = _elapsed_ms()
            return result

        candidates = _run_judge_stage(
            user_prompt=user_prompt,
            candidates=candidates,
            judge=judge,
            cost_ceiling_usd=cost_ceiling_usd,
            overshoot_state_path=overshoot_state_path,
            result=result,
        )
        candidates = candidates[:max_candidates]

        result.candidates = candidates
        result.additional_context = build_router_context(candidates)
        result.elapsed_ms = _elapsed_ms()
        return result

    except Exception as exc:  # noqa: BLE001
        result.error = f"unexpected: {exc}"
        result.elapsed_ms = _elapsed_ms()
        return result
