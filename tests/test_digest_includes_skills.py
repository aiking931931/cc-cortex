"""Tests for 2.30.0 digest extension — skills auto-detection.

The GUI polls ``/api/features/digest`` every 3 seconds; the digest must
flip when a skill is added, renamed or removed so the client re-fetches
``/api/skills`` automatically. Added in 2.30.0 per the red/blue
verdict on ``concinno-2.30.0-autoregister-scaffolding-spec.md``.
"""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

import pytest


@pytest.fixture()
def isolated_home(tmp_path, monkeypatch):
    """Redirect ``Path.home()`` to a temp dir so skill discovery is
    deterministic across developer machines."""
    fake_home = tmp_path / "home"
    (fake_home / ".claude" / "skills").mkdir(parents=True)
    (fake_home / ".concinno").mkdir(parents=True)
    monkeypatch.setattr("pathlib.Path.home", lambda: fake_home)
    monkeypatch.chdir(tmp_path)
    return fake_home


def _write_skill(home: Path, *, scope: str, name: str, body: str = "# stub\n") -> Path:
    skill_dir = home / ".claude" / "skills" / scope / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    sk = skill_dir / "SKILL.md"
    sk.write_text(
        f"---\nname: {name}\ndescription: test skill\n---\n\n{body}",
        encoding="utf-8",
    )
    return sk


def test_digest_flips_when_skill_added(isolated_home: Path) -> None:
    from concinno.gui.server import _config_digest

    baseline = _config_digest()["digest"]
    _write_skill(isolated_home, scope="user", name="new_skill")
    after = _config_digest()["digest"]
    assert baseline != after, "adding a SKILL.md must flip the digest"


def test_digest_flips_when_skill_removed(isolated_home: Path) -> None:
    from concinno.gui.server import _config_digest

    sk = _write_skill(isolated_home, scope="user", name="doomed")
    baseline = _config_digest()["digest"]
    shutil.rmtree(sk.parent)
    after = _config_digest()["digest"]
    assert baseline != after, "removing a skill must flip the digest"


def test_digest_flips_when_skill_mtime_touched(isolated_home: Path) -> None:
    from concinno.gui.server import _config_digest

    sk = _write_skill(isolated_home, scope="user", name="touchable")
    baseline = _config_digest()["digest"]
    time.sleep(0.05)
    new_time = sk.stat().st_mtime + 10.0
    os.utime(sk, (new_time, new_time))
    after = _config_digest()["digest"]
    assert baseline != after, "editing SKILL.md must flip the digest"


def test_digest_excludes_git_and_node_modules_noise(isolated_home: Path) -> None:
    from concinno.gui.server import _config_digest

    submod = isolated_home / ".claude" / "skills" / "a_submodule" / ".git" / "skills"
    submod.mkdir(parents=True)
    (submod / "SKILL.md").write_text("noise", encoding="utf-8")

    nm = isolated_home / ".claude" / "skills" / "node_modules" / "pkg"
    nm.mkdir(parents=True)
    (nm / "SKILL.md").write_text("noise", encoding="utf-8")

    baseline = _config_digest()["digest"]

    time.sleep(0.05)
    new_time = (submod / "SKILL.md").stat().st_mtime + 10
    os.utime(submod / "SKILL.md", (new_time, new_time))
    os.utime(nm / "SKILL.md", (new_time, new_time))

    after = _config_digest()["digest"]
    assert baseline == after, "noise inside .git / node_modules must not flip digest"


def test_digest_deduplicates_overlapping_roots(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two different skill roots that resolve to the same path collapse."""
    from concinno.gui.server import _config_digest

    monkeypatch.chdir(isolated_home)
    _write_skill(isolated_home, scope="user", name="deduped")

    first = _config_digest()["digest"]
    second = _config_digest()["digest"]
    assert first == second


def test_digest_scale_fallback(isolated_home: Path) -> None:
    """Beyond 200 SKILL.md files the loop falls back to directory
    mtime hashing — verify it still returns a valid digest."""
    from concinno.gui.server import _DIGEST_SKILL_SCALE_THRESHOLD, _config_digest

    for i in range(_DIGEST_SKILL_SCALE_THRESHOLD + 10):
        _write_skill(isolated_home, scope="user", name=f"s{i:03d}")

    result = _config_digest()
    assert len(result["digest"]) == 16
    assert isinstance(result["mtime"], float)
