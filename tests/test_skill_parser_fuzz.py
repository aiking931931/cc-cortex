"""Fuzz round-trip tests for concinno.skill_parser.parse_skill_md.

Hardened parser must tolerate malformed third-party-plugin SKILL.md
input without crashing. 15+ parametrised cases cover BOM, CRLF, block
lists, truthy tokens, missing closing fence, etc.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from concinno.skill_parser import parse_skill_md


def _write(tmp_path: Path, text: str, *, encoding: str = "utf-8",
           as_bytes: bytes | None = None) -> Path:
    p = tmp_path / "SKILL.md"
    if as_bytes is not None:
        p.write_bytes(as_bytes)
    else:
        p.write_text(text, encoding=encoding)
    return p


class TestValidCases:
    def test_minimal_inline_list(self, tmp_path):
        path = _write(tmp_path, "---\nname: s\ntriggers: [a, b]\n---\nbody\n")
        meta = parse_skill_md(path)
        assert meta["name"] == "s"
        assert meta["triggers"] == ["a", "b"]

    def test_block_list_triggers(self, tmp_path):
        path = _write(
            tmp_path,
            "---\nname: s\ntriggers:\n  - a\n  - b\n  - c\n---\nbody\n",
        )
        meta = parse_skill_md(path)
        assert meta["triggers"] == ["a", "b", "c"]

    def test_user_invocable_truthy_tokens(self, tmp_path):
        for val, expected in [
            ("true", True), ("True", True), ("yes", True), ("on", True),
            ("false", False), ("no", False), ("off", False), ("0", False),
            ("1", True),
        ]:
            path = _write(tmp_path, f"---\nname: s\nuser-invocable: {val}\n---\n")
            meta = parse_skill_md(path)
            assert meta.get("user-invocable") is expected, (
                f"{val!r} -> {meta.get('user-invocable')!r}, expected {expected}"
            )

    def test_long_description(self, tmp_path):
        desc = "x" * 800
        path = _write(tmp_path, f"---\nname: s\ndescription: {desc}\n---\n")
        meta = parse_skill_md(path)
        assert meta["description"] == desc

    def test_quoted_value_strips_quotes(self, tmp_path):
        path = _write(tmp_path, '---\nname: "my skill"\n---\n')
        meta = parse_skill_md(path)
        assert meta["name"] == "my skill"

    def test_single_quoted_value(self, tmp_path):
        path = _write(tmp_path, "---\nname: 'my skill'\n---\n")
        meta = parse_skill_md(path)
        assert meta["name"] == "my skill"

    def test_unicode_name_preserved(self, tmp_path):
        path = _write(tmp_path, "---\nname: 你好\n---\n")
        meta = parse_skill_md(path)
        assert meta["name"] == "你好"

    def test_emoji_in_description(self, tmp_path):
        path = _write(tmp_path, "---\nname: s\ndescription: hi 🚀\n---\n")
        meta = parse_skill_md(path)
        assert "🚀" in meta["description"]


class TestMalformationTolerance:
    def test_bom_prefix_tolerated(self, tmp_path):
        path = _write(tmp_path, "", as_bytes=b"\xef\xbb\xbf---\nname: s\n---\n")
        meta = parse_skill_md(path)
        assert meta["name"] == "s"

    def test_crlf_line_endings(self, tmp_path):
        path = _write(tmp_path, "", as_bytes=b"---\r\nname: s\r\ntriggers: [a]\r\n---\r\n")
        meta = parse_skill_md(path)
        assert meta["name"] == "s"
        assert meta["triggers"] == ["a"]

    def test_missing_closing_fence_recovers(self, tmp_path):
        path = _write(tmp_path, "---\nname: s\ndescription: d\nbody goes here")
        meta = parse_skill_md(path)
        # Recovers whatever it can before EOF.
        assert meta.get("name") == "s"

    def test_only_opening_fence(self, tmp_path):
        path = _write(tmp_path, "---\n")
        meta = parse_skill_md(path)
        # Just open with no content -> empty dict (no crash).
        assert isinstance(meta, dict)

    def test_empty_file(self, tmp_path):
        path = _write(tmp_path, "")
        meta = parse_skill_md(path)
        assert meta == {}

    def test_only_frontmatter_no_body(self, tmp_path):
        path = _write(tmp_path, "---\nname: s\n---")
        meta = parse_skill_md(path)
        assert meta["name"] == "s"

    def test_only_body_no_frontmatter(self, tmp_path):
        path = _write(tmp_path, "# just a body\nno fence here\n")
        meta = parse_skill_md(path)
        assert meta == {}

    def test_unreadable_path_returns_empty(self, tmp_path):
        nonexistent = tmp_path / "does_not_exist.md"
        meta = parse_skill_md(nonexistent)
        assert meta == {}

    def test_colon_in_value(self, tmp_path):
        path = _write(tmp_path, "---\nname: s\ndescription: a: b: c\n---\n")
        meta = parse_skill_md(path)
        # partition() takes first ':' so rest stays as value.
        assert meta["description"] == "a: b: c"

    def test_blank_lines_inside_frontmatter(self, tmp_path):
        path = _write(tmp_path, "---\nname: s\n\ndescription: d\n\n---\n")
        meta = parse_skill_md(path)
        assert meta["name"] == "s"
        assert meta["description"] == "d"

    def test_malformed_inline_list(self, tmp_path):
        path = _write(tmp_path, "---\nname: s\ntriggers: [a,,b,]\n---\n")
        meta = parse_skill_md(path)
        # Strips empty items.
        assert meta["triggers"] == ["a", "b"]


class TestRoundtripSurvives:
    """Synthesising and re-parsing should preserve critical fields."""

    @pytest.mark.parametrize("payload", [
        {"name": "a", "description": "x"},
        {"name": "b", "triggers": ["t1", "t2"]},
        {"name": "c", "user-invocable": True, "scope": "user"},
    ])
    def test_roundtrip(self, tmp_path, payload):
        lines = ["---"]
        for k, v in payload.items():
            if isinstance(v, list):
                lines.append(f"{k}: [{', '.join(v)}]")
            elif isinstance(v, bool):
                lines.append(f"{k}: {str(v).lower()}")
            else:
                lines.append(f"{k}: {v}")
        lines.append("---")
        lines.append("body")
        path = _write(tmp_path, "\n".join(lines) + "\n")
        meta = parse_skill_md(path)
        for k, v in payload.items():
            assert meta[k] == v, f"{k}: {meta[k]!r} != {v!r}"
