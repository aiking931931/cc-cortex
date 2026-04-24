"""Tests for concinno.plugins package-level helpers."""
from __future__ import annotations

import pytest

from concinno.plugins import (
    _is_pkg_allowed,
    is_plugins_enabled,
    plugin_allowlist,
)


class TestIsPluginsEnabled:
    def test_default_on(self, monkeypatch):
        monkeypatch.delenv("CONCINNO_PLUGINS_ENABLED", raising=False)
        assert is_plugins_enabled() is True

    @pytest.mark.parametrize("val", ["0", "false", "no", "off", "False", "OFF"])
    def test_env_var_disables(self, monkeypatch, val):
        monkeypatch.setenv("CONCINNO_PLUGINS_ENABLED", val)
        assert is_plugins_enabled() is False

    @pytest.mark.parametrize("val", ["1", "true", "yes", "on", ""])
    def test_env_var_keeps_enabled(self, monkeypatch, val):
        monkeypatch.setenv("CONCINNO_PLUGINS_ENABLED", val)
        assert is_plugins_enabled() is True


class TestPluginAllowlist:
    def test_unset_returns_none(self, monkeypatch):
        monkeypatch.delenv("CONCINNO_PLUGINS_ALLOWLIST", raising=False)
        assert plugin_allowlist() is None

    def test_empty_returns_none(self, monkeypatch):
        monkeypatch.setenv("CONCINNO_PLUGINS_ALLOWLIST", "")
        assert plugin_allowlist() is None

    def test_single_pkg(self, monkeypatch):
        monkeypatch.setenv("CONCINNO_PLUGINS_ALLOWLIST", "pkg-a")
        assert plugin_allowlist() == frozenset({"pkg-a"})

    def test_multi_pkg(self, monkeypatch):
        monkeypatch.setenv("CONCINNO_PLUGINS_ALLOWLIST", "pkg-a,pkg-b, pkg-c ")
        assert plugin_allowlist() == frozenset({"pkg-a", "pkg-b", "pkg-c"})


class TestIsPkgAllowed:
    def test_none_allowlist_allows_all(self):
        assert _is_pkg_allowed("anything", None) is True

    def test_exact_match(self):
        assert _is_pkg_allowed("pkg-a", {"pkg-a"}) is True
        assert _is_pkg_allowed("pkg-b", {"pkg-a"}) is False

    def test_underscore_dash_variants(self):
        assert _is_pkg_allowed("pkg_a", {"pkg-a"}) is True
        assert _is_pkg_allowed("pkg-a", {"pkg_a"}) is True
