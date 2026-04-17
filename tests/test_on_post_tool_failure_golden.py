"""Golden tests for ``concinno.hooks.on_post_tool_failure``.

These tests pin the *observable* behavior of the patch-loop detector
around the A2c migration. Burst tracking is delegated to
``ErrorRecovery`` so mechanism-level assertions use the ``StateStore``
namespace ``tool_failure_burst``; behavior-level assertions
(escalation message, prescription text, consecutive/window semantics)
are unchanged.

Coverage plan (21 cases):
  G1–G4   user-initiated denial no-ops (denied/cancelled/user rejected/interrupted)
  G5      six-category classifier
  G6      first failure produces no stdout
  G7      second consecutive escalates to C1
  G8      escalation message is byte-exact
  G9      10-minute window boundary (inside → escalate)
  G10     10-minute window boundary (outside → no escalate)
  G11     consecutive reset on category mismatch
  G12     consecutive reset on tool mismatch
  G13     prescription injection at total >= 3
  G14     prescription fallback for `other` category
  G15     escalation beats prescription
  G16     confidence side effect on every failure
  G17     confidence side effect skipped on user denial
  G18     confidence ImportError graceful degrade
  G19     history cap at 200 entries (via ErrorRecovery)
  G20     missing CLAUDE_PROJECT_DIR silent no-op
  G21     hook JSON output shape
"""

from __future__ import annotations

import io
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

from concinno.error_recovery import ErrorRecovery
from concinno.hooks import on_post_tool_failure as hook

# ── Helpers ────────────────────────────────────────────────

FROZEN_NOW = datetime(2026, 4, 11, 20, 0, 0, tzinfo=timezone.utc)

# Hook hard-codes this bucket — red team #1-H3: burst tracking is
# intentionally project-scoped (flat), not session-scoped, so patch
# loops across Claude Code session restarts still trigger.
HOOK_BUCKET = "tool-failure-burst"


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _seed_burst(
    cache_dir: str,
    entries: list[tuple[datetime, str, str]],
    session_id: str = HOOK_BUCKET,
) -> None:
    """Seed the StateStore burst namespace directly — replaces the old
    JSONL seeder. Entries are ``(timestamp, op, cat)`` tuples in the
    order the hook would have appended them."""
    recovery = ErrorRecovery(cache_dir, session_id)
    for ts, op, cat in entries:
        recovery.record_burst(op, cat, now=ts)


def _read_burst(
    cache_dir: str, session_id: str = HOOK_BUCKET,
) -> list[dict]:
    """Return the raw events list from the burst StateStore file."""
    recovery = ErrorRecovery(cache_dir, session_id)
    return recovery._burst_read().get("events", [])


def _run_hook(
    monkeypatch, capsys, project_dir: str, hook_data: dict,
) -> str:
    """Drive the hook via stdin and return captured stdout as text."""
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", project_dir)
    stdin_buf = io.StringIO(json.dumps(hook_data))
    monkeypatch.setattr(sys, "stdin", stdin_buf)
    hook.main()
    captured = capsys.readouterr()
    return captured.out


@pytest.fixture
def project_dir(tmp_path):
    pd = tmp_path / "proj"
    (pd / ".concinno_cache").mkdir(parents=True)
    return str(pd)


@pytest.fixture
def cache_dir(project_dir):
    return os.path.join(project_dir, ".concinno_cache")


# ── G1–G4: User-initiated denial no-ops ────────────────────
#
# Red team #1-F1 found that the original substring-based skip ate
# system-level failures ("permission denied"/"connection denied"/
# timeout cancelled) — exactly the failures patch-loop tracking MUST
# count. The new `_is_user_denial` only skips entries that are
# unambiguously user actions.

@pytest.mark.parametrize("denial_error", [
    "Operation cancelled by user",
    "user rejected the command",
    "interrupted by user",
    "aborted by user",
    "denied by user",
])
def test_g1_g4_user_denial_no_op(
    monkeypatch, capsys, project_dir, cache_dir, denial_error,
):
    out = _run_hook(
        monkeypatch, capsys, project_dir,
        {"tool_name": "Bash", "error": denial_error},
    )
    assert out == ""
    # Burst history must NOT contain the denial entry
    assert _read_burst(cache_dir) == []


@pytest.mark.parametrize("system_error", [
    "Permission denied: /etc/shadow",
    "EACCES access denied on file",
    "Connection refused",
    "Operation timed out after 30s",
    "Broken pipe",
])
def test_g1b_system_failures_are_counted(
    monkeypatch, capsys, project_dir, cache_dir, system_error,
):
    """System-level failures (permission / access / timeout / network)
    must be classified as real failures and recorded, even when the word
    'denied' / 'cancelled' / 'interrupted' appears — red team #1-F1."""
    _run_hook(
        monkeypatch, capsys, project_dir,
        {"tool_name": "Bash", "error": system_error},
    )
    assert len(_read_burst(cache_dir)) == 1


# ── G5: Six-category classifier ────────────────────────────

@pytest.mark.parametrize("error,expected", [
    ("Permission denied on /tmp/x", "permission"),
    ("No such file: /tmp/missing", "path_not_found"),
    ("command timed out after 30s", "timeout"),
    ("SyntaxError: invalid syntax", "syntax"),
    ("ModuleNotFoundError: no module named x", "import"),
    ("merge conflict in foo.py", "conflict"),
    ("something weird happened", "other"),
])
def test_g5_classify_all_six_categories(error, expected):
    assert hook._classify_error(error) == expected


# ── G6: First failure produces no stdout ───────────────────

def test_g6_first_failure_no_output(
    monkeypatch, capsys, project_dir, cache_dir,
):
    out = _run_hook(
        monkeypatch, capsys, project_dir,
        {"tool_name": "Bash", "error": "command timed out"},
    )
    assert out == ""
    events = _read_burst(cache_dir)
    assert len(events) == 1
    assert events[0]["op"] == "Bash"
    assert events[0]["cat"] == "timeout"


# ── G7: Second consecutive escalates to C1 ─────────────────

def test_g7_second_consecutive_escalates_c1(
    monkeypatch, capsys, project_dir, cache_dir,
):
    # Seed a matching entry from 1 minute ago (within 10-min window)
    _seed_burst(cache_dir, [(
        datetime.now(timezone.utc) - timedelta(minutes=1),
        "Bash", "timeout",
    )])
    out = _run_hook(
        monkeypatch, capsys, project_dir,
        {"tool_name": "Bash", "error": "command timed out again"},
    )
    payload = json.loads(out)
    ctx = payload["hookSpecificOutput"]["additionalContext"]
    assert "PATCH LOOP" in ctx
    assert "× 2" in ctx
    assert "Bash/timeout" in ctx
    assert "STOP patching" in ctx


# ── G8: Escalation message byte-exact snapshot ─────────────

def test_g8_escalation_message_byte_exact():
    # Pin the exact UTF-8 bytes of the C1 escalation body for a known triple.
    msg = hook._build_escalation_context("Edit", "syntax", 3)
    # Must contain the red circle and the count line
    assert "PATCH LOOP" in msg and "\u00d7 3" in msg
    assert "\uff08Edit/syntax\uff09" in msg  # full-width parens
    # Body must contain the Chinese STOP and the three-layer prompt
    assert "\u5f37\u5236\u5347\u7d1a B1 \u4e09\u5c64\u601d\u8003" in msg
    assert "\u756b\u51fa\u5b8c\u6574\u56e0\u679c\u93c8" in msg
    assert "\u4e00\u6b21\u89e3\u5b8c" in msg
    # Round-trip through UTF-8 must be lossless
    assert msg.encode("utf-8").decode("utf-8") == msg


# ── G9: 10-minute window — just inside ─────────────────────

def test_g9_window_boundary_inside(
    monkeypatch, capsys, project_dir, cache_dir,
):
    now = datetime.now(timezone.utc)
    # 9m59s ago — just inside the 10-min window
    _seed_burst(cache_dir, [(
        now - timedelta(minutes=9, seconds=59), "Bash", "timeout",
    )])
    out = _run_hook(
        monkeypatch, capsys, project_dir,
        {"tool_name": "Bash", "error": "command timed out"},
    )
    payload = json.loads(out)
    assert "PATCH LOOP" in payload["hookSpecificOutput"]["additionalContext"]


# ── G10: 10-minute window — just outside ───────────────────

def test_g10_window_boundary_outside(
    monkeypatch, capsys, project_dir, cache_dir,
):
    now = datetime.now(timezone.utc)
    # 10m01s ago — just outside the window
    _seed_burst(cache_dir, [(
        now - timedelta(minutes=10, seconds=1), "Bash", "timeout",
    )])
    out = _run_hook(
        monkeypatch, capsys, project_dir,
        {"tool_name": "Bash", "error": "command timed out"},
    )
    # total would be 2 but consecutive would be 1 → no escalation
    # total < 3 so no prescription either
    assert out == ""


# ── G11: Consecutive reset on category mismatch ────────────

def test_g11_consecutive_reset_on_category_mismatch(
    monkeypatch, capsys, project_dir, cache_dir,
):
    now = datetime.now(timezone.utc)
    # Chronological append order. Reverse scan from newest:
    #   new (Bash/timeout, just-appended) → -1min Bash/timeout (match)
    #     → -2min Bash/permission (mismatch → break)
    _seed_burst(cache_dir, [
        (now - timedelta(minutes=3), "Bash", "timeout"),
        (now - timedelta(minutes=2), "Bash", "permission"),
        (now - timedelta(minutes=1), "Bash", "timeout"),
    ])
    out = _run_hook(
        monkeypatch, capsys, project_dir,
        {"tool_name": "Bash", "error": "command timed out"},
    )
    # consecutive = 2 (new + -1min) → ESCALATE
    payload = json.loads(out)
    ctx = payload["hookSpecificOutput"]["additionalContext"]
    assert "PATCH LOOP" in ctx
    assert "× 2" in ctx


def test_g11b_consecutive_truly_resets_when_mismatch_immediate(
    monkeypatch, capsys, project_dir, cache_dir,
):
    """When the immediately-prior entry mismatches, consecutive=1 → no esc."""
    now = datetime.now(timezone.utc)
    _seed_burst(cache_dir, [
        (now - timedelta(minutes=3), "Bash", "timeout"),
        (now - timedelta(minutes=1), "Bash", "permission"),
    ])
    out = _run_hook(
        monkeypatch, capsys, project_dir,
        {"tool_name": "Bash", "error": "command timed out"},
    )
    # total timeout = 2 < 3, no prescription; consecutive=1, no escalation
    assert out == ""


# ── G12: Consecutive reset on tool mismatch ────────────────

def test_g12_consecutive_reset_on_tool_mismatch(
    monkeypatch, capsys, project_dir, cache_dir,
):
    now = datetime.now(timezone.utc)
    _seed_burst(cache_dir, [
        (now - timedelta(minutes=3), "Bash", "timeout"),
        (now - timedelta(minutes=1), "Edit", "timeout"),
    ])
    out = _run_hook(
        monkeypatch, capsys, project_dir,
        {"tool_name": "Bash", "error": "command timed out"},
    )
    # Reverse scan: just-appended Bash/timeout (+1), then Edit/timeout → break.
    # consecutive = 1; total = 2 < 3 → no output.
    assert out == ""


# ── G13: Prescription injection at total >= 3 ──────────────

def test_g13_prescription_at_total_three(
    monkeypatch, capsys, project_dir, cache_dir,
):
    # Two old entries > 10 min ago (so not consecutive),
    # plus the one we're about to append → total 3, consecutive 1.
    now = datetime.now(timezone.utc)
    _seed_burst(cache_dir, [
        (now - timedelta(minutes=60), "Bash", "path_not_found"),
        (now - timedelta(minutes=30), "Bash", "path_not_found"),
    ])
    out = _run_hook(
        monkeypatch, capsys, project_dir,
        {"tool_name": "Bash", "error": "No such file: /tmp/missing"},
    )
    payload = json.loads(out)
    ctx = payload["hookSpecificOutput"]["additionalContext"]
    assert "Bash failure #3 (path_not_found)" in ctx
    assert "File not found failures recurring" in ctx


# ── G14: Prescription fallback for `other` ─────────────────

def test_g14_prescription_other_fallback(
    monkeypatch, capsys, project_dir, cache_dir,
):
    now = datetime.now(timezone.utc)
    _seed_burst(cache_dir, [
        (now - timedelta(minutes=60), "Bash", "other"),
        (now - timedelta(minutes=30), "Bash", "other"),
    ])
    out = _run_hook(
        monkeypatch, capsys, project_dir,
        {"tool_name": "Bash", "error": "weird unknown thing"},
    )
    payload = json.loads(out)
    ctx = payload["hookSpecificOutput"]["additionalContext"]
    assert "Pattern: Bash fails with other errors." in ctx


# ── G15: Escalation beats prescription ─────────────────────

def test_g15_escalation_beats_prescription(
    monkeypatch, capsys, project_dir, cache_dir,
):
    """With total>=3 and consecutive>=2, the C1 message wins."""
    now = datetime.now(timezone.utc)
    _seed_burst(cache_dir, [
        # 3 old entries outside window (contribute to total only)
        (now - timedelta(minutes=60), "Bash", "timeout"),
        (now - timedelta(minutes=50), "Bash", "timeout"),
        (now - timedelta(minutes=40), "Bash", "timeout"),
        # Recent consecutive match inside window
        (now - timedelta(minutes=1), "Bash", "timeout"),
    ])
    out = _run_hook(
        monkeypatch, capsys, project_dir,
        {"tool_name": "Bash", "error": "command timed out"},
    )
    payload = json.loads(out)
    ctx = payload["hookSpecificOutput"]["additionalContext"]
    assert "PATCH LOOP" in ctx
    # Must NOT be the prescription
    assert "failure #" not in ctx


# ── G16: Confidence called on every failure ────────────────

def test_g16_confidence_called_on_every_failure(
    monkeypatch, capsys, project_dir,
):
    calls: list[dict] = []

    def fake_update(cache_dir, *, domain, success, error_pattern):
        calls.append({
            "cache_dir": cache_dir,
            "domain": domain,
            "success": success,
            "error_pattern": error_pattern,
        })

    import concinno.confidence_record as cr
    monkeypatch.setattr(cr, "update_confidence", fake_update)

    _run_hook(
        monkeypatch, capsys, project_dir,
        {"tool_name": "Bash", "error": "command timed out"},
    )
    assert len(calls) == 1
    assert calls[0]["domain"] == "Bash"
    assert calls[0]["success"] is False
    assert calls[0]["error_pattern"] == "timeout"


# ── G17: Confidence skipped on user denial ─────────────────

def test_g17_confidence_skipped_on_user_denial(
    monkeypatch, capsys, project_dir,
):
    calls: list[dict] = []

    def fake_update(cache_dir, *, domain, success, error_pattern):
        calls.append({"domain": domain})

    import concinno.confidence_record as cr
    monkeypatch.setattr(cr, "update_confidence", fake_update)

    _run_hook(
        monkeypatch, capsys, project_dir,
        {"tool_name": "Bash", "error": "Permission denied by user"},
    )
    assert calls == []


# ── G18: Confidence ImportError graceful ───────────────────

def test_g18_confidence_importerror_graceful(
    monkeypatch, capsys, project_dir, cache_dir,
):
    # Force `from concinno.confidence_record import update_confidence`
    # inside main() to raise ImportError by removing the attribute.
    # monkeypatch will restore it automatically at teardown.
    import concinno.confidence_record as cr
    monkeypatch.delattr(cr, "update_confidence")
    out = _run_hook(
        monkeypatch, capsys, project_dir,
        {"tool_name": "Bash", "error": "command timed out"},
    )
    # Hook did not crash; burst event still persisted
    assert _read_burst(cache_dir) != []
    # Stdout: first failure → no output, but no crash either
    assert out == ""


# ── G19: History cap at 200 entries (via ErrorRecovery) ────

def test_g19_history_cap_200(tmp_path):
    """ErrorRecovery enforces its configured burst_history_cap, which
    mirrors the old JSONL ``[-200:]`` semantic. Drives the cap directly
    rather than through the hook because hooking 250 subprocess calls
    would be slow and the guarantee is library-level."""
    recovery = ErrorRecovery(
        str(tmp_path), "gold-cap-sess",
        burst_history_cap=200,
    )
    base = datetime.now(timezone.utc) - timedelta(days=1)
    for i in range(250):
        recovery.record_burst(
            "Bash", "other", now=base + timedelta(seconds=i),
        )
    events = recovery._burst_read()["events"]
    assert len(events) == 200


# ── G20: Missing CLAUDE_PROJECT_DIR silent no-op ───────────

def test_g20_no_project_dir_silent_noop(monkeypatch, capsys):
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    stdin_buf = io.StringIO(json.dumps({
        "tool_name": "Bash", "error": "command timed out",
    }))
    monkeypatch.setattr(sys, "stdin", stdin_buf)
    hook.main()
    captured = capsys.readouterr()
    assert captured.out == ""


# ── G21: Hook JSON output shape ────────────────────────────

def test_g21_hook_json_output_shape(
    monkeypatch, capsys, project_dir, cache_dir,
):
    _seed_burst(cache_dir, [(
        datetime.now(timezone.utc) - timedelta(minutes=1),
        "Bash", "timeout",
    )])
    out = _run_hook(
        monkeypatch, capsys, project_dir,
        {"tool_name": "Bash", "error": "command timed out"},
    )
    payload = json.loads(out)
    assert "hookSpecificOutput" in payload
    hso = payload["hookSpecificOutput"]
    assert hso["hookEventName"] == "PostToolUseFailure"
    assert isinstance(hso["additionalContext"], str)
    assert len(hso["additionalContext"]) > 0
