"""Tests for ToolRegistry.load_plugins — entry_points plugin discovery.

Covers: fake EntryPoint objects, name collision skip, malformed EP skip,
description probe via module:attr, missing group returns empty.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from concinno.tools.registry import ToolRegistry

# ── Fake entry-point infra ─────────────────────────────────────────────


@dataclass
class _FakeEP:
    """Minimal stand-in for importlib.metadata.EntryPoint."""

    name: str
    value: str
    group: str = "concinno.tools"


class _FakeEntryPoints:
    """Stand-in for ``importlib.metadata.EntryPoints`` / dict result."""

    def __init__(self, eps: list[_FakeEP]) -> None:
        self._eps = eps

    def select(self, *, group: str) -> list[_FakeEP]:
        return [ep for ep in self._eps if ep.group == group]


class _FakePluginTool:
    """Valid Tool-protocol class with a ``description`` attribute."""

    name = "FakePlugin"
    description = "from class attribute"
    is_concurrency_safe = True

    def call(self, **kwargs: object) -> str:  # noqa: ARG002
        return "ok"


# Module-level names for `module:attr` resolution from tests.
_fake_plugin_instance = _FakePluginTool()
_fake_plugin_cls = _FakePluginTool


# ── Basic discovery ────────────────────────────────────────────────────


class TestBasicDiscovery:
    def test_empty_entry_points_returns_empty_list(self):
        reg = ToolRegistry()
        loaded = reg.load_plugins(entry_points_override=_FakeEntryPoints([]))
        assert loaded == []

    def test_single_plugin_registered(self):
        reg = ToolRegistry()
        eps = _FakeEntryPoints(
            [
                _FakeEP(
                    name="MyPlugin",
                    value="tests.tools.test_registry_plugins:_fake_plugin_cls",
                ),
            ]
        )
        loaded = reg.load_plugins(entry_points_override=eps)
        assert loaded == ["MyPlugin"]
        assert "MyPlugin" in reg.list_deferred()

    def test_multiple_plugins_registered_in_order(self):
        reg = ToolRegistry()
        eps = _FakeEntryPoints(
            [
                _FakeEP(name="A", value="tests.tools.test_registry_plugins:_fake_plugin_cls"),
                _FakeEP(name="B", value="tests.tools.test_registry_plugins:_fake_plugin_cls"),
            ]
        )
        loaded = reg.load_plugins(entry_points_override=eps)
        assert loaded == ["A", "B"]


# ── Collision handling ─────────────────────────────────────────────────


class TestCollision:
    def test_collision_with_core_skipped(self, caplog):
        import logging as _logging

        from concinno.tools.registry import ToolEntry  # noqa: F401 — smoke import

        reg = ToolRegistry()
        # Pre-register a core tool with the same name.
        existing = _FakePluginTool()
        existing.name = "Taken"
        reg.register_core(existing)

        eps = _FakeEntryPoints(
            [
                _FakeEP(
                    name="Taken",
                    value="tests.tools.test_registry_plugins:_fake_plugin_cls",
                ),
            ]
        )
        with caplog.at_level(_logging.INFO, logger="concinno.tools.registry"):
            loaded = reg.load_plugins(entry_points_override=eps)
        assert loaded == []  # plugin was skipped
        assert any("already registered" in rec.message for rec in caplog.records)

    def test_collision_with_existing_deferred_skipped(self, caplog):
        import logging as _logging

        reg = ToolRegistry()
        reg.register_deferred(
            "Shared",
            "tests.tools.test_registry_plugins:_fake_plugin_cls",
            "first",
        )
        eps = _FakeEntryPoints(
            [
                _FakeEP(
                    name="Shared",
                    value="tests.tools.test_registry_plugins:_fake_plugin_cls",
                ),
            ]
        )
        with caplog.at_level(_logging.INFO, logger="concinno.tools.registry"):
            loaded = reg.load_plugins(entry_points_override=eps)
        assert loaded == []


# ── Malformed EP handling ──────────────────────────────────────────────


class TestMalformed:
    def test_missing_name_skipped(self, caplog):
        import logging as _logging

        reg = ToolRegistry()
        eps = _FakeEntryPoints(
            [_FakeEP(name="", value="tests.tools.test_registry_plugins:_fake_plugin_cls")]
        )
        with caplog.at_level(_logging.WARNING, logger="concinno.tools.registry"):
            loaded = reg.load_plugins(entry_points_override=eps)
        assert loaded == []
        assert any("malformed" in rec.message for rec in caplog.records)

    def test_missing_value_skipped(self):
        reg = ToolRegistry()
        eps = _FakeEntryPoints([_FakeEP(name="X", value="")])
        assert reg.load_plugins(entry_points_override=eps) == []


# ── Description probe ──────────────────────────────────────────────────


class TestDescriptionProbe:
    def test_description_from_class_attr(self):
        reg = ToolRegistry()
        eps = _FakeEntryPoints(
            [
                _FakeEP(
                    name="Probed",
                    value="tests.tools.test_registry_plugins:_fake_plugin_cls",
                ),
            ]
        )
        reg.load_plugins(entry_points_override=eps)
        # The registered ToolEntry's description should be the class attr.
        assert any(
            reg._deferred[n].description == "from class attribute"
            for n in reg.list_deferred()
            if n == "Probed"
        )

    def test_fallback_description_when_no_attr(self):
        reg = ToolRegistry()
        eps = _FakeEntryPoints(
            [
                _FakeEP(
                    name="NoDesc",
                    # Points at a builtin with no `description` attr.
                    value="builtins:list",
                ),
            ]
        )
        reg.load_plugins(entry_points_override=eps)
        # Fallback form: "Plugin tool: <name>"
        assert "NoDesc" in reg.list_deferred()


# ── Iterable EP fallback ───────────────────────────────────────────────


class TestIterableFallback:
    def test_plain_list_without_select(self):
        """A bare list of EPs (no .select() method) should still work."""
        reg = ToolRegistry()
        eps_list = [
            _FakeEP(
                name="Listed",
                value="tests.tools.test_registry_plugins:_fake_plugin_cls",
            ),
        ]
        loaded = reg.load_plugins(entry_points_override=eps_list)
        assert loaded == ["Listed"]


# ── get_default_registry env opt-in ────────────────────────────────────


class TestDefaultRegistryOptIn:
    def test_load_plugins_off_by_default(self, monkeypatch):
        monkeypatch.delenv("CONCINNO_LOAD_PLUGINS", raising=False)
        from concinno.tools.registry import get_default_registry

        reg = get_default_registry()
        # Only the hard-coded core + Shell deferred; no plugin names surface.
        assert set(reg.list_core()) == {"Read", "Write", "Edit", "Glob", "Grep"}
        assert "Shell" in reg.list_deferred()

    def test_load_plugins_on_does_not_crash(self, monkeypatch):
        """Even with env set, the default registry must build without crashing
        when no plugins are installed (real-world fresh env)."""
        monkeypatch.setenv("CONCINNO_LOAD_PLUGINS", "1")
        from concinno.tools.registry import get_default_registry

        reg = get_default_registry()
        # Sanity: core tools still there.
        assert "Read" in reg.list_core()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
