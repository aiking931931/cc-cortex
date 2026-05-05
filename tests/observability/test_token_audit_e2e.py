"""End-to-end test for the Token Audit Autopilot pipeline (W3, 4.5.0).

Simulates the full session lifecycle: SessionStart hook constructs
the audit → guards/dispatchers record overhead during the session →
Stop hook flushes the jsonl + asserts the record matches expected
totals.

Single fixture, single end-to-end pass — keeps the integration
contract test cheap and stable. Per-bucket math lives in
``test_token_audit.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from concinno.observability.token_audit.audit import (
    TokenAudit,
    get_audit,
    reset_audit,
)


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    reset_audit()


def test_session_lifecycle_writes_jsonl(tmp_path: Path) -> None:
    """SessionStart → record_* → Stop produces a jsonl record on disk."""
    audit_dir = tmp_path / "audit-e2e"
    audit = TokenAudit(
        session_id="e2e-session-1",
        enabled=True,
        audit_dir=audit_dir,
    )

    # Hook: SessionStart begins the session.
    audit.begin_session()

    # During session: skill loads + sub-agent + mcp tools.
    audit.record_skill_load(
        "kb_handoff", l2_chars=4400, frontmatter_chars=400,
    )
    audit.record_skill_load(
        "kb_runpod", l2_chars=2200, frontmatter_chars=200,
    )
    audit.record_subagent(brief_chars=8000, return_chars=4000)
    audit.record_mcp_tool("mcp-a", "tool-1", schema_chars=1200)
    audit.record_skill_invocation("kb_handoff")  # actually used

    # Hook: Stop flushes the jsonl.
    summary = audit.end_session()

    # Verify: jsonl exists with one line, totals match.
    jsonl = audit_dir / f"token_audit_{audit.session_id}.jsonl"
    assert jsonl.exists(), "stop hook should have flushed jsonl"
    lines = jsonl.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])

    # Expected math (chars/4 = tokens):
    #   skills:   (4400+400 + 2200+200) = 7200 / 4 = 1800
    #   subagent: (8000 + 4000) = 12000 / 4 = 3000
    #   mcp:      1200 / 4 = 300
    #   floor:    14000 (default)
    #   total:    14000 + 1800 + 3000 + 300 = 19100
    assert payload["session_id"] == "e2e-session-1"
    assert payload["skills_loaded"] == 2
    assert payload["skills_total_overhead"] == 1800
    assert payload["subagents_spawned"] == 1
    assert payload["subagents_total_overhead"] == 3000
    assert payload["mcp_tools_loaded"] == 1
    assert payload["mcp_total_overhead"] == 300
    assert payload["system_prompt_floor"] == 14000
    assert payload["total_estimated_tokens"] == 19100
    assert summary.total_estimated_tokens == 19100


def test_singleton_lifecycle_via_hooks(tmp_path: Path) -> None:
    """Hook-style get_audit() round-trip: SessionStart → record → Stop."""
    # SessionStart — construct via singleton (mirrors hooks/on_session_start.py).
    audit = get_audit(session_id="e2e-singleton", enabled=True)
    audit.begin_session()
    audit.record_skill_load("foo", l2_chars=400)

    # Stop — fetch same singleton + end_session (mirrors on_stop.py).
    audit2 = get_audit()
    assert audit2 is audit
    summary = audit2.end_session()
    assert summary.skills_loaded == 1
    assert summary.session_id == "e2e-singleton"
