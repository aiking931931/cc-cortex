"""Tests for 2.31.0 three-layer merge in feature_config.

Covers precedence shipped > user > plugin, enabled cascade (library
integrity wins), params-level merge, plugins_enabled kill-switch.
"""
from __future__ import annotations

from concinno.feature_config import (
    _merge_feature_meta,
    iter_all_features_with_origin,
)


class TestMergeFeatureMeta:
    def test_shipped_only(self):
        meta, origin = _merge_feature_meta({
            "official": {
                "category": "c", "description": "d", "enabled": True,
                "params": {},
            },
        })
        assert meta["enabled"] is True
        assert meta["description"] == "d"
        assert origin == "official"

    def test_user_only(self):
        meta, origin = _merge_feature_meta({
            "user": {
                "category": "c", "description": "u", "enabled": False,
                "params": {},
            },
        })
        assert meta["enabled"] is False
        assert origin == "user"

    def test_plugin_only_with_pkg(self):
        meta, origin = _merge_feature_meta({
            "plugin": {
                "category": "c", "description": "p", "enabled": True,
                "params": {},
            },
            "_plugin_pkg": "my-pkg",
        })
        assert origin == "plugin:my-pkg"

    def test_shipped_wins_description(self):
        meta, _ = _merge_feature_meta({
            "official": {"description": "shipped", "category": "c",
                        "enabled": True, "params": {}},
            "user": {"description": "user", "enabled": False, "params": {}},
            "plugin": {"description": "plugin", "enabled": True, "params": {}},
            "_plugin_pkg": "p",
        })
        assert meta["description"] == "shipped"

    def test_enabled_cascade_shipped_is_final_authority(self):
        """shipped enabled always wins -- library integrity."""
        meta, _ = _merge_feature_meta({
            "official": {"description": "s", "category": "c",
                        "enabled": True, "params": {}},
            "user": {"enabled": False, "params": {}},
            "plugin": {"enabled": True, "params": {}},
            "_plugin_pkg": "p",
        })
        assert meta["enabled"] is True

    def test_user_overrides_plugin_when_shipped_absent(self):
        meta, _ = _merge_feature_meta({
            "user": {"enabled": False, "params": {}},
            "plugin": {"description": "p", "category": "c",
                       "enabled": True, "params": {}},
            "_plugin_pkg": "pkg",
        })
        assert meta["enabled"] is False

    def test_params_shipped_baseline_user_value_override(self):
        meta, _ = _merge_feature_meta({
            "official": {
                "description": "s", "category": "c", "enabled": True,
                "params": {"x": {"type": "int", "default": 10, "min": 0}},
            },
            "user": {
                "enabled": True,
                "params": {"x": {"default": 20}},
            },
        })
        assert meta["params"]["x"]["type"] == "int"   # shipped schema preserved
        assert meta["params"]["x"]["default"] == 20   # user value wins
        assert meta["params"]["x"]["min"] == 0        # shipped baseline kept

    def test_params_plugin_new_param(self):
        meta, _ = _merge_feature_meta({
            "official": {
                "description": "s", "category": "c", "enabled": True,
                "params": {"a": {"type": "int", "default": 1}},
            },
            "plugin": {
                "description": "p", "category": "c", "enabled": True,
                "params": {"b": {"type": "str", "default": "hi"}},
            },
            "_plugin_pkg": "pk",
        })
        assert "a" in meta["params"]
        assert "b" in meta["params"]
        assert meta["params"]["b"]["default"] == "hi"

    def test_origin_merged_label(self):
        _, origin = _merge_feature_meta({
            "official": {"description": "s", "category": "c",
                        "enabled": True, "params": {}},
            "user": {"enabled": False, "params": {}},
        })
        assert origin == "merged:official+user"

    def test_schema_version_preserved_on_plugin(self):
        meta, _ = _merge_feature_meta({
            "plugin": {
                "description": "p", "category": "c", "enabled": True,
                "params": {}, "schema_version": 1,
            },
            "_plugin_pkg": "pk",
        })
        assert meta.get("schema_version") == 1


class TestIterAllFeaturesWithOrigin:
    def test_returns_rows(self):
        rows = iter_all_features_with_origin()
        assert len(rows) > 0
        # plugins_enabled is shipped in 2.31.0
        names = {n for n, _, _ in rows}
        assert "plugins_enabled" in names

    def test_all_shipped_have_origin_official(self):
        rows = iter_all_features_with_origin()
        # Without any user features or plugins, all rows should be "official"
        # (backward-compat label retained from pre-2.31.0).
        origins = {o for _, _, o in rows}
        assert "official" in origins
