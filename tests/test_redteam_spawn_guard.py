"""Tests for concinno.redteam_spawn_guard — per-event spawn cap + ledger.

Scope
-----
- cap boundary (at cap / over cap / batch over)
- ledger persistence (append-only, multi-event isolation)
- env override (CONCINNO_REDTEAM_MAX_SPAWNS_PER_EVENT)
- reset semantics (single event / whole ledger)
- input validation (non-positive estimated_spawns)
- malformed ledger lines tolerated
- default cap fallback for junk env values
- SpawnLimitExceeded payload exposes event_id + attempted + cap

These tests isolate the ledger to ``tmp_path`` so the real
``_AI_BRAIN/00_System/redteam_ledger.jsonl`` is never touched during
CI runs.
"""

from __future__ import annotations  # noqa: I001

import json

import pytest

from concinno.redteam_spawn_guard import (
    DEFAULT_MAX_SPAWNS_PER_EVENT,
    ENV_MAX_SPAWNS,
    LEDGER_FILENAME,
    REDTEAM_GRANDCHILD_DIRECTIVE,
    RedteamSpawnLedger,
    SpawnLimitExceeded,
    SpawnRecord,
    before_spawn_redteam,
    reset_ledger,
)


# ── Helpers ──────────────────────────────────────────────


def _scoped(tmp_path):
    """Shared kwargs pinning the ledger under ``tmp_path``."""
    return {"cache_dir": str(tmp_path)}


# ── Tests ────────────────────────────────────────────────


def test_single_spawn_within_cap_records_ledger(tmp_path):
    """Happy path: one spawn for one event → one ledger line."""
    accepted = before_spawn_redteam(
        "evt-1", estimated_spawns=1, **_scoped(tmp_path),
    )
    assert accepted is True

    ledger_file = tmp_path / LEDGER_FILENAME
    assert ledger_file.exists()
    lines = ledger_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1

    record = json.loads(lines[0])
    assert record["event_id"] == "evt-1"
    assert record["spawn_count"] == 1
    assert record["cap"] == DEFAULT_MAX_SPAWNS_PER_EVENT


def test_spawns_accumulate_until_cap_boundary(tmp_path):
    """Exactly at the cap is allowed; one over raises."""
    cap = DEFAULT_MAX_SPAWNS_PER_EVENT  # 5
    for i in range(cap):
        before_spawn_redteam(
            "evt-boundary", estimated_spawns=1, **_scoped(tmp_path),
        )

    ledger = RedteamSpawnLedger(cache_dir=str(tmp_path))
    assert ledger.current_count("evt-boundary") == cap

    with pytest.raises(SpawnLimitExceeded) as exc:
        before_spawn_redteam(
            "evt-boundary", estimated_spawns=1, **_scoped(tmp_path),
        )
    assert exc.value.event_id == "evt-boundary"
    assert exc.value.attempted == cap + 1
    assert exc.value.cap == cap
    # failure must NOT write a record
    assert ledger.current_count("evt-boundary") == cap


def test_batch_spawn_over_cap_rejected_atomically(tmp_path):
    """Requesting a batch that would exceed cap writes zero records."""
    before_spawn_redteam(
        "evt-batch", estimated_spawns=3, **_scoped(tmp_path),
    )
    ledger = RedteamSpawnLedger(cache_dir=str(tmp_path))
    assert ledger.current_count("evt-batch") == 3

    # default cap = 5, 3 + 3 = 6 > 5 → reject
    with pytest.raises(SpawnLimitExceeded):
        before_spawn_redteam(
            "evt-batch", estimated_spawns=3, **_scoped(tmp_path),
        )
    assert ledger.current_count("evt-batch") == 3  # unchanged


def test_separate_events_do_not_share_budget(tmp_path):
    """evt-A hitting cap must not block evt-B."""
    cap = DEFAULT_MAX_SPAWNS_PER_EVENT
    for _ in range(cap):
        before_spawn_redteam("evt-A", estimated_spawns=1, **_scoped(tmp_path))

    # evt-B starts fresh
    accepted = before_spawn_redteam(
        "evt-B", estimated_spawns=1, **_scoped(tmp_path),
    )
    assert accepted is True

    ledger = RedteamSpawnLedger(cache_dir=str(tmp_path))
    assert ledger.current_count("evt-A") == cap
    assert ledger.current_count("evt-B") == 1


def test_env_override_lowers_cap(tmp_path, monkeypatch):
    """env var pins a smaller cap; over raises earlier."""
    monkeypatch.setenv(ENV_MAX_SPAWNS, "2")
    before_spawn_redteam("evt-env", estimated_spawns=1, **_scoped(tmp_path))
    before_spawn_redteam("evt-env", estimated_spawns=1, **_scoped(tmp_path))

    with pytest.raises(SpawnLimitExceeded) as exc:
        before_spawn_redteam(
            "evt-env", estimated_spawns=1, **_scoped(tmp_path),
        )
    assert exc.value.cap == 2


def test_env_override_junk_falls_back_to_default(tmp_path, monkeypatch):
    """Non-int / negative env values are ignored, default cap applies."""
    monkeypatch.setenv(ENV_MAX_SPAWNS, "not-a-number")
    cap = DEFAULT_MAX_SPAWNS_PER_EVENT
    for _ in range(cap):
        before_spawn_redteam(
            "evt-junk", estimated_spawns=1, **_scoped(tmp_path),
        )

    monkeypatch.setenv(ENV_MAX_SPAWNS, "0")  # non-positive → default
    with pytest.raises(SpawnLimitExceeded) as exc:
        before_spawn_redteam(
            "evt-junk", estimated_spawns=1, **_scoped(tmp_path),
        )
    assert exc.value.cap == cap


def test_reset_single_event(tmp_path):
    """reset_ledger(event_id) drops that event, leaves others."""
    before_spawn_redteam("evt-keep", estimated_spawns=2, **_scoped(tmp_path))
    before_spawn_redteam("evt-drop", estimated_spawns=3, **_scoped(tmp_path))

    dropped = reset_ledger("evt-drop", cache_dir=str(tmp_path))
    assert dropped == 3

    ledger = RedteamSpawnLedger(cache_dir=str(tmp_path))
    assert ledger.current_count("evt-keep") == 2
    assert ledger.current_count("evt-drop") == 0

    # and a fresh spawn on the dropped event is allowed again
    before_spawn_redteam("evt-drop", estimated_spawns=1, **_scoped(tmp_path))
    assert ledger.current_count("evt-drop") == 1


def test_reset_all(tmp_path):
    """reset_ledger() with no event_id wipes everything."""
    before_spawn_redteam("evt-X", estimated_spawns=2, **_scoped(tmp_path))
    before_spawn_redteam("evt-Y", estimated_spawns=1, **_scoped(tmp_path))

    dropped = reset_ledger(cache_dir=str(tmp_path))
    assert dropped == 3

    ledger = RedteamSpawnLedger(cache_dir=str(tmp_path))
    assert ledger.current_count("evt-X") == 0
    assert ledger.current_count("evt-Y") == 0
    assert not (tmp_path / LEDGER_FILENAME).exists()


def test_malformed_ledger_lines_tolerated(tmp_path):
    """Garbled ledger rows do not break current_count or append."""
    ledger_file = tmp_path / LEDGER_FILENAME
    ledger_file.write_text(
        "\n".join(
            [
                "not-json-at-all",
                json.dumps({"event_id": "evt-M", "spawn_count": 1}),
                "{broken: json",
                "",
            ],
        ),
        encoding="utf-8",
    )

    ledger = RedteamSpawnLedger(cache_dir=str(tmp_path))
    assert ledger.current_count("evt-M") == 1
    # append still works
    before_spawn_redteam("evt-M", estimated_spawns=1, **_scoped(tmp_path))
    assert ledger.current_count("evt-M") == 2


def test_non_positive_estimated_spawns_raises(tmp_path):
    """estimated_spawns <= 0 is a programmer error."""
    with pytest.raises(ValueError):
        before_spawn_redteam(
            "evt-bad", estimated_spawns=0, **_scoped(tmp_path),
        )
    with pytest.raises(ValueError):
        before_spawn_redteam(
            "evt-bad", estimated_spawns=-1, **_scoped(tmp_path),
        )


def test_explicit_cap_kwarg_wins_over_env(tmp_path, monkeypatch):
    """A caller can override env via the ``cap`` kwarg."""
    monkeypatch.setenv(ENV_MAX_SPAWNS, "99")
    with pytest.raises(SpawnLimitExceeded) as exc:
        before_spawn_redteam(
            "evt-cap",
            estimated_spawns=2,
            cap=1,
            **_scoped(tmp_path),
        )
    assert exc.value.cap == 1


def test_record_shape_and_grandchild_directive():
    """SpawnRecord is JSON-safe and grandchild directive exported."""
    rec = SpawnRecord(
        event_id="evt-shape",
        spawn_count=1,
        cap=5,
        timestamp="2026-04-18T00:00:00Z",
        estimated_spawns=1,
        role="redteam",
    )
    loaded = json.loads(rec.to_jsonl())
    assert loaded["event_id"] == "evt-shape"
    assert loaded["role"] == "redteam"
    assert isinstance(loaded["extra"], dict)

    assert "禁孫代理" in REDTEAM_GRANDCHILD_DIRECTIVE
    assert "MUST NOT spawn further sub-agents" in REDTEAM_GRANDCHILD_DIRECTIVE
