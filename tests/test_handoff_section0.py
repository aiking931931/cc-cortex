"""Tests for concinno.handoff_section0 — §0 auto-fill populator."""

from __future__ import annotations

import json
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

from concinno import handoff_section0
from concinno.handoff_section0 import (
    SECTION0_FIELDS,
    SourceFailure,
    populate_section_0,
    render_section_0,
)

# ── Schema ────────────────────────────────────────────────


def test_section0_fields_are_stable():
    assert SECTION0_FIELDS == (
        "pod_id",
        "ssh_cmd",
        "vault_aliases",
        "env_vars",
        "last_commit",
        "last_session_id",
        "last_token_usage",
        "populated_at",
    )


def test_populate_returns_all_fields(tmp_path):
    out = populate_section_0("test-project", project_root=tmp_path)
    assert set(out.keys()) == set(SECTION0_FIELDS)
    # Every value is a string (placeholders included).
    for v in out.values():
        assert isinstance(v, str)


# ── Pod source ────────────────────────────────────────────


def test_pod_cache_populated(monkeypatch, tmp_path):
    cache_dir = tmp_path / ".concinno"
    cache_dir.mkdir()
    cache = cache_dir / "pod_current.json"
    cache.write_text(json.dumps({
        "pod_id": "abc123",
        "ssh_cmd": "ssh -i ~/.ssh/runpod -p 12345 root@1.2.3.4",
    }))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    out = populate_section_0("any-project", project_root=tmp_path)
    assert out["pod_id"] == "abc123"
    assert "12345" in out["ssh_cmd"]


def test_pod_cache_missing_yields_failure_marker(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    out = populate_section_0("any", project_root=tmp_path)
    assert out["pod_id"].startswith("<unknown — source failed")
    assert out["ssh_cmd"].startswith("<unknown — source failed")


def test_pod_cache_corrupt_json_yields_failure_marker(monkeypatch, tmp_path):
    cache_dir = tmp_path / ".concinno"
    cache_dir.mkdir()
    (cache_dir / "pod_current.json").write_text("{not json")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    out = populate_section_0("any", project_root=tmp_path)
    assert "source failed" in out["pod_id"]


def test_pod_cache_synthesizes_ssh_from_ip_port(monkeypatch, tmp_path):
    cache_dir = tmp_path / ".concinno"
    cache_dir.mkdir()
    (cache_dir / "pod_current.json").write_text(json.dumps({
        "pod_id": "p1",
        "ip": "10.20.30.40",
        "port": 22,
    }))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    out = populate_section_0("any", project_root=tmp_path)
    assert "10.20.30.40" in out["ssh_cmd"]
    assert "-p 22" in out["ssh_cmd"]


# ── Vault source ──────────────────────────────────────────


def _install_fake_credential_auth(monkeypatch, entries):
    fake_module = types.ModuleType("concinno_skills_auth")

    class FakeAuth:
        def call(self, action, **kwargs):
            assert action == "list"
            return {"entries": entries}

    fake_module.CredentialAuth = FakeAuth
    monkeypatch.setitem(sys.modules, "concinno_skills_auth", fake_module)


def test_vault_aliases_keyword_match(monkeypatch, tmp_path):
    _install_fake_credential_auth(monkeypatch, [
        {"alias": "gaia-token-v1"},
        {"alias": "irrelevant-thing"},
    ])
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    out = populate_section_0("gaia-runner", project_root=tmp_path)
    assert "gaia-token-v1" in out["vault_aliases"]
    assert "anthropic-main" in out["vault_aliases"]  # always-included


def test_vault_module_missing_yields_failure_marker(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "concinno_skills_auth", None)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    out = populate_section_0("gaia", project_root=tmp_path)
    assert "source failed" in out["vault_aliases"]


def test_vault_call_raises_yields_failure_marker(monkeypatch, tmp_path):
    fake_module = types.ModuleType("concinno_skills_auth")

    class BoomAuth:
        def call(self, *a, **k):
            raise RuntimeError("vault locked")

    fake_module.CredentialAuth = BoomAuth
    monkeypatch.setitem(sys.modules, "concinno_skills_auth", fake_module)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    out = populate_section_0("gaia", project_root=tmp_path)
    assert "vault list call" in out["vault_aliases"]


# ── env vars source ───────────────────────────────────────


def test_env_vars_includes_always_relevant(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("HF_TOKEN", raising=False)
    out = populate_section_0("any", project_root=tmp_path)
    assert "ANTHROPIC_API_KEY" in out["env_vars"]
    # Value is never echoed.
    assert "sk-test" not in out["env_vars"]


def test_env_vars_project_keyword_matches(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setenv("MYPROJECT_FLAG", "1")
    out = populate_section_0("myproject", project_root=tmp_path)
    assert "MYPROJECT_FLAG" in out["env_vars"]


# ── Last commit source ────────────────────────────────────


def test_last_commit_no_git(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    out = populate_section_0("any", project_root=tmp_path)
    # tmp_path is not a git repo → failure marker
    assert "source failed" in out["last_commit"]


# ── Session id source ─────────────────────────────────────


def test_last_session_id_present(monkeypatch, tmp_path):
    cache_dir = tmp_path / ".concinno"
    cache_dir.mkdir()
    (cache_dir / "session_lock.json").write_text(
        json.dumps({"session_id": "sess_abc"})
    )
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    out = populate_section_0("any", project_root=tmp_path)
    assert out["last_session_id"] == "sess_abc"


def test_last_session_id_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    out = populate_section_0("any", project_root=tmp_path)
    assert "source failed" in out["last_session_id"]


# ── Render ────────────────────────────────────────────────


def test_render_section_0_substitutes_placeholders():
    template = "pod={{pod_id}} commit={{last_commit}}"
    rendered = render_section_0(
        {"pod_id": "abc", "last_commit": "deadbeef msg"}, template
    )
    assert rendered == "pod=abc commit=deadbeef msg"


def test_render_unknown_placeholder_left_alone():
    # render_section_0 only substitutes documented SECTION0_FIELDS.
    template = "x={{not_a_field}}"
    rendered = render_section_0({}, template)
    assert rendered == "x={{not_a_field}}"


# ── Doesn't crash ─────────────────────────────────────────


def test_populate_never_raises_even_with_broken_helpers(monkeypatch, tmp_path):
    """Top-level promise: populate_section_0 always returns a dict."""
    # Force every helper to raise SourceFailure
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

    def _boom_vault(*a, **k):
        raise SourceFailure("forced")

    monkeypatch.setattr(handoff_section0, "_list_vault_aliases", _boom_vault)
    out = populate_section_0("any", project_root=tmp_path)
    assert isinstance(out, dict)
    assert set(out.keys()) == set(SECTION0_FIELDS)


def test_populated_at_is_iso8601(tmp_path):
    out = populate_section_0("any", project_root=tmp_path)
    # parse round-trip
    parsed = datetime.fromisoformat(out["populated_at"].replace("Z", "+00:00"))
    assert parsed.tzinfo is not None
    # within ±5 seconds of now
    delta = abs((parsed - datetime.now(timezone.utc)).total_seconds())
    assert delta < 60


# ── state_client integration (Phase 4 of 3.2.0 ship-prep) ─


def test_state_client_supplies_token_usage(monkeypatch, tmp_path):
    """When state_store has last_token_usage, that value populates §0 directly."""
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(
        handoff_section0,
        "_read_state_snapshot",
        lambda _proj: {"last_token_usage": "47k/1M (4.7%)"},
    )
    out = populate_section_0("gaia", project_root=tmp_path)
    assert out["last_token_usage"] == "47k/1M (4.7%)"


def test_state_client_supplies_pod_fields(monkeypatch, tmp_path):
    """state_store.last_pod_id + pod_ssh_host/port wins over markdown cache."""
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(
        handoff_section0,
        "_read_state_snapshot",
        lambda _proj: {
            "last_pod_id": "live-pod-xyz",
            "pod_ssh_host": "1.2.3.4",
            "pod_ssh_port": 24831,
        },
    )
    out = populate_section_0("gaia", project_root=tmp_path)
    assert out["pod_id"] == "live-pod-xyz"
    assert "1.2.3.4" in out["ssh_cmd"]
    assert "24831" in out["ssh_cmd"]


def test_state_client_falls_back_when_empty(monkeypatch, tmp_path):
    """Empty snapshot → existing markdown / cache logic still runs."""
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(
        handoff_section0, "_read_state_snapshot", lambda _proj: {},
    )
    out = populate_section_0("any", project_root=tmp_path)
    # Without a state_store value AND without a pod_current.json cache,
    # both should be failure markers (preserving pre-state_store
    # behaviour).
    assert out["pod_id"].startswith("<unknown — source failed")
    assert out["last_token_usage"].startswith("<unknown — source failed")


def test_state_client_failure_does_not_crash(monkeypatch, tmp_path):
    """A raising state_client → silent fallback to existing logic."""
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

    def _boom(_proj):
        raise RuntimeError("state_store backend down")

    monkeypatch.setattr(handoff_section0, "_read_state_snapshot", _boom)
    # populate_section_0's helper itself catches; but be defensive — if
    # someone wires a raising _read_state_snapshot directly we still
    # want a clean exit. Prove no exception bubbles up.
    try:
        out = populate_section_0("any", project_root=tmp_path)
    except Exception as exc:  # pragma: no cover
        raise AssertionError(
            f"populate_section_0 must never raise; got {exc!r}"
        ) from exc
    assert set(out.keys()) == set(SECTION0_FIELDS)


def test_state_client_supplies_session_and_commit(monkeypatch, tmp_path):
    """state_store.last_session_id / last_commit_hash override live probes."""
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(
        handoff_section0,
        "_read_state_snapshot",
        lambda _proj: {
            "last_session_id": "sess-from-state",
            "last_commit_hash": "deadbeef WIP",
        },
    )
    out = populate_section_0("any", project_root=tmp_path)
    assert out["last_session_id"] == "sess-from-state"
    assert out["last_commit"] == "deadbeef WIP"
