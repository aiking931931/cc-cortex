"""Tests for concinno.security.policy_gate — PolicyGate base class (4.3.0).

The L9 PolicyEngine layer of the same module is covered separately in
``tests/test_policy_gate.py``. This file scopes only to the new shared
base class for security guards (Plan B Step 2).
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

import pytest

from concinno.security import (
    Finding,
    PolicyGate,
    PolicyGateResult,
)
from concinno.security import policy_gate as pg_mod

# ── Shared helpers ─────────────────────────────────────────────


@pytest.fixture
def audit_tmp(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Path:
    """Redirect audit writes to a per-test tmp dir."""
    monkeypatch.setenv("CONCINNO_AUDIT_DIR", str(tmp_path))
    # Hard-disable the ZIQ bus so we don't trip cross-test singletons.
    monkeypatch.setenv("CONCINNO_ZIQ_BUS_DISABLED", "1")
    return tmp_path


class _BadWordGuard(PolicyGate):
    """Tiny concrete guard used by most tests — flags ``BAD``."""

    name = "test_bad_word_guard"

    def scan(self, payload: str | bytes | dict[str, Any]) -> list[Finding]:
        text = self._payload_to_text(payload)
        if "BAD" in text:
            return [
                Finding(
                    type="bad_word",
                    span=(text.find("BAD"), text.find("BAD") + 3),
                    snippet="BAD",
                    severity="high",
                )
            ]
        return []


class _MultiFindingGuard(PolicyGate):
    """Returns one Finding per ``BAD`` occurrence."""

    name = "test_multi_finding_guard"

    def scan(self, payload: str | bytes | dict[str, Any]) -> list[Finding]:
        text = self._payload_to_text(payload)
        out: list[Finding] = []
        i = 0
        while True:
            idx = text.find("BAD", i)
            if idx < 0:
                break
            out.append(
                Finding(
                    type="bad",
                    span=(idx, idx + 3),
                    snippet="BAD",
                    severity="medium",
                )
            )
            i = idx + 3
        return out


# ── 1. Abstract NotImplementedError ────────────────────────────


def test_base_scan_raises_not_implemented(audit_tmp: Path) -> None:
    """A direct PolicyGate subclass that forgets to implement ``scan``
    must raise on the first ``evaluate`` call — not silently accept."""

    class _Empty(PolicyGate):
        name = "empty_guard"

    with pytest.raises(NotImplementedError):
        _Empty().evaluate("anything")


# ── 2. Four fail-mode decisions ────────────────────────────────


@pytest.mark.parametrize(
    "fail_mode,expected_decision",
    [
        ("silent", "accept"),
        ("warn", "warn"),
        ("warn+log", "warn"),
        ("hard_deny", "deny"),
    ],
)
def test_each_fail_mode_maps_to_correct_decision(
    audit_tmp: Path,
    capsys: pytest.CaptureFixture[str],
    fail_mode: str,
    expected_decision: str,
) -> None:
    g = _BadWordGuard(fail_mode_override=fail_mode)  # type: ignore[arg-type]
    r = g.evaluate("contains BAD inside")
    assert r.decision == expected_decision
    assert r.fail_mode == fail_mode

    # Stderr side-effect: only `warn` decisions should print to stderr.
    err = capsys.readouterr().err
    if expected_decision == "warn":
        assert g.name in err
    else:
        assert g.name not in err


# ── 3. Escape hatch ────────────────────────────────────────────


def test_escape_pattern_short_circuits_to_accept(audit_tmp: Path) -> None:
    g = _BadWordGuard(fail_mode_override="hard_deny")
    payload = "# CONCINNO_DISABLE: legacy_test_data\nBAD"
    r = g.evaluate(payload)
    assert r.decision == "accept"
    assert r.escaped is True
    assert r.findings == ()
    assert "escape pattern" in r.reason


def test_escape_pattern_custom_token(audit_tmp: Path) -> None:
    g = _BadWordGuard(fail_mode_override="hard_deny")
    r = g.evaluate("XX_DISABLE:foo BAD", escape_pattern="XX_DISABLE:")
    assert r.escaped is True
    assert r.decision == "accept"


# ── 4. Constructor override beats profile chain ────────────────


def test_constructor_override_beats_profile_default(
    audit_tmp: Path,
) -> None:
    # The "lite" profile defaults to silent for unregistered features;
    # constructor override must be the last word.
    g = _BadWordGuard(profile="lite", fail_mode_override="hard_deny")
    r = g.evaluate("BAD")
    assert r.fail_mode == "hard_deny"
    assert r.decision == "deny"


# ── 5. Profile resolution falls through to feature_config ──────


def test_profile_resolves_via_feature_config(audit_tmp: Path) -> None:
    """When no override is supplied, the gate consults
    :func:`feature_config.get_fail_mode` for the active profile.
    For an unregistered feature on the ``paranoid`` profile the
    catch-all is ``hard_deny``."""

    g = _BadWordGuard(profile="paranoid")
    r = g.evaluate("BAD")
    # paranoid catch-all is hard_deny per feature_config FEATURE_TOGGLE_PROFILES
    assert r.fail_mode == "hard_deny"
    assert r.decision == "deny"


def test_profile_lookup_unknown_profile_falls_back_to_warn(
    audit_tmp: Path,
) -> None:
    """A misspelled profile must not crash — the resolver swallows
    and returns ``warn`` (visible, non-blocking)."""

    g = _BadWordGuard(profile="nonexistent_xyz")
    r = g.evaluate("BAD")
    assert r.fail_mode == "warn"
    assert r.decision == "warn"


# ── 6. Audit log write — JSON line + mkdir ─────────────────────


def test_audit_log_writes_json_line(audit_tmp: Path) -> None:
    g = _BadWordGuard(fail_mode_override="warn+log")
    g.evaluate("hello BAD world")

    log_path = audit_tmp / "test_bad_word_guard.jsonl"
    assert log_path.exists()
    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["guard"] == "test_bad_word_guard"
    assert record["decision"] == "warn"
    assert record["fail_mode"] == "warn+log"
    assert len(record["findings"]) == 1
    assert record["findings"][0]["type"] == "bad_word"


def test_audit_skipped_for_silent_and_warn(audit_tmp: Path) -> None:
    """Only ``warn+log`` and ``hard_deny`` should produce a file."""

    _BadWordGuard(fail_mode_override="silent").evaluate("BAD")
    _BadWordGuard(fail_mode_override="warn").evaluate("BAD")
    log_path = audit_tmp / "test_bad_word_guard.jsonl"
    assert not log_path.exists()


def test_audit_dir_lazy_mkdir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The audit dir must be created on demand, not at import time."""

    target = tmp_path / "deep" / "nested" / "audit"
    monkeypatch.setenv("CONCINNO_AUDIT_DIR", str(target))
    monkeypatch.setenv("CONCINNO_ZIQ_BUS_DISABLED", "1")
    assert not target.exists()
    _BadWordGuard(fail_mode_override="hard_deny").evaluate("BAD")
    assert target.exists()
    assert (target / "test_bad_word_guard.jsonl").exists()


# ── 7. Audit log rotation @ 10 MB ──────────────────────────────


def test_audit_log_rotates_past_threshold(
    monkeypatch: pytest.MonkeyPatch, audit_tmp: Path
) -> None:
    """A pre-existing log over the size threshold must be rotated to
    ``<name>.1`` on the next append, leaving a fresh main file."""

    log_path = audit_tmp / "test_bad_word_guard.jsonl"
    # Force a tiny threshold so the test stays fast.
    monkeypatch.setattr(pg_mod, "_AUDIT_ROTATE_MAX_BYTES", 100)
    log_path.write_text("x" * 200, encoding="utf-8")

    _BadWordGuard(fail_mode_override="warn+log").evaluate("BAD")

    archive = audit_tmp / "test_bad_word_guard.jsonl.1"
    assert archive.exists()
    assert archive.read_text(encoding="utf-8") == "x" * 200
    # Main file now contains only the fresh entry.
    fresh = log_path.read_text(encoding="utf-8")
    assert "bad_word" in fresh


# ── 8. ZIQ bus graceful degrade ────────────────────────────────


def test_ziq_bus_disabled_does_not_break_evaluate(
    monkeypatch: pytest.MonkeyPatch, audit_tmp: Path
) -> None:
    """Already covered indirectly by the ``audit_tmp`` fixture, but we
    add an explicit assertion: the call returns normally even with the
    bus killed."""

    monkeypatch.setenv("CONCINNO_ZIQ_BUS_DISABLED", "1")
    r = _BadWordGuard(fail_mode_override="warn").evaluate("BAD")
    assert isinstance(r, PolicyGateResult)


def test_ziq_bus_emit_exception_swallowed(
    monkeypatch: pytest.MonkeyPatch, audit_tmp: Path
) -> None:
    """A bus that raises on emit must not break the guard chain."""

    # Re-enable bus, then break it.
    monkeypatch.delenv("CONCINNO_ZIQ_BUS_DISABLED", raising=False)

    from concinno import ziq_outcome_bus as bus_mod

    class _BrokenBus:
        def emit(self, _outcome: object) -> None:
            raise RuntimeError("boom")

    monkeypatch.setattr(bus_mod, "get_bus", lambda: _BrokenBus())

    r = _BadWordGuard(fail_mode_override="warn").evaluate("BAD")
    assert r.decision == "warn"  # call survived


# ── 9. Smoke: a minimal MockGuard subclass ─────────────────────


def test_mock_guard_smoke(audit_tmp: Path) -> None:
    g = _BadWordGuard()
    r = g.evaluate("everything is fine")
    assert r.decision == "accept"
    assert r.findings == ()


# ── 10. Multi-finding accumulation ─────────────────────────────


def test_multiple_findings_accumulate(audit_tmp: Path) -> None:
    g = _MultiFindingGuard(fail_mode_override="warn+log")
    r = g.evaluate("BAD then BAD again, plus BAD")
    assert len(r.findings) == 3
    log_path = audit_tmp / "test_multi_finding_guard.jsonl"
    record = json.loads(log_path.read_text(encoding="utf-8").strip())
    assert len(record["findings"]) == 3
    # Spans are distinct + ordered.
    spans = [tuple(f["span"]) for f in record["findings"]]
    assert spans == sorted(spans)
    assert len(set(spans)) == 3


# ── 11-15. Edge cases ──────────────────────────────────────────


def test_empty_payload(audit_tmp: Path) -> None:
    r = _BadWordGuard().evaluate("")
    assert r.decision == "accept"
    assert r.findings == ()


def test_unicode_payload(audit_tmp: Path) -> None:
    """Non-ASCII payloads must not crash the scan/audit pipeline.

    Audit logs deliberately do NOT capture the raw payload (privacy +
    log-bloat) \u2014 the snippet captured is the redacted match, not the
    surrounding payload. We verify the call survives unicode end-to-end
    and that ASCII metadata lands in the audit line as expected.

    Uses ``\\u`` escape sequences so the test source is pure ASCII and
    pytest cannot misread the literals under a Windows MBCS default
    file encoding.
    """

    greet = "\u3053\u3093\u306b\u3061\u306f"  # konnichiwa
    world = "\u4e16\u754c"  # sekai
    g = _BadWordGuard(fail_mode_override="warn+log")
    r = g.evaluate(f"{greet} BAD {world}")
    assert r.decision == "warn"
    assert len(r.findings) == 1
    log_path = audit_tmp / "test_bad_word_guard.jsonl"
    record = json.loads(log_path.read_text(encoding="utf-8").strip())
    # Payload is intentionally not in the audit; only the redacted
    # match snippet is.
    assert record["guard"] == "test_bad_word_guard"
    assert record["findings"][0]["type"] == "bad_word"
    assert record["findings"][0]["snippet"] == "BAD"


def test_bytes_payload(audit_tmp: Path) -> None:
    r = _BadWordGuard(fail_mode_override="hard_deny").evaluate(b"BAD bytes")
    assert r.decision == "deny"
    assert len(r.findings) == 1


def test_dict_payload_serialises(audit_tmp: Path) -> None:
    payload = {"cmd": "echo BAD", "nested": {"k": "v"}}
    r = _BadWordGuard(fail_mode_override="hard_deny").evaluate(payload)
    # 'BAD' lives inside one of the values; the base coerces dict →
    # JSON before the escape-pattern scan, and the subclass's scan
    # uses the same coercion via _payload_to_text.
    assert r.decision == "deny"


def test_large_payload_does_not_blow_audit_snippet(audit_tmp: Path) -> None:
    """Findings that include a multi-MB snippet must be truncated."""

    class _BigSnippet(PolicyGate):
        name = "test_big_snippet_guard"

        def scan(self, payload: object) -> list[Finding]:
            return [
                Finding(
                    type="big",
                    span=(0, 1),
                    snippet="A" * 10_000,  # deliberately oversized
                    severity="low",
                )
            ]

    g = _BigSnippet(fail_mode_override="warn+log")
    g.evaluate("trigger")
    record = json.loads(
        (audit_tmp / "test_big_snippet_guard.jsonl")
        .read_text(encoding="utf-8")
        .strip()
    )
    snippet = record["findings"][0]["snippet"]
    assert len(snippet) <= 80
    assert snippet.endswith("...")


def test_concurrent_evaluate_thread_safe(audit_tmp: Path) -> None:
    """Append-only writes must not corrupt audit lines under load.

    We don't take an explicit lock — we rely on the single-syscall
    write of one JSON line per ``evaluate`` and the OS's append-mode
    atomicity. This test sanity-checks both: every line is parseable
    JSON and the count matches the number of calls."""

    g = _BadWordGuard(fail_mode_override="warn+log")
    n_threads = 8
    per_thread = 25

    def worker() -> None:
        for _ in range(per_thread):
            g.evaluate("concurrent BAD payload")

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    log_path = audit_tmp / "test_bad_word_guard.jsonl"
    lines = [
        line for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(lines) == n_threads * per_thread
    # Every line must parse — no torn writes.
    for line in lines:
        record = json.loads(line)
        assert record["guard"] == g.name


def test_subclass_without_name_raises(audit_tmp: Path) -> None:
    """Forgetting to override ``name`` must fail loud at construction."""

    class _Nameless(PolicyGate):
        # name = ""  intentionally inherited blank
        def scan(self, payload: object) -> list[Finding]:
            return []

    with pytest.raises(TypeError, match="name is empty"):
        _Nameless()


def test_finding_validates_severity(audit_tmp: Path) -> None:
    with pytest.raises(ValueError, match="severity"):
        Finding(
            type="x",
            span=(0, 1),
            snippet="x",
            severity="bogus",  # type: ignore[arg-type]
        )


def test_finding_validates_span_shape(audit_tmp: Path) -> None:
    with pytest.raises(TypeError):
        Finding(
            type="x",
            span=(0, 1, 2),  # type: ignore[arg-type]
            snippet="x",
            severity="low",
        )


def test_invalid_fail_mode_override_raises(audit_tmp: Path) -> None:
    with pytest.raises(ValueError, match="fail_mode_override"):
        _BadWordGuard(fail_mode_override="bogus")  # type: ignore[arg-type]


def test_audit_entry_attached_even_when_silent(audit_tmp: Path) -> None:
    """The ``audit_entry`` field on the result is populated regardless
    of whether the file write actually happens — useful for callers
    that ship audit through their own sink."""

    r = _BadWordGuard(fail_mode_override="silent").evaluate("BAD")
    assert r.decision == "accept"
    assert r.audit_entry["guard"] == "test_bad_word_guard"
    assert r.audit_entry["decision"] == "accept"
    # File must NOT exist since fail_mode is silent.
    assert not (audit_tmp / "test_bad_word_guard.jsonl").exists()


def test_audit_dir_env_override_isolates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``CONCINNO_AUDIT_DIR`` must redirect writes — proves tests stay
    hermetic without monkeypatching ``Path.home``."""

    a = tmp_path / "a"
    b = tmp_path / "b"
    monkeypatch.setenv("CONCINNO_ZIQ_BUS_DISABLED", "1")

    monkeypatch.setenv("CONCINNO_AUDIT_DIR", str(a))
    _BadWordGuard(fail_mode_override="warn+log").evaluate("BAD")

    monkeypatch.setenv("CONCINNO_AUDIT_DIR", str(b))
    _BadWordGuard(fail_mode_override="warn+log").evaluate("BAD")

    assert (a / "test_bad_word_guard.jsonl").exists()
    assert (b / "test_bad_word_guard.jsonl").exists()
    # Independent files — count is 1 each, not 2 in either.
    for d in (a, b):
        lines = (d / "test_bad_word_guard.jsonl").read_text("utf-8").splitlines()
        assert len(lines) == 1


def test_ziq_emit_value_set_even_for_clean(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When the bus is enabled, every evaluate (clean or not) emits
    one Outcome — the reward signal differs but the source/tunable
    must be stable."""

    monkeypatch.setenv("CONCINNO_AUDIT_DIR", str(tmp_path))
    monkeypatch.delenv("CONCINNO_ZIQ_BUS_DISABLED", raising=False)

    from concinno import ziq_outcome_bus as bus_mod

    captured: list[Any] = []

    class _CaptureBus:
        def emit(self, outcome: Any) -> None:
            captured.append(outcome)

    monkeypatch.setattr(bus_mod, "get_bus", lambda: _CaptureBus())

    _BadWordGuard(fail_mode_override="hard_deny").evaluate("BAD")
    _BadWordGuard(fail_mode_override="silent").evaluate("clean")

    assert len(captured) == 2
    tunables = {o.tunable for o in captured}
    assert tunables == {"security.test_bad_word_guard"}
    rewards = sorted(o.reward for o in captured)
    # deny → 0.0, accept (silent) → 1.0
    assert rewards == [0.0, 1.0]


def test_payload_to_text_roundtrips_unparseable_dict() -> None:
    """A dict containing a non-serialisable object must still produce
    a string — repr() fallback path."""

    class _Weird:
        def __repr__(self) -> str:
            return "<weird>"

    payload = {"k": _Weird()}
    text = PolicyGate._payload_to_text(payload)
    assert isinstance(text, str)
    # default=str makes the object printable; either "weird" appears
    # in the JSON or in the repr fallback — both acceptable.
    assert text  # non-empty


# ── Sanity: the file-system override env var really is local ───


def test_audit_env_var_default_is_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CONCINNO_AUDIT_DIR", raising=False)
    expected = Path.home() / ".concinno" / "audit"
    assert pg_mod._audit_dir() == expected


# Convenience marker so the file is collected last in alphabetical
# ordering with the L9 PolicyEngine tests — keeps the failure list
# tidy when both fail.
os.environ.setdefault("CONCINNO_POLICY_GATE_TESTS_LOADED", "1")
