"""Tests for ``concinno.gui.switcher`` — federation reverse-proxy.

Covers:

* Token write-on-create + middleware gating.
* ``/api/health`` and ``/`` bypass auth (loopback liveness + landing).
* ``/api/backends`` reads both backend tokens off disk and reports
  reachable / token_present per backend.
* ``/proxy/{backend}/{path}`` forwards Bearer header from the *backend's*
  on-disk token (httpx mock asserts the header value).
* Wrong switcher token → 401 across /api/* and /proxy/*.
* Disk-only Sancio token mirror does not import ``persona.*``.
* CLI integration: ``--switcher --print-token-path`` prints the
  switcher token path (not the main GUI token path).

Skips cleanly when ``concinno[gui]`` extras are not installed.
"""

from __future__ import annotations

import sys

import pytest

fastapi = pytest.importorskip("fastapi")  # noqa: F401
testclient = pytest.importorskip("fastapi.testclient")
httpx = pytest.importorskip("httpx")


# ── shared fixture ───────────────────────────────────────────


@pytest.fixture
def switcher_app(tmp_path, monkeypatch):
    """Build a switcher app with a known token + isolated token paths.

    Redirects ``HOME`` / ``USERPROFILE`` / ``LOCALAPPDATA`` so the
    test does not write into the real user's ``~/.concinno/`` or
    ``~/.sancio/`` directories.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))

    from concinno.gui.switcher import create_switcher_app

    # Use ports that are guaranteed unreachable in unit tests (no real
    # GUI process listens there). Default 8400/8401 can collide with a
    # running ``concinno gui`` instance, causing reachable=True when the
    # test expects False.
    app = create_switcher_app(
        token="UNIT-SWITCHER-TOKEN",
        token_path=tmp_path / "switcher_token",
        concinno_port=19400,
        sancio_port=19401,
    )
    return app, "UNIT-SWITCHER-TOKEN", tmp_path


# ── token + middleware basics ────────────────────────────────


def test_create_switcher_app_writes_token(switcher_app):
    app, token, tmp_path = switcher_app
    assert app.state.switcher_token == token
    assert app.state.switcher_token_path.is_file()
    assert app.state.switcher_token_path.read_text(encoding="utf-8") == token


def test_health_route_bypasses_auth(switcher_app):
    from fastapi.testclient import TestClient

    app, _tok, _tmp = switcher_app
    with TestClient(app) as client:
        r = client.get("/api/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"
        assert r.json()["service"] == "concinno-switcher"


def test_landing_page_bypasses_auth_and_renders_tabs(switcher_app):
    from fastapi.testclient import TestClient

    app, _tok, _tmp = switcher_app
    with TestClient(app) as client:
        r = client.get("/")
        assert r.status_code == 200
        body = r.text
        # Two tab buttons + iframe + ports surfaced.
        # Fixture uses 19400/19401 to avoid colliding with a running GUI.
        assert "Concinno (:19400)" in body
        assert "Sancio (:19401)" in body
        assert "<iframe" in body
        # Switcher banner present so operator knows what they are looking at.
        assert "Switcher :8399" in body


def test_backends_route_requires_bearer(switcher_app):
    from fastapi.testclient import TestClient

    app, _tok, _tmp = switcher_app
    with TestClient(app) as client:
        r = client.get("/api/backends")
        assert r.status_code == 401


def test_proxy_route_requires_bearer(switcher_app):
    from fastapi.testclient import TestClient

    app, _tok, _tmp = switcher_app
    with TestClient(app) as client:
        r = client.get("/proxy/concinno/api/health")
        assert r.status_code == 401


def test_wrong_switcher_token_rejected(switcher_app):
    from fastapi.testclient import TestClient

    app, _tok, _tmp = switcher_app
    with TestClient(app) as client:
        r = client.get(
            "/api/backends",
            headers={"Authorization": "Bearer not-the-token"},
        )
        assert r.status_code == 401


# ── /api/backends ────────────────────────────────────────────


def test_backends_no_tokens_present_reports_absent(switcher_app):
    """Both backend tokens absent on disk → token_present=False both
    sides, reachable=False both sides (nothing actually listens on
    8400/8401 in unit tests).
    """
    from fastapi.testclient import TestClient

    app, tok, _tmp = switcher_app
    with TestClient(app) as client:
        r = client.get(
            "/api/backends",
            headers={"Authorization": f"Bearer {tok}"},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["concinno"]["token_present"] is False
        assert data["sancio"]["token_present"] is False
        assert data["concinno"]["reachable"] is False
        assert data["sancio"]["reachable"] is False
        # URLs reflect the configured backend ports (19400/19401 in fixture).
        assert data["concinno"]["url"] == "http://127.0.0.1:19400"
        assert data["sancio"]["url"] == "http://127.0.0.1:19401"


def test_backends_reads_concinno_token_from_disk(switcher_app):
    """Write a fake concinno token to disk → token_present=True."""
    from fastapi.testclient import TestClient

    from concinno.gui.auth import get_token_path, write_token

    app, tok, _tmp = switcher_app
    write_token("fake-concinno-token", path=get_token_path())
    with TestClient(app) as client:
        r = client.get(
            "/api/backends",
            headers={"Authorization": f"Bearer {tok}"},
        )
        data = r.json()
        assert data["concinno"]["token_present"] is True
        # Sancio side still absent.
        assert data["sancio"]["token_present"] is False


def test_backends_reads_sancio_token_from_disk(switcher_app):
    """Write a fake sancio token to disk → token_present=True for
    sancio side, and crucially we did NOT import persona.* to do it
    (the path is computed by the in-module mirror).
    """
    from fastapi.testclient import TestClient

    from concinno.gui.auth import write_token
    from concinno.gui.switcher import get_sancio_token_path_via_disk

    app, tok, _tmp = switcher_app
    sancio_path = get_sancio_token_path_via_disk()
    write_token("fake-sancio-token", path=sancio_path)
    with TestClient(app) as client:
        r = client.get(
            "/api/backends",
            headers={"Authorization": f"Bearer {tok}"},
        )
        data = r.json()
        assert data["sancio"]["token_present"] is True


# ── /proxy/{backend}/{path} forwarding ──────────────────────


def test_proxy_concinno_forwards_backend_token(switcher_app, monkeypatch):
    """Proxy reads the on-disk concinno token and forwards it as
    ``Authorization: Bearer <concinno-token>`` to the upstream — the
    *switcher* token never leaks to the backend.
    """
    from fastapi.testclient import TestClient

    import concinno.gui.switcher as sw_mod
    from concinno.gui.auth import get_token_path, write_token

    app, switcher_tok, _tmp = switcher_app
    write_token("REAL-CONCINNO-TOKEN", path=get_token_path())

    captured: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["method"] = request.method
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(_handler)

    class _PatchedAsyncClient(httpx.AsyncClient):
        def __init__(self, *a, **kw):
            kw["transport"] = transport
            super().__init__(*a, **kw)

    monkeypatch.setattr(sw_mod.httpx, "AsyncClient", _PatchedAsyncClient)

    with TestClient(app) as client:
        r = client.get(
            "/proxy/concinno/api/features",
            headers={"Authorization": f"Bearer {switcher_tok}"},
        )
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    # Switcher must have rewritten the auth header to the *backend's*
    # token, not echoed the switcher's own token through.
    assert captured["auth"] == "Bearer REAL-CONCINNO-TOKEN"
    assert captured["auth"] != f"Bearer {switcher_tok}"
    # URL points at the right backend port + path (19400 in fixture).
    assert captured["url"].startswith("http://127.0.0.1:19400/")
    assert "/api/features" in captured["url"]


def test_proxy_sancio_forwards_backend_token(switcher_app, monkeypatch):
    from fastapi.testclient import TestClient

    import concinno.gui.switcher as sw_mod
    from concinno.gui.auth import write_token
    from concinno.gui.switcher import get_sancio_token_path_via_disk

    app, switcher_tok, _tmp = switcher_app
    write_token("REAL-SANCIO-TOKEN", path=get_sancio_token_path_via_disk())

    captured: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"sancio": "alive"})

    transport = httpx.MockTransport(_handler)

    class _PatchedAsyncClient(httpx.AsyncClient):
        def __init__(self, *a, **kw):
            kw["transport"] = transport
            super().__init__(*a, **kw)

    monkeypatch.setattr(sw_mod.httpx, "AsyncClient", _PatchedAsyncClient)

    with TestClient(app) as client:
        r = client.get(
            "/proxy/sancio/api/runtime/state",
            headers={"Authorization": f"Bearer {switcher_tok}"},
        )
    assert r.status_code == 200
    assert captured["auth"] == "Bearer REAL-SANCIO-TOKEN"
    assert captured["url"].startswith("http://127.0.0.1:19401/")


def test_proxy_returns_503_when_backend_token_absent(switcher_app):
    """Backend not started yet → no token on disk → 503 fail-fast,
    not a confusing upstream-error 500.
    """
    from fastapi.testclient import TestClient

    app, switcher_tok, _tmp = switcher_app
    with TestClient(app) as client:
        r = client.get(
            "/proxy/concinno/api/features",
            headers={"Authorization": f"Bearer {switcher_tok}"},
        )
        assert r.status_code == 503
        assert "concinno" in r.json()["detail"]


def test_proxy_unknown_backend_404(switcher_app):
    from fastapi.testclient import TestClient

    app, switcher_tok, _tmp = switcher_app
    with TestClient(app) as client:
        r = client.get(
            "/proxy/unknown/some/path",
            headers={"Authorization": f"Bearer {switcher_tok}"},
        )
        assert r.status_code == 404


# ── disk-only coupling invariant ────────────────────────────


def test_switcher_module_does_not_import_persona():
    """Layer rule: ``concinno.gui.switcher`` must never reach into
    ``persona.*`` — token path is mirrored on the disk path, not in
    Python imports. If this regresses we have re-introduced the cross-
    layer coupling that the design doc forbade (CLAUDE.md Hard Rule #2).

    Uses :mod:`ast` rather than substring grep so the module docstring
    (which legitimately *names* ``persona.gui.server`` for cross-
    referencing) does not trip the check — only real Python ``import``
    or ``from … import`` statements count.
    """
    import ast

    import concinno.gui.switcher as sw_mod
    src = sw_mod.__file__
    with open(src, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    bad: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == "persona":
                    bad.append(f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.module.split(".")[0] == "persona":
                bad.append(f"from {node.module} import …")
    assert not bad, f"switcher must not import persona.*; found: {bad}"


def test_get_sancio_token_path_via_disk_matches_persona_layout():
    """Path mirror sanity check: same OS-conditional shape as
    :func:`persona.gui.server.get_sancio_token_path` (Windows uses
    ``%LOCALAPPDATA%\\sancio\\gui_token``, POSIX uses
    ``~/.sancio/gui_token``). Cross-checked indirectly via path
    string assertions so we do not actually import persona here.
    """
    from concinno.gui.switcher import get_sancio_token_path_via_disk

    p = get_sancio_token_path_via_disk()
    if sys.platform == "win32":
        assert "sancio" in str(p).lower()
        assert p.name == "gui_token"
    else:
        assert ".sancio" in p.parts
        assert p.name == "gui_token"


# ── CLI integration ─────────────────────────────────────────


def test_cli_switcher_print_token_path_prints_switcher_token(
    capsys, monkeypatch, tmp_path,
):
    """``concinno gui --switcher --print-token-path`` prints the
    switcher token path, NOT the main GUI token path.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))
    import argparse

    from concinno.cli.main import _register_gui, cmd_gui

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    _register_gui(sub)
    args = parser.parse_args(["gui", "--switcher", "--print-token-path"])
    cmd_gui(args)
    out = capsys.readouterr().out
    assert "switcher_token" in out
    assert "gui_token" not in out.replace("switcher_token", "")


def test_cli_default_print_token_path_prints_main_token(
    capsys, monkeypatch, tmp_path,
):
    """Without ``--switcher``, ``--print-token-path`` prints the main
    GUI token path (regression guard against the dispatch flipping).
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))
    import argparse

    from concinno.cli.main import _register_gui, cmd_gui

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    _register_gui(sub)
    args = parser.parse_args(["gui", "--print-token-path"])
    cmd_gui(args)
    out = capsys.readouterr().out
    assert "gui_token" in out
    assert "switcher_token" not in out


def test_cli_switcher_flag_parsed_with_backend_ports():
    """Argparse round-trip — the new flags exist and have the right defaults."""
    import argparse

    from concinno.cli.main import _register_gui

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    _register_gui(sub)
    args = parser.parse_args(
        ["gui", "--switcher", "--concinno-port", "9000", "--sancio-port", "9001"],
    )
    assert args.switcher is True
    assert args.concinno_port == 9000
    assert args.sancio_port == 9001
    # Default port stays None so cmd_gui can pick 8399 vs 8400 by mode.
    assert args.port is None
