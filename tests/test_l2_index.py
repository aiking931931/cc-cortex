"""Tests for concinno.l2_index — L2 frontmatter walker + reverse index.

Sub-agent K wave-2 (4.4.0). Plan v1 line 64.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from concinno.l2_index import (
    REVERSE_INDEX_VERSION,
    L2Frontmatter,
    build_reverse_index,
    default_skill_roots,
    default_triggers_json_path,
    main,
    parse_frontmatter,
    query_trigger,
    read_triggers_json,
    walk_skills,
    write_triggers_json,
)

# ── parse_frontmatter ───────────────────────────────────────────────


def test_parse_frontmatter_full_block() -> None:
    raw = """---
name: my-skill
description: A test skill
triggers:
  - foo
  - bar
  - baz
category: test
last_used: 2026-04-28T00:00:00Z
---

# Body
"""
    fm = parse_frontmatter(raw, source_path="/tmp/x.md")
    assert fm.name == "my-skill"
    assert fm.triggers == ("foo", "bar", "baz")
    assert fm.category == "test"
    assert fm.last_used == "2026-04-28T00:00:00Z"
    assert fm.description == "A test skill"
    assert fm.is_valid


def test_parse_frontmatter_inline_list() -> None:
    raw = """---
name: inline-list
triggers: [a, b, c]
---
"""
    fm = parse_frontmatter(raw)
    assert fm.triggers == ("a", "b", "c")


def test_parse_frontmatter_no_frontmatter() -> None:
    fm = parse_frontmatter("# Just a body\n\nText here.")
    assert fm.name == ""
    assert fm.triggers == ()
    assert not fm.is_valid


def test_parse_frontmatter_unclosed_block() -> None:
    raw = "---\nname: x\ntriggers:\n  - a\n"
    fm = parse_frontmatter(raw)
    assert fm.name == ""
    assert not fm.is_valid


def test_parse_frontmatter_extra_keys_preserved() -> None:
    raw = """---
name: with-extras
triggers:
  - go
custom_key: custom_value
another_key: 42
---
"""
    fm = parse_frontmatter(raw)
    assert fm.name == "with-extras"
    assert fm.extra.get("custom_key") == "custom_value"
    assert fm.extra.get("another_key") == "42"


def test_parse_frontmatter_quoted_values() -> None:
    raw = '''---
name: "quoted-name"
description: 'single quoted'
triggers:
  - "first"
  - 'second'
---
'''
    fm = parse_frontmatter(raw)
    assert fm.name == "quoted-name"
    assert fm.description == "single quoted"
    assert fm.triggers == ("first", "second")


# ── L2Frontmatter dataclass ────────────────────────────────────────


def test_l2_frontmatter_default_invalid() -> None:
    fm = L2Frontmatter()
    assert not fm.is_valid


def test_l2_frontmatter_name_only_invalid() -> None:
    fm = L2Frontmatter(name="x", triggers=())
    assert not fm.is_valid


def test_l2_frontmatter_with_triggers_valid() -> None:
    fm = L2Frontmatter(name="x", triggers=("t",))
    assert fm.is_valid


# ── build_reverse_index ─────────────────────────────────────────────


def test_build_reverse_index_basic() -> None:
    entries = [
        L2Frontmatter(name="alpha", triggers=("foo", "bar")),
        L2Frontmatter(name="beta", triggers=("foo", "qux")),
    ]
    rev = build_reverse_index(entries)
    assert rev["foo"] == ["alpha", "beta"]
    assert rev["bar"] == ["alpha"]
    assert rev["qux"] == ["beta"]


def test_build_reverse_index_dedup_within_skill() -> None:
    entries = [
        L2Frontmatter(name="alpha", triggers=("foo", "FOO ", " foo  ")),
    ]
    rev = build_reverse_index(entries)
    # Normalised to single key, single skill in bucket.
    assert rev["foo"] == ["alpha"]


def test_build_reverse_index_skips_invalid() -> None:
    entries = [
        L2Frontmatter(name="", triggers=("orphan",)),
        L2Frontmatter(name="valid", triggers=()),
    ]
    rev = build_reverse_index(entries)
    assert rev == {}


def test_build_reverse_index_normalises_case() -> None:
    entries = [
        L2Frontmatter(name="alpha", triggers=("Handoff",)),
        L2Frontmatter(name="beta", triggers=("HANDOFF",)),
    ]
    rev = build_reverse_index(entries)
    assert rev["handoff"] == ["alpha", "beta"]


# ── walk_skills ─────────────────────────────────────────────────────


def test_walk_skills_returns_synthetic_skills(tmp_path: Path) -> None:
    skill_dir = tmp_path / "my-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        """---
name: my-skill
triggers:
  - alpha
  - beta
---
""",
        encoding="utf-8",
    )
    entries = walk_skills([tmp_path])
    assert len(entries) == 1
    assert entries[0].name == "my-skill"
    assert entries[0].triggers == ("alpha", "beta")


def test_walk_skills_falls_back_to_dirname(tmp_path: Path) -> None:
    skill_dir = tmp_path / "fallback-name"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        """---
triggers:
  - xx
---
""",
        encoding="utf-8",
    )
    entries = walk_skills([tmp_path])
    assert len(entries) == 1
    assert entries[0].name == "fallback-name"


def test_walk_skills_missing_root_silent(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    entries = walk_skills([missing])
    assert entries == []


def test_walk_skills_skips_quarantine(tmp_path: Path) -> None:
    quarantined = tmp_path / "_quarantine" / "junk"
    quarantined.mkdir(parents=True)
    (quarantined / "SKILL.md").write_text(
        "---\nname: junk\ntriggers:\n  - x\n---\n",
        encoding="utf-8",
    )
    live = tmp_path / "live"
    live.mkdir()
    (live / "SKILL.md").write_text(
        "---\nname: live\ntriggers:\n  - y\n---\n",
        encoding="utf-8",
    )
    entries = walk_skills([tmp_path], skip_quarantine=True)
    names = {e.name for e in entries}
    assert "live" in names
    assert "junk" not in names


# ── default roots ─────────────────────────────────────────────────


def test_default_skill_roots_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    monkeypatch.setenv(
        "CONCINNO_SKILL_ROOTS",
        os.pathsep.join(["/tmp/a", "/tmp/b"]),
    )
    roots = default_skill_roots()
    assert any(str(r) == str(Path("/tmp/a")) for r in roots)
    assert any(str(r) == str(Path("/tmp/b")) for r in roots)


def test_default_triggers_json_path_env_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "custom.json"
    monkeypatch.setenv("CONCINNO_TRIGGERS_JSON_PATH", str(target))
    assert default_triggers_json_path() == target


# ── persistence (write + read + query) ───────────────────────────────


def test_write_triggers_json_round_trip(tmp_path: Path) -> None:
    rev = {"foo": ["alpha"], "bar": ["beta", "alpha"]}
    target = tmp_path / "out.json"
    written = write_triggers_json(rev, path=target, skills_scanned=42)
    assert written == target
    assert written.exists()
    payload = json.loads(written.read_text(encoding="utf-8"))
    assert payload["version"] == REVERSE_INDEX_VERSION
    assert payload["skills_scanned"] == 42
    assert payload["trigger_to_skills"]["foo"] == ["alpha"]


def test_read_triggers_json_missing_returns_empty(tmp_path: Path) -> None:
    payload = read_triggers_json(path=tmp_path / "nope.json")
    assert payload == {}


def test_read_triggers_json_corrupt_returns_empty(tmp_path: Path) -> None:
    target = tmp_path / "corrupt.json"
    target.write_text("not valid json {", encoding="utf-8")
    assert read_triggers_json(path=target) == {}


def test_query_trigger_normalises(tmp_path: Path) -> None:
    rev = {"handoff": ["alpha", "beta"]}
    target = tmp_path / "q.json"
    write_triggers_json(rev, path=target, skills_scanned=2)
    assert query_trigger("Handoff", path=target) == ["alpha", "beta"]
    assert query_trigger("  HANDOFF  ", path=target) == ["alpha", "beta"]
    assert query_trigger("missing", path=target) == []


# ── CLI ───────────────────────────────────────────────────────────


def test_cli_build_then_query(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Set up a fake skill root.
    skill_root = tmp_path / "skills"
    skill_root.mkdir()
    s1 = skill_root / "alpha"
    s1.mkdir()
    (s1 / "SKILL.md").write_text(
        "---\nname: alpha\ntriggers:\n  - foo\n  - bar\n---\n",
        encoding="utf-8",
    )
    s2 = skill_root / "beta"
    s2.mkdir()
    (s2 / "SKILL.md").write_text(
        "---\nname: beta\ntriggers:\n  - foo\n---\n",
        encoding="utf-8",
    )

    out_path = tmp_path / "triggers.json"

    # build
    rc = main(["build", "--root", str(skill_root), "--out", str(out_path)])
    assert rc == 0
    assert out_path.exists()
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload["skills_scanned"] == 2

    # query — found
    rc = main(["query", "foo", "--in", str(out_path)])
    out, _ = capsys.readouterr()
    assert rc == 0
    assert "alpha" in out
    assert "beta" in out

    # query — missed
    rc = main(["query", "missing", "--in", str(out_path)])
    assert rc == 1
