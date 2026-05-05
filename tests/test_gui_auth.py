"""Tests for ``concinno.gui.auth`` token-file infra and the
``BearerTokenMiddleware`` that gates ``/api/*`` routes.

Skips cleanly when ``concinno[gui]`` extras are missing.
"""

from __future__ import annotations

import os
import stat
import sys

import pytest

fastapi = pytest.importorskip("fastapi")
testclient = pytest.importorskip("fastapi.testclient")

from concinno.gui import auth as gui_auth  # noqa: E402
from concinno.gui.auth import (  # noqa: E402
    TokenAuthError,
    generate_token,
    get_token_path,
    read_token,
    verify_token_header,
    write_token,
)

# ── path resolution ──────────────────────────────────────────


def test_get_token_path_windows_uses_localappdata(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\fake\AppData\Local")
    p = get_token_path()
    assert "concinno" in str(p).lower()
    assert "gui_token" in p.name
    # Path is under LocalAppData when env var is set.
    assert "AppData" in str(p)


def test_get_token_path_posix_uses_home(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("HOME", str(tmp_path))
    # On Windows runners, Path.home() looks at USERPROFILE.
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    p = get_token_path()
    assert p.name == "gui_token"
    assert ".concinno" in p.parts


# ── atomic write ─────────────────────────────────────────────


def test_write_token_atomic_and_chmod_0600(tmp_path):
    target = tmp_path / "subdir" / "gui_token"
    written = write_token("mysecret-abc", path=target)
    assert written == target
    assert target.read_text(encoding="utf-8") == "mysecret-abc"
    # tmp side-file should not linger.
    tmp_side = target.with_suffix(target.suffix + ".tmp")
    assert not tmp_side.exists()
    if sys.platform != "win32":
        # On POSIX, file mode must be exactly 0600.
        mode = stat.S_IMODE(target.stat().st_mode)
        assert mode == 0o600, f"expected 0o600, got {oct(mode)}"


def test_write_token_rejects_empty_string(tmp_path):
    with pytest.raises(TokenAuthError):
        write_token("", path=tmp_path / "gui_token")


def test_write_token_overwrite_replaces_atomically(tmp_path):
    target = tmp_path / "gui_token"
    write_token("first", path=target)
    write_token("second", path=target)
    assert target.read_text(encoding="utf-8") == "second"


def test_read_token_absent_returns_none(tmp_path):
    assert read_token(path=tmp_path / "no_such_file") is None


def test_read_token_round_trip(tmp_path):
    target = tmp_path / "gui_token"
    written = write_token("round-trip-token-xyz", path=target)
    assert read_token(path=written) == "round-trip-token-xyz"


# ── header verification (constant-time) ──────────────────────


def test_verify_token_header_happy_path():
    tok = generate_token()
    assert verify_token_header(f"Bearer {tok}", tok) is True


def test_verify_token_header_case_insensitive_scheme():
    tok = "abc123"
    assert verify_token_header("bearer abc123", tok) is True
    assert verify_token_header("BEARER abc123", tok) is True


def test_verify_token_header_rejects_wrong_scheme():
    tok = "abc123"
    assert verify_token_header("Basic abc123", tok) is False


def test_verify_token_header_rejects_wrong_token():
    assert verify_token_header("Bearer wrong", "right") is False


def test_verify_token_header_rejects_missing_or_empty():
    tok = "abc123"
    assert verify_token_header(None, tok) is False
    assert verify_token_header("", tok) is False
    assert verify_token_header("Bearer ", tok) is False


def test_verify_token_header_handles_quoted_token():
    tok = "abc123"
    assert verify_token_header('Bearer "abc123"', tok) is True


# ── middleware end-to-end ────────────────────────────────────


@pytest.fixture
def app_with_known_token(tmp_path):
    """Build a fresh app with a known token + isolated token path."""
    from concinno.gui.server import create_app

    token_path = tmp_path / "gui_token"
    app = create_app(token="UNIT-TEST-TOKEN", token_path=token_path)
    return app, "UNIT-TEST-TOKEN", token_path


def test_create_app_writes_token_to_disk(app_with_known_token):
    _app, token, token_path = app_with_known_token
    assert token_path.is_file()
    assert token_path.read_text(encoding="utf-8") == token


def test_health_route_bypasses_auth(app_with_known_token):
    from fastapi.testclient import TestClient

    app, _tok, _path = app_with_known_token
    with TestClient(app) as client:
        r = client.get("/api/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}


def test_features_route_requires_bearer(app_with_known_token):
    from fastapi.testclient import TestClient

    app, _tok, _path = app_with_known_token
    with TestClient(app) as client:
        r = client.get("/api/features")
        assert r.status_code == 401


def test_features_route_accepts_correct_bearer(app_with_known_token):
    from fastapi.testclient import TestClient

    app, tok, _path = app_with_known_token
    with TestClient(app) as client:
        r = client.get(
            "/api/features",
            headers={"Authorization": f"Bearer {tok}"},
        )
        assert r.status_code == 200
        assert "features" in r.json()


def test_features_route_rejects_wrong_bearer(app_with_known_token):
    from fastapi.testclient import TestClient

    app, _tok, _path = app_with_known_token
    with TestClient(app) as client:
        r = client.get(
            "/api/features",
            headers={"Authorization": "Bearer not-the-token"},
        )
        assert r.status_code == 401


def test_create_app_factory_no_args_still_works(tmp_path, monkeypatch):
    """uvicorn ``factory=True`` calls ``create_app()`` with no args.

    Redirect the token path away from the user's real home so the
    test does not stomp the real ``~/.concinno/gui_token``.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))
    from concinno.gui.server import create_app

    app = create_app()
    # App should expose its (auto-generated) token via app.state for tests.
    assert app.state.gui_token  # non-empty string
    assert isinstance(app.state.gui_token, str)
    assert app.state.gui_token_path is not None


def test_token_never_empty_string_after_write(tmp_path):
    target = tmp_path / "gui_token"
    write_token("non-empty", path=target)
    assert target.read_text(encoding="utf-8")
    # And the same path can be re-read by read_token.
    assert read_token(path=target) == "non-empty"


def test_module_exports_match_dunder_all():
    # Sanity: __all__ list and actual public surface line up.
    expected = {"get_token_path", "generate_token", "write_token",
                "read_token", "verify_token_header", "TokenAuthError"}
    assert expected.issubset(set(gui_auth.__all__))
    for name in expected:
        assert hasattr(gui_auth, name)


def test_chmod_failure_does_not_corrupt_existing_file(tmp_path, monkeypatch):
    """Smoke: a failed chmod should leave the target unchanged.

    Implementation detail: tmp file is written first, chmodded, then
    renamed. If the chmod step fails on POSIX we should NOT have a
    half-written target hiding in place.
    """
    if sys.platform == "win32":
        pytest.skip("chmod path is POSIX-only")

    target = tmp_path / "gui_token"
    write_token("original", path=target)

    def boom(*_a, **_k):
        raise OSError("simulated chmod failure")

    monkeypatch.setattr(os, "chmod", boom)
    with pytest.raises(TokenAuthError):
        write_token("replacement", path=target)
    # Original survives.
    assert target.read_text(encoding="utf-8") == "original"
