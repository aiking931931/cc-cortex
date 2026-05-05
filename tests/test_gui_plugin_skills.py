"""GUI integration: mock a plugin skill root and assert it surfaces.

The GUI layer consumes :func:`concinno.plugins.skills.iter_plugin_skill_roots`
inside :func:`concinno.gui.server._discover_skills`. We monkeypatch
the former and call the latter end-to-end.
"""
from __future__ import annotations

from pathlib import Path

from concinno.gui.server import _discover_skills


def _make_plugin_skill(tmp_path: Path, skill_name: str) -> Path:
    """Build a fake plugin's skills root with one SKILL.md inside."""
    root = tmp_path / "plugin_skills_root"
    root.mkdir()
    skill_dir = root / skill_name
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {skill_name}\ndescription: from plugin\n"
        f"triggers: [plug, test]\n---\n\n# {skill_name} body\n",
        encoding="utf-8",
    )
    return root


class TestPluginSkillDiscovery:
    def test_plugin_skill_appears_in_catalogue(self, monkeypatch, tmp_path):
        plugin_root = _make_plugin_skill(tmp_path, "fake_plugin_skill")

        import concinno.plugins.skills as skills_mod
        monkeypatch.setattr(
            skills_mod,
            "iter_plugin_skill_roots",
            lambda: iter([(plugin_root, "fake-concinno-skills-pkg")]),
        )
        # Also patch the re-export in concinno.plugins so the
        # gui.server.import hits the patched version.
        import concinno.plugins as plugins_mod
        monkeypatch.setattr(
            plugins_mod,
            "iter_plugin_skill_roots",
            lambda: iter([(plugin_root, "fake-concinno-skills-pkg")]),
        )

        rows = _discover_skills()
        names = [r["name"] for r in rows]
        assert "fake_plugin_skill" in names
        row = next(r for r in rows if r["name"] == "fake_plugin_skill")
        assert row["scope"] == "plugin:fake-concinno-skills-pkg"

    def test_plugin_skill_does_not_shadow_project(self, monkeypatch, tmp_path):
        """Project > plugin precedence: first-wins dedup means plugin
        never overrides a project-local skill of the same name.
        """
        # Same-name plugin skill that should not appear because
        # project-local occupancy happens first.
        plugin_root = _make_plugin_skill(tmp_path, "my_skill")

        import concinno.plugins as plugins_mod
        monkeypatch.setattr(
            plugins_mod,
            "iter_plugin_skill_roots",
            lambda: iter([(plugin_root, "some-pkg")]),
        )

        # Fabricate a project-local SKILL.md in a temp cwd.
        project_root = tmp_path / "proj"
        (project_root / ".claude" / "skills" / "my_skill").mkdir(parents=True)
        (project_root / ".claude" / "skills" / "my_skill" / "SKILL.md").write_text(
            "---\nname: my_skill\ndescription: from project\n---\n",
            encoding="utf-8",
        )
        monkeypatch.chdir(project_root)

        rows = _discover_skills()
        row = next((r for r in rows if r["name"] == "my_skill"), None)
        assert row is not None
        # Project scope wins because it was ingested before the plugin
        # fallback.
        assert row["scope"] == "project", (
            f"expected project but got {row['scope']!r}"
        )

    def test_plugin_failure_does_not_break_discovery(self, monkeypatch):
        def boom():
            raise RuntimeError("plugin discovery crashed")

        import concinno.plugins as plugins_mod
        monkeypatch.setattr(plugins_mod, "iter_plugin_skill_roots", boom)

        # Must still return (at least home skills) without raising.
        rows = _discover_skills()
        assert isinstance(rows, list)
