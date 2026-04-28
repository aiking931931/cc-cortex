"""W3.x carryover #8 — multi-process file-level lock for ``ArchiveAdvisor``.

Verifies that two ``ArchiveAdvisor`` instances pointed at the same
state file (the multi-process scenario) cannot last-writer-wins
each other's accept / reject FTRL updates. Locking is implemented
via the stdlib (``fcntl`` POSIX, ``msvcrt`` Windows) so the test
is portable.

Two layers of coverage:

1. ``_file_lock`` direct semantics — exclusive acquisition, peers
   wait, lock auto-releases when the owning process exits.
2. ``ArchiveAdvisor._locked_state_op`` end-to-end — N processes
   each call ``record_invocation`` in a tight loop and the final
   ``count`` equals the sum of all per-process increments
   (no lost writes).
"""
from __future__ import annotations

import json
import multiprocessing
import os
import threading
import time
from pathlib import Path

import pytest

from concinno.observability.token_audit.archive_advisor import (
    ArchiveAdvisor,
    _file_lock,
)

# ── _file_lock direct semantics ───────────────────────────────────


def test_file_lock_serialises_two_threads(tmp_path: Path) -> None:
    """Within one process, two threads racing on the same lock file
    must observe sequential entry into the critical section."""
    lock_path = tmp_path / "x.lock"
    counter = {"n": 0}
    snapshots: list[int] = []

    def worker() -> None:
        with _file_lock(lock_path):
            n = counter["n"]
            time.sleep(0.01)  # widen the race window
            counter["n"] = n + 1
            snapshots.append(counter["n"])

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert counter["n"] == 8
    # Each snapshot is the unique post-increment value — no lost writes.
    assert sorted(snapshots) == list(range(1, 9))


def test_file_lock_reentry_after_release(tmp_path: Path) -> None:
    """Acquiring the same lock back-to-back works (no leak / no
    sticky lock after release)."""
    lock_path = tmp_path / "y.lock"
    for _ in range(5):
        with _file_lock(lock_path):
            pass


def test_file_lock_creates_parent_directories(tmp_path: Path) -> None:
    """``mkdir(parents=True, exist_ok=True)`` runs inside the lock
    helper so callers do not need to pre-create the directory."""
    lock_path = tmp_path / "deep" / "nested" / "lock"
    with _file_lock(lock_path):
        pass
    assert lock_path.exists()


# ── End-to-end multi-process ──────────────────────────────────────


def _record_in_subprocess(
    state_path_str: str,
    skill: str,
    iterations: int,
    barrier_path_str: str,
) -> None:
    """Subprocess entry: spin until the barrier file vanishes, then
    fire ``iterations`` invocations through ``record_invocation``."""
    state_path = Path(state_path_str)
    barrier = Path(barrier_path_str)
    advisor = ArchiveAdvisor(
        state_path=state_path,
        archive_root=state_path.parent / "archives",
    )
    # Wait for the parent to release the barrier so all workers
    # start at roughly the same instant — widens the race window.
    deadline = time.time() + 5.0
    while barrier.exists() and time.time() < deadline:
        time.sleep(0.005)
    for i in range(iterations):
        advisor.record_invocation(skill, session_id=f"pid{os.getpid()}-{i}")


@pytest.mark.timeout(30)
def test_multiprocess_record_invocation_no_lost_writes(
    tmp_path: Path,
) -> None:
    """Spawn 4 child processes, each calling ``record_invocation``
    100 times against the same skill on the same state path. The
    final ``count`` field must equal 400 — no lost writes."""
    state_path = tmp_path / "shared.json"
    barrier = tmp_path / "barrier"
    barrier.write_text("hold", encoding="utf-8")

    procs: list[multiprocessing.Process] = []
    for _ in range(4):
        p = multiprocessing.Process(
            target=_record_in_subprocess,
            args=(str(state_path), "shared-skill", 100, str(barrier)),
        )
        p.start()
        procs.append(p)

    # Brief settle so every child has fork-spawned and is spinning
    # on the barrier check.
    time.sleep(0.2)
    barrier.unlink()  # release — all workers begin in lockstep

    for p in procs:
        p.join(timeout=20)
        assert p.exitcode == 0, (
            f"worker pid={p.pid} exited with code {p.exitcode}"
        )

    raw = json.loads(state_path.read_text(encoding="utf-8"))
    assert "shared-skill" in raw
    assert raw["shared-skill"]["count"] == 400, (
        "lost writes — multi-process lock failed: "
        f"got {raw['shared-skill']['count']} expected 400"
    )


@pytest.mark.timeout(30)
def test_multiprocess_accept_reject_serialise(tmp_path: Path) -> None:
    """4 processes each call ``accept`` 25 times then ``reject`` 25
    times against distinct skills. Every per-skill FTRL weight
    update must land — no silent dropouts."""
    state_path = tmp_path / "ftrl.json"
    barrier = tmp_path / "barrier-ftrl"
    barrier.write_text("hold", encoding="utf-8")

    procs: list[multiprocessing.Process] = []
    for i in range(4):
        p = multiprocessing.Process(
            target=_accept_reject_subprocess,
            args=(str(state_path), f"skill-{i}", 25, str(barrier)),
        )
        p.start()
        procs.append(p)

    time.sleep(0.2)
    barrier.unlink()

    for p in procs:
        p.join(timeout=20)
        assert p.exitcode == 0

    raw = json.loads(state_path.read_text(encoding="utf-8"))
    # Every skill saw 25 accepts then 25 rejects → final ``last_decision``
    # is "reject" and the FTRL weight is well within the soft cap.
    for i in range(4):
        rec = raw[f"skill-{i}"]
        assert rec["last_decision"] == "reject"
        # 25 accepts (+0.1*1) + 25 rejects (+0.1*-1) = net 0 then
        # capped, so weight is ≈ 0 ± numeric noise.
        assert abs(rec["ftrl_weight"]) < 1e-6


def _accept_reject_subprocess(
    state_path_str: str,
    skill: str,
    iterations: int,
    barrier_path_str: str,
) -> None:
    state_path = Path(state_path_str)
    barrier = Path(barrier_path_str)
    advisor = ArchiveAdvisor(
        state_path=state_path,
        archive_root=state_path.parent / "archives",
    )
    deadline = time.time() + 5.0
    while barrier.exists() and time.time() < deadline:
        time.sleep(0.005)
    for _ in range(iterations):
        advisor.accept(skill)
    for _ in range(iterations):
        advisor.reject(skill)


# ── Lock path matches state path ─────────────────────────────────


def test_lock_path_sidecar_to_state(tmp_path: Path) -> None:
    """Lock file lives next to the state file — separate advisors
    pointed at distinct paths must NOT contend."""
    a = ArchiveAdvisor(state_path=tmp_path / "a.json")
    b = ArchiveAdvisor(state_path=tmp_path / "b.json")
    assert a._lock_path != b._lock_path
    assert a._lock_path == tmp_path / "a.json.lock"
    assert b._lock_path == tmp_path / "b.json.lock"


def test_locked_state_op_persists_on_clean_exit(tmp_path: Path) -> None:
    """Mutations made inside the context manager land on disk."""
    advisor = ArchiveAdvisor(state_path=tmp_path / "ok.json")
    with advisor._locked_state_op() as state:
        state["my-skill"] = {"count": 7, "ftrl_weight": 0.0}

    raw = json.loads((tmp_path / "ok.json").read_text(encoding="utf-8"))
    assert raw == {"my-skill": {"count": 7, "ftrl_weight": 0.0}}


def test_locked_state_op_persists_on_exception(tmp_path: Path) -> None:
    """Even when the caller raises, partial mutations are flushed
    so the lock owner does not orphan in-flight state."""
    advisor = ArchiveAdvisor(state_path=tmp_path / "boom.json")
    with pytest.raises(RuntimeError):
        with advisor._locked_state_op() as state:
            state["partial"] = {"count": 1}
            raise RuntimeError("boom")

    raw = json.loads((tmp_path / "boom.json").read_text(encoding="utf-8"))
    assert raw == {"partial": {"count": 1}}
