"""Tests for :mod:`concinno.ziq_persist` — disk-persisted FTRL state.

Closes the 4.6.0 audit C-axis "FTRL never converges across restart"
finding: weights now survive process restart via append-only jsonl +
periodic snapshot compaction at ``~/.concinno/ziq_state/``.

The test suite covers:

* round-trip — write 50 updates in process A, restart in fresh
  subprocess, assert weights survive,
* cross-platform path — every test honours ``tmp_path`` so no real
  ``~/.concinno/`` write happens,
* atomic write — simulate mid-write crash via a monkeypatched
  ``json.dumps`` raising ``OSError``; verify the snapshot stays
  valid (no half-written file replaces a good one),
* compaction — assert jsonl shrinks after compact, snapshot reflects
  the merged state,
* kill-switch — ``CONCINNO_ZIQ_PERSIST_DISABLED`` collapses every API
  to a fail-open no-op,
* SkillDisclosure integration — wired consumer round-trips weights
  through the persistence layer.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from concinno import ziq_persist
from concinno.ziq_persist import (
    compact_ftrl_state,
    is_disabled,
    jsonl_path,
    load_ftrl_state,
    record_ftrl_update,
    snapshot_path,
    state_dir,
)

# ── fixtures ─────────────────────────────────────────────────────────


@pytest.fixture()
def ziq_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Pin the ZIQ state directory to ``tmp_path / ziq_state`` for this test.

    The autouse ``conftest._isolate_state_dir`` already does this for
    every test in the suite, but pinning it locally too lets these
    tests assert on a path they fully own.
    """
    target = tmp_path / "ziq_state"
    monkeypatch.setenv("CONCINNO_ZIQ_STATE_DIR", str(target))
    monkeypatch.delenv("CONCINNO_ZIQ_PERSIST_DISABLED", raising=False)
    return target


# ── path helpers ─────────────────────────────────────────────────────


def test_state_dir_honours_env_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CONCINNO_ZIQ_STATE_DIR", str(tmp_path / "custom"))
    assert state_dir() == tmp_path / "custom"


def test_state_dir_defaults_to_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CONCINNO_ZIQ_STATE_DIR", raising=False)
    assert state_dir() == Path.home() / ".concinno" / "ziq_state"


def test_filename_is_filesystem_safe(ziq_state: Path) -> None:
    """Path traversal — separators in feature name collapse to ``_``.

    Note: ``_safe_feature`` allows ``.`` because legitimate feature
    names (``archive_advisor.accept`` etc.) use it. The security
    property we enforce is that *path separators* (``/`` ``\\``)
    cannot escape ``state_dir()`` — a feature name like
    ``../../etc/passwd`` therefore lands as
    ``.._.._etc_passwd_ftrl.jsonl`` inside the state dir, not
    ``/etc/passwd``.
    """
    p = jsonl_path("../../etc/passwd")
    assert "/" not in p.name
    assert "\\" not in p.name
    # The resolved file is inside state_dir, not outside it.
    assert p.parent == state_dir()


# ── round-trip ───────────────────────────────────────────────────────


def test_record_then_load_round_trip(ziq_state: Path) -> None:
    """50 updates → load returns the last-written value per key."""
    feature = "skill_disclosure"
    for i in range(50):
        weight = 0.1 + 0.01 * i  # 0.10 → 0.59
        record_ftrl_update(
            feature,
            "kb_runpod",
            weight_before=weight - 0.01,
            weight_after=weight,
            signal=1.0,
        )
    loaded = load_ftrl_state(feature)
    assert "kb_runpod" in loaded
    # Last write wins — weight at i=49 is 0.10 + 0.01 * 49 = 0.59.
    assert loaded["kb_runpod"] == pytest.approx(0.59, abs=1e-9)


def test_round_trip_multiple_keys(ziq_state: Path) -> None:
    feature = "multi"
    record_ftrl_update(
        feature, "alpha", weight_before=1.0, weight_after=0.5,
    )
    record_ftrl_update(
        feature, "beta", weight_before=1.0, weight_after=1.5,
    )
    record_ftrl_update(
        feature, "alpha", weight_before=0.5, weight_after=0.25,
    )
    loaded = load_ftrl_state(feature)
    assert loaded["alpha"] == pytest.approx(0.25)
    assert loaded["beta"] == pytest.approx(1.5)


def test_subprocess_restart_survives_state(ziq_state: Path) -> None:
    """The audit C-axis fix: a fresh process must see written weights.

    A subprocess gives us true cold start (fresh module imports, no
    in-memory leak from the parent test). The env var carries the
    state-dir override so the child writes / reads from the same
    isolated location.
    """
    # Parent process: write 10 updates.
    feature = "restart_test"
    for i in range(10):
        record_ftrl_update(
            feature,
            f"key_{i}",
            weight_before=1.0,
            weight_after=float(i) / 10.0,
        )
    # Force compaction so the child sees a snapshot (covers the
    # snapshot-load path that pure jsonl-replay would skip).
    compact_ftrl_state(feature)
    # Child process: import fresh, load, print as JSON.
    child_code = textwrap.dedent(
        """
        from concinno.ziq_persist import load_ftrl_state
        import json, sys
        weights = load_ftrl_state("restart_test")
        sys.stdout.write(json.dumps(weights, sort_keys=True))
        """
    )
    child_env = dict(os.environ)
    # The autouse fixture already pinned the dir for this test; the
    # subprocess inherits the env, but we set it explicitly so the
    # round-trip is provable from the test source.
    child_env["CONCINNO_ZIQ_STATE_DIR"] = str(ziq_state)
    child_env.pop("CONCINNO_ZIQ_PERSIST_DISABLED", None)
    result = subprocess.run(
        [sys.executable, "-c", child_code],
        env=child_env,
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    parsed = json.loads(result.stdout)
    assert len(parsed) == 10
    for i in range(10):
        assert parsed[f"key_{i}"] == pytest.approx(float(i) / 10.0)


# ── cold start ───────────────────────────────────────────────────────


def test_cold_start_returns_empty(ziq_state: Path) -> None:
    """Absent state files yield empty dict, never raise."""
    assert load_ftrl_state("never_written") == {}
    # State dir need not exist either — sanity.
    assert not ziq_state.exists() or ziq_state.is_dir()


def test_load_ignores_corrupt_snapshot(ziq_state: Path) -> None:
    """A corrupt snapshot.json must NOT crash; falls back to empty."""
    feature = "corrupt"
    p = snapshot_path(feature)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not-valid-json", encoding="utf-8")
    # Still empty — corrupt JSON treated as cold start.
    assert load_ftrl_state(feature) == {}


def test_load_ignores_corrupt_jsonl_lines(ziq_state: Path) -> None:
    """A corrupt jsonl line must be skipped, not abort the replay."""
    feature = "partial_corrupt"
    record_ftrl_update(
        feature, "good", weight_before=1.0, weight_after=0.7,
    )
    log = jsonl_path(feature)
    # Append two malformed lines + one good one.
    with log.open("a", encoding="utf-8") as fh:
        fh.write("{this-is-not-json\n")
        fh.write('{"feature": "x"}\n')  # missing key/weight_after
        fh.write(
            '{"key": "extra", "weight_after": 0.42}\n',
        )
    loaded = load_ftrl_state(feature)
    assert loaded["good"] == pytest.approx(0.7)
    assert loaded["extra"] == pytest.approx(0.42)


# ── kill switch ──────────────────────────────────────────────────────


def test_kill_switch_collapses_to_noop(
    ziq_state: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``CONCINNO_ZIQ_PERSIST_DISABLED=1`` makes every API a no-op."""
    monkeypatch.setenv("CONCINNO_ZIQ_PERSIST_DISABLED", "1")
    assert is_disabled() is True
    record_ftrl_update(
        "kill", "k", weight_before=1.0, weight_after=0.5,
    )
    # Nothing was written.
    assert not jsonl_path("kill").exists()
    # Load returns empty without trying to read disk.
    assert load_ftrl_state("kill") == {}
    # Compact is a no-op too.
    assert compact_ftrl_state("kill") == {}


# ── compaction ───────────────────────────────────────────────────────


def test_compact_writes_snapshot_and_reflects_state(
    ziq_state: Path,
) -> None:
    feature = "compact_test"
    for i in range(5):
        record_ftrl_update(
            feature,
            "k",
            weight_before=1.0 - 0.1 * i,
            weight_after=1.0 - 0.1 * (i + 1),
        )
    snap = snapshot_path(feature)
    assert not snap.exists(), "snapshot should not exist before compact"
    merged = compact_ftrl_state(feature)
    assert snap.is_file()
    doc = json.loads(snap.read_text(encoding="utf-8"))
    assert doc["schema"] == 1
    assert doc["feature"] == feature
    assert doc["weights"]["k"] == pytest.approx(0.5)
    assert merged == doc["weights"]


def test_compact_truncates_jsonl_to_keep_last_n(ziq_state: Path) -> None:
    feature = "trunc"
    for i in range(20):
        record_ftrl_update(
            feature, "k", weight_before=0.0, weight_after=float(i) / 100.0,
        )
    log = jsonl_path(feature)
    pre_lines = log.read_text(encoding="utf-8").splitlines()
    assert len(pre_lines) == 20
    compact_ftrl_state(feature, keep_last_n=5)
    post_lines = log.read_text(encoding="utf-8").splitlines()
    assert len(post_lines) == 5
    # Snapshot still reflects the latest weight regardless of trim.
    loaded = load_ftrl_state(feature)
    assert loaded["k"] == pytest.approx(0.19)


def test_compact_idempotent_on_empty_state(ziq_state: Path) -> None:
    """Compacting a never-touched feature is a no-op + returns ``{}``."""
    assert compact_ftrl_state("never_existed") == {}
    assert not snapshot_path("never_existed").exists()
    assert not jsonl_path("never_existed").exists()


# ── atomic write ─────────────────────────────────────────────────────


def test_compact_partial_failure_keeps_snapshot_valid(
    ziq_state: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inject a serialisation failure during compact; snapshot must
    not be replaced with a partial / corrupt file."""
    feature = "atomic"
    record_ftrl_update(
        feature, "k", weight_before=1.0, weight_after=0.5,
    )
    # First compact succeeds and writes a known-good snapshot.
    compact_ftrl_state(feature)
    snap = snapshot_path(feature)
    good_doc = json.loads(snap.read_text(encoding="utf-8"))

    # Now monkeypatch ``write_text`` on Path to raise OSError on the
    # specific tmp file used by compact (any subsequent call). The
    # implementation catches OSError and returns the loaded weights
    # without touching the on-disk snapshot.
    real_write_text = Path.write_text

    def boom(self: Path, *args: object, **kwargs: object) -> int:  # type: ignore[no-untyped-def]
        if self.name.endswith(".tmp"):
            raise OSError("simulated mid-write crash")
        return real_write_text(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "write_text", boom)

    # New update + compact attempt.
    record_ftrl_update(
        feature, "k", weight_before=0.5, weight_after=0.99,
    )
    # Should NOT raise — best-effort persistence.
    compact_ftrl_state(feature)

    # Snapshot file is the one we wrote successfully earlier; the
    # failed write left no partial replacement.
    monkeypatch.setattr(Path, "write_text", real_write_text)
    on_disk = json.loads(snap.read_text(encoding="utf-8"))
    assert on_disk == good_doc, (
        "snapshot was replaced despite OSError — atomic guarantee broken"
    )


def test_record_failure_is_silent(
    ziq_state: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disk write failure inside ``record_ftrl_update`` must not propagate."""
    real_open = Path.open

    def boom(self: Path, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        if str(self).endswith(".jsonl"):
            raise OSError("disk gone")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", boom)

    # Should not raise.
    record_ftrl_update(
        "silent_fail", "k", weight_before=1.0, weight_after=0.5,
    )


# ── empty inputs ─────────────────────────────────────────────────────


def test_record_empty_feature_or_key_is_noop(ziq_state: Path) -> None:
    record_ftrl_update("", "k", weight_before=1.0, weight_after=0.5)
    record_ftrl_update("f", "", weight_before=1.0, weight_after=0.5)
    # Nothing was written.
    assert not jsonl_path("").exists() or load_ftrl_state("") == {}
    assert load_ftrl_state("f") == {}


def test_load_empty_feature_returns_empty(ziq_state: Path) -> None:
    assert load_ftrl_state("") == {}


# ── module __all__ ───────────────────────────────────────────────────


def test_module_exports() -> None:
    """The public surface stays explicit — protects refactors."""
    expected = {
        "compact_ftrl_state",
        "is_disabled",
        "jsonl_path",
        "load_ftrl_state",
        "record_ftrl_update",
        "snapshot_path",
        "state_dir",
    }
    assert set(ziq_persist.__all__) == expected


# ── SkillDisclosure integration ──────────────────────────────────────


def test_skill_disclosure_wires_persistence(
    ziq_state: Path,
    tmp_path: Path,
) -> None:
    """End-to-end: SkillDisclosure.observe_use writes through the layer."""
    from concinno.skills.disclosure import SkillDisclosure

    skills_root = tmp_path / "skills"
    skill_dir = skills_root / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo\ndescription: Demo skill\n---\n# demo\n",
        encoding="utf-8",
    )

    sd = SkillDisclosure(skills_dir=skills_root, ftrl_alpha=0.5)
    for _ in range(10):
        sd.observe_use("demo", helpful=False)
    weight_in_memory = sd._ftrl_weight("demo")
    assert weight_in_memory < 0.5

    loaded = load_ftrl_state("skill_disclosure")
    assert "demo" in loaded
    assert loaded["demo"] == pytest.approx(weight_in_memory, abs=1e-9)


def test_skill_disclosure_cold_load_from_disk(
    ziq_state: Path,
    tmp_path: Path,
) -> None:
    """Restart simulation: write weights, build a fresh SkillDisclosure,
    weight is preserved."""
    from concinno.skills.disclosure import SkillDisclosure

    skills_root = tmp_path / "skills"
    skill_dir = skills_root / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo\ndescription: Demo skill\n---\n# demo\n",
        encoding="utf-8",
    )

    record_ftrl_update(
        "skill_disclosure",
        "demo",
        weight_before=1.0,
        weight_after=0.42,
    )
    sd = SkillDisclosure(skills_dir=skills_root)
    assert sd._ftrl_weight("demo") == pytest.approx(0.42)


def test_skill_disclosure_persist_false_disables_disk(
    ziq_state: Path,
    tmp_path: Path,
) -> None:
    """``persist=False`` opts out: no disk read, no disk write."""
    from concinno.skills.disclosure import SkillDisclosure

    skills_root = tmp_path / "skills"
    skill_dir = skills_root / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo\ndescription: Demo skill\n---\n",
        encoding="utf-8",
    )
    # Pre-seed disk so a load_ftrl_state call would surface 0.99.
    record_ftrl_update(
        "skill_disclosure",
        "demo",
        weight_before=1.0,
        weight_after=0.99,
    )
    sd = SkillDisclosure(skills_dir=skills_root, persist=False)
    # Cold load skipped — observe in-memory default (1.0).
    assert sd._ftrl_weight("demo") == pytest.approx(1.0)
    sd.observe_use("demo", helpful=False)
    # Disk untouched by the persist=False instance — the seeded
    # 0.99 value is still the latest on disk.
    on_disk = load_ftrl_state("skill_disclosure")
    assert on_disk["demo"] == pytest.approx(0.99)
