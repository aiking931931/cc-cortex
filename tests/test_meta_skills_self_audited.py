"""tests.test_meta_skills_self_audited — SelfAuditedWrapper unit tests.

Verifies:
  - Callable-style guards deny + allow paths
  - PermissionDenied carries (tool_name, guard_name, reason)
  - Decision journal writes JSONL under HOME/.concinno/decision_journal.jsonl
  - Error path records verdict="error" and re-raises
  - Decorator form wraps instances automatically
  - Known-guard strings that can't be resolved are silently skipped
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any

import pytest

from concinno.meta_skills.self_audited import (
    PermissionDenied,
    SelfAuditedWrapper,
    self_audited,
)

sa_mod = importlib.import_module("concinno.meta_skills.self_audited")


class _DummyTool:
    name = "dummy"
    description = "return kwargs unchanged"
    is_concurrency_safe = True

    def call(self, **kwargs: Any) -> dict:
        return {"echoed": kwargs}


class _BoomTool:
    name = "boom"
    description = "always raises"
    is_concurrency_safe = False

    def call(self, **kwargs: Any) -> Any:  # noqa: ARG002
        msg = "kaboom"
        raise RuntimeError(msg)


@pytest.fixture
def home_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point Path.home() at a tmp dir so journal writes are isolated."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Windows
    # re-patch Path.home directly — covers both OSes.
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    # Reset module-level constant captured at import time.
    monkeypatch.setattr(sa_mod, "_JOURNAL_DIR", tmp_path / ".concinno")
    return tmp_path


def _read_journal(home: Path) -> list[dict]:
    path = home / ".concinno" / "decision_journal.jsonl"
    if not path.exists():
        return []
    entries: list[dict] = []
    for line in path.read_text("utf-8").splitlines():
        if line.strip():
            entries.append(json.loads(line))
    return entries


def test_allow_path_records_journal(home_tmp: Path) -> None:
    tool = SelfAuditedWrapper(_DummyTool(), guards=[lambda _n, _a: None])
    out = tool.call(x=1, y="abc")
    assert out == {"echoed": {"x": 1, "y": "abc"}}
    entries = _read_journal(home_tmp)
    assert len(entries) == 1
    assert entries[0]["tool_name"] == "dummy"
    assert entries[0]["verdict"] == "allowed"
    assert entries[0]["args"] == {"x": 1, "y": "abc"}
    assert entries[0]["result"] == {"echoed": {"x": 1, "y": "abc"}}


def test_deny_path_raises_and_records(home_tmp: Path) -> None:
    def blocker(_name: str, _args: dict) -> str:
        return "too risky"

    tool = SelfAuditedWrapper(_DummyTool(), guards=[blocker])
    with pytest.raises(PermissionDenied) as exc_info:
        tool.call(z=42)
    assert exc_info.value.tool_name == "dummy"
    assert exc_info.value.guard_name == "blocker"
    assert exc_info.value.reason == "too risky"
    entries = _read_journal(home_tmp)
    assert len(entries) == 1
    assert entries[0]["verdict"] == "denied"
    assert entries[0]["guard"] == "blocker"


def test_error_path_records_and_reraises(home_tmp: Path) -> None:
    tool = SelfAuditedWrapper(_BoomTool(), guards=[])
    with pytest.raises(RuntimeError, match="kaboom"):
        tool.call()
    entries = _read_journal(home_tmp)
    assert len(entries) == 1
    assert entries[0]["verdict"] == "error"
    assert entries[0]["error_type"] == "RuntimeError"


def test_first_deny_wins(home_tmp: Path) -> None:
    calls: list[str] = []

    def allow(_n: str, _a: dict) -> None:
        calls.append("allow")
        return None

    def deny(_n: str, _a: dict) -> str:
        calls.append("deny")
        return "nope"

    def never(_n: str, _a: dict) -> str:
        calls.append("never")
        return "should not run"

    tool = SelfAuditedWrapper(_DummyTool(), guards=[allow, deny, never])
    with pytest.raises(PermissionDenied):
        tool.call()
    assert calls == ["allow", "deny"]


def test_decorator_wraps_instance(home_tmp: Path) -> None:
    @self_audited(guards=[])
    class Echo:
        name = "echo"
        description = "decorator target"
        is_concurrency_safe = True

        def call(self, **kwargs: Any) -> dict:
            return kwargs

    instance = Echo()
    assert isinstance(instance, SelfAuditedWrapper)
    result = instance.call(greeting="hi")
    assert result == {"greeting": "hi"}
    entries = _read_journal(home_tmp)
    assert entries and entries[0]["tool_name"] == "echo"


def test_unknown_named_guard_silently_skipped(home_tmp: Path) -> None:
    tool = SelfAuditedWrapper(
        _DummyTool(), guards=["no_such_guard", "butterfly"]
    )
    # Should not raise on construction. Calling should still work —
    # unknown-string guards get dropped, known ones (if available in
    # the test env) may or may not allow. We only assert the call runs.
    out = tool.call(x=1)
    assert out["echoed"] == {"x": 1}


def test_preserves_tool_protocol_attrs(home_tmp: Path) -> None:  # noqa: ARG001
    inner = _DummyTool()
    wrapped = SelfAuditedWrapper(inner, guards=[])
    assert wrapped.name == inner.name
    assert wrapped.description == inner.description
    assert wrapped.is_concurrency_safe == inner.is_concurrency_safe


def test_guard_exception_is_treated_as_allow(home_tmp: Path) -> None:
    def busted(_n: str, _a: dict) -> str:
        msg = "guard crashed"
        raise RuntimeError(msg)

    tool = SelfAuditedWrapper(_DummyTool(), guards=[busted])
    # Should not raise — guard bug must not DoS the pipeline.
    out = tool.call()
    assert "echoed" in out
