"""Unit tests for ``concinno.marketplace.discovery``.

Covers: distribution name validation, ``importlib.metadata`` walking,
PyPI fallback, and merge logic for installed + available rows.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from concinno.marketplace.discovery import (
    HARDCODED_AVAILABLE,
    MarketplaceRow,
    is_valid_dist_name,
    list_available_pypi,
    list_installed_concinno_skills,
    merge_installed_and_available,
)


@dataclass
class _FakeEntryPoint:
    group: str
    name: str
    value: str


@dataclass
class _FakeDist:
    """Minimal stand-in for :class:`importlib.metadata.Distribution`."""

    name: str
    version: str = "0.1.0"
    summary: str = "fake summary"
    eps: list[_FakeEntryPoint] = field(default_factory=list)

    @property
    def metadata(self) -> dict[str, str]:
        return {"Name": self.name, "Summary": self.summary}

    @property
    def entry_points(self) -> list[_FakeEntryPoint]:
        return self.eps

    @property
    def files(self) -> list[Any]:
        return []


def test_is_valid_dist_name_accepts_canonical() -> None:
    assert is_valid_dist_name("concinno-skills-memory")
    assert is_valid_dist_name("concinno-skills-session-search")


def test_is_valid_dist_name_rejects_garbage() -> None:
    assert not is_valid_dist_name("requests")
    assert not is_valid_dist_name("concinno-skills-")
    assert not is_valid_dist_name("concinno-skills-A")  # uppercase rejected
    assert not is_valid_dist_name("../etc/passwd")
    assert not is_valid_dist_name("concinno-skills-x; rm -rf /")


def test_list_installed_filters_to_concinno_prefix() -> None:
    dists = [
        _FakeDist(name="requests", version="2.31"),
        _FakeDist(name="concinno-skills-memory", version="0.2.0"),
        _FakeDist(name="concinno-skills-memoria", version="0.4.14"),
    ]
    rows = list_installed_concinno_skills(distributions=dists)
    names = [r.name for r in rows]
    assert "requests" not in names
    assert "concinno-skills-memory" in names
    assert "concinno-skills-memoria" in names


def test_list_installed_classifies_kind() -> None:
    skill_pkg = _FakeDist(
        name="concinno-skills-memory",
        eps=[_FakeEntryPoint("concinno.skills", "memory", "memory:root")],
    )
    hook_pkg = _FakeDist(
        name="concinno-skills-memoria",
        eps=[_FakeEntryPoint(
            "concinno.hooks.on_session_start",
            "memoria",
            "memoria.lifecycle:on_session_start",
        )],
    )
    rows = list_installed_concinno_skills(distributions=[skill_pkg, hook_pkg])
    by_name = {r.name: r for r in rows}
    assert by_name["concinno-skills-memory"].kind == "skill-pkg"
    assert by_name["concinno-skills-memoria"].kind == "hook-pkg"


def test_list_installed_surfaces_hook_entry_points() -> None:
    """The bug-4b regression test: hook-only sub-pkgs show their hooks."""
    dist = _FakeDist(
        name="concinno-skills-session-search",
        eps=[
            _FakeEntryPoint(
                "concinno.hooks.on_stop",
                "session_search",
                "concinno_skills_session_search.lifecycle:on_stop",
            ),
            _FakeEntryPoint(
                "concinno.hooks.on_session_start",
                "session_search",
                "concinno_skills_session_search.lifecycle:on_session_start",
            ),
        ],
    )
    rows = list_installed_concinno_skills(distributions=[dist])
    assert len(rows) == 1
    assert rows[0].kind == "hook-pkg"
    assert len(rows[0].hook_entry_points) == 2
    groups = {ep["group"] for ep in rows[0].hook_entry_points}
    assert "concinno.hooks.on_stop" in groups
    assert "concinno.hooks.on_session_start" in groups


def test_list_installed_handles_empty() -> None:
    rows = list_installed_concinno_skills(distributions=[])
    assert rows == []


def test_list_installed_distributions_failure_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken metadata walk must not crash discovery."""
    import concinno.marketplace.discovery as disc_mod

    def boom() -> Any:
        raise RuntimeError("metadata corrupt")

    monkeypatch.setattr(
        disc_mod.importlib_metadata, "distributions", boom,
    )
    rows = list_installed_concinno_skills()
    assert rows == []


class _FakePyPIClient:
    def __init__(self, packages: list[dict[str, Any]],
                 reachable: bool = True, age: int = 42) -> None:
        self._packages = packages
        self._reachable = reachable
        self._age = age

    def list_concinno_skills_packages(self) -> list[dict[str, Any]]:
        if not self._reachable:
            from concinno.marketplace.pypi_client import PyPIUnreachableError
            raise PyPIUnreachableError("simulated")
        return self._packages

    def cache_age_seconds(self) -> int:
        return self._age


def test_list_available_pypi_happy_path() -> None:
    client = _FakePyPIClient(
        [{"name": "concinno-skills-memory", "version": "0.2.0", "summary": "memory"}],
        reachable=True, age=10,
    )
    rows, reachable, age = list_available_pypi(pypi_client=client)
    assert reachable is True
    assert age == 10
    assert rows[0].name == "concinno-skills-memory"
    assert rows[0].version_latest == "0.2.0"


def test_list_available_pypi_offline_falls_back_to_hardcoded() -> None:
    client = _FakePyPIClient([], reachable=False)
    rows, reachable, _age = list_available_pypi(
        pypi_client=client, cache_age_seconds=0,
    )
    assert reachable is False
    names = {r.name for r in rows}
    for hardcoded in HARDCODED_AVAILABLE:
        assert hardcoded in names


def test_merge_installed_and_available_dedups() -> None:
    installed = [
        MarketplaceRow(
            name="concinno-skills-memory",
            kind="skill-pkg",
            version_installed="0.2.0",
            version_latest=None,
            summary="memory",
            homepage="",
            install_state="installed",
        )
    ]
    available = [
        MarketplaceRow(
            name="concinno-skills-memory",
            kind="unknown",
            version_installed=None,
            version_latest="0.2.0",
            summary="memory pypi",
            homepage="",
            install_state="available",
        ),
        MarketplaceRow(
            name="concinno-skills-ziq",
            kind="unknown",
            version_installed=None,
            version_latest="0.1.0",
            summary="ziq",
            homepage="",
            install_state="available",
        ),
    ]
    merged_inst, remaining_avail = merge_installed_and_available(
        installed, available,
    )
    assert len(merged_inst) == 1
    assert merged_inst[0].version_latest == "0.2.0"
    assert merged_inst[0].install_state == "installed"
    assert len(remaining_avail) == 1
    assert remaining_avail[0].name == "concinno-skills-ziq"


def test_merge_marks_outdated_when_versions_differ() -> None:
    installed = [
        MarketplaceRow(
            name="concinno-skills-memory",
            kind="skill-pkg",
            version_installed="0.1.0",
            version_latest=None,
            summary="",
            homepage="",
            install_state="installed",
        )
    ]
    available = [
        MarketplaceRow(
            name="concinno-skills-memory",
            kind="unknown",
            version_installed=None,
            version_latest="0.3.0",
            summary="",
            homepage="",
            install_state="available",
        )
    ]
    merged_inst, _ = merge_installed_and_available(installed, available)
    assert merged_inst[0].install_state == "outdated"
    assert merged_inst[0].version_latest == "0.3.0"
