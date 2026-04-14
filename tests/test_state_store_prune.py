"""Tests for StateStore.prune_stale and prune_all_stale.

Covers the SessionStart hook contract: a single call sweeps every
namespace under base_dir of files older than the TTL.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from cc_cortex.core.state_store import StateStore


def _touch(path: Path, mtime_offset: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")
    target = time.time() + mtime_offset
    os.utime(path, (target, target))


def test_prune_stale_deletes_old_files_only(tmp_path: Path) -> None:
    store = StateStore(str(tmp_path))
    fresh = tmp_path / "ns_a" / "fresh.json"
    stale = tmp_path / "ns_a" / "stale.json"
    _touch(fresh, mtime_offset=0)
    _touch(stale, mtime_offset=-10 * 86400)

    deleted = store.prune_stale("ns_a", ttl_seconds=7 * 86400)

    assert deleted == 1
    assert fresh.exists()
    assert not stale.exists()


def test_prune_stale_skips_non_json(tmp_path: Path) -> None:
    store = StateStore(str(tmp_path))
    junk = tmp_path / "ns_b" / "old.lock"
    _touch(junk, mtime_offset=-10 * 86400)

    deleted = store.prune_stale("ns_b", ttl_seconds=7 * 86400)

    assert deleted == 0
    assert junk.exists()


def test_prune_stale_missing_namespace_returns_zero(tmp_path: Path) -> None:
    store = StateStore(str(tmp_path))
    assert store.prune_stale("nonexistent") == 0


def test_prune_all_stale_auto_discovers_namespaces(tmp_path: Path) -> None:
    store = StateStore(str(tmp_path))
    _touch(tmp_path / "cbua_pipeline" / "old.json", mtime_offset=-10 * 86400)
    _touch(tmp_path / "c0_route" / "old.json", mtime_offset=-10 * 86400)
    _touch(tmp_path / "session_memory" / "fresh.json", mtime_offset=0)

    report = store.prune_all_stale(ttl_seconds=7 * 86400)

    assert report == {
        "cbua_pipeline": 1,
        "c0_route": 1,
        "session_memory": 0,
    }
    assert not (tmp_path / "cbua_pipeline" / "old.json").exists()
    assert not (tmp_path / "c0_route" / "old.json").exists()
    assert (tmp_path / "session_memory" / "fresh.json").exists()


def test_prune_all_stale_explicit_list_respected(tmp_path: Path) -> None:
    store = StateStore(str(tmp_path))
    _touch(tmp_path / "cbua_pipeline" / "old.json", mtime_offset=-10 * 86400)
    _touch(tmp_path / "should_skip" / "old.json", mtime_offset=-10 * 86400)

    report = store.prune_all_stale(
        namespaces=("cbua_pipeline",),
        ttl_seconds=7 * 86400,
    )

    assert report == {"cbua_pipeline": 1}
    assert (tmp_path / "should_skip" / "old.json").exists()


def test_prune_all_stale_missing_base_dir(tmp_path: Path) -> None:
    store = StateStore(str(tmp_path / "nonexistent"))
    assert store.prune_all_stale() == {}
