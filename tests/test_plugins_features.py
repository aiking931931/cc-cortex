"""Tests for concinno.plugins.features entry-points discovery."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from concinno.plugins.features import (
    CURRENT_SCHEMA_VERSION,
    _extract_package_name,
    _load_single_entrypoint,
    _validate_feature_meta,
    discover_feature_entrypoints,
    iter_valid_feature_plugins,
)

# ── Fakes ──────────────────────────────────────────────────


@dataclass
class _FakeDist:
    name: str


class _FakeEP:
    """Stand-in for importlib.metadata.EntryPoint."""

    def __init__(self, name: str, value: str, load_returns: Any,
                 dist_name: str = "fake-pkg", load_raises: Exception | None = None) -> None:
        self.name = name
        self.value = value
        self.dist = _FakeDist(name=dist_name)
        self._load_returns = load_returns
        self._load_raises = load_raises

    def load(self) -> Any:
        if self._load_raises is not None:
            raise self._load_raises
        return self._load_returns


_VALID_META = {
    "my_feature": {
        "category": "user_gate",
        "description": "test feature",
        "enabled": True,
        "schema_version": 1,
        "params": {},
    },
}


# ── Validation ─────────────────────────────────────────────


class TestValidateFeatureMeta:
    def test_valid_accepts(self):
        errors = _validate_feature_meta("x", _VALID_META["my_feature"])
        assert errors == []

    def test_missing_core_fields_rejected(self):
        errors = _validate_feature_meta("x", {"category": "c"})
        assert any("missing required fields" in e for e in errors)

    def test_non_dict_rejected(self):
        errors = _validate_feature_meta("x", "not a dict")
        assert any("must be a dict" in e for e in errors)

    def test_schema_version_missing_accepted_with_warning(self, caplog):
        meta = dict(_VALID_META["my_feature"])
        del meta["schema_version"]
        errors = _validate_feature_meta("x", meta)
        assert errors == []  # accepted
        # warning emitted (caplog checks later via logging not stderr)

    def test_schema_version_forward_accepted(self):
        meta = dict(_VALID_META["my_feature"])
        meta["schema_version"] = CURRENT_SCHEMA_VERSION + 1
        errors = _validate_feature_meta("x", meta)
        assert errors == []  # accepted with warning

    def test_schema_version_negative_rejected(self):
        meta = dict(_VALID_META["my_feature"])
        meta["schema_version"] = -1
        errors = _validate_feature_meta("x", meta)
        assert any("negative" in e for e in errors)

    def test_schema_version_non_int_rejected(self):
        meta = dict(_VALID_META["my_feature"])
        meta["schema_version"] = "1.0"
        errors = _validate_feature_meta("x", meta)
        assert any("must be an int" in e for e in errors)

    def test_schema_version_bool_rejected(self):
        meta = dict(_VALID_META["my_feature"])
        meta["schema_version"] = True
        errors = _validate_feature_meta("x", meta)
        assert any("must be an int" in e for e in errors)


# ── Single entry-point load ───────────────────────────────


class TestLoadSingleEntrypoint:
    def test_valid_dict(self):
        ep = _FakeEP("x", "m:v", _VALID_META)
        result = _load_single_entrypoint(ep)
        assert result.valid
        assert "my_feature" in result.meta_dict

    def test_load_raises(self):
        ep = _FakeEP("x", "m:v", None, load_raises=RuntimeError("boom"))
        result = _load_single_entrypoint(ep)
        assert not result.valid
        assert any("ep.load()" in e for e in result.errors)

    def test_non_dict_rejected(self):
        ep = _FakeEP("x", "m:v", ["not", "a", "dict"])
        result = _load_single_entrypoint(ep)
        assert not result.valid
        assert any("expected dict" in e for e in result.errors)

    def test_callable_meta_provider(self):
        ep = _FakeEP("x", "m:v", lambda: _VALID_META)
        result = _load_single_entrypoint(ep)
        assert result.valid
        assert "my_feature" in result.meta_dict

    def test_callable_meta_provider_raises(self):
        def explode():
            raise RuntimeError("oops")
        ep = _FakeEP("x", "m:v", explode)
        result = _load_single_entrypoint(ep)
        assert not result.valid
        assert any("callable meta provider raised" in e for e in result.errors)

    def test_partial_valid_partial_invalid(self):
        raw = {
            "good": _VALID_META["my_feature"],
            "bad": {"category": "c"},  # missing description + enabled
        }
        ep = _FakeEP("x", "m:v", raw)
        result = _load_single_entrypoint(ep)
        # mixed validity -> errors populated but good feature kept
        assert "good" in result.meta_dict
        assert "bad" not in result.meta_dict
        assert any("bad" in e for e in result.errors)


# ── Discovery ──────────────────────────────────────────────


class TestDiscoverFeatureEntrypoints:
    def test_plugins_disabled_returns_empty(self, monkeypatch):
        monkeypatch.setenv("CONCINNO_PLUGINS_ENABLED", "0")
        eps = [_FakeEP("x", "m:v", _VALID_META)]
        assert discover_feature_entrypoints(entry_points_override=eps) == []

    def test_allowlist_filters(self, monkeypatch):
        monkeypatch.setenv("CONCINNO_PLUGINS_ALLOWLIST", "allowed-pkg")
        eps = [
            _FakeEP("a", "m:v", _VALID_META, dist_name="allowed-pkg"),
            _FakeEP("b", "m:v", _VALID_META, dist_name="blocked-pkg"),
        ]
        result = discover_feature_entrypoints(entry_points_override=eps)
        assert len(result) == 1
        assert result[0].package == "allowed-pkg"

    def test_empty_yields_empty(self, monkeypatch):
        monkeypatch.delenv("CONCINNO_PLUGINS_ENABLED", raising=False)
        monkeypatch.delenv("CONCINNO_PLUGINS_ALLOWLIST", raising=False)
        assert discover_feature_entrypoints(entry_points_override=[]) == []

    def test_iter_valid_skips_failed(self, monkeypatch):
        monkeypatch.delenv("CONCINNO_PLUGINS_ENABLED", raising=False)
        monkeypatch.delenv("CONCINNO_PLUGINS_ALLOWLIST", raising=False)
        # Patch _get_entrypoints to return a mix.
        bad = _FakeEP("bad", "m:v", None, load_raises=RuntimeError("x"))
        good = _FakeEP("good", "m:v", _VALID_META)
        # Use override; iter_valid reads via discover which respects override
        # but iter_valid_feature_plugins calls without override, so patch
        # internal discover.
        import concinno.plugins.features as mod
        orig = mod.discover_feature_entrypoints
        mod.discover_feature_entrypoints = lambda: [
            _load_single_entrypoint(bad), _load_single_entrypoint(good),
        ]
        try:
            names = [n for n, _, _ in iter_valid_feature_plugins()]
            assert names == ["my_feature"]  # only the good one
        finally:
            mod.discover_feature_entrypoints = orig


# ── Package name extraction ─────────────────────────────────


class TestExtractPackageName:
    def test_uses_dist_name(self):
        ep = _FakeEP("x", "pkg.mod:attr", None, dist_name="my-dist")
        assert _extract_package_name(ep) == "my-dist"

    def test_falls_back_to_value(self):
        class NoDistEP:
            name = "x"
            value = "pkg.mod:attr"
            dist = None

        assert _extract_package_name(NoDistEP()) == "pkg"
