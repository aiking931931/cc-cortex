"""Regression tests for F1 + F5 (2.7.1): installer symlink safety.

F1: ``shutil.rmtree(dest_dir)`` in ``install_skills`` followed symlinks
    on POSIX (and junctions on Windows) — if a user had symlinked
    ``<target>/public/<skill>`` to their personal dev workspace the
    rmtree would wipe the target.

F5: ``_ensure_junction`` failures on non-Windows used to silent-pass
    ``(OSError, CalledProcessError)``. Now emits stderr warning so
    failures are visible.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


def _make_bundled_public_dir(tmp_path: Path, skill_name: str) -> Path:
    """Build a fake ``src/concinno/skills/public/<skill>/`` structure.

    We monkeypatch ``SKILLS_DIR`` to point here so installer walks it
    instead of the real bundled one.
    """
    bundled = tmp_path / "bundled" / "public" / skill_name
    bundled.mkdir(parents=True)
    (bundled / "SKILL.md").write_text("# test skill\n", encoding="utf-8")
    return bundled


def test_rmtree_skips_symlink_to_user_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Existing symlink at dest_dir must be unlinked, never followed."""
    from concinno.skills import installer

    # Fake "bundled" skill source
    bundled_root = tmp_path / "bundled"
    _make_bundled_public_dir(tmp_path, "demo")
    monkeypatch.setattr(installer, "SKILLS_DIR", bundled_root)

    # Fake target with a symlink that points at a precious user repo.
    target = tmp_path / "target"
    (target / "public").mkdir(parents=True)
    user_repo = tmp_path / "user_repo"
    user_repo.mkdir()
    (user_repo / "precious.txt").write_text("DO NOT DELETE", encoding="utf-8")

    link_path = target / "public" / "demo"
    if sys.platform == "win32":
        # Prefer junction; fall back to skipping on non-privileged builds.
        import subprocess
        rc = subprocess.run(
            ["cmd.exe", "/c", "mklink", "/J", str(link_path), str(user_repo)],
            capture_output=True,
        )
        if rc.returncode != 0:
            pytest.skip(f"mklink failed: {rc.stderr!r}")
    else:
        os.symlink(str(user_repo), str(link_path), target_is_directory=True)

    installer.install_skills(str(target))

    # Precious file still there — rmtree did NOT follow the link.
    assert (user_repo / "precious.txt").exists(), (
        "symlink/junction was followed by rmtree — user data destroyed"
    )

    # New skill directory exists at the link position.
    assert (target / "public" / "demo" / "SKILL.md").exists()


def test_real_directory_still_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A REAL directory at dest_dir must still be replaced (legacy behaviour)."""
    from concinno.skills import installer

    bundled_root = tmp_path / "bundled"
    _make_bundled_public_dir(tmp_path, "demo")
    monkeypatch.setattr(installer, "SKILLS_DIR", bundled_root)

    target = tmp_path / "target"
    (target / "public" / "demo").mkdir(parents=True)
    (target / "public" / "demo" / "OLD.md").write_text("old", encoding="utf-8")

    installer.install_skills(str(target))

    # Old file gone (rmtree on real dir), new file installed.
    assert not (target / "public" / "demo" / "OLD.md").exists()
    assert (target / "public" / "demo" / "SKILL.md").exists()


def test_nonexistent_dest_skipped_gracefully(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No dest_dir yet → install proceeds without errors."""
    from concinno.skills import installer

    bundled_root = tmp_path / "bundled"
    _make_bundled_public_dir(tmp_path, "demo")
    monkeypatch.setattr(installer, "SKILLS_DIR", bundled_root)

    target = tmp_path / "target"
    installed = installer.install_skills(str(target))
    assert any("demo" in p for p in installed)


def test_junction_failure_emits_stderr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """F5: _ensure_junction failure produces a stderr warning (not silent)."""
    from concinno.skills import installer

    bundled_root = tmp_path / "bundled"
    _make_bundled_public_dir(tmp_path, "demo")
    monkeypatch.setattr(installer, "SKILLS_DIR", bundled_root)

    # Force _ensure_junction to raise.
    def _boom(link_path: str, target_path: str) -> bool:
        raise OSError("simulated junction failure")

    monkeypatch.setattr(installer, "_ensure_junction", _boom)

    target = tmp_path / "target"
    installer.install_skills(str(target))

    err = capsys.readouterr().err
    assert "warning" in err.lower()
    assert "junction" in err.lower() or "demo" in err
