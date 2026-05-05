"""Tests for entry_points consumer dispatch in preset_cascade.

Exercises :func:`concinno.preset_cascade._dispatch_consumers` with
synthetic ``concinno.preset_consumers`` entry_points — including a
broken consumer to confirm try/except isolation per narrower-scope v4
§2 fail-soft contract.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from concinno.preset_cascade import set_preset


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


class _FakeEntryPoint:
    """Minimal stand-in for ``importlib.metadata.EntryPoint``."""

    def __init__(self, name: str, fn: Any) -> None:
        self.name = name
        self._fn = fn

    def load(self) -> Any:
        return self._fn


def _patch_entry_points(
    monkeypatch: pytest.MonkeyPatch, eps: list[_FakeEntryPoint]
) -> None:
    """Monkeypatch importlib.metadata.entry_points to return ``eps``."""
    from importlib import metadata as _meta

    def _fake_ep(group: str = "", **_kw: Any) -> list[_FakeEntryPoint]:
        if group == "concinno.preset_consumers":
            return eps
        return []

    monkeypatch.setattr(_meta, "entry_points", _fake_ep)


def test_consumer_fires(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def hook(name: str) -> None:
        calls.append(name)

    _patch_entry_points(
        monkeypatch, [_FakeEntryPoint("sancio", hook)]
    )
    report = set_preset("benchmark")
    assert "sancio" in report.consumers_called
    assert calls == ["benchmark"]


def test_broken_consumer_isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    def exploding(_name: str) -> None:
        raise RuntimeError("boom")

    def good(name: str) -> None:
        good.seen = name  # type: ignore[attr-defined]

    _patch_entry_points(
        monkeypatch,
        [
            _FakeEntryPoint("broken", exploding),
            _FakeEntryPoint("good", good),
        ],
    )
    report = set_preset("benchmark")
    assert "good" in report.consumers_called
    assert "broken" not in report.consumers_called
    assert any("broken" in err for err in report.errors)


def test_consumer_load_failure_isolated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _BadLoad:
        name = "badload"

        def load(self) -> Any:
            raise ImportError("simulated")

    def good(name: str) -> None:
        pass

    _patch_entry_points(
        monkeypatch, [_BadLoad(), _FakeEntryPoint("good", good)]
    )
    report = set_preset("benchmark")
    assert "good" in report.consumers_called
    assert any("badload" in err for err in report.errors)


def test_no_consumers_is_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_entry_points(monkeypatch, [])
    report = set_preset("benchmark")
    assert report.consumers_called == []
    # No consumer-dispatch errors even though list is empty.
    assert not any(
        "consumer" in e.lower() for e in report.errors
    )
