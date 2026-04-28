"""Tests for concinno.skills.skill_emergence_guard.

Covers:
* Trigger detection (3 kinds): tool_pattern_repeat, error_to_working,
  user_correction
* Guard caps: daily, cooldown, min-occurrences floor
* ZIQ FTRL plumbing on accept / reject
* Draft slug uniqueness + 30-day auto-archive
* Disable via feature config short-circuits everything
* observe() never raises
* Concurrency safety
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from unittest import mock

import pytest

from concinno.skills.skill_emergence_guard import (
    EmergenceSignal,
    SkillDraft,
    SkillEmergenceGuard,
    draft_root,
    propose_draft,
)

# ── Test scaffolding ─────────────────────────────────────


@pytest.fixture
def isolated_drafts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the staging dir + state file to a fresh tmp folder."""
    monkeypatch.setenv("CONCINNO_SKILL_DRAFT_DIR", str(tmp_path))
    monkeypatch.setenv("CONCINNO_SKILL_DRAFT_STATE", str(tmp_path / "_state.json"))
    return tmp_path


@pytest.fixture
def enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the feature on regardless of the user's config file."""
    monkeypatch.setattr(
        "concinno.skills.skill_emergence_guard._is_enabled",
        lambda: True,
    )


def _signal(
    *,
    tool: str = "Bash",
    shape: str = "ruff check",
    success: bool = True,
    correction: bool = False,
    ts: float | None = None,
) -> EmergenceSignal:
    return EmergenceSignal(
        tool_name=tool,
        canonical_shape=shape,
        success=success,
        timestamp=ts if ts is not None else time.time(),
        had_user_correction=correction,
    )


# ── Trigger detection ────────────────────────────────────


def test_trigger_detection_tool_pattern_repeat(
    isolated_drafts: Path, enabled: None,
) -> None:
    g = SkillEmergenceGuard(min_pattern_occurrences=3)
    # First two should not fire
    assert g.observe(_signal()) is None
    assert g.observe(_signal()) is None
    # Third hit fires the draft
    draft = g.observe(_signal())
    assert draft is not None
    assert draft.trigger_kind == "tool_pattern_repeat"
    md = isolated_drafts / f"{draft.slug}.md"
    assert md.exists()
    assert "tool_pattern_repeat" in md.read_text(encoding="utf-8")


def test_trigger_detection_error_to_working(
    isolated_drafts: Path, enabled: None,
) -> None:
    # min_occurrences high so trigger #1 doesn't fire first
    g = SkillEmergenceGuard(min_pattern_occurrences=99)
    g.observe(_signal(success=False))
    g.observe(_signal(success=False))
    draft = g.observe(_signal(success=True))
    assert draft is not None
    assert draft.trigger_kind == "error_to_working"


def test_trigger_detection_user_correction(
    isolated_drafts: Path, enabled: None,
) -> None:
    g = SkillEmergenceGuard(min_pattern_occurrences=99)
    draft = g.observe(_signal(correction=True))
    assert draft is not None
    assert draft.trigger_kind == "user_correction"


# ── Caps ─────────────────────────────────────────────────


def test_max_auto_skills_per_day_cap(
    isolated_drafts: Path, enabled: None,
) -> None:
    g = SkillEmergenceGuard(
        min_pattern_occurrences=1,
        cooldown_hours=0.0,
        max_auto_skills_per_day=2,
    )
    seen: list[SkillDraft] = []
    for i in range(5):
        d = g.observe(_signal(shape=f"cmd-{i}"))
        if d is not None:
            seen.append(d)
    assert len(seen) == 2  # cap enforced — daily ceiling stops at 2


def test_cooldown_prevents_duplicate(
    isolated_drafts: Path, enabled: None,
) -> None:
    g = SkillEmergenceGuard(
        min_pattern_occurrences=1,
        cooldown_hours=1.0,
        max_auto_skills_per_day=10,
    )
    first = g.observe(_signal(shape="ruff check"))
    second = g.observe(_signal(shape="ruff check"))
    assert first is not None
    assert second is None  # cooldown active


def test_min_pattern_occurrences_floor(
    isolated_drafts: Path, enabled: None,
) -> None:
    g = SkillEmergenceGuard(min_pattern_occurrences=5)
    for _ in range(4):
        assert g.observe(_signal()) is None
    fifth = g.observe(_signal())
    assert fifth is not None


def test_min_pattern_occurrences_floor_clamps_to_one(
    isolated_drafts: Path, enabled: None,
) -> None:
    """Negative / zero values get clamped up to the 1-occurrence floor."""
    g = SkillEmergenceGuard(min_pattern_occurrences=0)
    assert g._min_occurrences() == 1
    g2 = SkillEmergenceGuard(min_pattern_occurrences=-3)
    assert g2._min_occurrences() == 1


def test_hard_ceiling_per_day(
    isolated_drafts: Path, enabled: None,
) -> None:
    g = SkillEmergenceGuard(max_auto_skills_per_day=999)
    # Hard ceiling is 20 regardless of config
    assert g._max_per_day() == 20


# ── ZIQ accept / reject ──────────────────────────────────


def test_record_accept_returns_true_for_known(
    isolated_drafts: Path, enabled: None,
) -> None:
    g = SkillEmergenceGuard(min_pattern_occurrences=1)
    draft = g.observe(_signal())
    assert draft is not None
    with mock.patch(
        "concinno.ziq_emit_helpers.emit_classification_outcome",
    ) as m_emit:
        assert g.record_accept(draft.slug) is True
    m_emit.assert_called_once()
    kwargs = m_emit.call_args.kwargs
    assert kwargs["correct"] is True


def test_record_reject_returns_true_for_known(
    isolated_drafts: Path, enabled: None,
) -> None:
    g = SkillEmergenceGuard(min_pattern_occurrences=1)
    draft = g.observe(_signal())
    assert draft is not None
    with mock.patch(
        "concinno.ziq_emit_helpers.emit_classification_outcome",
    ) as m_emit:
        assert g.record_reject(draft.slug) is True
    m_emit.assert_called_once()
    assert m_emit.call_args.kwargs["correct"] is False


def test_record_unknown_slug_returns_false(
    isolated_drafts: Path, enabled: None,
) -> None:
    g = SkillEmergenceGuard()
    assert g.record_accept("bogus-slug") is False
    assert g.record_reject("bogus-slug") is False


def test_record_outcome_swallows_emit_failure(
    isolated_drafts: Path, enabled: None,
) -> None:
    g = SkillEmergenceGuard(min_pattern_occurrences=1)
    draft = g.observe(_signal())
    assert draft is not None
    with mock.patch(
        "concinno.ziq_emit_helpers.emit_classification_outcome",
        side_effect=RuntimeError("ziq down"),
    ):
        # Must still return True — index update happened, ZIQ is best-effort
        assert g.record_accept(draft.slug) is True


# ── Draft retention / housekeeping ───────────────────────


def test_draft_auto_archive_after_retention(
    isolated_drafts: Path, enabled: None,
) -> None:
    """Backdated draft should be pruned when a later observe runs.

    We assert via the proposed_at timestamp in the index rather than
    file existence, because a colliding slug from a fresh observation
    can re-create the same filename — the data we care about is "did
    the OLD record survive", not "is the path empty".
    """
    g = SkillEmergenceGuard(
        min_pattern_occurrences=1,
        cooldown_hours=0.0,
        max_auto_skills_per_day=10,
        draft_retention_days=1,
    )
    draft = g.observe(_signal(shape="old-cmd"))
    assert draft is not None
    state_path = isolated_drafts / "_state.json"
    backdated = time.time() - 5 * 86400
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["drafts_index"][draft.slug]["proposed_at"] = backdated
    state_path.write_text(json.dumps(state), encoding="utf-8")
    # Trigger another observe to run pruning
    g.observe(_signal(shape="new-cmd"))
    state_after = json.loads(state_path.read_text(encoding="utf-8"))
    surviving = state_after["drafts_index"]
    # Old timestamp must be gone (either slug evicted, or replaced by
    # a fresh proposed_at from the same-slug new draft).
    for payload in surviving.values():
        assert float(payload.get("proposed_at", 0)) > backdated + 86400


def test_draft_retention_zero_disables_pruning(
    isolated_drafts: Path, enabled: None,
) -> None:
    g = SkillEmergenceGuard(
        min_pattern_occurrences=1,
        cooldown_hours=0.0,
        draft_retention_days=0,
    )
    g.observe(_signal())
    state = json.loads((isolated_drafts / "_state.json").read_text("utf-8"))
    assert len(state["drafts_index"]) == 1


# ── Slug uniqueness ──────────────────────────────────────


def test_skill_slug_unique_on_collision(
    isolated_drafts: Path, enabled: None,
) -> None:
    """Two patterns that hash to the same head get distinct slugs."""
    g = SkillEmergenceGuard(
        min_pattern_occurrences=1,
        cooldown_hours=0.0,
        max_auto_skills_per_day=10,
    )
    # Force same head by reusing the canonical shape — should differ via uuid
    a = propose_draft(SkillDraft(
        slug="workflow-ruff",
        name="workflow-ruff",
        description="x",
        trigger_keywords=[],
        pattern_signature="Bash|ruff",
        proposed_at=time.time(),
        trigger_kind="tool_pattern_repeat",
        occurrences=3,
        sample_canonical_shapes=["ruff"],
    ))
    assert a.exists()
    # Now go through guard which should NOT clobber
    draft = g.observe(_signal(shape="ruff"))
    assert draft is not None
    assert draft.slug != "workflow-ruff"
    assert (isolated_drafts / f"{draft.slug}.md").exists()


def test_compose_draft_frontmatter_valid(
    isolated_drafts: Path, enabled: None,
) -> None:
    g = SkillEmergenceGuard(min_pattern_occurrences=1)
    draft = g.observe(_signal(shape="pytest tests"))
    assert draft is not None
    md_text = (isolated_drafts / f"{draft.slug}.md").read_text("utf-8")
    # YAML-style frontmatter
    assert md_text.startswith("---\n")
    assert "name: " in md_text
    assert "description: " in md_text
    assert "triggers: [" in md_text


# ── Disable / short-circuit ──────────────────────────────


def test_no_propose_when_disabled(
    isolated_drafts: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "concinno.skills.skill_emergence_guard._is_enabled",
        lambda: False,
    )
    g = SkillEmergenceGuard(min_pattern_occurrences=1)
    for _ in range(20):
        assert g.observe(_signal()) is None


def test_observational_only_tools_skipped(
    isolated_drafts: Path, enabled: None,
) -> None:
    g = SkillEmergenceGuard(min_pattern_occurrences=1)
    # Read tool repetitions never propose
    for _ in range(10):
        assert g.observe(_signal(tool="Read", shape="src/x.py")) is None


def test_disable_via_feature_config(
    isolated_drafts: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real config layer route: cfg.feature(...,'enabled') False → no fire."""
    fake_cfg = mock.MagicMock()
    fake_cfg.feature.return_value = False
    monkeypatch.setattr(
        "concinno.core.config.get_config", lambda: fake_cfg,
    )
    g = SkillEmergenceGuard(min_pattern_occurrences=1)
    assert g.observe(_signal()) is None


# ── observe() never raises ───────────────────────────────


def test_observe_swallows_state_corruption(
    isolated_drafts: Path, enabled: None,
) -> None:
    state_path = isolated_drafts / "_state.json"
    state_path.write_text("not valid json {{{", encoding="utf-8")
    g = SkillEmergenceGuard(min_pattern_occurrences=1)
    # Should not raise, just silently start with empty state
    g.observe(_signal())


def test_observe_swallows_disk_full(
    isolated_drafts: Path, enabled: None,
) -> None:
    g = SkillEmergenceGuard(min_pattern_occurrences=1)
    with mock.patch(
        "concinno.skills.skill_emergence_guard.propose_draft",
        side_effect=OSError("disk full"),
    ):
        # Should silently no-op rather than raising into the hook
        result = g.observe(_signal())
        assert result is None


# ── Hook entry-point recording ───────────────────────────


def test_observe_tool_call_records(
    isolated_drafts: Path, enabled: None,
) -> None:
    g = SkillEmergenceGuard(min_pattern_occurrences=99)  # never fire
    for _ in range(3):
        g.observe(_signal())
    state = json.loads((isolated_drafts / "_state.json").read_text("utf-8"))
    assert state["occurrence_counts"]["Bash|ruff check"] == 3


def test_signal_on_post_tool_use_hook(
    isolated_drafts: Path, enabled: None,
) -> None:
    """Smoke test: the hook helper builds shape + observes without crashing.

    This is the per-spec ``test_signal_on_post_tool_use_hook`` case —
    smoke-tests that the on_post_tool wiring lays down the right
    EmergenceSignal shape for both Bash and Edit shapes.
    """
    from concinno.hooks.on_post_tool import _build_emergence_shape

    assert _build_emergence_shape("Bash", {"command": "pytest -x tests/"}) == (
        "pytest -x"
    )
    assert _build_emergence_shape(
        "Edit", {"file_path": "src/foo.py"},
    ) == "py"
    assert _build_emergence_shape(
        "Write", {"path": "x.md"},
    ) == "md"
    assert _build_emergence_shape("Read", {}) == ""


# ── Concurrency ──────────────────────────────────────────


def test_concurrent_observe_thread_safe(
    isolated_drafts: Path, enabled: None,
) -> None:
    g = SkillEmergenceGuard(
        min_pattern_occurrences=2, max_auto_skills_per_day=10,
    )

    def worker() -> None:
        for _ in range(20):
            g.observe(_signal(shape=f"cmd-{threading.get_ident() % 5}"))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # No exceptions = pass; state file should be valid JSON
    state = json.loads((isolated_drafts / "_state.json").read_text("utf-8"))
    assert isinstance(state["occurrence_counts"], dict)


def test_record_event_is_thread_safe(
    isolated_drafts: Path, enabled: None,
) -> None:
    g = SkillEmergenceGuard(min_pattern_occurrences=999)

    def worker() -> None:
        for _ in range(50):
            g.observe(_signal(shape="shared"))

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    state = json.loads((isolated_drafts / "_state.json").read_text("utf-8"))
    # All 4*50 = 200 events should land in the counter
    assert state["occurrence_counts"]["Bash|shared"] == 200


# ── Stderr emit ──────────────────────────────────────────


def test_stderr_emit_called_on_propose(
    isolated_drafts: Path, enabled: None,
) -> None:
    captured: list[str] = []

    def emit(msg: str) -> None:
        captured.append(msg)

    g = SkillEmergenceGuard(
        min_pattern_occurrences=1, stderr_emit=emit,
    )
    g.observe(_signal())
    assert len(captured) == 1
    # 4.5.0 ship-fix (red-team HIGH-4): the inline notice now points the
    # operator at the manual move/delete workflow because the
    # ``concinno skill-emerge`` argparse subcommand is deferred to a
    # 4.5.x patch wave. Both the install path and the discard path must
    # still be present so the operator knows what to do without the
    # subcommand.
    assert "~/.claude/skills" in captured[0]
    assert "delete" in captured[0]


def test_stderr_emit_failure_swallowed(
    isolated_drafts: Path, enabled: None,
) -> None:
    def boom(msg: str) -> None:
        raise RuntimeError("stderr broken")

    g = SkillEmergenceGuard(
        min_pattern_occurrences=1, stderr_emit=boom,
    )
    # Must not raise
    g.observe(_signal())


# ── Path / env override ──────────────────────────────────


def test_draft_root_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONCINNO_SKILL_DRAFT_DIR", "/tmp/fake-skill-dir")
    p = draft_root()
    # Path normalises separators on the active OS — compare logically.
    assert p == Path("/tmp/fake-skill-dir")


def test_draft_root_default_under_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CONCINNO_SKILL_DRAFT_DIR", raising=False)
    p = draft_root()
    parts = p.parts
    assert ".concinno" in parts
    assert "skill_drafts" in parts


# ── FEATURE_META wiring ──────────────────────────────────


def test_feature_meta_registered() -> None:
    """The FEATURE_META entry must exist + be retrievable."""
    from concinno.feature_config import FEATURE_META
    assert "skill_emergence_guard" in FEATURE_META
    meta = FEATURE_META["skill_emergence_guard"]
    assert meta["category"] == "behavioral"
    assert meta["enabled"] is False  # 4.0.0 SEMVER baseline default-OFF
    assert meta["ziq_autotunable"] is True
    assert meta["cosmetic"] is False
    params = meta["params"]
    assert params["max_auto_skills_per_day"]["default"] == 5
    assert params["min_pattern_occurrences"]["default"] == 3
    assert params["cooldown_hours"]["default"] == 2.0
    assert params["draft_retention_days"]["default"] == 30


def test_feature_meta_has_zh_description() -> None:
    """description_zh must exist for i18n parity."""
    from concinno.feature_config import FEATURE_META
    meta = FEATURE_META["skill_emergence_guard"]
    assert meta["description_zh"]
    assert meta["description"]


# ── Markdown shape ───────────────────────────────────────


def test_to_markdown_includes_acceptance_commands() -> None:
    d = SkillDraft(
        slug="ws-foo",
        name="workflow-foo",
        description="desc",
        trigger_keywords=["foo"],
        pattern_signature="Bash|foo",
        proposed_at=time.time(),
        trigger_kind="tool_pattern_repeat",
        occurrences=3,
        sample_canonical_shapes=["foo"],
    )
    md = d.to_markdown()
    # 4.5.0 ship-fix (red-team HIGH-4): the Acceptance section now spells
    # out the manual move/delete workflow plus a forward-pointer to the
    # 4.5.x ``concinno skill-emerge`` subcommand. Both the install and
    # discard paths must be present, plus the slug must appear so the
    # operator knows which draft they are acting on.
    assert "~/.claude/skills/ws-foo/SKILL.md" in md
    assert "ws-foo.md" in md
    assert "## Suggested workflow" in md


def test_emergence_signal_defaults() -> None:
    """Smoke check: defaults yield a usable signal."""
    s = EmergenceSignal(tool_name="Bash", canonical_shape="ls")
    assert s.success is True
    assert s.had_user_correction is False
    assert s.timestamp > 0


# ── Sanity: no personal paths leaked ─────────────────────


def test_no_personal_path_in_module() -> None:
    """Per L1 boundary rule #1: no E:\\ / C:\\Users\\ in source."""
    src = Path(__file__).parent.parent / "src" / "concinno" / "skills" / (
        "skill_emergence_guard.py"
    )
    text = src.read_text(encoding="utf-8")
    # Lower-cased so windows-pathstyle and unix-pathstyle both checked
    low = text.lower()
    assert "c:\\users" not in low
    assert "e:\\cursor" not in low
    assert "_ai_brain" not in low


# ── Sanity: hook never crashes on weird input ────────────


def test_hook_helper_never_raises_on_garbage(
    isolated_drafts: Path, enabled: None,
) -> None:
    from concinno.hooks.on_post_tool import _run_skill_emergence

    # Garbage tool_input must not raise
    _run_skill_emergence("Bash", {}, {})
    _run_skill_emergence("Bash", {"command": None}, {})  # type: ignore[arg-type]
    _run_skill_emergence("Edit", {"file_path": ""}, {"tool_result": ""})
    _run_skill_emergence("UnknownTool", {}, {"tool_result": {}})


# ── Counter helper ───────────────────────────────────────


def _count_drafts(root: Path) -> int:
    return len([p for p in root.iterdir() if p.suffix == ".md"])


def test_propose_draft_writes_file(isolated_drafts: Path) -> None:
    d = SkillDraft(
        slug="propose-direct",
        name="x",
        description="y",
        trigger_keywords=[],
        pattern_signature="sig",
        proposed_at=time.time(),
        trigger_kind="tool_pattern_repeat",
        occurrences=1,
        sample_canonical_shapes=["s"],
    )
    p = propose_draft(d)
    assert p.exists()
    assert _count_drafts(isolated_drafts) >= 1
    state = json.loads((isolated_drafts / "_state.json").read_text("utf-8"))
    assert "propose-direct" in state["drafts_index"]
