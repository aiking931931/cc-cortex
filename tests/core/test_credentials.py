"""Tests for concinno.core.credentials — 4-source precedence + thread safety."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from concinno.core.credentials import (
    CredentialStore,
    _env_key_for,
    _resolve_ref,
    get_default_store,
)

# ── _env_key_for ───────────────────────────────────────────────────────


class TestEnvKeyFor:
    def test_simple_key(self):
        assert _env_key_for("api_key") == "CONCINNO_CRED_API_KEY"

    def test_mixed_case_upper(self):
        assert _env_key_for("GoogleOAuth") == "CONCINNO_CRED_GOOGLEOAUTH"

    def test_non_alnum_sanitized(self):
        # `-` and `.` become `_`
        assert _env_key_for("api-key.v1") == "CONCINNO_CRED_API_KEY_V1"

    def test_empty_key(self):
        # Degenerate but shouldn't crash; env name is still valid shape.
        assert _env_key_for("") == "CONCINNO_CRED_"


# ── _resolve_ref ───────────────────────────────────────────────────────


class TestResolveRef:
    def test_plain_string_passthrough(self):
        assert _resolve_ref("hello") == "hello"

    def test_plain_dict_passthrough(self):
        assert _resolve_ref({"not_ref": "x"}) == {"not_ref": "x"}

    def test_env_ref_resolves(self, monkeypatch):
        monkeypatch.setenv("MY_SECRET_42", "sk-abc")
        assert _resolve_ref({"$ref": "env:MY_SECRET_42"}) == "sk-abc"

    def test_env_ref_missing_returns_none(self, monkeypatch):
        monkeypatch.delenv("NOPE_SECRET_XYZ", raising=False)
        assert _resolve_ref({"$ref": "env:NOPE_SECRET_XYZ"}) is None

    def test_unknown_ref_scheme_passthrough(self):
        # file: scheme not implemented — returned untouched for future extension
        value = {"$ref": "file:/tmp/x"}
        assert _resolve_ref(value) == value

    def test_none_passthrough(self):
        assert _resolve_ref(None) is None


# ── Source precedence ──────────────────────────────────────────────────


class TestPrecedence:
    def test_default_when_missing(self, tmp_path):
        cfg = tmp_path / "credentials.json"
        store = CredentialStore(config_path=cfg)
        assert store.get("nope") is None
        assert store.get("nope", default="fallback") == "fallback"

    def test_file_source(self, tmp_path):
        cfg = tmp_path / "credentials.json"
        cfg.write_text(json.dumps({"k": "from_file"}), encoding="utf-8")
        store = CredentialStore(config_path=cfg)
        assert store.get("k") == "from_file"

    def test_env_overrides_file(self, tmp_path, monkeypatch):
        cfg = tmp_path / "credentials.json"
        cfg.write_text(json.dumps({"k": "from_file"}), encoding="utf-8")
        monkeypatch.setenv("CONCINNO_CRED_K", "from_env")
        store = CredentialStore(config_path=cfg)
        assert store.get("k") == "from_env"

    def test_runtime_overrides_env(self, tmp_path, monkeypatch):
        cfg = tmp_path / "credentials.json"
        cfg.write_text(json.dumps({"k": "from_file"}), encoding="utf-8")
        monkeypatch.setenv("CONCINNO_CRED_K", "from_env")
        store = CredentialStore(config_path=cfg)
        store.set("k", "from_runtime")
        assert store.get("k") == "from_runtime"

    def test_runtime_delete_falls_back(self, tmp_path, monkeypatch):
        cfg = tmp_path / "credentials.json"
        cfg.write_text(json.dumps({"k": "from_file"}), encoding="utf-8")
        monkeypatch.setenv("CONCINNO_CRED_K", "from_env")
        store = CredentialStore(config_path=cfg)
        store.set("k", "from_runtime")
        store.delete("k")
        assert store.get("k") == "from_env"  # falls back to env

    def test_delete_missing_is_silent(self, tmp_path):
        cfg = tmp_path / "credentials.json"
        store = CredentialStore(config_path=cfg)
        # Should not raise.
        store.delete("never_set")


# ── $ref dereference ───────────────────────────────────────────────────


class TestRefDereference:
    def test_env_ref_in_file(self, tmp_path, monkeypatch):
        cfg = tmp_path / "credentials.json"
        cfg.write_text(
            json.dumps({"token": {"$ref": "env:MY_TOK_42"}}),
            encoding="utf-8",
        )
        monkeypatch.setenv("MY_TOK_42", "secret123")
        store = CredentialStore(config_path=cfg)
        assert store.get("token") == "secret123"

    def test_env_ref_missing_falls_through_to_default(self, tmp_path, monkeypatch):
        cfg = tmp_path / "credentials.json"
        cfg.write_text(
            json.dumps({"token": {"$ref": "env:MISSING_XYZ"}}),
            encoding="utf-8",
        )
        monkeypatch.delenv("MISSING_XYZ", raising=False)
        store = CredentialStore(config_path=cfg)
        # $ref resolves to None → treat as missing → default returned.
        assert store.get("token", default="fallback") == "fallback"


# ── File edge cases ────────────────────────────────────────────────────


class TestFileEdgeCases:
    def test_malformed_json_treated_as_empty(self, tmp_path, caplog):
        cfg = tmp_path / "credentials.json"
        cfg.write_text("{ not valid json", encoding="utf-8")
        import logging as _logging

        with caplog.at_level(_logging.WARNING, logger="concinno.core.credentials"):
            store = CredentialStore(config_path=cfg)
            assert store.get("anything") is None
        assert any("failed to read" in rec.message for rec in caplog.records)

    def test_file_is_list_not_dict(self, tmp_path, caplog):
        cfg = tmp_path / "credentials.json"
        cfg.write_text(json.dumps(["a", "b"]), encoding="utf-8")
        import logging as _logging

        with caplog.at_level(_logging.WARNING, logger="concinno.core.credentials"):
            store = CredentialStore(config_path=cfg)
            assert store.get("anything") is None
        assert any("not a JSON object" in rec.message for rec in caplog.records)

    def test_reload_picks_up_new_file(self, tmp_path):
        cfg = tmp_path / "credentials.json"
        cfg.write_text(json.dumps({"k": "v1"}), encoding="utf-8")
        store = CredentialStore(config_path=cfg)
        assert store.get("k") == "v1"
        # Rewrite & reload.
        cfg.write_text(json.dumps({"k": "v2"}), encoding="utf-8")
        assert store.get("k") == "v1"  # still cached
        store.reload()
        assert store.get("k") == "v2"


# ── keys() / has() ─────────────────────────────────────────────────────


class TestIntrospection:
    def test_keys_union(self, tmp_path):
        cfg = tmp_path / "credentials.json"
        cfg.write_text(json.dumps({"a": 1, "b": 2}), encoding="utf-8")
        store = CredentialStore(config_path=cfg)
        store.set("c", 3)
        assert store.keys() == ["a", "b", "c"]

    def test_has_true_for_file(self, tmp_path):
        cfg = tmp_path / "credentials.json"
        cfg.write_text(json.dumps({"k": "v"}), encoding="utf-8")
        store = CredentialStore(config_path=cfg)
        assert store.has("k") is True

    def test_has_false_for_missing(self, tmp_path):
        cfg = tmp_path / "credentials.json"
        store = CredentialStore(config_path=cfg)
        assert store.has("never") is False

    def test_has_true_for_env(self, tmp_path, monkeypatch):
        cfg = tmp_path / "credentials.json"
        monkeypatch.setenv("CONCINNO_CRED_ENVONLY", "x")
        store = CredentialStore(config_path=cfg)
        assert store.has("envonly") is True


# ── Thread safety ──────────────────────────────────────────────────────


class TestThreadSafety:
    def test_concurrent_set_get(self, tmp_path):
        cfg = tmp_path / "credentials.json"
        store = CredentialStore(config_path=cfg)
        errors: list[BaseException] = []

        def worker(i: int) -> None:
            try:
                for j in range(100):
                    store.set(f"k{i}", f"v{j}")
                    store.get(f"k{i}")
            except BaseException as exc:  # noqa: BLE001 — collect for assert
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        # Every writer's final value should be "v99" (last iteration).
        for i in range(8):
            assert store.get(f"k{i}") == "v99"


# ── Default store ──────────────────────────────────────────────────────


class TestDefaultStore:
    def test_singleton_same_instance(self):
        a = get_default_store()
        b = get_default_store()
        assert a is b

    def test_home_path_used_by_default(self):
        store = CredentialStore()
        expected = Path.home() / ".concinno" / "credentials.json"
        # _config_path is private — we read it via get() on a known-missing key
        # and assert behavior is a no-op (no crash even if file absent).
        assert store.get("never_defined_key") is None
        # And the path attr should equal the home-rooted default.
        assert store._config_path == expected


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
