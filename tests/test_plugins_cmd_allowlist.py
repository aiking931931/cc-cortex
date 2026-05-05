"""Tests for ``concinno plugins allowlist add/remove/show/export-env`` CLI."""
from __future__ import annotations

import argparse
import json

import pytest


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("CONCINNO_PLUGINS_ALLOWLIST", raising=False)
    return tmp_path


class TestAdd:
    def test_add_emits_stdout(self, isolated_home, capsys):
        from concinno.cli.plugins_cmd import cmd_plugins_allowlist_add
        ns = argparse.Namespace(package="concinno-skills-foo", note="")
        cmd_plugins_allowlist_add(ns)
        out = capsys.readouterr().out
        assert "Added 'concinno-skills-foo'" in out

    def test_add_idempotent_stdout_differs(self, isolated_home, capsys):
        from concinno.cli.plugins_cmd import cmd_plugins_allowlist_add
        ns = argparse.Namespace(package="concinno-skills-foo", note="")
        cmd_plugins_allowlist_add(ns)
        capsys.readouterr()  # drain
        cmd_plugins_allowlist_add(ns)
        out = capsys.readouterr().out
        assert "already in the allowlist" in out

    def test_add_not_installed_warns(self, isolated_home, capsys):
        """Red Opus HIGH-1 pre-install warning."""
        from concinno.cli.plugins_cmd import cmd_plugins_allowlist_add
        ns = argparse.Namespace(
            package="definitely-not-installed-pkg-xyz",
            note="",
        )
        cmd_plugins_allowlist_add(ns)
        err = capsys.readouterr().err
        assert "not currently installed" in err

    def test_add_empty_pkg_exits(self, isolated_home, capsys):
        from concinno.cli.plugins_cmd import cmd_plugins_allowlist_add
        ns = argparse.Namespace(package="   ", note="")
        with pytest.raises(SystemExit) as exc:
            cmd_plugins_allowlist_add(ns)
        assert exc.value.code == 2


class TestRemove:
    def test_remove_existing(self, isolated_home, capsys):
        from concinno.cli.plugins_cmd import (
            cmd_plugins_allowlist_add,
            cmd_plugins_allowlist_remove,
        )
        cmd_plugins_allowlist_add(argparse.Namespace(package="pkg-a", note=""))
        capsys.readouterr()
        cmd_plugins_allowlist_remove(argparse.Namespace(package="pkg-a"))
        out = capsys.readouterr().out
        assert "Removed 'pkg-a'" in out

    def test_remove_nonexistent_idempotent(self, isolated_home, capsys):
        from concinno.cli.plugins_cmd import cmd_plugins_allowlist_remove
        cmd_plugins_allowlist_remove(argparse.Namespace(package="not-there"))
        out = capsys.readouterr().out
        assert "was not in the allowlist" in out


class TestShow:
    def test_show_empty_text(self, isolated_home, capsys):
        from concinno.cli.plugins_cmd import cmd_plugins_allowlist_show
        ns = argparse.Namespace(format="text")
        cmd_plugins_allowlist_show(ns)
        out = capsys.readouterr().out
        assert "file entries (0)" in out
        assert "env entries (0)" in out
        assert "Runtime gating reads env var only" in out

    def test_show_json_shape(self, isolated_home, monkeypatch, capsys):
        monkeypatch.setenv("CONCINNO_PLUGINS_ALLOWLIST", "env-pkg")
        from concinno.cli.plugins_cmd import (
            OUTPUT_SCHEMA_VERSION,
            cmd_plugins_allowlist_add,
            cmd_plugins_allowlist_show,
        )
        cmd_plugins_allowlist_add(argparse.Namespace(
            package="file-pkg", note="operator note",
        ))
        capsys.readouterr()
        cmd_plugins_allowlist_show(argparse.Namespace(format="json"))
        payload = json.loads(capsys.readouterr().out)
        assert payload["schema_version"] == OUTPUT_SCHEMA_VERSION
        assert [r["package"] for r in payload["file"]] == ["file-pkg"]
        assert [r["package"] for r in payload["env"]] == ["env-pkg"]
        assert payload["note"] == "operator note"
        assert payload["updated_at"] is not None


class TestExportEnv:
    def test_export_env_text(self, isolated_home, capsys):
        from concinno.cli.plugins_cmd import (
            cmd_plugins_allowlist_add,
            cmd_plugins_allowlist_export_env,
        )
        cmd_plugins_allowlist_add(argparse.Namespace(package="pkg-1", note=""))
        cmd_plugins_allowlist_add(argparse.Namespace(package="pkg-2", note=""))
        capsys.readouterr()
        cmd_plugins_allowlist_export_env(argparse.Namespace(format="text"))
        out = capsys.readouterr().out.strip()
        assert out == "export CONCINNO_PLUGINS_ALLOWLIST=pkg-1,pkg-2"

    def test_export_env_json(self, isolated_home, capsys):
        from concinno.cli.plugins_cmd import (
            cmd_plugins_allowlist_add,
            cmd_plugins_allowlist_export_env,
        )
        cmd_plugins_allowlist_add(argparse.Namespace(package="a", note=""))
        capsys.readouterr()
        cmd_plugins_allowlist_export_env(argparse.Namespace(format="json"))
        payload = json.loads(capsys.readouterr().out)
        assert payload["env_var"] == "CONCINNO_PLUGINS_ALLOWLIST"
        assert payload["value"] == "a"

    def test_export_env_empty(self, isolated_home, capsys):
        from concinno.cli.plugins_cmd import cmd_plugins_allowlist_export_env
        cmd_plugins_allowlist_export_env(argparse.Namespace(format="text"))
        out = capsys.readouterr().out.strip()
        assert out == "export CONCINNO_PLUGINS_ALLOWLIST="
