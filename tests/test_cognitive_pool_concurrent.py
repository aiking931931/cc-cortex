"""Regression tests for F2 (2.7.1): cognitive pool file lock.

Before 2.7.1, ``CognitivePool.save()`` was a naive tmp-rename. Two
concurrent writers could both ``load()`` an empty pool, both ``save()``
with their own single section, and the later ``replace()`` wiped the
earlier writer's section — last-write-wins silently dropped memories.

After 2.7.1, ``upsert_section`` / ``remove_section`` / ``prune_stale``
hold a cross-process advisory lock and reload before modifying, so
both writers' sections survive.
"""

from __future__ import annotations

import threading
from pathlib import Path

from concinno.cache.cognitive_pool import CognitivePool


def test_concurrent_upserts_preserve_both_sections(tmp_path: Path) -> None:
    """Two threads upsert different titles — both must survive."""
    barrier = threading.Barrier(2)

    def _writer(title: str, body: str) -> None:
        # Each thread builds its own pool instance to match the real-
        # world pattern where different subagents load independently.
        local = CognitivePool(root=tmp_path)
        barrier.wait()
        local.upsert_section(title=title, body=body)

    t1 = threading.Thread(target=_writer, args=("alpha", "one"))
    t2 = threading.Thread(target=_writer, args=("beta", "two"))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)
    assert not t1.is_alive() and not t2.is_alive()

    # Fresh reader — confirm both sections made it to disk.
    reader = CognitivePool(root=tmp_path)
    titles = {s.title for s in reader.read_all()}
    assert titles == {"alpha", "beta"}, (
        f"expected both sections, got {titles}"
    )


def test_concurrent_same_title_keeps_one(tmp_path: Path) -> None:
    """Two threads upsert the SAME title — exactly one section remains."""

    def _writer(body: str) -> None:
        local = CognitivePool(root=tmp_path)
        local.upsert_section(title="shared", body=body)

    threads = [threading.Thread(target=_writer, args=(f"body{i}",))
               for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
        assert not t.is_alive()

    reader = CognitivePool(root=tmp_path)
    sections = reader.read_all()
    assert len(sections) == 1
    assert sections[0].title == "shared"
    # Body must be one of the three writers' payloads — not garbled.
    assert sections[0].body in {"body0", "body1", "body2"}


def test_lock_file_path_does_not_collide(tmp_path: Path) -> None:
    """Lock file is a sibling of the pool, not the pool itself."""
    pool = CognitivePool(root=tmp_path)
    pool.upsert_section(title="a", body="x")
    assert pool.pool_path().exists()
    # Lock file created beside the pool.
    lock = pool.pool_path().with_suffix(pool.pool_path().suffix + ".lock")
    assert lock.exists()
    # Pool is not truncated by lock ops.
    assert pool.pool_path().read_text("utf-8").strip()
