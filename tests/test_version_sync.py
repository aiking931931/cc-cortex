"""SSOT guard: concinno.__version__ must match pyproject.toml and CHANGELOG.md.

Red team #3 found the three sources out of sync (pyproject=1.3.0,
CHANGELOG=1.3.0, __init__.py=1.1.0). A stranger printing
``concinno.__version__`` at runtime got a value that did not match any
published release. This test pins them together.
"""

from __future__ import annotations

import re
from pathlib import Path

import concinno

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PYPROJECT = _REPO_ROOT / "pyproject.toml"
_CHANGELOG = _REPO_ROOT / "CHANGELOG.md"


def _pyproject_version() -> str:
    text = _PYPROJECT.read_text(encoding="utf-8")
    # Only the [project] table's ``version = "X.Y.Z"`` line counts —
    # any ``version`` fields in other tables are irrelevant.
    in_project = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            in_project = stripped == "[project]"
            continue
        if in_project and stripped.startswith("version"):
            m = re.match(r'version\s*=\s*"([^"]+)"', stripped)
            if m:
                return m.group(1)
    raise AssertionError("pyproject.toml [project].version not found")


def _changelog_latest_version() -> str:
    text = _CHANGELOG.read_text(encoding="utf-8")
    # Find the first `## [X.Y.Z] - DATE` header; skip `## [Unreleased]`.
    for line in text.splitlines():
        m = re.match(r"##\s+\[(\d+\.\d+\.\d+(?:[-+][\w.]+)?)\]", line)
        if m:
            return m.group(1)
    raise AssertionError("CHANGELOG.md has no versioned `## [X.Y.Z]` header")


def test_version_sources_are_aligned():
    dunder = concinno.__version__
    pyproj = _pyproject_version()
    changelog = _changelog_latest_version()
    assert dunder == pyproj == changelog, (
        f"Version drift detected: "
        f"concinno.__version__={dunder!r}, "
        f"pyproject.toml={pyproj!r}, "
        f"CHANGELOG.md latest={changelog!r}. "
        f"Update all three before publishing."
    )


def test_version_is_semver():
    assert re.fullmatch(
        r"\d+\.\d+\.\d+(?:[-+][\w.]+)?", concinno.__version__,
    ), f"Not a SemVer string: {concinno.__version__!r}"
