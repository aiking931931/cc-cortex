"""Smoke tests for concinno.gui.server REST surface.

Skips cleanly when ``concinno[gui]`` extras are not installed — keeps the
default pytest run green for operators who did not opt into FastAPI.
"""

from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi")  # noqa: F401
testclient = pytest.importorskip("fastapi.testclient")


@pytest.fixture
def client(tmp_path, monkeypatch):
    """TestClient pre-authenticated with the per-process bearer token.

    Concinno 2.36.0a1 added :class:`BearerTokenMiddleware`; legacy tests
    were written before auth existed. Rather than rewrite every test,
    we (a) point the token file at a tmp dir so the real
    ``~/.concinno/gui_token`` is not stomped, and (b) inject the token
    as a default header on the TestClient.
    """
    from fastapi.testclient import TestClient

    from concinno.gui.server import create_app

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))

    token = "TEST-FIXTURE-TOKEN"
    app = create_app(token=token, token_path=tmp_path / "gui_token")
    tc = TestClient(app)
    tc.headers.update({"Authorization": f"Bearer {token}"})
    return tc


def test_list_features_shape(client):
    r = client.get("/api/features")
    assert r.status_code == 200
    data = r.json()
    assert "features" in data
    assert isinstance(data["features"], list)
    assert len(data["features"]) >= 8  # at least the 8 GAIA switches
    sample = data["features"][0]
    for k in ("name", "category", "params", "enabled",
              "ziq_autotunable", "cosmetic", "effect_scope"):
        assert k in sample


def test_effect_scope_valid(client):
    r = client.get("/api/features")
    scopes = {f["effect_scope"] for f in r.json()["features"]}
    assert scopes.issubset({"immediate", "process_restart", "session_restart"})


def test_effect_scope_session_restart_for_session_switches(client):
    r = client.get("/api/features/session_switches")
    assert r.status_code == 200
    assert r.json()["effect_scope"] == "session_restart"


def test_effect_scope_immediate_default(client):
    r = client.get("/api/features/gaia_tool_router")
    assert r.status_code == 200
    assert r.json()["effect_scope"] == "immediate"


def test_get_feature_known(client):
    r = client.get("/api/features/gaia_tool_router")
    assert r.status_code == 200
    data = r.json()
    assert data["name"] == "gaia_tool_router"
    assert data["category"] == "context"


def test_get_feature_unknown_404(client):
    r = client.get("/api/features/nonexistent_feature_xyz")
    assert r.status_code == 404


def test_post_feature_toggle(client, tmp_path, monkeypatch):
    # Redirect concinno config to a tmp dir so the test doesn't mutate
    # the real user config file. concinno.core.config.get_config reads
    # CONCINNO_CONFIG_DIR when present.
    monkeypatch.setenv("CONCINNO_CONFIG_DIR", str(tmp_path))
    # Toggle a benign feature off then on.
    r1 = client.post(
        "/api/features/image_upscale_4x",
        json={"key": "enabled", "value": False},
    )
    assert r1.status_code == 200
    body = r1.json()
    assert body["feature"] == "image_upscale_4x"
    assert body["key"] == "enabled"


def test_post_feature_bad_body(client):
    r = client.post("/api/features/gaia_tool_router", json={})
    assert r.status_code == 400


def test_harness_settings_reports_files(client):
    r = client.get("/api/harness/settings")
    assert r.status_code == 200
    data = r.json()
    assert "files" in data
    for entry in data["files"]:
        assert "path" in entry
        assert "present" in entry


def test_ziq_posterior_shape(client):
    r = client.get("/api/ziq/posterior")
    assert r.status_code == 200
    data = r.json()
    assert "present" in data
    assert "posterior" in data


def test_concinno_state_shape(client):
    r = client.get("/api/concinno/state")
    assert r.status_code == 200
    data = r.json()
    assert "release_authorization" in data


def test_root_serves_index(client):
    r = client.get("/")
    # Either 200 (static mounted) or 404 (static dir missing in env)
    assert r.status_code in (200, 404)
    if r.status_code == 200:
        assert "Concinno Config" in r.text


def test_run_refuses_public_bind_without_env(monkeypatch):
    from concinno.gui.server import run

    monkeypatch.delenv("CONCINNO_GUI_ALLOW_PUBLIC_BIND", raising=False)
    with pytest.raises(SystemExit):
        run(host="0.0.0.0", port=8400)


def test_run_allows_localhost(monkeypatch):
    # Stub uvicorn.run so we don't actually bind.
    import sys
    import types

    fake = types.ModuleType("uvicorn")
    called = {}

    def _fake_run(*args, **kwargs):
        called["args"] = args
        called["kwargs"] = kwargs
    fake.run = _fake_run
    monkeypatch.setitem(sys.modules, "uvicorn", fake)

    from concinno.gui.server import run

    run(host="127.0.0.1", port=8401)
    assert called["kwargs"]["host"] == "127.0.0.1"
    assert called["kwargs"]["port"] == 8401


def test_static_dir_present():
    from concinno.gui.server import STATIC_DIR

    assert STATIC_DIR.is_dir(), f"static dir missing: {STATIC_DIR}"
    for name in ("index.html", "app.js", "style.css"):
        assert (STATIC_DIR / name).is_file(), f"missing {name}"


# ── 2.23.0 additions ────────────────────────────────────────

def test_digest_endpoint(client):
    r = client.get("/api/features/digest")
    assert r.status_code == 200
    data = r.json()
    assert "digest" in data and isinstance(data["digest"], str)
    assert "mtime" in data


def test_feature_entry_has_example_and_ziq_fields(client):
    r = client.get("/api/features/gaia_tool_router")
    assert r.status_code == 200
    data = r.json()
    for k in ("example", "ziq_opt_out", "ziq_effective"):
        assert k in data
    # ziq_effective == ziq_autotunable AND NOT ziq_opt_out
    assert data["ziq_effective"] == (data["ziq_autotunable"] and not data["ziq_opt_out"])


def test_feature_param_has_manual_pinned_flag(client):
    r = client.get("/api/features/image_upscale_4x")
    assert r.status_code == 200
    data = r.json()
    p = data["params"]["min_side"]
    assert "manual_pinned" in p
    assert "is_modified" in p


def test_ziq_posterior_has_overrides_list(client):
    r = client.get("/api/ziq/posterior")
    data = r.json()
    assert "overrides" in data  # always present even when file absent
    assert isinstance(data["overrides"], list)


def test_post_ziq_opt_out_accepted(client, tmp_path, monkeypatch):
    monkeypatch.setenv("CONCINNO_CONFIG_DIR", str(tmp_path))
    r = client.post(
        "/api/features/gaia_tool_router",
        json={"key": "ziq_opt_out", "value": True},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["key"] == "ziq_opt_out"


def test_post_manual_pin_accepted(client, tmp_path, monkeypatch):
    monkeypatch.setenv("CONCINNO_CONFIG_DIR", str(tmp_path))
    r = client.post(
        "/api/features/image_upscale_4x",
        json={"key": "min_side__pinned", "value": True},
    )
    assert r.status_code == 200


def test_example_populated_for_gaia_features(client):
    r = client.get("/api/features")
    examples = {f["name"]: f["example"] for f in r.json()["features"]}
    for name in ("gaia_tool_router", "gaia_music_image_upscale",
                 "gaia_polygon_image_upscale", "image_upscale_4x"):
        assert examples.get(name), f"missing example for {name}"
