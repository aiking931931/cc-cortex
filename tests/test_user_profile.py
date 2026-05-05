"""Tests for ``concinno.user_profile``.

Coverage:
    * Schema round-trip (parse → render → parse stable)
    * Empty / missing file → default :class:`UserProfile`
    * ``update_user_profile`` writes + creates history
    * Frozen snapshot bound to :data:`HISTORY_MAX`
    * ``restore_snapshot`` rolls back to any of the last N entries
    * Char-budget clamp + truncation marker
    * ZIQ autotunable bounds (env / explicit override)
    * FieldRead bridge formatter — empty profile produces empty string
    * Unknown markdown sections preserved as ``free``
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from concinno import user_profile as up

# ── Fixture: redirect HOME so we don't touch the real ~/.concinno ────


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Redirect ``Path.home`` to ``tmp_path`` so all ~/.concinno writes
    land in a sandbox the test runner controls. Also clears the env
    override so tests get clean defaults.
    """
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.delenv(up._BUDGET_ENV, raising=False)
    yield tmp_path


# ── Path helpers ────────────────────────────────────────────────────


def test_paths_under_concinno_root(fake_home: Path) -> None:
    assert up.profile_path() == fake_home / ".concinno" / "USER.md"
    assert up.history_path() == fake_home / ".concinno" / "USER.history.jsonl"


# ── Defaults / read-empty ───────────────────────────────────────────


def test_read_user_profile_missing_returns_default(fake_home: Path) -> None:
    p = up.read_user_profile()
    assert p == up.UserProfile()
    assert p.language == up.DEFAULT_LANGUAGE


def test_read_user_profile_handles_unreadable_file(fake_home: Path) -> None:
    # Create a directory where the file should be — read_user_profile
    # must degrade to default rather than crash.
    (fake_home / ".concinno").mkdir()
    (fake_home / ".concinno" / "USER.md").mkdir()
    p = up.read_user_profile()
    assert p == up.UserProfile()


# ── Update + round-trip ─────────────────────────────────────────────


def test_update_writes_markdown_and_round_trips(fake_home: Path) -> None:
    profile = up.update_user_profile(
        {
            "role": "AI King — engineer",
            "language": "zh-TW",
            "domains": ["benchmarks", "agents"],
            "tools": ["Concinno", "RunPod"],
            "directives": ["never ask publish authorization"],
        },
        reason="initial",
    )
    assert not profile.truncated

    text = up.profile_path().read_text(encoding="utf-8")
    assert "## role" in text
    assert "AI King" in text
    assert "## directives" in text

    again = up.read_user_profile()
    assert again.role == "AI King — engineer"
    assert again.language == "zh-TW"
    assert again.domains == ("benchmarks", "agents")
    assert again.tools == ("Concinno", "RunPod")
    assert again.directives == ("never ask publish authorization",)


def test_update_string_section_splitlines(fake_home: Path) -> None:
    p = up.update_user_profile({"domains": "a\nb\nc"})
    assert p.domains == ("a", "b", "c")


def test_update_unknown_keys_silently_ignored(fake_home: Path) -> None:
    up.update_user_profile({"role": "ok", "totally_bogus": "x"})
    p = up.read_user_profile()
    assert p.role == "ok"


def test_update_partial_only_overrides_supplied_keys(fake_home: Path) -> None:
    up.update_user_profile({"role": "first", "tools": ["t1"]})
    up.update_user_profile({"role": "second"})
    p = up.read_user_profile()
    assert p.role == "second"
    assert p.tools == ("t1",)  # untouched


def test_update_blank_language_falls_back_to_default(fake_home: Path) -> None:
    up.update_user_profile({"role": "x", "language": ""})
    p = up.read_user_profile()
    assert p.language == up.DEFAULT_LANGUAGE


# ── Snapshot history ────────────────────────────────────────────────


def test_first_update_creates_history_with_empty_previous(
    fake_home: Path,
) -> None:
    up.update_user_profile({"role": "v1"}, reason="initial")
    h = up.history_path()
    assert h.is_file()
    lines = h.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["previous"] == ""
    assert rec["reason"] == "initial"


def test_history_bounded_to_history_max(fake_home: Path) -> None:
    for i in range(up.HISTORY_MAX + 5):
        up.update_user_profile({"role": f"v{i}"}, reason=f"r{i}")
    lines = up.history_path().read_text(encoding="utf-8").splitlines()
    assert len(lines) == up.HISTORY_MAX


def test_restore_snapshot_zero_rolls_back_one_step(fake_home: Path) -> None:
    up.update_user_profile({"role": "v1"})
    up.update_user_profile({"role": "v2"})
    up.update_user_profile({"role": "v3"})
    restored = up.restore_snapshot(0)
    # idx=0 is "the file before the most recent update" → v2 state.
    assert restored.role == "v2"


def test_restore_snapshot_one_rolls_back_two_steps(fake_home: Path) -> None:
    up.update_user_profile({"role": "v1"})
    up.update_user_profile({"role": "v2"})
    up.update_user_profile({"role": "v3"})
    restored = up.restore_snapshot(1)
    assert restored.role == "v1"


def test_restore_snapshot_out_of_range_raises(fake_home: Path) -> None:
    up.update_user_profile({"role": "only"})
    with pytest.raises(IndexError):
        up.restore_snapshot(99)


def test_restore_snapshot_negative_raises(fake_home: Path) -> None:
    with pytest.raises(IndexError):
        up.restore_snapshot(-1)


def test_restore_snapshot_no_history_raises(fake_home: Path) -> None:
    with pytest.raises(FileNotFoundError):
        up.restore_snapshot(0)


# ── Char budget ─────────────────────────────────────────────────────


def test_default_char_budget_is_1375(fake_home: Path) -> None:
    assert up.current_char_budget() == up.DEFAULT_CHAR_BUDGET == 1375


def test_char_budget_env_override_clamped(fake_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(up._BUDGET_ENV, "1500")
    assert up.current_char_budget() == 1500


def test_char_budget_below_min_clamps_to_min(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(up._BUDGET_ENV, "100")
    assert up.current_char_budget() == up.MIN_CHAR_BUDGET


def test_char_budget_above_max_clamps_to_max(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(up._BUDGET_ENV, "999999")
    assert up.current_char_budget() == up.MAX_CHAR_BUDGET


def test_char_budget_bad_env_value_falls_back(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(up._BUDGET_ENV, "not-a-number")
    assert up.current_char_budget() == up.DEFAULT_CHAR_BUDGET


def test_char_budget_explicit_override_wins(fake_home: Path) -> None:
    assert up.current_char_budget(config_override=1234) == 1234


def test_update_truncates_when_over_budget(fake_home: Path) -> None:
    big = "x" * 5000
    profile = up.update_user_profile(
        {"role": big},
        char_budget=1100,
    )
    assert profile.truncated
    text = up.profile_path().read_text(encoding="utf-8")
    assert len(text) <= 1100
    assert "truncated" in text


# ── FieldRead bridge ────────────────────────────────────────────────


def test_render_empty_profile_returns_empty_string(fake_home: Path) -> None:
    assert up.render_profile_for_field_read() == ""


def test_render_includes_high_signal_sections(fake_home: Path) -> None:
    up.update_user_profile(
        {
            "role": "engineer",
            "tools": ["Concinno"],
            "directives": ["d1", "d2"],
        }
    )
    out = up.render_profile_for_field_read()
    assert "## USER profile" in out
    assert "engineer" in out
    assert "Concinno" in out
    assert "d1" in out and "d2" in out


def test_render_respects_max_chars_argument(fake_home: Path) -> None:
    up.update_user_profile({"role": "x" * 500, "tools": ["a", "b"]})
    out = up.render_profile_for_field_read(max_chars=100)
    assert len(out) <= 100


def test_render_skipped_when_only_default_language_present(fake_home: Path) -> None:
    """Explicit zh-TW language with no other sections still counts as
    nothing meaningful — FieldRead should skip injection."""
    up.update_user_profile({"language": up.DEFAULT_LANGUAGE})
    assert up.render_profile_for_field_read() == ""


# ── Free-form preservation ──────────────────────────────────────────


def test_unknown_section_preserved_in_free_block(fake_home: Path) -> None:
    p = up.profile_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "# USER\n## role\nengineer\n## quirks\n- coffee\n",
        encoding="utf-8",
    )
    profile = up.read_user_profile()
    assert profile.role == "engineer"
    assert "quirks" in profile.free
    assert "coffee" in profile.free


def test_round_trip_preserves_free_block(fake_home: Path) -> None:
    p = up.profile_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "# USER\n## role\nengineer\n## quirks\n- one\n- two\n",
        encoding="utf-8",
    )
    # Touch the profile via update — round-trip should keep the free
    # block.
    up.update_user_profile({"role": "engineer"})
    refreshed = up.read_user_profile()
    assert "quirks" in refreshed.free


# ── Sanity: env cleanup independence ────────────────────────────────


def test_env_cleanup_idempotent(fake_home: Path) -> None:
    """Make sure the fixture cleared the env so consecutive tests
    don't poison each other (regression guard)."""
    assert os.environ.get(up._BUDGET_ENV) is None


# ── FieldRead integration ──────────────────────────────────────────


def test_field_read_includes_user_profile_when_present(
    fake_home: Path, tmp_path: Path,
) -> None:
    """FieldRead build_field_context_v2 should include a USER profile
    block when ~/.concinno/USER.md has content."""
    from concinno.field_read import build_field_context_v2

    # Set up USER.md with high-signal content.
    up.update_user_profile(
        {
            "role": "AI King",
            "tools": ["Concinno"],
            "directives": ["never ask for publish authorization"],
        }
    )

    # Workspace doesn't need real handoffs — USER profile injects
    # regardless because it's an independent source.
    workspace = tmp_path / "ws"
    workspace.mkdir()
    result = build_field_context_v2(str(workspace))

    assert "## USER profile" in result.content
    assert "AI King" in result.content
    assert "user_profile:overview" in result.sections_kept


def test_field_read_skips_user_profile_when_absent(
    fake_home: Path, tmp_path: Path,
) -> None:
    """No USER.md → no profile block in FieldRead output."""
    from concinno.field_read import build_field_context_v2

    workspace = tmp_path / "ws"
    workspace.mkdir()
    result = build_field_context_v2(str(workspace))

    assert "## USER profile" not in result.content
