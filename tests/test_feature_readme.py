"""Tests for concinno.feature_readme — README sync + sort invariant."""

from __future__ import annotations

import textwrap

from concinno import feature_readme


def test_render_markdown_has_header_and_rows():
    md = feature_readme.render_markdown()
    assert "| Feature | Category | Effect scope | ZIQ-tunable | Description |" in md
    # at least the 8 GAIA switches
    assert md.count("\n") >= 10


def test_sort_order_matches_gui_default():
    """``SORT_KEY`` must produce identical ordering to the GUI's
    default `category-name` sort."""
    features = [
        ("zzz", {"category": "a"}),
        ("alpha", {"category": "b"}),
        ("beta", {"category": "a"}),
    ]
    ordered = sorted(features, key=feature_readme.SORT_KEY)
    assert [n for n, _ in ordered] == ["beta", "zzz", "alpha"]


def test_sync_readme_creates_block(tmp_path):
    readme = tmp_path / "README.md"
    readme.write_text("# Hello\n\n## CLI\n", encoding="utf-8")
    changed = feature_readme.sync_readme(readme)
    assert changed is True
    text = readme.read_text(encoding="utf-8")
    assert feature_readme.ANCHOR_BEGIN in text
    assert feature_readme.ANCHOR_END in text
    assert "## Feature Switches" in text
    # anchors must come before ## CLI
    assert text.index(feature_readme.ANCHOR_END) < text.index("## CLI")


def test_sync_readme_replaces_existing_block(tmp_path):
    readme = tmp_path / "README.md"
    readme.write_text(
        textwrap.dedent(f"""\
        # Hello

        ## Feature Switches

        {feature_readme.ANCHOR_BEGIN}
        stale content that must be replaced
        {feature_readme.ANCHOR_END}

        ## CLI
        """),
        encoding="utf-8",
    )
    feature_readme.sync_readme(readme)
    text = readme.read_text(encoding="utf-8")
    assert "stale content" not in text
    assert feature_readme.ANCHOR_BEGIN in text
    assert "| Feature |" in text


def test_sync_readme_idempotent(tmp_path):
    readme = tmp_path / "README.md"
    readme.write_text("# Hello\n\n## CLI\n", encoding="utf-8")
    feature_readme.sync_readme(readme)
    changed = feature_readme.sync_readme(readme)
    assert changed is False  # second call: no diff → no write


def test_effect_scope_column_present_in_render():
    md = feature_readme.render_markdown(
        features=[("token_gate", {"category": "hard_gate",
                                   "description": "demo"})],
        effect_scopes={"token_gate": "immediate"},
    )
    assert "immediate" in md
    assert "`token_gate`" in md


def test_escape_pipes_in_description():
    md = feature_readme.render_markdown(
        features=[("pipe_feat", {"category": "x",
                                  "description": "foo | bar | baz"})],
        effect_scopes={"pipe_feat": "immediate"},
    )
    # table cell must escape the pipe
    assert "foo \\| bar \\| baz" in md


def test_cli_export_readme(capsys):
    rc = feature_readme.main(["export-readme"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "| Feature |" in out


def test_cli_sync_readme(tmp_path, capsys):
    readme = tmp_path / "README.md"
    readme.write_text("# Hello\n\n## CLI\n", encoding="utf-8")
    rc = feature_readme.main(["sync-readme", str(readme)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "updated" in out or "already in sync" in out


def test_cli_unknown_command(capsys):
    rc = feature_readme.main(["nope"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "unknown" in err
