"""Tests for ``concinno.skill_proactive_router``.

Coverage matrix (≥18 cases):

  1. Empty / blank prompt → skipped reason set, no judge call.
  2. Disabled flag (``enabled=False``) → skipped, judge never invoked.
  3. Env opt-out → skipped, judge never invoked.
  4. Missing index file → warning recorded, no candidates, no judge call.
  5. Malformed index JSON → graceful empty dict.
  6. Unsupported index version → returns empty dict.
  7. Index hit (English trigger) → cheap candidate produced.
  8. Index hit (multilingual via substring fallback) → cheap candidate.
  9. No index hit → judge skipped (re-ranker, never generator).
 10. Mock judge re-ranks index hits → score lifted into high band.
 11. Judge that invents new names → invented entries dropped.
 12. Cost guard pre-flight skip when ceiling too low.
 13. Cost guard records actual cost from injected judge.
 14. Judge raising → fail-soft, warning logged, original candidates kept.
 15. ``build_router_context`` rendering with multiple candidates.
 16. ``build_router_context`` empty list → "" (no inject branch).
 17. ``build_router_context`` filters score < 0.3 (negligible).
 18. ``estimate_haiku_cost_usd`` matches list price.
 19. Index path override applied.
 20. Catastrophic exception inside ``propose_skills`` populates ``error``.
"""
from __future__ import annotations

import json

import pytest

from concinno import skill_proactive_router as mod
from concinno.skill_proactive_router import (
    DEFAULT_HAIKU_MODEL,
    DEFAULT_MAX_TOKENS,
    MAX_HAIKU_COST_USD,
    ProactiveRouterResult,
    SkillCandidate,
    build_router_context,
    estimate_haiku_cost_usd,
    load_triggers_index,
    propose_skills,
)

# ── fixtures ──────────────────────────────────────────────


@pytest.fixture
def index_file(tmp_path):
    """Write a small ``_triggers.json`` and return its path."""
    payload = {
        "version": 1,
        "skills": {
            "memoria": {
                "triggers": [
                    "memoria",
                    "memory cleanup",
                    "ram cleanup",
                    "記憶體整理",
                ],
                "category": "windows",
                "description": "RAM cleanup helper",
            },
            "kb_handoff": {
                "triggers": [
                    "handoff",
                    "交接",
                    "hand over",
                ],
                "category": "kb",
                "description": "Handoff hygiene KB",
            },
            "ablation_runner": {
                "triggers": [
                    "runpod",
                    "ablation",
                    "GPU experiment",
                ],
                "category": "ops",
                "description": "RunPod GPU ablation runner",
            },
        },
    }
    p = tmp_path / "_triggers.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


@pytest.fixture
def clean_env(monkeypatch):
    monkeypatch.delenv(
        "CONCINNO_SKILL_PROACTIVE_ROUTER_DISABLED", raising=False,
    )
    yield


def _stub_judge(verdict: list[dict], in_tok: int = 100, out_tok: int = 50):
    """Return a deterministic judge callable returning the supplied verdict."""

    def _judge(user_prompt: str, candidates: list[SkillCandidate]):
        return {
            "verdict": verdict,
            "input_tokens": in_tok,
            "output_tokens": out_tok,
        }

    return _judge


# ── 1. blank prompt ───────────────────────────────────────


def test_blank_prompt_skipped(index_file, clean_env):
    result = propose_skills("", index_path=index_file, judge=_stub_judge([]))
    assert isinstance(result, ProactiveRouterResult)
    assert result.skipped_reason == "prompt too short"
    assert result.judge_called is False
    assert result.additional_context == ""


# ── 2. enabled=False ──────────────────────────────────────


def test_enabled_false_skipped(index_file, clean_env):
    result = propose_skills(
        "please do a memoria cleanup right now",
        index_path=index_file,
        judge=_stub_judge([]),
        enabled=False,
    )
    assert result.skipped_reason == "disabled"
    assert result.judge_called is False


# ── 3. env opt-out ────────────────────────────────────────


def test_env_opt_out_skipped(index_file, monkeypatch):
    monkeypatch.setenv("CONCINNO_SKILL_PROACTIVE_ROUTER_DISABLED", "1")
    result = propose_skills(
        "please run memoria cleanup right now",
        index_path=index_file,
        judge=_stub_judge([]),
    )
    assert result.skipped_reason == "disabled via env"
    assert result.judge_called is False


# ── 4. missing index ──────────────────────────────────────


def test_missing_index_warns_no_judge(tmp_path, clean_env):
    nonexistent = tmp_path / "does_not_exist.json"
    result = propose_skills(
        "please run memoria cleanup right now",
        index_path=nonexistent,
        judge=_stub_judge([]),
    )
    assert any("missing or empty" in w for w in result.warnings)
    assert result.candidates == []
    assert result.judge_called is False


# ── 5. malformed index JSON ───────────────────────────────


def test_malformed_index_returns_empty(tmp_path):
    bad = tmp_path / "_triggers.json"
    bad.write_text("not even json {", encoding="utf-8")
    assert load_triggers_index(path=bad) == {}


# ── 6. unsupported version ────────────────────────────────


def test_unsupported_version_returns_empty(tmp_path):
    bad = tmp_path / "_triggers.json"
    bad.write_text(
        json.dumps({"version": 999, "skills": {"foo": {"triggers": ["bar"]}}}),
        encoding="utf-8",
    )
    assert load_triggers_index(path=bad) == {}


# ── 7. English trigger index hit ──────────────────────────


def test_index_hit_english(index_file, clean_env):
    result = propose_skills(
        "please run memoria cleanup right now",
        index_path=index_file,
        judge=_stub_judge([]),  # judge returns no lift → score stays in low band
    )
    names = [c.name for c in result.candidates]
    assert "memoria" in names


# ── 8. multilingual substring fallback ────────────────────


def test_index_hit_multilingual_substring(index_file, clean_env):
    result = propose_skills(
        "請幫我做交接，這個專案要切換了，謝謝",
        index_path=index_file,
        judge=_stub_judge([]),
    )
    names = [c.name for c in result.candidates]
    assert "kb_handoff" in names


# ── 9. no index hit → judge skipped ───────────────────────


def test_no_index_hit_skips_judge(index_file, clean_env):
    judge_called = {"hit": False}

    def _judge(user_prompt, candidates):
        judge_called["hit"] = True
        return {"verdict": [], "input_tokens": 0, "output_tokens": 0}

    result = propose_skills(
        "what is the capital of France today",  # no overlap with any trigger
        index_path=index_file,
        judge=_judge,
    )
    assert result.candidates == []
    assert judge_called["hit"] is False
    assert result.judge_called is False


# ── 10. judge re-ranks → score lifted ─────────────────────


def test_judge_lifts_score_into_high_band(index_file, clean_env):
    judge = _stub_judge([
        {"name": "memoria", "score": 0.92, "rationale": "exact intent match"},
    ])
    result = propose_skills(
        "please run memoria cleanup right now",
        index_path=index_file,
        judge=judge,
    )
    memoria = next(c for c in result.candidates if c.name == "memoria")
    assert memoria.score >= 0.9
    assert "exact intent match" in memoria.rationale
    assert result.judge_called is True


# ── 11. judge invents new names → dropped ─────────────────


def test_judge_invented_names_dropped(index_file, clean_env):
    judge = _stub_judge([
        {"name": "totally_invented", "score": 0.99, "rationale": "made up"},
    ])
    result = propose_skills(
        "please run memoria cleanup right now",
        index_path=index_file,
        judge=judge,
    )
    names = [c.name for c in result.candidates]
    assert "totally_invented" not in names


# ── 12. cost guard pre-flight skip ────────────────────────


def test_cost_guard_preflight_skips_judge(index_file, clean_env):
    judge_calls = {"n": 0}

    def _judge(p, c):
        judge_calls["n"] += 1
        return {"verdict": [], "input_tokens": 0, "output_tokens": 0}

    result = propose_skills(
        "please run memoria cleanup right now",
        index_path=index_file,
        judge=_judge,
        cost_ceiling_usd=0.0000001,  # absurdly low → pre-flight skip
    )
    assert judge_calls["n"] == 0
    assert any("pre-flight" in w for w in result.warnings)


# ── 13. judge cost recorded ───────────────────────────────


def test_judge_cost_recorded(index_file, clean_env):
    judge = _stub_judge(
        [{"name": "memoria", "score": 0.85, "rationale": "ok"}],
        in_tok=200, out_tok=100,
    )
    result = propose_skills(
        "please run memoria cleanup right now",
        index_path=index_file,
        judge=judge,
    )
    expected = estimate_haiku_cost_usd(input_tokens=200, output_tokens=100)
    assert result.judge_cost_usd == expected
    assert result.judge_called is True


# ── 14. judge raising → fail-soft ─────────────────────────


def test_judge_exception_fails_soft(index_file, clean_env):
    def _bad_judge(p, c):
        raise RuntimeError("simulated judge crash")

    result = propose_skills(
        "please run memoria cleanup right now",
        index_path=index_file,
        judge=_bad_judge,
    )
    assert any("simulated" in w for w in result.warnings)
    assert result.error is None
    # Original cheap candidates survive:
    names = [c.name for c in result.candidates]
    assert "memoria" in names


# ── 15. render multiple candidates ────────────────────────


def test_render_multiple_candidates():
    cands = [
        SkillCandidate(name="a", score=0.92, rationale="strong match"),
        SkillCandidate(name="b", score=0.55, rationale="weak match"),
    ]
    text = build_router_context(cands)
    assert "/a" in text and "/b" in text
    assert "0.92" in text
    assert "Skill suggestion" in text


# ── 16. render empty ──────────────────────────────────────


def test_render_empty_returns_empty():
    assert build_router_context([]) == ""


# ── 17. render filters negligible ─────────────────────────


def test_render_filters_negligible_score():
    cands = [
        SkillCandidate(name="too_low", score=0.05, rationale="noise"),
        SkillCandidate(name="kept", score=0.7, rationale="strong"),
    ]
    text = build_router_context(cands)
    assert "/kept" in text
    assert "/too_low" not in text


# ── 18. cost calc list price ──────────────────────────────


def test_cost_calc_matches_list_price():
    """1M input @ $1 + 1M output @ $5 = $6 USD; sanity-check the maths."""
    cost = estimate_haiku_cost_usd(input_tokens=1_000_000, output_tokens=1_000_000)
    assert cost == pytest.approx(6.0, rel=1e-3)


# ── 19. index path override ───────────────────────────────


def test_explicit_index_path_used(tmp_path, clean_env):
    explicit = tmp_path / "alt_index.json"
    explicit.write_text(
        json.dumps({
            "version": 1,
            "skills": {
                "uniquely_named": {"triggers": ["zibblezok"]},
            },
        }),
        encoding="utf-8",
    )
    result = propose_skills(
        "the zibblezok protocol is now active in production",
        index_path=explicit,
        judge=_stub_judge([]),
    )
    names = [c.name for c in result.candidates]
    assert "uniquely_named" in names


# ── 20. catastrophic failure caught ───────────────────────


def test_catastrophic_failure_caught(index_file, clean_env, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("simulated index loader explosion")

    monkeypatch.setattr(mod, "load_triggers_index", boom)
    result = propose_skills(
        "please run memoria cleanup right now",
        index_path=index_file,
        judge=_stub_judge([]),
    )
    assert result.error is not None
    assert "simulated index loader explosion" in result.error


# ── 21. default judge no-API fail-soft ────────────────────


def test_default_judge_returns_empty_without_api_key(monkeypatch):
    """default_haiku_judge silently no-ops when ANTHROPIC_API_KEY missing."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    out = mod.default_haiku_judge(
        "test prompt with substance",
        [SkillCandidate(name="x", score=0.5, rationale="seed")],
    )
    assert out == {"verdict": [], "input_tokens": 0, "output_tokens": 0}


# ── 22. constants are documented ──────────────────────────


def test_module_constants_documented():
    """Tunables are exposed at module level for ZIQ wiring later."""
    assert DEFAULT_HAIKU_MODEL.startswith("claude-haiku-")
    assert DEFAULT_MAX_TOKENS > 0
    assert MAX_HAIKU_COST_USD > 0


# ── 23. cost cap pre-flight HARD DENIES (Goodhart-2 fix) ──


def test_cost_cap_preflight_emits_cheap_only(index_file, clean_env):
    """Pre-flight estimate > ceiling now hard-denies the judge but still
    surfaces cheap-stage candidates so the user gets *some* advisory.
    Replaces the old behaviour where the judge ran anyway."""
    judge_calls = {"n": 0}

    def _judge(p, c):
        judge_calls["n"] += 1
        return {"verdict": [{"name": "memoria", "score": 0.99, "rationale": "x"}],
                "input_tokens": 100, "output_tokens": 50}

    result = propose_skills(
        "please run memoria cleanup right now",
        index_path=index_file,
        judge=_judge,
        cost_ceiling_usd=0.0000001,  # absurdly low → hard deny
    )
    assert judge_calls["n"] == 0, "judge must NOT be called pre-flight deny"
    assert result.judge_called is False
    # Cheap candidates still emitted — they cost nothing.
    names = [c.name for c in result.candidates]
    assert "memoria" in names
    assert any("hard-denied" in w for w in result.warnings)


# ── 24. emergency deny: ceiling=0 skips everything ────────


def test_emergency_deny_ceiling_zero(index_file, clean_env):
    """``cost_ceiling_usd=0`` is operator-killed for the session."""
    result = propose_skills(
        "please run memoria cleanup right now",
        index_path=index_file,
        judge=_stub_judge([{"name": "memoria", "score": 0.99}]),
        cost_ceiling_usd=0.0,
    )
    assert result.skipped_reason == "cost ceiling zero — emergency deny"
    assert result.candidates == []
    assert result.judge_called is False


# ── 25. safety net writes to persistent overshoot ledger ──


def test_safety_net_records_overshoot(index_file, clean_env, tmp_path):
    """Actual judge cost > ceiling × OVERSHOOT_MULTIPLIER_HARD_BLOCK
    appends to the persistent overshoot ledger."""
    from concinno.skill_proactive_router import (
        OVERSHOOT_MULTIPLIER_HARD_BLOCK,
        read_cost_overshoot_state,
    )

    # Drive actual cost into the safety zone: 1M output tokens = $5,
    # which is ≫ 0.001 × 2 = 0.002 ceiling.
    judge = _stub_judge(
        [{"name": "memoria", "score": 0.85, "rationale": "ok"}],
        in_tok=1_000_000, out_tok=1_000_000,
    )

    ledger_path = tmp_path / "overshoot.json"
    # Use a generous ceiling so pre-flight permits the call (we want to
    # exercise the *post-call* safety net, not the pre-flight gate).
    result = propose_skills(
        "please run memoria cleanup right now",
        index_path=index_file,
        judge=judge,
        cost_ceiling_usd=10.0,  # pre-flight admits; safety net = 20.0
        overshoot_state_path=ledger_path,
    )
    # 1M+1M tokens cost $6.0 > 20.0? No, 6.0 < 20.0 → no safety trip.
    # Sanity-check the multiplier and pick numbers that DO trip:
    assert OVERSHOOT_MULTIPLIER_HARD_BLOCK > 1.0
    assert result.judge_called is True

    # Tighten ceiling so safety_threshold = 1.0 × 2 = 2.0 < 6.0 spend.
    result2 = propose_skills(
        "please run memoria cleanup right now",
        index_path=index_file,
        judge=judge,
        cost_ceiling_usd=1.0,
        overshoot_state_path=ledger_path,
    )
    assert result2.judge_called is True
    assert any("safety threshold" in w for w in result2.warnings)
    state = read_cost_overshoot_state(path=ledger_path)
    assert state["count"] >= 1
    assert state["last_overshoot_usd"] == pytest.approx(6.0, rel=1e-3)


# ── 26. persistent overshoot count denies future calls ────


def test_persistent_overshoot_denies_judge(index_file, clean_env, tmp_path):
    """When ledger.count >= max_persistent_overshoots, judge is denied."""
    import json as _json
    ledger_path = tmp_path / "overshoot.json"
    ledger_path.write_text(
        _json.dumps({
            "count": 5,
            "last_overshoot_usd": 0.5,
            "last_seen": 1.0,
        }),
        encoding="utf-8",
    )

    judge_calls = {"n": 0}

    def _judge(p, c):
        judge_calls["n"] += 1
        return {"verdict": [], "input_tokens": 0, "output_tokens": 0}

    result = propose_skills(
        "please run memoria cleanup right now",
        index_path=index_file,
        judge=_judge,
        overshoot_state_path=ledger_path,
        max_persistent_overshoots=3,  # 5 >= 3 → deny
    )
    assert judge_calls["n"] == 0
    assert result.judge_called is False
    assert any("persistent overshoot count" in w for w in result.warnings)
    # Cheap candidates still surface:
    names = [c.name for c in result.candidates]
    assert "memoria" in names


# ── 27. record_cost_overshoot persists incrementally ──────


def test_record_cost_overshoot_increments(tmp_path):
    """Each call to record_cost_overshoot increments count by 1."""
    from concinno.skill_proactive_router import (
        read_cost_overshoot_state,
        record_cost_overshoot,
    )

    ledger_path = tmp_path / "ledger.json"
    s1 = record_cost_overshoot(0.5, path=ledger_path)
    assert s1["count"] == 1

    s2 = record_cost_overshoot(1.2, path=ledger_path)
    assert s2["count"] == 2
    assert s2["last_overshoot_usd"] == 1.2

    state = read_cost_overshoot_state(path=ledger_path)
    assert state["count"] == 2
    assert state["last_overshoot_usd"] == 1.2
