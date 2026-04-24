"""Real entry-points integration test (Red Opus FATAL-1 mitigation).

Unlike the mock-based plugin tests, this fixture installs a real one-file
fake distribution into the test environment using ``pip install -e`` then
verifies ``concinno plugins list --format json`` actually discovers it.
Skipped when the environment cannot pip-install (sandboxed CI, no
writable site-packages, etc).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import textwrap

import pytest

_FAKE_PKG_NAME = "concinno-skills-fake-for-integration-test"
_FAKE_MODULE = "concinno_skills_fake_for_integration_test"


@pytest.fixture
def fake_plugin_installed(tmp_path, monkeypatch):
    """Create a minimal source distribution and pip install -e it.

    Yields True when install succeeded, None+skip when it could not.
    """
    pkg_dir = tmp_path / "fake_pkg"
    (pkg_dir / "src" / _FAKE_MODULE).mkdir(parents=True)
    (pkg_dir / "pyproject.toml").write_text(textwrap.dedent(f"""
        [build-system]
        requires = ["hatchling"]
        build-backend = "hatchling.build"

        [project]
        name = "{_FAKE_PKG_NAME}"
        version = "0.0.1"
        description = "integration test fake plugin"
        requires-python = ">=3.10"

        [project.entry-points."concinno.features"]
        fake_feat = "{_FAKE_MODULE}:FEATURE_META"

        [tool.hatch.build.targets.wheel]
        packages = ["src/{_FAKE_MODULE}"]
    """).strip(), encoding="utf-8")

    (pkg_dir / "src" / _FAKE_MODULE / "__init__.py").write_text(textwrap.dedent("""
        FEATURE_META = {
            "integration_test_feature": {
                "category": "plugin_test",
                "description": "fake feature for real-EP integration test",
                "enabled": True,
                "schema_version": 1,
                "params": {},
            },
        }
    """).strip(), encoding="utf-8")

    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-e", str(pkg_dir),
             "--quiet", "--no-build-isolation"],
            check=True,
            capture_output=True,
            timeout=120,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
        pytest.skip(f"pip install -e failed in this environment: {exc}")

    yield True

    # Cleanup: uninstall to leave the environment clean
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "uninstall", "-y", _FAKE_PKG_NAME,
             "--quiet"],
            check=False,
            capture_output=True,
            timeout=60,
        )
    except Exception:
        pass


class TestRealEntryPointIntegration:
    def test_list_discovers_real_installed_plugin(
        self, fake_plugin_installed, capsys, monkeypatch
    ):
        """Verifies discover_feature_entrypoints sees the live plugin.

        Red Opus FATAL-1 addressed: prior 2.31.0/2.32.0 tests all
        used entry_points_override mocks, so the real importlib.metadata
        code path was never exercised against an installed package.
        """
        monkeypatch.delenv("CONCINNO_PLUGINS_ALLOWLIST", raising=False)
        monkeypatch.delenv("CONCINNO_PLUGINS_ENABLED", raising=False)

        # Force importlib.metadata to rescan after pip install.
        import importlib.metadata
        importlib.reload(importlib.metadata)

        from concinno.cli.plugins_cmd import cmd_plugins_list
        ns = argparse.Namespace(format="json", verbose=True)
        cmd_plugins_list(ns)
        payload = json.loads(capsys.readouterr().out)
        features = payload["features"]
        packages = [row["package"] for row in features]
        assert _FAKE_PKG_NAME in packages, (
            f"installed fake plugin not discovered; packages={packages}"
        )
        fake_row = next(r for r in features if r["package"] == _FAKE_PKG_NAME)
        assert fake_row["valid"] is True
        assert "integration_test_feature" in fake_row["features"]
