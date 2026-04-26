"""Tests for ``concinno.state_client``.

Covers the full public API of the v1 file backend, the Sancio HTTP
fallback chain (with a mocked daemon), TTL semantics, project name
canonicalization, concurrent writes, and the legacy-fallback read path.

The Sancio daemon is **not** shipped yet; the tests verify that the
``auto`` backend silently degrades to ``file`` when the daemon is
unreachable, and uses ``sancio_http`` when a fake daemon is wired in.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from concinno import state_client as sc
from concinno.state_client import (
    BackendUnavailable,
    StateClient,
    StateClientConfig,
    canonicalize_project,
    load_config,
    reset_default_client,
)

# ── Shared fixtures ──────────────────────────────────────────────────


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    """Isolated state directory per test (overrides ~/.sancio/state)."""
    d = tmp_path / "sancio_state"
    d.mkdir()
    return d


@pytest.fixture
def legacy_dir(tmp_path: Path) -> Path:
    """Isolated legacy directory per test (overrides ~/.concinno/state)."""
    d = tmp_path / "concinno_state"
    d.mkdir()
    return d


@pytest.fixture
def file_only_config(state_dir: Path, legacy_dir: Path) -> StateClientConfig:
    """Config that pins the file backend (no Sancio probe)."""
    return StateClientConfig(
        preferred_backend="file",
        sancio_port=8530,
        state_dir=state_dir,
        legacy_dir=legacy_dir,
        source="test",
    )


@pytest.fixture
def auto_config(state_dir: Path, legacy_dir: Path) -> StateClientConfig:
    """Config that lets state_client auto-pick (file when daemon down)."""
    return StateClientConfig(
        preferred_backend="auto",
        sancio_port=8530,
        state_dir=state_dir,
        legacy_dir=legacy_dir,
        source="test",
    )


@pytest.fixture(autouse=True)
def _reset_default():
    """Tests must not see each other's default client."""
    reset_default_client()
    yield
    reset_default_client()


# ── Project name canonicalization (test 9) ───────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Sancio", "sancio"),
        (" Arb Bot ", "arb_bot"),
        ("foo/bar", "foo_bar"),
        (".concinno.", "concinno"),
        ("", "_unknown"),
        ("Cigito\\v2", "cigito_v2"),
        ("  ", "_unknown"),
        ("digital-persona", "digital-persona"),
    ],
)
def test_canonicalize_project(raw: str, expected: str) -> None:
    assert canonicalize_project(raw) == expected


def test_canonicalize_project_non_string() -> None:
    assert canonicalize_project(None) == "_unknown"  # type: ignore[arg-type]
    assert canonicalize_project(123) == "_unknown"  # type: ignore[arg-type]


# ── File backend round-trip (tests 1, 2, 3, 4, 12, 16, 17) ───────────


def test_set_then_get_roundtrip_file_backend(
    file_only_config: StateClientConfig,
) -> None:
    client = StateClient(config=file_only_config)
    client.set("gaia", "pod_ssh_port", 24831)
    assert client.get("gaia", "pod_ssh_port") == 24831


def test_get_returns_none_for_absent_key(
    file_only_config: StateClientConfig,
) -> None:
    client = StateClient(config=file_only_config)
    assert client.get("gaia", "missing") is None


def test_set_complex_values(file_only_config: StateClientConfig) -> None:
    client = StateClient(config=file_only_config)
    payload = {
        "host": "ssh.runpod.io", "port": 24831,
        "tags": ["A100", "us-west"], "uptime_hours": 1.5,
    }
    client.set("gaia", "pod_info", payload)
    assert client.get("gaia", "pod_info") == payload


def test_snapshot_returns_all_keys(
    file_only_config: StateClientConfig,
) -> None:
    client = StateClient(config=file_only_config)
    client.set("gaia", "k1", "v1")
    client.set("gaia", "k2", 42)
    client.set("gaia", "k3", [1, 2, 3])

    snap = client.snapshot("gaia")
    assert snap == {"k1": "v1", "k2": 42, "k3": [1, 2, 3]}


def test_snapshot_of_unknown_project_is_empty(
    file_only_config: StateClientConfig,
) -> None:
    client = StateClient(config=file_only_config)
    assert client.snapshot("never_existed") == {}


def test_list_keys(file_only_config: StateClientConfig) -> None:
    client = StateClient(config=file_only_config)
    client.set("gaia", "a", 1)
    client.set("gaia", "b", 2)
    client.set("gaia", "c", 3)
    assert client.list_keys("gaia") == ["a", "b", "c"]


def test_delete_removes_key(file_only_config: StateClientConfig) -> None:
    client = StateClient(config=file_only_config)
    client.set("gaia", "to_remove", "bye")
    assert client.get("gaia", "to_remove") == "bye"

    client.delete("gaia", "to_remove")
    assert client.get("gaia", "to_remove") is None
    assert "to_remove" not in client.list_keys("gaia")


def test_delete_absent_key_is_noop(
    file_only_config: StateClientConfig,
) -> None:
    client = StateClient(config=file_only_config)
    # Project file doesn't exist yet — delete should not raise
    client.delete("gaia", "never_set")

    client.set("gaia", "real", 1)
    client.delete("gaia", "still_never_set")  # file exists, key absent
    assert client.get("gaia", "real") == 1


def test_file_backend_auto_creates_state_dir(tmp_path: Path) -> None:
    """state_dir does not exist at construction → first ``set`` creates it."""
    nested = tmp_path / "deeply" / "nested" / "state"
    cfg = StateClientConfig(
        preferred_backend="file",
        state_dir=nested,
        legacy_dir=tmp_path / "legacy",
        source="test",
    )
    assert not nested.exists()

    client = StateClient(config=cfg)
    client.set("p", "k", "v")

    assert nested.exists()
    assert (nested / "p.json").is_file()


# ── TTL semantics (tests 7, 8) ───────────────────────────────────────


def test_ttl_expiry_returns_none(
    file_only_config: StateClientConfig,
) -> None:
    client = StateClient(config=file_only_config)
    client.set("gaia", "ephemeral", "transient", ttl_seconds=1)
    assert client.get("gaia", "ephemeral") == "transient"

    time.sleep(1.1)
    assert client.get("gaia", "ephemeral") is None


def test_ttl_none_never_expires(
    file_only_config: StateClientConfig,
) -> None:
    client = StateClient(config=file_only_config)
    client.set("gaia", "permanent", "stays", ttl_seconds=None)
    # Sleep a touch and confirm not expired
    time.sleep(0.1)
    assert client.get("gaia", "permanent") == "stays"


def test_ttl_excludes_expired_from_snapshot(
    file_only_config: StateClientConfig,
) -> None:
    client = StateClient(config=file_only_config)
    client.set("gaia", "live", "alive", ttl_seconds=None)
    client.set("gaia", "ghost", "boo", ttl_seconds=1)

    time.sleep(1.1)
    snap = client.snapshot("gaia")
    assert snap == {"live": "alive"}
    assert client.list_keys("gaia") == ["live"]


def test_ttl_lazy_gc_on_next_set(
    file_only_config: StateClientConfig, state_dir: Path,
) -> None:
    """Expired neighbours are pruned from the file when any ``set`` runs."""
    client = StateClient(config=file_only_config)
    client.set("gaia", "ghost", "boo", ttl_seconds=1)
    client.set("gaia", "live", "ok", ttl_seconds=None)

    time.sleep(1.1)
    client.set("gaia", "fresh", 1)  # triggers GC

    raw = json.loads((state_dir / "gaia.json").read_text(encoding="utf-8"))
    assert "ghost" not in raw
    assert "live" in raw
    assert "fresh" in raw


# ── Health / backend identification (tests 10, 11, 18) ───────────────


def test_health_reports_file_backend(
    file_only_config: StateClientConfig,
) -> None:
    client = StateClient(config=file_only_config)
    h = client.health()
    assert h["backend"] == "file"
    assert h["writable"] is True
    assert h["preferred_backend"] == "file"


def test_health_reports_sancio_when_daemon_up(
    auto_config: StateClientConfig,
) -> None:
    """Wire a fake HTTP client whose health() returns True."""

    class FakeHTTP:
        def health(self) -> bool:
            return True

        # Stubs — health() is the only method exercised here.
        def snapshot(self, project: str) -> dict[str, Any]:
            return {}

        def get(self, project: str, key: str) -> Any:
            return None

        def set(
            self, project: str, key: str, value: Any,
            ttl_seconds: int | None = None,
        ) -> None:
            pass

        def delete(self, project: str, key: str) -> None:
            pass

        def list_keys(self, project: str) -> list[str]:
            return []

    client = StateClient(config=auto_config, http_client=FakeHTTP())
    h = client.health()
    assert h["backend"] == "sancio_http"
    assert h["writable"] is True


def test_sancio_500_silently_degrades_to_file(
    state_dir: Path, legacy_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Daemon returns HTTP 500 → fall back to file, log to stderr.

    Uses ``preferred_backend='sancio_http'`` to exercise the explicit
    "user picked sancio but daemon is dead" code path that emits the
    audible warning. ``auto`` mode degrades silently by design.
    """
    cfg = StateClientConfig(
        preferred_backend="sancio_http",
        sancio_port=8530,
        state_dir=state_dir,
        legacy_dir=legacy_dir,
        source="test",
    )

    class Failing500HTTP:
        def health(self) -> bool:
            # Simulate the spec §6.4 "Network errors talking to Sancio
            # daemon → silently degrade to file backend"
            return False

        def snapshot(self, project: str) -> dict[str, Any]:
            raise BackendUnavailable("http 500")

        def get(self, project: str, key: str) -> Any:
            raise BackendUnavailable("http 500")

        def set(
            self, project: str, key: str, value: Any,
            ttl_seconds: int | None = None,
        ) -> None:
            raise BackendUnavailable("http 500")

        def delete(self, project: str, key: str) -> None:
            raise BackendUnavailable("http 500")

        def list_keys(self, project: str) -> list[str]:
            raise BackendUnavailable("http 500")

    client = StateClient(config=cfg, http_client=Failing500HTTP())

    # Even with a "broken daemon", set + get round-trip via file backend.
    client.set("gaia", "pod_port", 24831)
    assert client.get("gaia", "pod_port") == 24831

    err = capsys.readouterr().err
    assert "sancio daemon unreachable" in err


# ── Concurrent writes (test 13) ──────────────────────────────────────


def test_concurrent_writes_serialized(
    file_only_config: StateClientConfig,
) -> None:
    client = StateClient(config=file_only_config)
    keys = [f"k{i}" for i in range(5)]

    def writer(k: str) -> None:
        client.set("gaia", k, k.upper())

    threads = [threading.Thread(target=writer, args=(k,)) for k in keys]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    snap = client.snapshot("gaia")
    assert snap == {k: k.upper() for k in keys}


# ── Legacy fallback (test 14) ────────────────────────────────────────


def test_legacy_fallback_when_file_backend_empty(
    auto_config: StateClientConfig,
    state_dir: Path, legacy_dir: Path,
) -> None:
    """Legacy ``~/.concinno/state/<p>.json`` exists but new file backend
    file does not → snapshot reads from legacy."""
    # Seed legacy file with envelope-shaped data
    legacy_path = legacy_dir / "ancient_proj.json"
    legacy_path.write_text(json.dumps({
        "old_key": {"value": "from_legacy", "expires_at": None,
                    "set_at": "2020-01-01T00:00:00+00:00"},
    }), encoding="utf-8")
    # Force daemon-down with an http client whose health is False
    class DownHTTP:
        def health(self): return False
        def snapshot(self, p): return {}
        def get(self, p, k): return None
        def set(self, p, k, v, ttl_seconds=None): pass
        def delete(self, p, k): pass
        def list_keys(self, p): return []

    client = StateClient(config=auto_config, http_client=DownHTTP())
    # Confirm file backend has no file for this project
    assert not (state_dir / "ancient_proj.json").exists()

    assert client.snapshot("ancient_proj") == {"old_key": "from_legacy"}
    assert client.get("ancient_proj", "old_key") == "from_legacy"


def test_legacy_fallback_with_raw_value_envelope(
    auto_config: StateClientConfig, legacy_dir: Path,
) -> None:
    """Hand-edited legacy files may have raw values rather than envelopes
    — _unwrap should return them as-is."""
    legacy_path = legacy_dir / "raw_proj.json"
    legacy_path.write_text(json.dumps({
        "plain_key": "plain_value",
    }), encoding="utf-8")

    class DownHTTP:
        def health(self): return False
        def snapshot(self, p): return {}
        def get(self, p, k): return None
        def set(self, p, k, v, ttl_seconds=None): pass
        def delete(self, p, k): pass
        def list_keys(self, p): return []

    client = StateClient(config=auto_config, http_client=DownHTTP())
    assert client.get("raw_proj", "plain_key") == "plain_value"


# ── Error paths (test 15) ────────────────────────────────────────────


def test_set_non_json_serializable_raises(
    file_only_config: StateClientConfig,
) -> None:
    client = StateClient(config=file_only_config)
    with pytest.raises(TypeError) as excinfo:
        client.set("gaia", "bad", {1, 2, 3})  # set is not JSON-serializable
    msg = str(excinfo.value)
    assert "not JSON-serializable" in msg
    # File should not have been created — pre-flight serialize check
    assert client.snapshot("gaia") == {}


def test_set_empty_key_raises(file_only_config: StateClientConfig) -> None:
    client = StateClient(config=file_only_config)
    with pytest.raises(ValueError):
        client.set("gaia", "", "value")


# ── Config loader ────────────────────────────────────────────────────


def test_load_config_env_overrides_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg_file = tmp_path / "state_client.json"
    cfg_file.write_text(json.dumps({
        "preferred_backend": "file",
        "sancio_port": 9999,
    }), encoding="utf-8")
    monkeypatch.setenv("CONCINNO_STATE_BACKEND", "sancio_http")
    monkeypatch.setenv("SANCIO_STATE_STORE_PORT", "8888")
    monkeypatch.setenv("SANCIO_STATE_DIR", str(tmp_path / "custom_state"))

    cfg = load_config(path=cfg_file)
    assert cfg.preferred_backend == "sancio_http"
    assert cfg.sancio_port == 8888
    assert cfg.state_dir == tmp_path / "custom_state"
    assert "env:backend" in cfg.source
    assert "env:port" in cfg.source
    assert "env:dir" in cfg.source


def test_load_config_invalid_env_logged_not_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CONCINNO_STATE_BACKEND", "nonsense")
    monkeypatch.setenv("SANCIO_STATE_STORE_PORT", "not-a-number")

    cfg = load_config(path=Path("/nonexistent/state_client.json"))
    assert cfg.preferred_backend == "auto"  # invalid value ignored
    assert cfg.sancio_port == sc.DEFAULT_SANCIO_PORT
    assert any("CONCINNO_STATE_BACKEND" in w for w in cfg.warnings)
    assert any("SANCIO_STATE_STORE_PORT" in w for w in cfg.warnings)


def test_load_config_malformed_file_logged_not_raised(tmp_path: Path) -> None:
    bad = tmp_path / "broken.json"
    bad.write_text("{this is not json", encoding="utf-8")

    cfg = load_config(path=bad)
    # Falls back to defaults
    assert cfg.preferred_backend == "auto"
    assert any("malformed JSON" in w for w in cfg.warnings)


# ── Module-level convenience singleton ───────────────────────────────


def test_module_level_get_set_uses_default_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Module-level ``get`` / ``set`` / etc. all share a single
    ``default_client()`` — verify by isolating with env vars."""
    monkeypatch.setenv("SANCIO_STATE_DIR", str(tmp_path / "modlevel"))
    monkeypatch.setenv("CONCINNO_STATE_BACKEND", "file")
    reset_default_client()

    sc.set("modproj", "alpha", 1)
    sc.set("modproj", "beta", "two")

    assert sc.get("modproj", "alpha") == 1
    assert sorted(sc.list_keys("modproj")) == ["alpha", "beta"]
    assert sc.snapshot("modproj") == {"alpha": 1, "beta": "two"}

    sc.delete("modproj", "alpha")
    assert sc.get("modproj", "alpha") is None
    assert sc.health()["backend"] == "file"


# ── Sancio HTTP client direct unit (urllib mocked) ───────────────────


def test_sancio_http_unreachable_returns_false() -> None:
    """``_SancioHTTP.health()`` returns False on connection error."""
    # Pick a port that is virtually guaranteed not to be listening.
    # urllib will refuse fast; SANCIO_HTTP_TIMEOUT (0.25s) bounds worst case.
    http = sc._SancioHTTP(port=1, timeout=0.1)
    assert http.health() is False


def test_sancio_http_health_via_mocked_urlopen() -> None:
    """When urllib succeeds, health() returns True."""

    class FakeResp:
        status = 200

        def read(self) -> bytes:
            return b'{"ok": true}'

        def __enter__(self) -> "FakeResp":
            return self

        def __exit__(self, *args: Any) -> None:
            return None

    http = sc._SancioHTTP(port=8530, timeout=0.5)
    with patch("concinno.state_client.urllib_request.urlopen",
               return_value=FakeResp()):
        assert http.health() is True


# ── Daemon-down auto fallback (covers spec §6 fallback chain) ────────


def test_auto_backend_falls_back_to_file_when_daemon_unreachable(
    auto_config: StateClientConfig, capsys: pytest.CaptureFixture[str],
) -> None:
    """Real ``_SancioHTTP`` against a closed port → file backend used."""
    auto_config_unreachable_port = StateClientConfig(
        preferred_backend="auto",
        sancio_port=1,  # nothing listens here
        state_dir=auto_config.state_dir,
        legacy_dir=auto_config.legacy_dir,
        source="test",
    )
    # Use real _SancioHTTP — exercises actual stdlib urllib failure path
    real_http = sc._SancioHTTP(port=1, timeout=0.05)
    client = StateClient(
        config=auto_config_unreachable_port, http_client=real_http,
    )

    client.set("gaia", "x", 1)
    assert client.get("gaia", "x") == 1
    assert client.health()["backend"] == "file"


# ── Cross-process write race using two StateClient instances ─────────


def test_two_clients_share_file_backend(
    file_only_config: StateClientConfig,
) -> None:
    """A second client sees writes from the first via the JSON file."""
    a = StateClient(config=file_only_config)
    b = StateClient(config=file_only_config)
    a.set("gaia", "shared", "from_a")
    assert b.get("gaia", "shared") == "from_a"

    b.set("gaia", "another", "from_b")
    assert a.get("gaia", "another") == "from_b"
    assert sorted(a.list_keys("gaia")) == ["another", "shared"]
