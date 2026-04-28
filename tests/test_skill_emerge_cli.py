"""Tests for ``concinno skill-emerge {list,show,accept,reject,prune}`` CLI.

Exercises the CLI handlers directly (no subprocess) with isolated draft
+ live-skill roots via env-var monkeypatching, mirroring the fixture
shape from ``test_skill_emergence_guard.py``.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import pytest

from concinno.cli.skill_emerge_cmd import (
    _cmd_accept,
    _cmd_list,
    _cmd_prune,
    _cmd_reject,
    _cmd_show,
    register,
)
from concinno.skills.skill_emergence_guard import (
    SkillDraft,
    _load_state,
    propose_draft,
)


@pytest.fixture
def isolated_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, Path]:
    """Isolate both staging dir and live skill root.

    Returns ``(draft_root, live_root)``.
    """
    draft_dir = tmp_path / "drafts"
    live_dir = tmp_path / "live"
    monkeypatch.setenv("CONCINNO_SKILL_DRAFT_DIR", str(draft_dir))
    monkeypatch.setenv("CONCINNO_SKILL_DRAFT_STATE", str(draft_dir / "_state.json"))
    monkeypatch.setenv("CONCINNO_LIVE_SKILL_ROOT", str(live_dir))
    return draft_dir, live_dir


def _make_draft(slug: str = "test-workflow") -> SkillDraft:
    return SkillDraft(
        slug=slug,
        name=slug,
        description=f"Test draft {slug}",
        trigger_keywords=["test", slug],
        pattern_signature=f"Bash|{slug}",
        proposed_at=time.time(),
        trigger_kind="tool_pattern_repeat",
        occurrences=3,
        sample_canonical_shapes=[slug],
    )


# ── register() ────────────────────────────────────────────


def test_register_adds_skill_emerge_subcommand() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    register(sub)
    args = parser.parse_args(["skill-emerge", "list"])
    assert args.command == "skill-emerge"
    assert args.skill_emerge_action == "list"


def test_register_accept_takes_slug_and_force() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    register(sub)
    args = parser.parse_args(
        ["skill-emerge", "accept", "my-slug", "--force", "--keep-draft"],
    )
    assert args.skill_emerge_action == "accept"
    assert args.slug == "my-slug"
    assert args.force is True
    assert args.keep_draft is True


# ── list ──────────────────────────────────────────────────


def test_list_empty_when_no_drafts(
    isolated_dirs: tuple[Path, Path], capsys: pytest.CaptureFixture[str],
) -> None:
    rc = _cmd_list(argparse.Namespace())
    assert rc == 0
    assert "no drafts staged" in capsys.readouterr().out


def test_list_shows_pending_drafts(
    isolated_dirs: tuple[Path, Path], capsys: pytest.CaptureFixture[str],
) -> None:
    propose_draft(_make_draft("alpha"))
    propose_draft(_make_draft("bravo"))
    rc = _cmd_list(argparse.Namespace())
    assert rc == 0
    out = capsys.readouterr().out
    assert "alpha" in out
    assert "bravo" in out
    assert "pending" in out


# ── show ──────────────────────────────────────────────────


def test_show_prints_draft_markdown(
    isolated_dirs: tuple[Path, Path], capsys: pytest.CaptureFixture[str],
) -> None:
    propose_draft(_make_draft("alpha"))
    rc = _cmd_show(argparse.Namespace(slug="alpha"))
    assert rc == 0
    out = capsys.readouterr().out
    assert "alpha" in out
    assert "name:" in out  # Frontmatter present


def test_show_missing_slug_returns_1(
    isolated_dirs: tuple[Path, Path], capsys: pytest.CaptureFixture[str],
) -> None:
    rc = _cmd_show(argparse.Namespace(slug="ghost"))
    assert rc == 1
    assert "no draft found" in capsys.readouterr().err


# ── accept ────────────────────────────────────────────────


def test_accept_installs_draft_to_live_root(
    isolated_dirs: tuple[Path, Path],
) -> None:
    draft_dir, live_dir = isolated_dirs
    propose_draft(_make_draft("alpha"))
    rc = _cmd_accept(
        argparse.Namespace(slug="alpha", force=False, keep_draft=False),
    )
    assert rc == 0
    installed = live_dir / "alpha" / "SKILL.md"
    assert installed.exists()
    body = installed.read_text(encoding="utf-8")
    assert "name: alpha" in body or "alpha" in body
    # Default behavior removes the draft after install
    assert not (draft_dir / "alpha.md").exists()


def test_accept_marks_resolution_in_state(
    isolated_dirs: tuple[Path, Path],
) -> None:
    propose_draft(_make_draft("alpha"))
    _cmd_accept(
        argparse.Namespace(slug="alpha", force=False, keep_draft=True),
    )
    state = _load_state()
    assert state.drafts_index["alpha"]["resolution"] == "accepted"
    assert "resolved_at" in state.drafts_index["alpha"]


def test_accept_keep_draft_preserves_staging(
    isolated_dirs: tuple[Path, Path],
) -> None:
    draft_dir, _live = isolated_dirs
    propose_draft(_make_draft("alpha"))
    _cmd_accept(
        argparse.Namespace(slug="alpha", force=False, keep_draft=True),
    )
    assert (draft_dir / "alpha.md").exists()


def test_accept_refuses_overwrite_without_force(
    isolated_dirs: tuple[Path, Path], capsys: pytest.CaptureFixture[str],
) -> None:
    _draft_dir, live_dir = isolated_dirs
    propose_draft(_make_draft("alpha"))
    target = live_dir / "alpha" / "SKILL.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("hand-tuned skill", encoding="utf-8")

    rc = _cmd_accept(
        argparse.Namespace(slug="alpha", force=False, keep_draft=False),
    )
    assert rc == 2
    assert "refusing to overwrite" in capsys.readouterr().err
    assert target.read_text(encoding="utf-8") == "hand-tuned skill"


def test_accept_force_overwrites_existing(
    isolated_dirs: tuple[Path, Path],
) -> None:
    _draft_dir, live_dir = isolated_dirs
    propose_draft(_make_draft("alpha"))
    target = live_dir / "alpha" / "SKILL.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("hand-tuned skill", encoding="utf-8")

    rc = _cmd_accept(
        argparse.Namespace(slug="alpha", force=True, keep_draft=False),
    )
    assert rc == 0
    assert "name: alpha" in target.read_text(encoding="utf-8")


def test_accept_missing_draft_returns_1(
    isolated_dirs: tuple[Path, Path], capsys: pytest.CaptureFixture[str],
) -> None:
    rc = _cmd_accept(
        argparse.Namespace(slug="ghost", force=False, keep_draft=False),
    )
    assert rc == 1
    assert "no draft found" in capsys.readouterr().err


# ── reject ────────────────────────────────────────────────


def test_reject_removes_draft_and_marks_state(
    isolated_dirs: tuple[Path, Path],
) -> None:
    draft_dir, _live = isolated_dirs
    propose_draft(_make_draft("alpha"))
    rc = _cmd_reject(argparse.Namespace(slug="alpha"))
    assert rc == 0
    assert not (draft_dir / "alpha.md").exists()
    state = _load_state()
    assert state.drafts_index["alpha"]["resolution"] == "rejected"


def test_reject_missing_slug_returns_1(
    isolated_dirs: tuple[Path, Path], capsys: pytest.CaptureFixture[str],
) -> None:
    rc = _cmd_reject(argparse.Namespace(slug="ghost"))
    assert rc == 1
    assert "no draft found" in capsys.readouterr().err


# ── prune ─────────────────────────────────────────────────


def test_prune_removes_resolved_drafts_only(
    isolated_dirs: tuple[Path, Path],
) -> None:
    draft_dir, _live = isolated_dirs
    propose_draft(_make_draft("alpha"))
    propose_draft(_make_draft("bravo"))
    propose_draft(_make_draft("charlie"))
    _cmd_accept(
        argparse.Namespace(slug="alpha", force=False, keep_draft=False),
    )
    _cmd_reject(argparse.Namespace(slug="bravo"))
    # charlie remains pending

    rc = _cmd_prune(argparse.Namespace())
    assert rc == 0
    state = _load_state()
    assert "alpha" not in state.drafts_index
    assert "bravo" not in state.drafts_index
    assert "charlie" in state.drafts_index
    assert (draft_dir / "charlie.md").exists()


def test_prune_empty_when_nothing_resolved(
    isolated_dirs: tuple[Path, Path], capsys: pytest.CaptureFixture[str],
) -> None:
    propose_draft(_make_draft("alpha"))
    rc = _cmd_prune(argparse.Namespace())
    assert rc == 0
    assert "nothing to prune" in capsys.readouterr().out


# ── live_skill_root override ──────────────────────────────


def test_live_skill_root_respects_env_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from concinno.skills.skill_emergence_guard import live_skill_root

    monkeypatch.setenv("CONCINNO_LIVE_SKILL_ROOT", str(tmp_path / "custom"))
    assert live_skill_root() == tmp_path / "custom"

    monkeypatch.delenv("CONCINNO_LIVE_SKILL_ROOT", raising=False)
    # Default — relative to home, never under /tmp
    default = live_skill_root()
    assert default.parts[-2:] == (".claude", "skills")
