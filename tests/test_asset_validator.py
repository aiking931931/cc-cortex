"""Tests for cc_cortex.asset_validator — Universal asset validation framework."""

from __future__ import annotations

import json

from cc_cortex.asset_validator import (
    AssetType,
    AssetValidator,
    DimensionResult,
    ProjectCascade,
    ValidationResult,
    WiredoDimension,
    detect_asset_type,
    get_schema,
    load_wiredo_config,
)

# ── detect_asset_type ─────────────────────────────────────────


def test_detect_python():
    assert detect_asset_type("main.py") == AssetType.CODE


def test_detect_typescript():
    assert detect_asset_type("app.tsx") == AssetType.CODE


def test_detect_png():
    assert detect_asset_type("photo.png") == AssetType.IMAGE


def test_detect_jpg():
    assert detect_asset_type("photo.jpg") == AssetType.IMAGE


def test_detect_webp():
    assert detect_asset_type("photo.webp") == AssetType.IMAGE


def test_detect_mp4():
    assert detect_asset_type("dance.mp4") == AssetType.VIDEO


def test_detect_mov():
    assert detect_asset_type("clip.mov") == AssetType.VIDEO


def test_detect_wav():
    assert detect_asset_type("voice.wav") == AssetType.AUDIO


def test_detect_mp3():
    assert detect_asset_type("song.mp3") == AssetType.AUDIO


def test_detect_docx():
    assert detect_asset_type("book.docx") == AssetType.DOCUMENT


def test_detect_markdown():
    assert detect_asset_type("readme.md") == AssetType.DOCUMENT


def test_detect_unknown():
    assert detect_asset_type("data.xyz") is None


# ── AssetSchema ──────────────────────────────────────────────


def test_schema_exists_for_all_types():
    all_types = [
        AssetType.CODE, AssetType.IMAGE, AssetType.VIDEO,
        AssetType.AUDIO, AssetType.DOCUMENT,
    ]
    for t in all_types:
        schema = get_schema(t)
        assert schema.asset_type == t
        assert len(schema.validators) == 6


def test_schema_image_has_na_observable():
    schema = get_schema(AssetType.IMAGE)
    assert WiredoDimension.OBSERVABLE in schema.na_dimensions


def test_schema_code_has_no_na():
    schema = get_schema(AssetType.CODE)
    assert len(schema.na_dimensions) == 0


# ── DimensionResult ──────────────────────────────────────────


def test_dimension_result_status_pass():
    r = DimensionResult(dimension=WiredoDimension.WIRED, passed=True, evidence="OK")
    assert r.status == "PASS"


def test_dimension_result_status_fail():
    r = DimensionResult(dimension=WiredoDimension.WIRED, passed=False, evidence="Bad")
    assert r.status == "FAIL"


def test_dimension_result_status_na():
    r = DimensionResult(dimension=WiredoDimension.OBSERVABLE, passed=False, na=True)
    assert r.status == "N/A"


# ── ValidationResult ─────────────────────────────────────────


def test_validation_result_passed():
    r = ValidationResult(
        asset_type=AssetType.CODE,
        asset_path="test.py",
        dimensions=[
            DimensionResult(WiredoDimension.WIRED, True, "OK"),
            DimensionResult(WiredoDimension.INHERITED, True, "OK"),
        ],
    )
    assert r.passed is True
    assert r.fail_count == 0


def test_validation_result_failed():
    r = ValidationResult(
        asset_type=AssetType.IMAGE,
        asset_path="bad.png",
        dimensions=[
            DimensionResult(WiredoDimension.WIRED, False, "tmp/"),
            DimensionResult(WiredoDimension.INHERITED, True, "OK"),
        ],
    )
    assert r.passed is False
    assert r.fail_count == 1


def test_validation_result_na_counts_as_pass():
    r = ValidationResult(
        asset_type=AssetType.IMAGE,
        asset_path="ok.png",
        dimensions=[
            DimensionResult(WiredoDimension.WIRED, True, "OK"),
            DimensionResult(WiredoDimension.OBSERVABLE, False, "N/A", na=True),
        ],
    )
    assert r.passed is True


def test_validation_result_to_table():
    r = ValidationResult(
        asset_type=AssetType.CODE,
        asset_path="test.py",
        dimensions=[
            DimensionResult(WiredoDimension.WIRED, True, "Imported by main"),
        ],
    )
    table = r.to_table()
    assert "W" in table
    assert "Imported by main" in table


# ── AssetValidator ───────────────────────────────────────────


def test_validator_code(tmp_path):
    v = AssetValidator(workspace=str(tmp_path))
    # Create a dummy file
    f = tmp_path / "test.py"
    f.write_text("print('hello')")
    result = v.validate(str(f), AssetType.CODE)
    assert result.passed  # Code validators are manual-check stubs


def test_validator_document(tmp_path):
    v = AssetValidator(workspace=str(tmp_path))
    f = tmp_path / "doc.md"
    f.write_text("# Title\nContent here")
    result = v.validate(str(f), AssetType.DOCUMENT)
    # Should pass basic checks
    assert result.asset_type == AssetType.DOCUMENT
    # Defended should pass (file exists)
    d_dim = [d for d in result.dimensions if d.dimension == WiredoDimension.DEFENDED][0]
    assert d_dim.passed


def test_validator_document_empty_file(tmp_path):
    v = AssetValidator(workspace=str(tmp_path))
    f = tmp_path / "empty.md"
    f.write_text("")
    result = v.validate(str(f), AssetType.DOCUMENT)
    d_dim = [d for d in result.dimensions if d.dimension == WiredoDimension.DEFENDED][0]
    assert d_dim.passed is False  # Empty file


def test_validator_image_in_tmp():
    v = AssetValidator(workspace="")
    result = v.validate("/tmp/orphan.png", AssetType.IMAGE)
    w_dim = [d for d in result.dimensions if d.dimension == WiredoDimension.WIRED][0]
    assert w_dim.passed is False


def test_validator_skip_dimensions():
    v = AssetValidator(workspace="")
    skip = frozenset({WiredoDimension.OBSERVABLE})
    result = v.validate("test.py", AssetType.CODE, skip_dimensions=skip)
    o_dim = [d for d in result.dimensions if d.dimension == WiredoDimension.OBSERVABLE][0]
    assert o_dim.na is True
    assert "Skipped" in o_dim.evidence


def test_validator_nonexistent_file():
    v = AssetValidator(workspace="")
    result = v.validate("/nonexistent/file.md", AssetType.DOCUMENT)
    d_dim = [d for d in result.dimensions if d.dimension == WiredoDimension.DEFENDED][0]
    assert d_dim.passed is False


# ── ProjectCascade ───────────────────────────────────────────


def test_cascade_basic():
    cascade = ProjectCascade(
        workspace="",
        stack={"psyche": ["infinite-agent"]},
    )
    result = cascade.validate_project(
        "infinite-agent",
        own_assets=[("agent.ts", AssetType.CODE)],
    )
    assert result.project == "infinite-agent"
    assert len(result.own_results) == 1
    assert len(result.inherited_from) == 0


def test_cascade_inherits_dep_results():
    cascade = ProjectCascade(
        workspace="",
        stack={"psyche": ["infinite-agent"]},
    )
    # First validate the dep
    cascade.validate_project(
        "infinite-agent",
        own_assets=[("agent.ts", AssetType.CODE)],
    )
    # Then validate psyche — should inherit
    result = cascade.validate_project(
        "psyche",
        own_assets=[("app.tsx", AssetType.CODE)],
    )
    assert "infinite-agent" in result.inherited_from
    assert len(result.inherited_from["infinite-agent"]) == 1


def test_cascade_with_dep_assets():
    cascade = ProjectCascade(
        workspace="",
        stack={"psyche": ["infinite-agent"]},
    )
    result = cascade.validate_project(
        "psyche",
        own_assets=[("app.tsx", AssetType.CODE)],
        dep_assets={"infinite-agent": [("agent.ts", AssetType.CODE)]},
    )
    assert "infinite-agent" in result.inherited_from


def test_cascade_cache():
    cascade = ProjectCascade(
        workspace="",
        stack={"psyche": ["infinite-agent"], "aegis": ["infinite-agent"]},
    )
    cascade.validate_project(
        "infinite-agent",
        own_assets=[("agent.ts", AssetType.CODE)],
    )
    cached = cascade.get_cached("infinite-agent")
    assert cached is not None
    assert len(cached) == 1


def test_cascade_clear_cache():
    cascade = ProjectCascade(workspace="", stack={})
    cascade.validate_project("test", own_assets=[("x.py", AssetType.CODE)])
    assert cascade.get_cached("test") is not None
    cascade.clear_cache()
    assert cascade.get_cached("test") is None


def test_cascade_summary_table():
    cascade = ProjectCascade(
        workspace="",
        stack={"psyche": ["infinite-agent"]},
    )
    cascade.validate_project("infinite-agent", own_assets=[("agent.ts", AssetType.CODE)])
    result = cascade.validate_project("psyche", own_assets=[("app.tsx", AssetType.CODE)])
    table = result.summary_table()
    assert "infinite-agent" in table
    assert "psyche" in table


# ── Config integration ───────────────────────────────────────


def test_config_defaults(tmp_path):
    cfg = load_wiredo_config(str(tmp_path))
    assert cfg["enabled"] is True
    for t in ["code", "image", "video", "audio", "document", "media"]:
        assert cfg["asset_types"][t] is True


def test_config_partial_override(tmp_path):
    config_dir = tmp_path / ".claude" / "hooks"
    config_dir.mkdir(parents=True)
    (config_dir / "cc_config.json").write_text(
        json.dumps({"wiredo": {"asset_types": {"video": False, "audio": False}}}),
        encoding="utf-8",
    )
    cfg = load_wiredo_config(str(tmp_path))
    assert cfg["asset_types"]["video"] is False
    assert cfg["asset_types"]["audio"] is False
    assert cfg["asset_types"]["code"] is True  # Untouched default


def test_config_backward_compat_wiredo_enabled(tmp_path):
    config_dir = tmp_path / ".claude" / "hooks"
    config_dir.mkdir(parents=True)
    (config_dir / "cc_config.json").write_text(
        json.dumps({"wiredo_enabled": False}), encoding="utf-8"
    )
    cfg = load_wiredo_config(str(tmp_path))
    assert cfg["enabled"] is False


def test_config_new_takes_precedence(tmp_path):
    config_dir = tmp_path / ".claude" / "hooks"
    config_dir.mkdir(parents=True)
    (config_dir / "cc_config.json").write_text(
        json.dumps({"wiredo_enabled": False, "wiredo": {"enabled": True}}),
        encoding="utf-8",
    )
    cfg = load_wiredo_config(str(tmp_path))
    assert cfg["enabled"] is True  # New wiredo.enabled takes precedence
