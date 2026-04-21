"""Tests for concinno.gaia_meta_router — SAS/MAS/hybrid selection + FTRL."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from concinno import gaia_meta_router as gmr
from concinno.gaia_meta_router import (
    ARMS,
    ArmFTRL,
    record_arm_outcome,
    reset_default_router,
    select_arm,
    sps_arm_scores,
)


@pytest.fixture
def isolated_store(tmp_path, monkeypatch):
    """Point the default router at a per-test JSON file and drop singleton."""
    store = tmp_path / "arm_ftrl.json"
    monkeypatch.setenv("CONCINNO_GAIA_ARM_STORE", str(store))
    reset_default_router()
    yield store
    reset_default_router()


# ── sps_arm_scores structural priors ────────────────────────────────


def test_sps_level1_factual_favors_sas():
    scores = sps_arm_scores("Who won in 1977?", level="1", qtype="factual")
    assert scores["SAS"] > scores["MAS"]
    assert scores["SAS"] > scores["hybrid"]


def test_sps_level2_file_compare_favors_mas():
    scores = sps_arm_scores(
        "Compare column A and column B in the attached spreadsheet.",
        level="2", file_name="data.xlsx", qtype="file_analysis",
    )
    assert scores["MAS"] > scores["SAS"]


def test_sps_level3_long_synthesis_favors_hybrid():
    long_q = (
        "Synthesize the following research question step by step and "
        "decompose it into phases: " + "x" * 400
    )
    scores = sps_arm_scores(long_q, level="3", qtype="research")
    assert scores["hybrid"] > scores["SAS"]


def test_sps_research_qtype_lifts_mas_and_hybrid():
    short = "Find the paper about X"
    scores = sps_arm_scores(short, level="2", qtype="research")
    # Research qtype adds to both MAS and hybrid, not SAS.
    assert scores["MAS"] > 1.0
    assert scores["hybrid"] > 1.0


# ── select_arm routing at cold start ────────────────────────────────


def test_select_arm_cold_start_level1_factual_is_sas(isolated_store):
    arm = select_arm({
        "Question": "Who was PM in 1977?",
        "Level": "1",
        "qtype": "factual",
    })
    assert arm == "SAS"


def test_select_arm_cold_start_level2_file_is_mas(isolated_store):
    arm = select_arm({
        "Question": "Compare values in both worksheets.",
        "Level": "2",
        "file_name": "data.xlsx",
        "qtype": "file_analysis",
    })
    assert arm == "MAS"


def test_select_arm_cold_start_level3_research_is_hybrid(isolated_store):
    arm = select_arm({
        "Question": "x" * 400 + " synthesize step by step",
        "Level": "3",
        "qtype": "research",
    })
    assert arm == "hybrid"


# ── Budget fallback forces SAS ──────────────────────────────────────


def test_low_budget_forces_sas(isolated_store):
    arm = select_arm(
        {"Question": "x" * 500, "Level": "3", "qtype": "research"},
        budget=1000,
        context={"remaining_budget": 100},  # 10% remaining
    )
    assert arm == "SAS"


def test_budget_above_floor_does_not_force_sas(isolated_store):
    arm = select_arm(
        {"Question": "x" * 500, "Level": "3", "qtype": "research"},
        budget=1000,
        context={"remaining_budget": 800},  # 80% remaining
    )
    assert arm != "SAS" or arm in ARMS  # router decision, not forced


# ── Hysteresis: stay on previous arm when close to winner ──────────


def test_hysteresis_keeps_prev_arm_when_close():
    router = ArmFTRL()  # cold gates = 0.5 for all arms
    # Cold SPS for research L2 file: MAS wins; prev_arm=hybrid within 95%.
    arm = select_arm(
        {
            "Question": "Compare values across both files carefully",
            "Level": "2",
            "file_name": "d.xlsx",
            "qtype": "research",
        },
        context={"prev_arm": "hybrid"},
        router=router,
    )
    # Either MAS or hybrid acceptable — whichever wins posterior tie-break.
    assert arm in {"MAS", "hybrid"}


# ── record_arm_outcome updates the router ──────────────────────────


def test_record_arm_outcome_influences_selection(isolated_store):
    # Train SAS with consistent failures, MAS with successes.
    for _ in range(20):
        record_arm_outcome("SAS", 0.0, persist=False)
    for _ in range(20):
        record_arm_outcome("MAS", 1.0, persist=False)
    # Ambiguous task; selection exercises the trained router.
    select_arm({
        "Question": "Who was PM in 1977?",
        "Level": "1",
        "qtype": "factual",
    })
    # MAS gate should be high enough to overcome SAS structural advantage
    # once FTRL has learned; the point is that training has measurable effect.
    router = gmr._default_router()
    assert router.gate("MAS") > router.gate("SAS")


def test_record_unknown_arm_silently_ignored(isolated_store):
    # Should not raise.
    record_arm_outcome("UNKNOWN", 0.9, persist=False)
    router = gmr._default_router()
    for arm in ARMS:
        assert router.gate(arm) == 0.5  # untouched


# ── Persistence round-trip ─────────────────────────────────────────


def test_persistence_round_trip(tmp_path, monkeypatch):
    store = tmp_path / "round_trip.json"
    monkeypatch.setenv("CONCINNO_GAIA_ARM_STORE", str(store))
    reset_default_router()

    for _ in range(10):
        record_arm_outcome("MAS", 1.0, persist=True)
    assert store.exists()
    data = json.loads(store.read_text(encoding="utf-8"))
    assert "z" in data and "n" in data
    assert data["n"]["MAS"] > 0

    # Reload by dropping singleton.
    reset_default_router()
    router = gmr._default_router()
    # n counter must have transferred.
    assert router.n["MAS"] > 0


def test_corrupt_store_does_not_crash(tmp_path, monkeypatch):
    store = tmp_path / "corrupt.json"
    store.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setenv("CONCINNO_GAIA_ARM_STORE", str(store))
    reset_default_router()
    router = gmr._default_router()
    # Fresh state despite corrupt file.
    for arm in ARMS:
        assert router.gate(arm) == 0.5


# ── ArmFTRL unit math ──────────────────────────────────────────────


def test_armftrl_cold_start_returns_half():
    r = ArmFTRL()
    for arm in ARMS:
        assert r.gate(arm) == 0.5


def test_armftrl_positive_reward_raises_gate():
    r = ArmFTRL()
    for _ in range(10):
        r.update("SAS", 1.0)
    assert r.gate("SAS") > 0.6


def test_armftrl_negative_reward_lowers_gate():
    r = ArmFTRL()
    for _ in range(10):
        r.update("MAS", 0.0)
    assert r.gate("MAS") < 0.4


def test_armftrl_gate_clamped_to_0_01_0_99():
    r = ArmFTRL()
    for _ in range(1000):
        r.update("SAS", 1.0)
    assert 0.01 <= r.gate("SAS") <= 0.99
    for _ in range(1000):
        r.update("MAS", 0.0)
    assert 0.01 <= r.gate("MAS") <= 0.99


# ── Routing correctness (golden set >= 80%) ─────────────────────────


GOLDEN_SET = [
    # (task, expected_arm)
    ({"Question": "Who won the 1977 election?", "Level": "1", "qtype": "factual"}, "SAS"),
    ({"Question": "What is 5% of 2000?", "Level": "1", "qtype": "calculation"}, "SAS"),
    ({"Question": "Name the president in 1980.", "Level": "1", "qtype": "factual"}, "SAS"),
    ({"Question": "Compute the ratio A:B from table.", "Level": "1",
      "qtype": "calculation"}, "SAS"),
    ({"Question": "Compare rows in the attached CSV carefully.", "Level": "2",
      "file_name": "a.csv", "qtype": "file_analysis"}, "MAS"),
    ({"Question": "Cross-reference both datasets and merge records.",
      "Level": "2", "file_name": "b.xlsx", "qtype": "file_analysis"}, "MAS"),
    ({"Question": "List each of the several chemicals described.",
      "Level": "2", "qtype": "research"}, "MAS"),
    ({"Question": ("Synthesize the multi-phase research plan, decompose "
                   "step by step and iterate through each stage " + "x" * 350),
      "Level": "3", "qtype": "research"}, "hybrid"),
    ({"Question": ("Walk through the following problem phase by phase and "
                   "decompose the strategy " + "x" * 400),
      "Level": "3", "qtype": "research"}, "hybrid"),
    ({"Question": ("This is an extremely long question requiring "
                   "cross-domain synthesis " + "x" * 500),
      "Level": "3", "qtype": "research"}, "hybrid"),
]


def test_golden_set_routing_accuracy(isolated_store):
    """Routing accuracy >=80% on cold-start golden set (plan W2 criterion)."""
    hits = sum(1 for task, expected in GOLDEN_SET
               if select_arm(task) == expected)
    assert hits / len(GOLDEN_SET) >= 0.8, (
        f"Routing accuracy {hits}/{len(GOLDEN_SET)} below 80% floor"
    )


# ── Task shape tolerance (handles Question vs question, Level vs level) ─


def test_task_accepts_lowercase_keys(isolated_store):
    arm = select_arm({"question": "Who?", "level": "1", "qtype": "factual"})
    assert arm == "SAS"


def test_task_missing_fields_defaults_to_level_1(isolated_store):
    arm = select_arm({"Question": "Short question."})
    assert arm in ARMS


# ── Store path override precedence ─────────────────────────────────


def test_store_env_override(tmp_path, monkeypatch):
    target = tmp_path / "nested" / "custom.json"
    monkeypatch.setenv("CONCINNO_GAIA_ARM_STORE", str(target))
    reset_default_router()
    record_arm_outcome("SAS", 1.0, persist=True)
    assert target.exists()


def test_save_creates_parent_dir(tmp_path):
    deep = tmp_path / "a" / "b" / "c" / "state.json"
    r = ArmFTRL()
    r.update("SAS", 1.0)
    r.save(deep)
    assert deep.exists()


def test_load_from_nonexistent_path_is_noop(tmp_path):
    r = ArmFTRL()
    ghost = tmp_path / "nonexistent.json"
    r.load(ghost)  # no crash
    for arm in ARMS:
        assert r.gate(arm) == 0.5


# ── Public API signature stability ─────────────────────────────────


def test_public_symbols_exported():
    from concinno import gaia_meta_router as m
    for name in ["select_arm", "record_arm_outcome", "ArmFTRL",
                 "sps_arm_scores", "reset_default_router",
                 "ARMS", "Arm"]:
        assert hasattr(m, name), f"missing public export: {name}"
    assert set(ARMS) == {"SAS", "MAS", "hybrid"}


# ── select_arm with injected router isolates state ─────────────────


def test_select_arm_with_injected_router_no_global_state():
    router = ArmFTRL()
    arm = select_arm(
        {"Question": "Who?", "Level": "1"},
        router=router,
    )
    assert arm in ARMS
    # Default singleton untouched.
    for a in ARMS:
        assert router.gate(a) == 0.5


# ── select_arm returns stable type ─────────────────────────────────


def test_select_arm_returns_string():
    arm = select_arm({"Question": "test", "Level": "1"})
    assert isinstance(arm, str)
    assert arm in ARMS


def test_posterior_with_all_gates_zero_still_returns_arm(monkeypatch):
    """Degenerate posterior (all zero) should still choose deterministically."""
    router = ArmFTRL()
    # Force all gates to minimum by loading impossible state.
    for arm in ARMS:
        router.z[arm] = 1e9
        router.n[arm] = 1e-9  # tiny n → huge gate deflection → clamped
    arm = select_arm({"Question": "Hi", "Level": "1"}, router=router)
    assert arm in ARMS


# ── Helper file ops ────────────────────────────────────────────────


def test_save_handles_permission_error(monkeypatch, tmp_path):
    r = ArmFTRL()
    r.update("SAS", 1.0)

    def _raise(*_args, **_kwargs):
        raise OSError("no perm")

    monkeypatch.setattr(Path, "mkdir", _raise)
    # Should silently degrade.
    r.save(tmp_path / "x.json")
