"""Tests for cc_cortex.handoff_validator."""

from cc_cortex.handoff_validator import (
    format_report,
    parse_frontmatter,
    validate_dir,
    validate_file,
)


class TestParseFrontmatter:
    def test_valid_frontmatter(self):
        content = "---\nstatus: active\nverified: true\nlast_updated: 2026-01-01\n---\n# Title"
        fields, rest = parse_frontmatter(content)
        assert fields is not None
        assert fields["status"] == "active"
        assert rest.strip() == "# Title"

    def test_missing_frontmatter(self):
        content = "# No frontmatter here"
        fields, rest = parse_frontmatter(content)
        assert fields is None
        assert rest == content

    def test_partial_frontmatter(self):
        content = "---\nstatus: active\n---\n# Title"
        fields, rest = parse_frontmatter(content)
        assert fields is not None
        assert "status" in fields


class TestValidateFile:
    def test_valid_file(self, tmp_path):
        f = tmp_path / "交接_test.md"
        f.write_text(
            "---\nstatus: active\nverified: false\nlast_updated: 2026-01-01\n---\n# Test",
            encoding="utf-8",
        )
        result = validate_file(str(f))
        assert result.ok

    def test_missing_field(self, tmp_path):
        f = tmp_path / "交接_test.md"
        f.write_text("---\nstatus: active\n---\n# Test", encoding="utf-8")
        result = validate_file(str(f))
        assert len(result.errors) > 0

    def test_invalid_status(self, tmp_path):
        f = tmp_path / "交接_test.md"
        f.write_text(
            "---\nstatus: bogus\nverified: false\nlast_updated: 2026-01-01\n---\n",
            encoding="utf-8",
        )
        result = validate_file(str(f))
        assert any("INVALID" in e for e in result.errors)

    def test_fix_missing_frontmatter(self, tmp_path):
        f = tmp_path / "交接_test.md"
        f.write_text("# No frontmatter", encoding="utf-8")
        result = validate_file(str(f), fix=True)
        assert result.fixed
        content = f.read_text(encoding="utf-8")
        assert content.startswith("---")

    def test_fix_missing_field(self, tmp_path):
        f = tmp_path / "交接_test.md"
        f.write_text("---\nstatus: active\n---\n# Title", encoding="utf-8")
        result = validate_file(str(f), fix=True)
        assert result.fixed

    def test_hard_pending_no_priority(self, tmp_path):
        f = tmp_path / "交接_test.md"
        f.write_text(
            "---\nstatus: active\nverified: false\nlast_updated: 2026-01-01\n---\n"
            "## Tasks\n⬜ Something to do\n",
            encoding="utf-8",
        )
        result = validate_file(str(f))
        assert any("dispatch" in w for w in result.warnings)

    def test_hard_pending_with_priority_ok(self, tmp_path):
        f = tmp_path / "交接_test.md"
        f.write_text(
            "---\nstatus: active\nverified: false\nlast_updated: 2026-01-01\n---\n"
            "## P1 Tasks\n⬜ Something\n",
            encoding="utf-8",
        )
        result = validate_file(str(f))
        assert not result.warnings

    def test_sub_handoff_exempt(self, tmp_path):
        f = tmp_path / "交接_test_P2-1.md"
        f.write_text(
            "---\nstatus: active\nverified: false\nlast_updated: 2026-01-01\n---\n"
            "⬜ Pending API deploy task\n",
            encoding="utf-8",
        )
        result = validate_file(str(f))
        assert not result.warnings


class TestValidateDir:
    def test_finds_files(self, tmp_path):
        sub = tmp_path / "project"
        sub.mkdir()
        f = sub / "交接_foo.md"
        f.write_text(
            "---\nstatus: active\nverified: false\nlast_updated: 2026-01-01\n---\n# Foo",
            encoding="utf-8",
        )
        results = validate_dir(str(tmp_path), pattern="*/交接_*.md")
        assert len(results) == 1
        assert results[0].ok

    def test_empty_dir(self, tmp_path):
        results = validate_dir(str(tmp_path))
        assert results == []


class TestFormatReport:
    def test_format(self, tmp_path):
        sub = tmp_path / "project"
        sub.mkdir()
        f = sub / "交接_foo.md"
        f.write_text("# No frontmatter", encoding="utf-8")
        results = validate_dir(str(tmp_path), pattern="*/交接_*.md")
        text = format_report(results)
        assert "ERR" in text
        assert "Files: 1" in text
