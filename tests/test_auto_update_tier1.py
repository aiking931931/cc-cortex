"""Tests for concinno.auto_update.tier1_registry.

Spec source:
``_AI_BRAIN/05_Planning/sancio-gui-extension-auto-update-design-2026-04-25.md``
``_AI_BRAIN/05_Planning/sancio-gui-extension-commander-verdict-2026-04-25.md``
(R#5 + R#10 amendments).
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from concinno.auto_update import (
    RegistryCache,
    RegistryDigest,
    RegistryRefreshResult,
    refresh_tier1_registry,
)
from concinno.auto_update import tier1_registry as t1  # noqa: E402

# ── fixtures ───────────────────────────────────────────────


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Redirect ``~/.concinno`` to a tmp dir for isolation."""
    home = tmp_path / "concinno_home"
    home.mkdir()
    monkeypatch.setattr(
        t1, "_concinno_home",
        lambda override=None: home if override is None else Path(override),
    )
    monkeypatch.setattr(
        t1, "_digest_cache_path",
        lambda override=None: home / "registry_digest",
    )
    return home


@pytest.fixture
def stub_gather(monkeypatch):
    """Replace _gather_safely with a deterministic small fixture."""

    def _stub(features: list[dict[str, Any]], skills: list[dict[str, Any]]) -> None:
        def fake() -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
            return list(features), list(skills), []

        monkeypatch.setattr(t1, "_gather_safely", fake)

    return _stub


@pytest.fixture
def stub_digest(monkeypatch):
    """Pin RegistryDigest.compute() to a known value."""

    def _stub(value: str) -> None:
        monkeypatch.setattr(RegistryDigest, "compute", staticmethod(lambda groups=None: value))

    return _stub


# ── #1 digest unchanged → skip rewrite ─────────────────────


def test_digest_unchanged_skips_rewrite(fake_home, stub_digest, stub_gather):
    """Pre-write digest = current → digest_hit=True, no caches written."""
    stub_digest("abc123")
    stub_gather(features=[], skills=[])
    # Pre-seed the digest cache file with the same value.
    t1._write_cached_digest(fake_home / "registry_digest", "abc123")

    skills_path = fake_home / "skills.json"
    features_path = fake_home / "features_registry.json"
    assert not skills_path.exists()

    result = refresh_tier1_registry(timeout_ms=5000)
    assert result.digest_hit is True
    assert result.error is None
    assert result.elapsed_ms < 200, f"digest-hit path should be fast, got {result.elapsed_ms}ms"
    # Caches NOT written on digest-hit:
    assert not skills_path.exists()
    assert not features_path.exists()


# ── #2 digest changed → rewrite ────────────────────────────


def test_digest_changed_triggers_rewrite(fake_home, stub_digest, stub_gather):
    """Invalidated digest → discovery + rewrite."""
    stub_digest("new_digest_v2")
    t1._write_cached_digest(fake_home / "registry_digest", "old_digest_v1")
    stub_gather(
        features=[{"package": "concinno-features-foo", "entry_point": "ep1",
                   "features": ["foo_feat"]}],
        skills=[{"package": "concinno-skills-bar", "entry_point": "ep2",
                 "resolved_path": "/x", "skills": ["bar_skill"]}],
    )

    result = refresh_tier1_registry(timeout_ms=5000)
    assert result.error is None
    assert result.digest_hit is False
    assert result.skills_count == 1
    assert result.features_count == 1

    skills_data = json.loads((fake_home / "skills.json").read_text(encoding="utf-8"))
    assert "bar_skill" in skills_data
    feats_data = json.loads(
        (fake_home / "features_registry.json").read_text(encoding="utf-8")
    )
    assert "foo_feat" in feats_data

    # Digest cache updated:
    assert t1._read_cached_digest(fake_home / "registry_digest") == "new_digest_v2"


# ── #3 state preservation (R#10) ───────────────────────────


def test_state_preservation_keeps_user_enabled_false(
    fake_home, stub_digest, stub_gather,
):
    """Pre-existing skills.json with enabled:false must survive refresh."""
    stub_digest("d_v3")
    skills_path = fake_home / "skills.json"
    skills_path.write_text(json.dumps({
        "user_disabled": {
            "name": "user_disabled",
            "enabled": False,
            "scope": "plugin:old",
        },
    }), encoding="utf-8")
    stub_gather(
        features=[],
        skills=[{"package": "concinno-skills-x", "entry_point": "x",
                 "resolved_path": "/x",
                 "skills": ["user_disabled", "fresh_skill"]}],
    )

    result = refresh_tier1_registry(timeout_ms=5000)
    assert result.error is None
    data = json.loads(skills_path.read_text(encoding="utf-8"))

    # Old user choice (enabled=False) preserved:
    assert data["user_disabled"]["enabled"] is False
    # Fresh skill has no enabled key (caller defaults to True at read time):
    assert "fresh_skill" in data
    # Library-supplied fields refreshed even on the preserved entry:
    assert data["user_disabled"]["scope"] == "plugin:concinno-skills-x"


# ── #4 race lock — concurrent merge_and_write ──────────────


def test_race_lock_no_torn_write(tmp_path):
    """Two threads calling merge_and_write must serialize and not corrupt."""
    cache_path = tmp_path / "x.json"
    cache = RegistryCache(cache_path)

    # Pre-populate so each thread has something to merge.
    cache.merge_and_write([{"name": "a", "v": 0}])

    errors: list[Exception] = []

    def worker(tag: str) -> None:
        try:
            for i in range(10):
                cache.merge_and_write([
                    {"name": f"{tag}_{i}", "v": i, "tag": tag},
                ])
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(t,)) for t in ("A", "B")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"thread errors: {errors}"
    # File parses cleanly (no torn write, no half-JSON):
    data = json.loads(cache_path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)


# ── #5 300ms budget fail-soft ──────────────────────────────


def test_timeout_budget_fails_soft(fake_home, monkeypatch, stub_gather):
    """Slow gather → timed_out=True, no exception, error stays None."""

    # Force a digest miss path:
    monkeypatch.setattr(
        RegistryDigest, "compute", staticmethod(lambda groups=None: "miss")
    )

    def slow_gather() -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
        time.sleep(0.4)  # 400ms — overruns the 300ms budget
        return [], [], []

    monkeypatch.setattr(t1, "_gather_safely", slow_gather)

    result = refresh_tier1_registry(timeout_ms=300)
    assert result.timed_out is True
    assert result.error is None  # fail-soft, not fatal


# ── #6 500-skill stress fixture ────────────────────────────


def test_500_skill_fixture_fast(fake_home, monkeypatch):
    """Synthesize 500 skills + 500 features → registry write completes well
    inside 1s on any reasonable machine. Validates we don't have an
    accidental O(n²) merge somewhere."""
    monkeypatch.setattr(
        RegistryDigest, "compute", staticmethod(lambda groups=None: "stress_v1")
    )
    big_skills = [{
        "package": f"pkg_{i}",
        "entry_point": "ep",
        "resolved_path": f"/x/{i}",
        "skills": [f"skill_{i}"],
    } for i in range(500)]
    big_features = [{
        "package": f"fpkg_{i}",
        "entry_point": "fep",
        "features": [f"feat_{i}"],
    } for i in range(500)]
    monkeypatch.setattr(
        t1, "_gather_safely",
        lambda: (big_features, big_skills, []),
    )

    t0 = time.monotonic()
    result = refresh_tier1_registry(timeout_ms=2000)
    elapsed = (time.monotonic() - t0) * 1000

    assert result.error is None
    assert result.skills_count == 500
    assert result.features_count == 500
    # Generous bound — the stress test passes if we're not catastrophic.
    assert elapsed < 1500, f"500-skill refresh took {elapsed:.0f}ms"


# ── #7 invariant: error is None on success path ────────────


def test_invariant_error_is_none_on_success(
    fake_home, stub_digest, stub_gather,
):
    stub_digest("inv_v1")
    stub_gather(features=[], skills=[])
    result = refresh_tier1_registry(timeout_ms=5000)
    assert isinstance(result, RegistryRefreshResult)
    assert result.error is None


# ── extra: digest stability across calls ───────────────────


def test_digest_is_stable_across_calls():
    """RegistryDigest.compute() must return the same hex on repeated calls."""
    a = RegistryDigest.compute()
    b = RegistryDigest.compute()
    assert a == b
    assert isinstance(a, str)
    assert len(a) == 32  # blake2b @ digest_size=16 → 32 hex chars


# ── extra: load_existing handles malformed gracefully ──────


def test_cache_load_malformed_returns_empty(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not valid json", encoding="utf-8")
    cache = RegistryCache(p)
    assert cache.load_existing() == {}


# ── extra: force=True bypasses digest hit ──────────────────


def test_force_bypasses_digest_hit(fake_home, stub_digest, stub_gather):
    stub_digest("same_v")
    t1._write_cached_digest(fake_home / "registry_digest", "same_v")
    stub_gather(
        features=[],
        skills=[{"package": "p", "entry_point": "e", "resolved_path": "/",
                 "skills": ["s1"]}],
    )

    r_normal = refresh_tier1_registry(timeout_ms=5000)
    assert r_normal.digest_hit is True

    r_force = refresh_tier1_registry(timeout_ms=5000, force=True)
    assert r_force.digest_hit is False
    assert r_force.skills_count == 1
