"""Tests for concinno.plugins.skills entry-points discovery."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from concinno.plugins.skills import (
    SkillPluginMeta,
    _load_single_entrypoint,
    _resolve_to_path,
    discover_skill_entrypoints,
    iter_plugin_skill_roots,
)


@dataclass
class _FakeDist:
    name: str


class _FakeEP:
    def __init__(self, name: str, value: str, load_returns: Any,
                 dist_name: str = "fake-skills-pkg",
                 load_raises: Exception | None = None) -> None:
        self.name = name
        self.value = value
        self.dist = _FakeDist(name=dist_name)
        self._load_returns = load_returns
        self._load_raises = load_raises

    def load(self) -> Any:
        if self._load_raises is not None:
            raise self._load_raises
        return self._load_returns


# ── _resolve_to_path ───────────────────────────────────────


class TestResolveToPath:
    def test_path_returned(self, tmp_path):
        assert _resolve_to_path(tmp_path) == tmp_path

    def test_str_returned_as_path(self, tmp_path):
        assert _resolve_to_path(str(tmp_path)) == Path(str(tmp_path))

    def test_callable_returning_path(self, tmp_path):
        assert _resolve_to_path(lambda: tmp_path) == tmp_path

    def test_callable_raises(self):
        def boom():
            raise RuntimeError("boom")
        assert _resolve_to_path(boom) is None

    def test_invalid_type(self):
        assert _resolve_to_path(42) is None
        assert _resolve_to_path([1, 2]) is None


# ── _load_single_entrypoint ────────────────────────────────


class TestLoadSingleEntrypoint:
    def test_valid_dir(self, tmp_path):
        # Create a plausible skill dir
        (tmp_path / "my_skill").mkdir()
        (tmp_path / "my_skill" / "SKILL.md").write_text(
            "---\nname: my_skill\n---\n", encoding="utf-8",
        )
        ep = _FakeEP("x", "m:v", tmp_path)
        result = _load_single_entrypoint(ep)
        assert result.valid
        assert result.resolved_path == tmp_path

    def test_load_raises(self):
        ep = _FakeEP("x", "m:v", None, load_raises=RuntimeError("boom"))
        result = _load_single_entrypoint(ep)
        assert not result.valid
        assert any("ep.load()" in e for e in result.errors)

    def test_not_a_directory(self, tmp_path):
        f = tmp_path / "not-a-dir"
        f.write_text("content", encoding="utf-8")
        ep = _FakeEP("x", "m:v", f)
        result = _load_single_entrypoint(ep)
        assert not result.valid
        assert any("not a directory" in e for e in result.errors)

    def test_invalid_type(self):
        ep = _FakeEP("x", "m:v", 42)
        result = _load_single_entrypoint(ep)
        assert not result.valid
        assert any("expected" in e for e in result.errors)


# ── discover_skill_entrypoints ─────────────────────────────


class TestDiscoverSkillEntrypoints:
    def test_plugins_disabled(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CONCINNO_PLUGINS_ENABLED", "0")
        ep = _FakeEP("x", "m:v", tmp_path)
        assert discover_skill_entrypoints(entry_points_override=[ep]) == []

    def test_empty_returns_empty(self, monkeypatch):
        monkeypatch.delenv("CONCINNO_PLUGINS_ENABLED", raising=False)
        monkeypatch.delenv("CONCINNO_PLUGINS_ALLOWLIST", raising=False)
        assert discover_skill_entrypoints(entry_points_override=[]) == []

    def test_allowlist_filters(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CONCINNO_PLUGINS_ALLOWLIST", "allowed-pkg")
        dir_a = tmp_path / "a"
        dir_a.mkdir()
        dir_b = tmp_path / "b"
        dir_b.mkdir()
        eps = [
            _FakeEP("a", "m:v", dir_a, dist_name="allowed-pkg"),
            _FakeEP("b", "m:v", dir_b, dist_name="blocked-pkg"),
        ]
        result = discover_skill_entrypoints(entry_points_override=eps)
        assert len(result) == 1
        assert result[0].package == "allowed-pkg"


class TestIterPluginSkillRoots:
    def test_yields_only_valid(self, monkeypatch, tmp_path):
        monkeypatch.delenv("CONCINNO_PLUGINS_ENABLED", raising=False)
        monkeypatch.delenv("CONCINNO_PLUGINS_ALLOWLIST", raising=False)
        good = tmp_path / "good"
        good.mkdir()
        tmp_path / "missing"

        import concinno.plugins.skills as mod
        orig = mod.discover_skill_entrypoints
        mod.discover_skill_entrypoints = lambda: [
            SkillPluginMeta(
                entry_point_name="good_ep", package="good-pkg",
                resolved_path=good,
            ),
            SkillPluginMeta(
                entry_point_name="bad_ep", package="bad-pkg",
                errors=["missing dir"],
            ),
        ]
        try:
            result = list(iter_plugin_skill_roots())
            assert result == [(good, "good-pkg")]
        finally:
            mod.discover_skill_entrypoints = orig
