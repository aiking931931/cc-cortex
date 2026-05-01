"""End-to-end tests for the GUI Marketplace REST surface.

Bug 4b regression: a hook-only ``concinno-skills-*`` distribution must
appear in ``GET /api/skills/marketplace``. Plus state-transition tests
on the install / uninstall handlers (subprocess mocked).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

fastapi = pytest.importorskip("fastapi")  # noqa: F401
testclient = pytest.importorskip("fastapi.testclient")


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Authenticated TestClient against a fresh GUI app."""
    from fastapi.testclient import TestClient

    from concinno.gui.server import create_app

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))

    token = "TEST-MARKETPLACE-TOKEN"
    app = create_app(token=token, token_path=tmp_path / "gui_token")
    tc = TestClient(app)
    tc.headers.update({"Authorization": f"Bearer {token}"})
    return tc


@dataclass
class _FakeEntryPoint:
    group: str
    name: str
    value: str


@dataclass
class _FakeDist:
    name: str
    version: str = "0.1.0"
    summary: str = "fake"
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


def _patch_distributions(monkeypatch: pytest.MonkeyPatch,
                         dists: list[_FakeDist]) -> None:
    import concinno.marketplace.discovery as disc

    monkeypatch.setattr(
        disc.importlib_metadata,
        "distributions",
        lambda: list(dists),
    )


def _patch_pypi_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the PyPI client to take the unreachable branch."""
    from concinno.marketplace.pypi_client import (
        PyPIClient,
        PyPIUnreachableError,
    )

    def fail(_self: PyPIClient, **_kw: Any) -> Any:
        raise PyPIUnreachableError("test offline")

    monkeypatch.setattr(
        PyPIClient, "list_concinno_skills_packages", fail,
    )


def test_marketplace_get_smoke(client, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_distributions(monkeypatch, [
        _FakeDist(name="concinno-skills-memory", version="0.2.0",
                  eps=[_FakeEntryPoint("concinno.skills", "memory",
                                       "memory:root")]),
    ])
    _patch_pypi_offline(monkeypatch)
    r = client.get("/api/skills/marketplace")
    assert r.status_code == 200
    body = r.json()
    assert "installed" in body
    assert "available" in body
    assert "pypi_reachable" in body
    assert "release_auth_disabled" in body


def test_marketplace_surfaces_hook_only_pkg(client,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    """Bug 4b regression: hook-only sub-pkg shows up even with no SKILL.md."""
    _patch_distributions(monkeypatch, [
        _FakeDist(
            name="concinno-skills-ziq",
            version="0.1.0",
            eps=[_FakeEntryPoint(
                "concinno.hooks.on_stop",
                "ziq",
                "concinno_skills_ziq.lifecycle:on_stop",
            )],
        ),
    ])
    _patch_pypi_offline(monkeypatch)
    r = client.get("/api/skills/marketplace")
    assert r.status_code == 200
    body = r.json()
    names = [row["name"] for row in body["installed"]]
    assert "concinno-skills-ziq" in names
    row = next(x for x in body["installed"]
               if x["name"] == "concinno-skills-ziq")
    assert row["kind"] == "hook-pkg"
    assert any(ep["group"] == "concinno.hooks.on_stop"
               for ep in row["hook_entry_points"])


def test_marketplace_install_rejects_arbitrary(client) -> None:
    r = client.post(
        "/api/skills/marketplace/install",
        json={"package": "requests", "confirm_token": "x"},
    )
    assert r.status_code == 400


def test_marketplace_install_requires_confirm_when_auth_enforced(
    client, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "concinno.gui.server._resolve_release_auth_disabled",
        lambda: False,
    )
    r = client.post(
        "/api/skills/marketplace/install",
        json={"package": "concinno-skills-memory"},
    )
    # 409 (gate not satisfied) — frontend must echo confirm_token first
    assert r.status_code == 409


def test_marketplace_install_success_when_auth_disabled(
    client, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "concinno.gui.server._resolve_release_auth_disabled",
        lambda: True,
    )

    class _FakeProc:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def fake_runner(args: list[str], **_kw: Any) -> _FakeProc:
        # Args must be a list (no shell injection) and include the right pkg
        assert isinstance(args, list)
        assert "concinno-skills-memory" in args[-1]
        return _FakeProc()

    import concinno.marketplace.installer as ins_mod
    monkeypatch.setattr(ins_mod.subprocess, "run", fake_runner)

    r = client.post(
        "/api/skills/marketplace/install",
        json={"package": "concinno-skills-memory"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["return_code"] == 0


def test_marketplace_uninstall_state_transition(
    client, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "concinno.gui.server._resolve_release_auth_disabled",
        lambda: True,
    )

    class _FakeProc:
        returncode = 0
        stdout = "uninstalled"
        stderr = ""

    def fake_runner(args: list[str], **_kw: Any) -> _FakeProc:
        assert "uninstall" in args
        return _FakeProc()

    import concinno.marketplace.installer as ins_mod
    monkeypatch.setattr(ins_mod.subprocess, "run", fake_runner)

    r = client.post(
        "/api/skills/marketplace/uninstall",
        json={"package": "concinno-skills-memory"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["return_code"] == 0


def test_marketplace_refresh_rate_limited(
    client, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Reset rate-limit state for this test
    from concinno.gui import server as srv
    srv._marketplace_last_refresh_at.clear()
    r1 = client.get("/api/skills/marketplace/refresh")
    assert r1.status_code == 200
    # Immediate second call should be rate-limited
    r2 = client.get("/api/skills/marketplace/refresh")
    assert r2.status_code == 429


def test_marketplace_requires_bearer_auth(tmp_path, monkeypatch) -> None:
    """All new routes must reject missing-bearer requests."""
    from fastapi.testclient import TestClient

    from concinno.gui.server import create_app

    monkeypatch.setenv("HOME", str(tmp_path))
    app = create_app(token="abc", token_path=tmp_path / "gui_token")
    tc = TestClient(app)  # no Authorization header
    r = tc.get("/api/skills/marketplace")
    assert r.status_code == 401
    r2 = tc.post(
        "/api/skills/marketplace/install",
        json={"package": "concinno-skills-memory"},
    )
    assert r2.status_code == 401
