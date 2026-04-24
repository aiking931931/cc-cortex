"""Tests for concinno.plugins.allowlist_file."""
from __future__ import annotations

import json

import pytest


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """Redirect HOME so the allowlist file lives inside tmp_path."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    return tmp_path


def _reload():
    """Force a fresh import so Path.home() re-evaluates."""
    import importlib

    import concinno.plugins.allowlist_file as mod
    return importlib.reload(mod)


class TestInitial:
    def test_empty_allowlist_when_file_absent(self, isolated_home):
        mod = _reload()
        assert mod.load_allowlist_file() == []
        assert mod.get_note() == ""
        assert mod.get_updated_at() is None

    def test_schema_version_constant(self):
        from concinno.plugins.allowlist_file import SCHEMA_VERSION
        assert SCHEMA_VERSION == 0


class TestAddRemove:
    def test_add_new_package(self, isolated_home):
        mod = _reload()
        success, newly_added = mod.add_to_allowlist("concinno-skills-foo")
        assert success is True
        assert newly_added is True
        assert mod.load_allowlist_file() == ["concinno-skills-foo"]

    def test_add_idempotent(self, isolated_home):
        mod = _reload()
        mod.add_to_allowlist("concinno-skills-foo")
        success, newly_added = mod.add_to_allowlist("concinno-skills-foo")
        assert success is True
        assert newly_added is False

    def test_add_note_overwrites(self, isolated_home):
        mod = _reload()
        mod.add_to_allowlist("pkg-a", note="note 1")
        mod.add_to_allowlist("pkg-b", note="note 2")
        assert mod.get_note() == "note 2"

    def test_remove_existing(self, isolated_home):
        mod = _reload()
        mod.add_to_allowlist("pkg-a")
        mod.add_to_allowlist("pkg-b")
        success, removed = mod.remove_from_allowlist("pkg-a")
        assert success is True
        assert removed is True
        assert mod.load_allowlist_file() == ["pkg-b"]

    def test_remove_idempotent(self, isolated_home):
        mod = _reload()
        success, removed = mod.remove_from_allowlist("not-there")
        assert success is True
        assert removed is False

    def test_empty_pkg_raises(self, isolated_home):
        mod = _reload()
        with pytest.raises(ValueError):
            mod.add_to_allowlist("   ")
        with pytest.raises(ValueError):
            mod.remove_from_allowlist("")


class TestPersistence:
    def test_file_schema_shape(self, isolated_home):
        mod = _reload()
        mod.add_to_allowlist("pkg-x", note="my note")
        path = mod._allowlist_path()
        doc = json.loads(path.read_text(encoding="utf-8"))
        assert doc["schema_version"] == 0
        assert doc["allowlist"] == ["pkg-x"]
        assert doc["note"] == "my note"
        assert doc["updated_at"] is not None

    def test_sorted_on_add(self, isolated_home):
        mod = _reload()
        mod.add_to_allowlist("zeta")
        mod.add_to_allowlist("alpha")
        mod.add_to_allowlist("mu")
        assert mod.load_allowlist_file() == ["alpha", "mu", "zeta"]


class TestFailClosed:
    def test_malformed_json_returns_empty(self, isolated_home):
        mod = _reload()
        path = mod._allowlist_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json at all", encoding="utf-8")
        assert mod.load_allowlist_file() == []

    def test_non_object_root_returns_empty(self, isolated_home):
        mod = _reload()
        path = mod._allowlist_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('["a","b"]', encoding="utf-8")
        assert mod.load_allowlist_file() == []

    def test_non_int_schema_version_rejected(self, isolated_home):
        mod = _reload()
        path = mod._allowlist_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            '{"schema_version": "wrong", "allowlist": ["a"]}',
            encoding="utf-8",
        )
        assert mod.load_allowlist_file() == []


class TestForwardCompat:
    def test_unknown_schema_version_accepted(self, isolated_home):
        mod = _reload()
        path = mod._allowlist_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            '{"schema_version": 99, "allowlist": ["fwd-pkg"], '
            '"future_field": "ignored"}',
            encoding="utf-8",
        )
        # Forward-compat: unknown schema_version still yields allowlist.
        assert mod.load_allowlist_file() == ["fwd-pkg"]

    def test_non_string_items_filtered(self, isolated_home):
        mod = _reload()
        path = mod._allowlist_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            '{"schema_version": 0, "allowlist": ["good", 42, null, "also-good"]}',
            encoding="utf-8",
        )
        assert mod.load_allowlist_file() == ["good", "also-good"]


class TestAtomicWrite:
    def test_concurrent_sibling_file_not_disturbed(self, isolated_home):
        """Ensure we use tempfile in the target dir and replace."""
        mod = _reload()
        mod.add_to_allowlist("first")
        # Drop a sibling file in the same directory
        concinno_dir = mod._allowlist_path().parent
        sibling = concinno_dir / "some_other_config.json"
        sibling.write_text('{"keep": "me"}', encoding="utf-8")
        mod.add_to_allowlist("second")
        assert sibling.is_file()
        assert json.loads(sibling.read_text(encoding="utf-8")) == {"keep": "me"}
