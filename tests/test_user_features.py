"""Tests for ``concinno.user_features`` module (2.30.1)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture()
def isolated_home(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    (fake_home / ".concinno").mkdir(parents=True)
    monkeypatch.setattr("pathlib.Path.home", lambda: fake_home)
    return fake_home


VALID_ENTRY = {
    "category": "user_gate",
    "description": "Test user feature",
    "enabled": True,
    "ziq_autotunable": False,
    "cosmetic": False,
}


def test_load_empty_when_file_absent(isolated_home: Path) -> None:
    from concinno.user_features import load_user_features
    assert load_user_features() == {}


def test_save_and_load_roundtrip(isolated_home: Path) -> None:
    from concinno.user_features import load_user_features, save_user_feature
    save_user_feature("my_guard", VALID_ENTRY)
    loaded = load_user_features()
    assert "my_guard" in loaded
    assert loaded["my_guard"]["category"] == "user_gate"


def test_atomic_write_no_partial_on_disk(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a write fails mid-way, the original file should be intact."""
    from concinno.user_features import save_user_feature, user_features_path
    save_user_feature("first", VALID_ENTRY)
    before = user_features_path().read_text(encoding="utf-8")

    # Monkeypatch os.replace to simulate crash between tmp write and rename
    import os
    original = os.replace
    calls = {"n": 0}

    def failing_replace(*a, **kw):
        calls["n"] += 1
        raise OSError("simulated crash")

    monkeypatch.setattr("os.replace", failing_replace)

    with pytest.raises(OSError):
        save_user_feature("second", VALID_ENTRY)

    monkeypatch.setattr("os.replace", original)
    # Original file should be unchanged
    after = user_features_path().read_text(encoding="utf-8")
    assert before == after


def test_malformed_json_fail_closed(isolated_home: Path) -> None:
    from concinno.user_features import load_user_features, user_features_path
    user_features_path().write_text("not json {{{", encoding="utf-8")
    assert load_user_features() == {}


def test_schema_migration_v0_to_v1(isolated_home: Path) -> None:
    """Legacy v0 file (no wrapper) migrates transparently."""
    from concinno.user_features import load_user_features, user_features_path
    user_features_path().write_text(
        json.dumps({
            "schema_version": 0,
            "legacy_feature": VALID_ENTRY,
        }),
        encoding="utf-8",
    )
    loaded = load_user_features()
    assert "legacy_feature" in loaded


def test_newer_schema_fails_closed(isolated_home: Path) -> None:
    """A schema_version newer than we understand must not crash."""
    from concinno.user_features import load_user_features, user_features_path
    user_features_path().write_text(
        json.dumps({
            "schema_version": 999,
            "features": {"x": VALID_ENTRY},
        }),
        encoding="utf-8",
    )
    assert load_user_features() == {}


def test_invalid_entry_skipped(isolated_home: Path) -> None:
    from concinno.user_features import load_user_features, user_features_path
    user_features_path().write_text(
        json.dumps({
            "schema_version": 1,
            "features": {
                "good": VALID_ENTRY,
                "bad": {"category": "x"},  # missing required fields
            },
        }),
        encoding="utf-8",
    )
    loaded = load_user_features()
    assert "good" in loaded
    assert "bad" not in loaded


def test_delete_user_feature(isolated_home: Path) -> None:
    from concinno.user_features import (
        delete_user_feature,
        load_user_features,
        save_user_feature,
    )
    save_user_feature("doomed", VALID_ENTRY)
    assert "doomed" in load_user_features()
    assert delete_user_feature("doomed") is True
    assert "doomed" not in load_user_features()
    assert delete_user_feature("doomed") is False


def test_validate_rejects_bad_shape() -> None:
    from concinno.user_features import validate_user_feature
    ok, _ = validate_user_feature("good_name", VALID_ENTRY)
    assert ok is True

    ok, reason = validate_user_feature("bad name with spaces", VALID_ENTRY)
    assert ok is False
    assert "alphanumeric" in reason

    bad_entry = dict(VALID_ENTRY)
    bad_entry.pop("enabled")
    ok, reason = validate_user_feature("name", bad_entry)
    assert ok is False
    assert "enabled" in reason


def test_collision_warning_on_merge(isolated_home: Path) -> None:
    """A user feature named like a shipped one must appear in
    collision_warnings() after a merge iteration."""
    from concinno.feature_config import FEATURE_META, iter_all_features_with_origin
    from concinno.user_features import collision_warnings, save_user_feature

    shipped_name = next(iter(FEATURE_META))
    save_user_feature(shipped_name, VALID_ENTRY)
    rows = iter_all_features_with_origin()
    origins = [row_origin for row_name, _meta, row_origin in rows
               if row_name == shipped_name]
    # 2.31.0 three-layer merge: shipped + user collision now yields a
    # single merged row (not a user-dropped row). shipped fields still
    # win per `_merge_feature_meta` semantics, but the user layer is
    # preserved inside the merge (enabling param-value overrides) and
    # the origin label reflects both sources.
    assert origins == ["merged:official+user"]
    warnings = collision_warnings()
    assert any(shipped_name in w for w in warnings)
