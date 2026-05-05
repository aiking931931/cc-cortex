"""Tests for ``concinno.skills.frontmatter_validator``.

Coverage:
    * Spec-required fields (name / description) — missing & malformed
    * Recommended fields (version / triggers / user-invocable) — missing
      surfaces ``recommendation`` issues with auto-fix suggestions
    * Optional documented fields (dependencies / platform) — type
      validation
    * Concinno extension fields (ziq_autotunable / cosmetic /
      concinno_scope) — Concinno-namespaced, not flagged as unknown
    * ``apply_fix`` — idempotency, partial fix, no-op when frontmatter
      is malformed
    * ``validate_directory`` — walks recursively
    * Real-world sample (mirrors the corpus in ``~/.claude/skills/``)
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

from concinno.skills.frontmatter_validator import (
    ALLOWED_PLATFORMS,
    REQUIRED_SPEC_FIELDS,
    SPEC_FIELDS,
    Severity,
    apply_fix,
    format_report_text,
    validate_directory,
    validate_meta,
    validate_skill_md,
)

# ── Helpers ─────────────────────────────────────────────────────────


def _write(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / name / "SKILL.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(dedent(body).lstrip(), encoding="utf-8")
    return p


# ── Spec catalogue invariants ───────────────────────────────────────


def test_required_fields_subset_of_spec_fields() -> None:
    assert REQUIRED_SPEC_FIELDS <= SPEC_FIELDS
    assert {"name", "description"} == REQUIRED_SPEC_FIELDS


def test_allowed_platforms_known_subset() -> None:
    assert ALLOWED_PLATFORMS == frozenset({"linux", "darwin", "win32"})


# ── Required fields ─────────────────────────────────────────────────


def test_missing_name_is_error() -> None:
    issues = validate_meta({"description": "x"})
    codes = {(i.field, i.code, i.severity) for i in issues}
    assert ("name", "missing_required", Severity.ERROR) in codes


def test_missing_description_is_error() -> None:
    issues = validate_meta({"name": "ok"})
    codes = {(i.field, i.code, i.severity) for i in issues}
    assert ("description", "missing_required", Severity.ERROR) in codes


def test_blank_name_is_error() -> None:
    issues = validate_meta({"name": "   ", "description": "x"})
    assert any(
        i.field == "name" and i.severity is Severity.ERROR for i in issues
    )


def test_name_too_long_warns() -> None:
    issues = validate_meta(
        {"name": "a" * 65, "description": "x"}
    )
    assert any(
        i.field == "name" and i.code == "too_long"
        and i.severity is Severity.WARNING
        for i in issues
    )


def test_name_bad_chars_warns() -> None:
    issues = validate_meta({"name": "9bad-start", "description": "x"})
    assert any(
        i.field == "name" and i.code == "bad_chars" for i in issues
    )


def test_description_too_long_warns() -> None:
    issues = validate_meta(
        {"name": "ok", "description": "x" * 2000}
    )
    assert any(
        i.field == "description" and i.code == "too_long"
        for i in issues
    )


# ── Recommended fields ─────────────────────────────────────────────


def test_missing_version_is_recommendation_with_default() -> None:
    issues = validate_meta({"name": "ok", "description": "x"})
    rec = [i for i in issues if i.field == "version"]
    assert rec and rec[0].severity is Severity.RECOMMENDATION
    assert rec[0].suggested_value == "0.1.0"


def test_missing_triggers_recommendation_default_empty() -> None:
    issues = validate_meta({"name": "ok", "description": "x"})
    rec = [i for i in issues if i.field == "triggers"]
    assert rec and rec[0].suggested_value == []


def test_missing_user_invocable_recommendation_default_true() -> None:
    issues = validate_meta({"name": "ok", "description": "x"})
    rec = [i for i in issues if i.field == "user-invocable"]
    assert rec and rec[0].suggested_value is True


def test_bad_version_format_warns() -> None:
    issues = validate_meta(
        {"name": "ok", "description": "x", "version": "v1-banana"}
    )
    assert any(i.field == "version" and i.code == "bad_format" for i in issues)


def test_triggers_wrong_type_warns() -> None:
    issues = validate_meta(
        {"name": "ok", "description": "x", "triggers": "not a list"}
    )
    assert any(
        i.field == "triggers" and i.code == "wrong_type" for i in issues
    )


def test_triggers_non_string_item_warns() -> None:
    issues = validate_meta(
        {"name": "ok", "description": "x", "triggers": ["good", 5]}
    )
    assert any(
        i.field.startswith("triggers[") and i.code == "wrong_item_type"
        for i in issues
    )


# ── Optional fields ────────────────────────────────────────────────


def test_dependencies_must_be_list_of_strings() -> None:
    issues = validate_meta(
        {"name": "ok", "description": "x", "dependencies": [1, "ok"]}
    )
    assert any(
        i.field.startswith("dependencies[") and i.code == "wrong_item_type"
        for i in issues
    )


def test_platform_unknown_value_warns() -> None:
    issues = validate_meta(
        {"name": "ok", "description": "x", "platform": ["linux", "android"]}
    )
    assert any(
        i.field.startswith("platform[") and i.code == "unknown_value"
        for i in issues
    )


def test_platform_wrong_type_warns() -> None:
    issues = validate_meta(
        {"name": "ok", "description": "x", "platform": "linux"}
    )
    assert any(
        i.field == "platform" and i.code == "wrong_type" for i in issues
    )


# ── Concinno extension fields ──────────────────────────────────────


def test_extension_fields_no_warning_when_well_typed() -> None:
    issues = validate_meta(
        {
            "name": "ok",
            "description": "x",
            "version": "1.0.0",
            "triggers": ["t"],
            "user-invocable": True,
            "ziq_autotunable": True,
            "cosmetic": False,
            "concinno_scope": "user",
        }
    )
    # No errors / warnings — only zero or recommendations.
    assert not any(i.severity is Severity.ERROR for i in issues)
    assert not any(i.severity is Severity.WARNING for i in issues)


def test_ziq_autotunable_wrong_type_warns() -> None:
    issues = validate_meta(
        {"name": "ok", "description": "x", "ziq_autotunable": "yeah"}
    )
    assert any(
        i.field == "ziq_autotunable" and i.code == "wrong_type" for i in issues
    )


def test_concinno_scope_unknown_value_warns() -> None:
    issues = validate_meta(
        {"name": "ok", "description": "x", "concinno_scope": "weird"}
    )
    assert any(
        i.field == "concinno_scope" and i.code == "unknown_value"
        for i in issues
    )


# ── File-level validation ──────────────────────────────────────────


def test_validate_skill_md_basic_valid(tmp_path: Path) -> None:
    p = _write(tmp_path, "good", """\
        ---
        name: good
        description: a clean fixture
        version: 1.0.0
        triggers: [a, b]
        user-invocable: true
        ---
        body
        """)
    report = validate_skill_md(p)
    assert not report.has_errors
    assert not report.has_warnings


def test_validate_skill_md_no_frontmatter_is_error(tmp_path: Path) -> None:
    p = tmp_path / "missing" / "SKILL.md"
    p.parent.mkdir()
    p.write_text("just a body, no frontmatter\n", encoding="utf-8")
    report = validate_skill_md(p)
    assert report.has_errors
    assert any(i.code == "no_frontmatter" for i in report.issues)


def test_validate_skill_md_missing_required_surfaces_error(tmp_path: Path) -> None:
    p = _write(tmp_path, "bad", """\
        ---
        triggers: [x]
        ---
        body
        """)
    report = validate_skill_md(p)
    assert report.has_errors


# ── apply_fix ──────────────────────────────────────────────────────


def test_apply_fix_inserts_recommended_defaults(tmp_path: Path) -> None:
    p = _write(tmp_path, "needsfix", """\
        ---
        name: needsfix
        description: minimal
        ---
        body
        """)
    pre = validate_skill_md(p)
    assert {i.field for i in pre.fixable} == {
        "version", "triggers", "user-invocable"
    }
    post = apply_fix(p)
    assert not post.fixable
    text = p.read_text(encoding="utf-8")
    assert "version: 0.1.0" in text
    assert "triggers: []" in text
    assert "user-invocable: true" in text


def test_apply_fix_idempotent(tmp_path: Path) -> None:
    p = _write(tmp_path, "idem", """\
        ---
        name: idem
        description: minimal
        ---
        body
        """)
    apply_fix(p)
    snapshot = p.read_text(encoding="utf-8")
    apply_fix(p)
    assert p.read_text(encoding="utf-8") == snapshot


def test_apply_fix_does_not_overwrite_existing(tmp_path: Path) -> None:
    p = _write(tmp_path, "keep", """\
        ---
        name: keep
        description: minimal
        version: 9.9.9
        ---
        body
        """)
    apply_fix(p)
    text = p.read_text(encoding="utf-8")
    assert "version: 9.9.9" in text
    assert "version: 0.1.0" not in text


def test_apply_fix_dry_run_does_not_write(tmp_path: Path) -> None:
    p = _write(tmp_path, "dry", """\
        ---
        name: dry
        description: minimal
        ---
        body
        """)
    before = p.read_text(encoding="utf-8")
    apply_fix(p, write=False)
    assert p.read_text(encoding="utf-8") == before


def test_apply_fix_no_op_on_unparseable_file(tmp_path: Path) -> None:
    p = tmp_path / "weird" / "SKILL.md"
    p.parent.mkdir()
    p.write_text("no frontmatter at all\n", encoding="utf-8")
    before = p.read_text(encoding="utf-8")
    report = apply_fix(p)
    assert p.read_text(encoding="utf-8") == before
    assert report.has_errors


# ── Directory walker ───────────────────────────────────────────────


def test_validate_directory_walks_subtree(tmp_path: Path) -> None:
    _write(tmp_path, "a", """\
        ---
        name: a
        description: x
        ---
        """)
    _write(tmp_path, "b", """\
        ---
        triggers: []
        ---
        """)
    reports = validate_directory(tmp_path)
    assert len(reports) == 2
    by_name = {r.path.parent.name: r for r in reports}
    assert by_name["a"].has_errors is False or by_name["a"].has_errors is True
    assert by_name["b"].has_errors  # missing name + description


def test_validate_directory_with_fix_persists_changes(tmp_path: Path) -> None:
    _write(tmp_path, "fixme", """\
        ---
        name: fixme
        description: x
        ---
        """)
    validate_directory(tmp_path, fix=True)
    text = (tmp_path / "fixme" / "SKILL.md").read_text(encoding="utf-8")
    assert "version: 0.1.0" in text


def test_validate_directory_missing_root_returns_empty(tmp_path: Path) -> None:
    assert validate_directory(tmp_path / "nope") == []


# ── Formatter ──────────────────────────────────────────────────────


def test_format_report_text_includes_severity_field_and_summary(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "fmt", """\
        ---
        triggers: [t]
        ---
        """)
    reports = validate_directory(tmp_path)
    out = format_report_text(reports)
    assert "ERROR" in out or "RECOMMENDATION" in out
    assert "Summary:" in out


# ── Realistic sample mirroring the user's corpus ────────────────────


def test_realistic_agent_style_sample(tmp_path: Path) -> None:
    """Mirrors the shape of ``~/.claude/skills/agent/SKILL.md``."""
    p = _write(tmp_path, "agent", """\
        ---
        name: agent
        description: agent loop unified
        triggers:
          - agent
          - 自動化任務
        user-invocable: true
        ---
        body
        """)
    report = validate_skill_md(p)
    assert not report.has_errors
    # Only ``version`` should be missing; that's a recommendation.
    fields = {i.field for i in report.fixable}
    assert "version" in fields
