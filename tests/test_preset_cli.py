"""Tests for concinno.cli.preset_cmd — CLI subcommand surface.

Uses ``capsys`` to assert stdout shape. argparse wiring is covered by
the end-to-end ``register()`` -> invocation path.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from concinno.cli.preset_cmd import (
    cmd_preset_autotune_log,
    cmd_preset_list,
    cmd_preset_set,
    cmd_preset_show,
    register,
)


@pytest.fixture(autouse=True)
def _isolate_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    import concinno.core.config as _ccfg_mod
    from concinno.core.config import get_config

    hooks_tmp = tmp_path / "hooks"
    hooks_tmp.mkdir(parents=True, exist_ok=True)
    _ccfg_mod._instance = None  # type: ignore[attr-defined]
    get_config(hooks_dir=str(hooks_tmp))
    yield tmp_path
    _ccfg_mod._instance = None  # type: ignore[attr-defined]


def test_register_adds_preset_subparser() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    register(sub)
    # Parsing should accept ``preset list``.
    ns = parser.parse_args(["preset", "list", "--format", "json"])
    assert ns.command == "preset"
    assert ns.preset_command == "list"
    assert ns.format == "json"


def test_list_text(capsys: pytest.CaptureFixture) -> None:
    ns = argparse.Namespace(format="text")
    cmd_preset_list(ns)
    out = capsys.readouterr().out
    assert "benchmark" in out
    assert "general" in out


def test_list_json(capsys: pytest.CaptureFixture) -> None:
    ns = argparse.Namespace(format="json")
    cmd_preset_list(ns)
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert "benchmark" in parsed["presets"]
    assert parsed["active"] in parsed["presets"]


def test_show_text_default_active(
    capsys: pytest.CaptureFixture,
) -> None:
    ns = argparse.Namespace(format="text")
    cmd_preset_show(ns)
    out = capsys.readouterr().out
    assert "active preset:" in out


def test_show_json_schema_stable(
    capsys: pytest.CaptureFixture,
) -> None:
    ns = argparse.Namespace(format="json")
    cmd_preset_show(ns)
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["schema"] == "concinno.preset.v1"
    assert "active_preset" in parsed
    assert "effective_values" in parsed


def test_set_text(capsys: pytest.CaptureFixture) -> None:
    ns = argparse.Namespace(name="benchmark", format="text")
    cmd_preset_set(ns)
    out = capsys.readouterr().out
    assert "active preset now: benchmark" in out


def test_set_unknown_exits_nonzero(
    capsys: pytest.CaptureFixture,
) -> None:
    ns = argparse.Namespace(name="nonexistent", format="text")
    with pytest.raises(SystemExit) as exc:
        cmd_preset_set(ns)
    assert exc.value.code == 1


def test_autotune_log_empty(capsys: pytest.CaptureFixture) -> None:
    ns = argparse.Namespace(tail=0, format="text")
    cmd_preset_autotune_log(ns)
    out = capsys.readouterr().out
    assert "no autotune log" in out


def test_autotune_log_tail(
    capsys: pytest.CaptureFixture, tmp_path: Path
) -> None:
    log_path = tmp_path / ".concinno" / "ziq_autotune_log.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for i in range(5):
        lines.append(json.dumps({"ts": "now", "key": f"k{i}"}))
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    ns = argparse.Namespace(tail=2, format="text")
    cmd_preset_autotune_log(ns)
    out = capsys.readouterr().out
    assert '"k3"' in out
    assert '"k4"' in out
    assert '"k0"' not in out
