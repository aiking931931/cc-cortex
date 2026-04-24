"""Tests for ``concinno.llm_runtime`` — LlamaCppBackend + LlamaCppServer.

The package is pure Python glue around ``openai`` + ``httpx`` +
``subprocess``; every networking touchpoint is mocked so this suite
runs without llama-cpp-python or a live server. Integration probes
against a real ``python -m llama_cpp.server`` live in
``experiments/gaia_31b/`` and document the diagnostic that motivated
the 2.20.0 split (Ollama degenerate loop vs direct llama.cpp).
"""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from concinno.llm_runtime import (
    LlamaCppBackend,
    LlamaCppServer,
)
from concinno.llm_runtime import llamacpp as llamacpp_mod
from concinno.llm_runtime.llamacpp import (
    _DEFAULT_BASE_URL,
    _DEFAULT_MODEL,
    _load_user_config,
)

# ── LlamaCppBackend.chat ──────────────────────────────────────────────────


def _fake_chat_response(content: str = "FINAL ANSWER: 4"):
    """Minimal shape matching openai.types.chat.ChatCompletion."""
    message = SimpleNamespace(content=content)
    choice = SimpleNamespace(message=message, finish_reason="stop")
    return SimpleNamespace(choices=[choice])


def test_chat_returns_assistant_content():
    backend = LlamaCppBackend(base_url="http://example:9000")
    mock_create = MagicMock(return_value=_fake_chat_response("pong"))
    backend._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=mock_create))
    )

    out = backend.chat(
        "You answer.",
        [{"role": "user", "content": "ping"}],
        max_tokens=50,
    )

    assert out == "pong"
    kwargs = mock_create.call_args.kwargs
    assert kwargs["model"] == _DEFAULT_MODEL
    assert kwargs["max_tokens"] == 50
    assert kwargs["messages"][0] == {"role": "system", "content": "You answer."}
    assert kwargs["messages"][1] == {"role": "user", "content": "ping"}


def test_chat_returns_empty_string_on_none_content():
    backend = LlamaCppBackend()
    mock_create = MagicMock(return_value=_fake_chat_response(None))
    backend._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=mock_create))
    )
    assert backend.chat("sys", [{"role": "user", "content": "x"}]) == ""


def test_chat_never_raises_on_provider_error():
    backend = LlamaCppBackend()
    mock_create = MagicMock(side_effect=RuntimeError("upstream blew up"))
    backend._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=mock_create))
    )
    assert backend.chat("sys", [{"role": "user", "content": "x"}]) == ""


def test_chat_does_not_send_ollama_options_extra_body():
    """Regression: 2.19.x gaia_agent passed ``extra_body={"options": {...}}``.
    llama-cpp-python's server 400s on unknown extras — the backend MUST NOT
    forward that key.
    """
    backend = LlamaCppBackend()
    mock_create = MagicMock(return_value=_fake_chat_response("ok"))
    backend._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=mock_create))
    )
    backend.chat("sys", [{"role": "user", "content": "x"}])
    kwargs = mock_create.call_args.kwargs
    assert "extra_body" not in kwargs
    assert "options" not in kwargs


def test_base_url_trailing_slash_stripped():
    backend = LlamaCppBackend(base_url="http://foo:9000/")
    assert backend.base_url == "http://foo:9000"


# ── LlamaCppBackend.health ───────────────────────────────────────────────


def test_health_true_when_models_endpoint_200():
    backend = LlamaCppBackend(base_url="http://foo:9000")
    with patch("concinno.llm_runtime.llamacpp.httpx.get") as g:
        g.return_value = SimpleNamespace(status_code=200)
        assert backend.health() is True
        g.assert_called_once_with("http://foo:9000/v1/models", timeout=5.0)


def test_health_false_when_models_endpoint_non_200():
    backend = LlamaCppBackend()
    with patch("concinno.llm_runtime.llamacpp.httpx.get") as g:
        g.return_value = SimpleNamespace(status_code=500)
        assert backend.health() is False


def test_health_false_when_connection_refused():
    backend = LlamaCppBackend()
    with patch(
        "concinno.llm_runtime.llamacpp.httpx.get",
        side_effect=OSError("ECONNREFUSED"),
    ):
        assert backend.health() is False


# ── from_config — 6-source precedence ─────────────────────────────────────


def test_from_config_defaults(monkeypatch, tmp_path):
    """No JSON, no env vars → baked-in defaults."""
    monkeypatch.setattr(llamacpp_mod, "_USER_CONFIG_PATH", tmp_path / "none.json")
    for k in (
        "CONCINNO_LLM_RUNTIME_BASE_URL",
        "CONCINNO_LLM_RUNTIME_MODEL",
        "CONCINNO_LLM_RUNTIME_TIMEOUT",
    ):
        monkeypatch.delenv(k, raising=False)

    backend = LlamaCppBackend.from_config()
    assert backend.base_url == _DEFAULT_BASE_URL.rstrip("/")
    assert backend.model == _DEFAULT_MODEL


def test_from_config_json_overrides_default(monkeypatch, tmp_path):
    cfg_path = tmp_path / "llm_runtime.json"
    cfg_path.write_text(
        json.dumps({"base_url": "http://json:1111", "model": "json-model"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(llamacpp_mod, "_USER_CONFIG_PATH", cfg_path)
    for k in (
        "CONCINNO_LLM_RUNTIME_BASE_URL",
        "CONCINNO_LLM_RUNTIME_MODEL",
        "CONCINNO_LLM_RUNTIME_TIMEOUT",
    ):
        monkeypatch.delenv(k, raising=False)

    backend = LlamaCppBackend.from_config()
    assert backend.base_url == "http://json:1111"
    assert backend.model == "json-model"


def test_from_config_env_overrides_json(monkeypatch, tmp_path):
    cfg_path = tmp_path / "llm_runtime.json"
    cfg_path.write_text(
        json.dumps({"base_url": "http://json:1111", "model": "json-model"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(llamacpp_mod, "_USER_CONFIG_PATH", cfg_path)
    monkeypatch.setenv("CONCINNO_LLM_RUNTIME_BASE_URL", "http://env:2222")
    monkeypatch.setenv("CONCINNO_LLM_RUNTIME_MODEL", "env-model")
    monkeypatch.setenv("CONCINNO_LLM_RUNTIME_TIMEOUT", "42")

    backend = LlamaCppBackend.from_config()
    assert backend.base_url == "http://env:2222"
    assert backend.model == "env-model"


def test_from_config_kwarg_overrides_env(monkeypatch, tmp_path):
    monkeypatch.setattr(llamacpp_mod, "_USER_CONFIG_PATH", tmp_path / "none.json")
    monkeypatch.setenv("CONCINNO_LLM_RUNTIME_BASE_URL", "http://env:2222")

    backend = LlamaCppBackend.from_config(base_url="http://kwarg:3333")
    assert backend.base_url == "http://kwarg:3333"


def test_from_config_bad_env_timeout_silently_falls_through(monkeypatch, tmp_path):
    monkeypatch.setattr(llamacpp_mod, "_USER_CONFIG_PATH", tmp_path / "none.json")
    monkeypatch.setenv("CONCINNO_LLM_RUNTIME_TIMEOUT", "not-a-number")

    # Doesn't raise. Default survives.
    backend = LlamaCppBackend.from_config()
    assert backend.base_url == _DEFAULT_BASE_URL
    # Internal client still constructed with default timeout.
    assert backend._client is not None


# ── _load_user_config ─────────────────────────────────────────────────────


def test_load_user_config_missing_returns_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(llamacpp_mod, "_USER_CONFIG_PATH", tmp_path / "ghost.json")
    assert _load_user_config() == {}


def test_load_user_config_malformed_json_returns_empty(monkeypatch, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not valid", encoding="utf-8")
    monkeypatch.setattr(llamacpp_mod, "_USER_CONFIG_PATH", bad)
    assert _load_user_config() == {}


def test_load_user_config_non_dict_returns_empty(monkeypatch, tmp_path):
    arr = tmp_path / "arr.json"
    arr.write_text("[1, 2, 3]", encoding="utf-8")
    monkeypatch.setattr(llamacpp_mod, "_USER_CONFIG_PATH", arr)
    assert _load_user_config() == {}


# ── LlamaCppServer — argv + lifecycle (Popen mocked) ──────────────────────


def test_server_argv_default():
    s = LlamaCppServer(model_path="/p/to.gguf")
    argv = s._argv()
    assert argv[:3] == ["python3", "-m", "llama_cpp.server"]
    assert "--model" in argv and "/p/to.gguf" in argv
    assert "--host" in argv and "127.0.0.1" in argv
    assert "--port" in argv and "9000" in argv
    assert "--n_ctx" in argv and "8192" in argv
    assert "--n_gpu_layers" in argv and "-1" in argv
    assert "--flash_attn" in argv and "true" in argv


def test_server_argv_flash_attn_off():
    s = LlamaCppServer(model_path="/p/to.gguf", flash_attn=False)
    argv = s._argv()
    assert "--flash_attn" not in argv


def test_server_argv_custom_port_and_ctx():
    s = LlamaCppServer(model_path="/p/to.gguf", port=9999, n_ctx=32768)
    argv = s._argv()
    assert "9999" in argv and "32768" in argv


def test_server_base_url_computed():
    s = LlamaCppServer(model_path="/p/to.gguf", host="0.0.0.0", port=9001)
    assert s.base_url == "http://0.0.0.0:9001"


def test_server_start_raises_when_subprocess_exits_early(monkeypatch):
    fake_proc = MagicMock()
    fake_proc.poll.return_value = 1
    fake_proc.returncode = 1
    monkeypatch.setattr(
        "concinno.llm_runtime.llamacpp.subprocess.Popen",
        lambda *a, **kw: fake_proc,
    )
    s = LlamaCppServer(model_path="/p/to.gguf", startup_timeout=5)
    with pytest.raises(RuntimeError, match="exited early"):
        s.start()


def test_server_start_raises_on_health_timeout(monkeypatch):
    """Subprocess stays alive but /v1/models never 200s within timeout."""
    fake_proc = MagicMock()
    fake_proc.poll.return_value = None  # still running
    monkeypatch.setattr(
        "concinno.llm_runtime.llamacpp.subprocess.Popen",
        lambda *a, **kw: fake_proc,
    )
    with patch(
        "concinno.llm_runtime.llamacpp.httpx.get",
        side_effect=OSError("refused"),
    ):
        with patch(
            "concinno.llm_runtime.llamacpp.time.sleep",
            return_value=None,
        ):
            s = LlamaCppServer(
                model_path="/p/to.gguf",
                startup_timeout=0.01,
            )
            with pytest.raises(RuntimeError, match="did not become healthy"):
                s.start()
    # stop() was called → proc.terminate invoked
    fake_proc.terminate.assert_called()


def test_server_stop_noop_when_never_started():
    s = LlamaCppServer(model_path="/p/to.gguf")
    s.stop()  # no exception
    assert s._proc is None


def test_server_context_manager_uses_start_and_stop(monkeypatch):
    calls = {"start": 0, "stop": 0}

    class _S(LlamaCppServer):
        def start(self):
            calls["start"] += 1
            return self

        def stop(self, kill_timeout=10.0):
            calls["stop"] += 1

    with _S(model_path="/p/to.gguf"):
        pass

    assert calls == {"start": 1, "stop": 1}


def test_server_stop_falls_back_to_kill_on_wait_timeout(monkeypatch):
    fake_proc = MagicMock()
    fake_proc.poll.return_value = None  # still running after terminate
    fake_proc.wait.side_effect = [
        subprocess.TimeoutExpired(cmd="x", timeout=1),  # after terminate
        None,  # after kill
    ]
    s = LlamaCppServer(model_path="/p/to.gguf")
    s._proc = fake_proc
    s.stop(kill_timeout=0.01)
    fake_proc.terminate.assert_called_once()
    fake_proc.kill.assert_called_once()
    assert s._proc is None
