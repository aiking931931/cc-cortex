"""Regression test for F6 (2.7.1): handoff_engine legacy mode alias.

Older cc_config.json files (pre-2.5) carry ``handoff_mode: autonomous``
and ``handoff_mode: save_token`` (underscore). After the rename these
names no longer appear in ``HANDOFF_MODES`` and 2.7.0 silently dropped
the value to the ship default. The alias normalizer now maps the old
spellings to the current canonical names.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from concinno import handoff_engine


def _write_legacy_config(tmp_path: Path, value: str) -> Path:
    """Create a fake <project>/.claude/hooks/cc_config.json."""
    hooks_dir = tmp_path / ".claude" / "hooks"
    hooks_dir.mkdir(parents=True)
    cfg = hooks_dir / "cc_config.json"
    cfg.write_text(json.dumps({"handoff_mode": value}), encoding="utf-8")
    return cfg


def test_autonomous_maps_to_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_legacy_config(tmp_path, "autonomous")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    assert handoff_engine.get_handoff_mode() == "full"


def test_save_token_underscore_maps_to_canonical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_legacy_config(tmp_path, "save_token")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    assert handoff_engine.get_handoff_mode() == "save-token"


def test_canonical_values_still_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-alias values pass through unchanged."""
    _write_legacy_config(tmp_path, "full")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    assert handoff_engine.get_handoff_mode() == "full"


def test_unknown_value_falls_through_to_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unknown alias → None → fall through to concinno.config or default."""
    _write_legacy_config(tmp_path, "definitely_not_a_mode")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    # No user/project concinno config either → ship default "phase".
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.chdir(home)
    assert handoff_engine.get_handoff_mode() in ("phase", "general", "save-token")
