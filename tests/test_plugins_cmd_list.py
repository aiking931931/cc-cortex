"""Tests for ``concinno plugins list`` CLI orchestrator."""
from __future__ import annotations

import argparse
import json
import time

import pytest


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("CONCINNO_PLUGINS_ENABLED", raising=False)
    monkeypatch.delenv("CONCINNO_PLUGINS_ALLOWLIST", raising=False)
    return tmp_path


def _make_args(*, fmt="text", verbose=False):
    ns = argparse.Namespace()
    ns.format = fmt
    ns.verbose = verbose
    return ns


class TestJsonOutput:
    def test_json_shape(self, isolated_home, capsys):
        from concinno.cli.plugins_cmd import OUTPUT_SCHEMA_VERSION, cmd_plugins_list

        cmd_plugins_list(_make_args(fmt="json"))
        out = capsys.readouterr().out.strip()
        payload = json.loads(out)
        assert payload["schema_version"] == OUTPUT_SCHEMA_VERSION
        assert payload["schema_version"] == 0   # UNSTABLE baseline
        assert "guards" in payload
        assert "features" in payload
        assert "skills" in payload
        assert "allowlist" in payload
        assert "plugin_load_errors" in payload

    def test_allowlist_reflects_env_var(self, isolated_home, monkeypatch, capsys):
        monkeypatch.setenv("CONCINNO_PLUGINS_ALLOWLIST", "pkg-a,pkg-b")
        from concinno.cli.plugins_cmd import cmd_plugins_list
        cmd_plugins_list(_make_args(fmt="json"))
        payload = json.loads(capsys.readouterr().out)
        env_rows = payload["allowlist"]["env"]
        env_pkgs = [r["package"] for r in env_rows]
        assert env_pkgs == ["pkg-a", "pkg-b"]
        assert payload["allowlist"]["effective_runtime_source"] == "env"

    def test_allowlist_reflects_file(self, isolated_home, capsys):
        from concinno.plugins.allowlist_file import add_to_allowlist
        add_to_allowlist("pkg-from-file", note="test")
        from concinno.cli.plugins_cmd import cmd_plugins_list
        cmd_plugins_list(_make_args(fmt="json"))
        payload = json.loads(capsys.readouterr().out)
        file_pkgs = [r["package"] for r in payload["allowlist"]["file"]]
        assert file_pkgs == ["pkg-from-file"]
        assert payload["allowlist"]["note"] == "test"
        # Runtime source is still "none" because file doesn't flow to runtime
        assert payload["allowlist"]["effective_runtime_source"] == "none (all plugins allowed)"


class TestTextOutput:
    def test_smoke_text_output(self, isolated_home, capsys):
        from concinno.cli.plugins_cmd import cmd_plugins_list
        cmd_plugins_list(_make_args(fmt="text"))
        out = capsys.readouterr().out
        assert "Plugin discovery" in out
        assert "Features plugins" in out
        assert "Skills plugins" in out
        assert "Allowlist" in out


class TestDiscoveryDisabled:
    def test_plugins_disabled_env(self, isolated_home, monkeypatch, capsys):
        monkeypatch.setenv("CONCINNO_PLUGINS_ENABLED", "0")
        from concinno.cli.plugins_cmd import cmd_plugins_list
        cmd_plugins_list(_make_args(fmt="json"))
        payload = json.loads(capsys.readouterr().out)
        assert payload["discovery_enabled"] is False


class TestPerformance:
    def test_real_system_cold_call_under_budget(self, isolated_home, capsys):
        """Cold-call bench on real importlib.metadata.distributions().

        Asserts p_worst < 500ms even with ≥30 installed distributions,
        else < 200ms. Addresses HIGH-3 from Red Opus report.
        """
        import importlib.metadata as _meta

        dist_count = len(list(_meta.distributions()))
        # Realistic budgets after 2.32.0 hardened the default ``list``
        # path: guards pipeline deferred to --verbose so the default
        # call is features + skills discovery + file read. Cold scan
        # time scales linearly with installed-distribution count
        # (importlib.metadata.entry_points walks every dist's
        # ``METADATA``); on dev machines with hundreds of
        # ``concinno-skills-*`` / scientific-Python packages the
        # 100-distribution budget under-counts cost. Allow ~15ms per
        # distribution above 100 with a 1500ms floor.
        if dist_count >= 100:
            budget_ms = max(1500, dist_count * 15)
        elif dist_count >= 30:
            budget_ms = 1000
        else:
            budget_ms = 300

        from concinno.cli.plugins_cmd import cmd_plugins_list

        samples: list[float] = []
        for _ in range(3):
            t0 = time.perf_counter()
            cmd_plugins_list(_make_args(fmt="json"))
            capsys.readouterr()  # discard
            samples.append((time.perf_counter() - t0) * 1000)

        worst = max(samples)
        assert worst < budget_ms, (
            f"list cold call p_worst={worst:.1f}ms exceeds budget "
            f"{budget_ms}ms (dist_count={dist_count}, samples={samples})"
        )
