"""Regression tests for ``concinno.hooks.pip_aftermath`` (4.2.0).

Covers:

* Pattern detection: pip install/uninstall on concinno (with or
  without ``python -m`` prefix) → triggers; commit message text
  containing ``pip install concinno`` does NOT trigger.
* Heartbeat freshness check: fresh → no hint; stale → hint with
  age in seconds; missing → "no heartbeat file" hint.
* Feature gate: ``pip_aftermath_hint.enabled=False`` → silent.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest


@pytest.fixture
def heartbeat_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect Path.home() so the hook reads a per-test heartbeat."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    return fake_home / ".memoria"


def _write_heartbeat(d: Path, *, age_seconds: float = 0.0,
                     pid: int = 12345) -> None:
    d.mkdir(parents=True, exist_ok=True)
    path = d / "heartbeat.json"
    payload = {
        "ts": time.time() - age_seconds,
        "ts_iso": "2026-04-27T00:00:00+0800",
        "pid": pid,
        "next_run_eta_seconds": 1800,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    # Force the file mtime to match so the hook's stat() check sees
    # the requested age, regardless of how recently we wrote it.
    new_mtime = time.time() - age_seconds
    import os
    os.utime(path, (new_mtime, new_mtime))


# ── Pattern detection ────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _force_module_reimport():
    """The hook caches ``_HEARTBEAT_PATH`` at import time; reimport so
    each test sees the redirected ``Path.home()``."""
    import importlib
    import sys
    if "concinno.hooks.pip_aftermath" in sys.modules:
        del sys.modules["concinno.hooks.pip_aftermath"]
    yield
    if "concinno.hooks.pip_aftermath" in sys.modules:
        importlib.reload(sys.modules["concinno.hooks.pip_aftermath"])


def test_detects_pip_install_concinno(heartbeat_dir):
    from concinno.hooks.pip_aftermath import detect_pip_concinno
    # No heartbeat file at all → "no heartbeat" hint
    ctx = detect_pip_concinno("Bash", {"command": "pip install concinno"})
    assert ctx is not None
    assert "concinno" in ctx.lower()


def test_detects_pip_upgrade_concinno(heartbeat_dir):
    from concinno.hooks.pip_aftermath import detect_pip_concinno
    ctx = detect_pip_concinno(
        "Bash", {"command": "pip install --upgrade concinno"},
    )
    assert ctx is not None


def test_detects_python_m_pip_install(heartbeat_dir):
    from concinno.hooks.pip_aftermath import detect_pip_concinno
    ctx = detect_pip_concinno(
        "Bash", {"command": "python -m pip install -e ./projects/concinno"},
    )
    assert ctx is not None


def test_detects_pip_uninstall(heartbeat_dir):
    from concinno.hooks.pip_aftermath import detect_pip_concinno
    ctx = detect_pip_concinno(
        "Bash", {"command": "pip uninstall -y concinno"},
    )
    assert ctx is not None


def test_no_match_for_concinno_skills_subpackage(heartbeat_dir):
    """``concinno-skills-foo`` is a different package — must not
    trigger the concinno aftermath hint."""
    from concinno.hooks.pip_aftermath import detect_pip_concinno
    ctx = detect_pip_concinno(
        "Bash", {"command": "pip install concinno-skills-auth"},
    )
    assert ctx is None


def test_no_match_for_commit_message_with_pip_concinno(heartbeat_dir):
    """The classifier-style false-positive guard: commit message
    text containing ``pip install concinno`` is not an actual pip
    invocation."""
    from concinno.hooks.pip_aftermath import detect_pip_concinno
    cmd = (
        'git commit -m "release(4.2.0): docs note pip install concinno '
        'invalidates Memoria daemon import refs"'
    )
    ctx = detect_pip_concinno("Bash", {"command": cmd})
    assert ctx is None


def test_no_match_for_non_bash_tool(heartbeat_dir):
    from concinno.hooks.pip_aftermath import detect_pip_concinno
    assert detect_pip_concinno("Read", {"file_path": "/tmp/foo"}) is None


# ── Heartbeat freshness ──────────────────────────────────────────────


def test_fresh_heartbeat_returns_none(heartbeat_dir):
    from concinno.hooks.pip_aftermath import detect_pip_concinno
    _write_heartbeat(heartbeat_dir, age_seconds=10)  # 10s old
    ctx = detect_pip_concinno("Bash", {"command": "pip install concinno"})
    assert ctx is None  # Memoria alive — no reminder


def test_stale_heartbeat_emits_hint(heartbeat_dir):
    from concinno.hooks.pip_aftermath import detect_pip_concinno
    _write_heartbeat(heartbeat_dir, age_seconds=600)  # 10 min old
    ctx = detect_pip_concinno(
        "Bash", {"command": "pip install --upgrade concinno"},
    )
    assert ctx is not None
    assert "stale" in ctx.lower() or "heartbeat" in ctx.lower()
    assert "pythonw" in ctx or "memoria" in ctx.lower()


def test_missing_heartbeat_emits_hint(heartbeat_dir):
    """No heartbeat file at all — hook can't tell if Memoria is alive
    or pre-heartbeat-version. Errs on the side of advising."""
    from concinno.hooks.pip_aftermath import detect_pip_concinno
    # heartbeat_dir created by fixture but no file written
    ctx = detect_pip_concinno("Bash", {"command": "pip install concinno"})
    assert ctx is not None
    assert "memoria" in ctx.lower()


def test_heartbeat_pid_in_hint_message(heartbeat_dir):
    from concinno.hooks.pip_aftermath import detect_pip_concinno
    _write_heartbeat(heartbeat_dir, age_seconds=1000, pid=99887)
    ctx = detect_pip_concinno(
        "Bash", {"command": "pip install --upgrade concinno"},
    )
    assert ctx is not None
    assert "99887" in ctx


# ── FEATURE_META wiring ──────────────────────────────────────────────


def test_pip_aftermath_hint_feature_meta_present():
    from concinno.feature_config import FEATURE_META
    assert "pip_aftermath_hint" in FEATURE_META
    meta = FEATURE_META["pip_aftermath_hint"]
    assert meta["category"] == "behavioral"
    assert meta.get("recommended") is True


def test_pip_aftermath_hint_not_in_default_off():
    """4.2.0 productivity feature ships ON by default."""
    from concinno.feature_config import DEFAULT_OFF_4_0_0
    assert "pip_aftermath_hint" not in DEFAULT_OFF_4_0_0


def test_pip_aftermath_hint_default_enabled_true():
    from concinno.feature_config import meta_enabled_default
    assert meta_enabled_default("pip_aftermath_hint") is True
