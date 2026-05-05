"""llama-cpp-python runtime backend + server launcher.

Drop-in replacement for Ollama when running Gemma 4 / Gemma 3 GGUF weights.
Matches the OpenAI /v1/chat/completions shape persona-api already speaks.

Minimum Concinno version: 2.20.0
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

import httpx
from openai import OpenAI

from concinno.llm_runtime.base import ChatMessage

_DEFAULT_BASE_URL = "http://127.0.0.1:9000"
_DEFAULT_MODEL = "gguf"
_DEFAULT_TIMEOUT = 300.0
_USER_CONFIG_PATH = Path.home() / ".concinno" / "llm_runtime.json"


def _load_user_config() -> dict[str, Any]:
    """Read ``~/.concinno/llm_runtime.json`` if present.

    Missing file / invalid JSON → return empty dict (callers fall back to
    baked-in defaults + env vars).
    """
    try:
        raw = _USER_CONFIG_PATH.read_text(encoding="utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError):
        return {}


class LlamaCppBackend:
    """Wraps an OpenAI client pointing at a local llama-cpp-python server.

    Unlike Ollama, llama-cpp-python's built-in server rejects unknown
    `extra_body.options` keys silently. This backend therefore emits only
    vanilla OpenAI chat.completions arguments.
    """

    def __init__(
        self,
        base_url: str = _DEFAULT_BASE_URL,
        model: str = _DEFAULT_MODEL,
        api_key: str = "none",
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._client = OpenAI(
            base_url=f"{self.base_url}/v1",
            api_key=api_key,
            timeout=timeout,
        )

    @classmethod
    def from_config(
        cls,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float | None = None,
    ) -> "LlamaCppBackend":
        """Resolve settings via the Concinno 6-source chain.

        Precedence (later wins):

        1. baked-in defaults (``_DEFAULT_*``)
        2. ``~/.concinno/llm_runtime.json`` keys ``base_url`` / ``model`` /
           ``timeout``
        3. env vars ``CONCINNO_LLM_RUNTIME_BASE_URL`` /
           ``CONCINNO_LLM_RUNTIME_MODEL`` /
           ``CONCINNO_LLM_RUNTIME_TIMEOUT``
        4. explicit kwargs (this call)

        Invalid env / JSON values fall through to the previous layer rather
        than raising; this keeps test and recovery paths resilient.
        """
        cfg = _load_user_config()
        resolved_base_url = cfg.get("base_url", _DEFAULT_BASE_URL)
        resolved_model = cfg.get("model", _DEFAULT_MODEL)
        resolved_timeout = cfg.get("timeout", _DEFAULT_TIMEOUT)

        env_base_url = os.environ.get("CONCINNO_LLM_RUNTIME_BASE_URL")
        if env_base_url:
            resolved_base_url = env_base_url
        env_model = os.environ.get("CONCINNO_LLM_RUNTIME_MODEL")
        if env_model:
            resolved_model = env_model
        env_timeout = os.environ.get("CONCINNO_LLM_RUNTIME_TIMEOUT")
        if env_timeout:
            try:
                resolved_timeout = float(env_timeout)
            except ValueError:
                pass

        if base_url is not None:
            resolved_base_url = base_url
        if model is not None:
            resolved_model = model
        if timeout is not None:
            resolved_timeout = timeout

        return cls(
            base_url=resolved_base_url,
            model=resolved_model,
            timeout=float(resolved_timeout),
        )

    def chat(
        self,
        system: str,
        messages: list[ChatMessage],
        max_tokens: int = 2000,
        temperature: float = 0.3,
    ) -> str:
        msgs: list[dict[str, Any]] = [{"role": "system", "content": system}]
        msgs.extend(dict(m) for m in messages)
        try:
            resp = self._client.chat.completions.create(
                model=self.model,
                messages=msgs,  # type: ignore[arg-type]
                max_tokens=max_tokens,
                temperature=temperature,
            )
            return resp.choices[0].message.content or ""
        except Exception:
            return ""

    def health(self) -> bool:
        try:
            r = httpx.get(f"{self.base_url}/v1/models", timeout=5.0)
            return r.status_code == 200
        except Exception:
            return False


class LlamaCppServer(AbstractContextManager["LlamaCppServer"]):
    """Spawn and manage a `python -m llama_cpp.server` subprocess.

    Use as a context manager; __enter__ blocks until /v1/models responds
    200 or `startup_timeout` elapses (in which case RuntimeError is
    raised and the subprocess is terminated).

    Example:
        with LlamaCppServer(model_path="/p/to.gguf", port=9000) as s:
            backend = LlamaCppBackend(base_url=s.base_url)
            out = backend.chat("You answer.", [{"role":"user","content":"2+2"}])
    """

    def __init__(
        self,
        model_path: str,
        port: int = 9000,
        host: str = "127.0.0.1",
        n_ctx: int = 8192,
        n_gpu_layers: int = -1,
        flash_attn: bool = True,
        startup_timeout: float = 180.0,
        log_path: str | None = None,
        python: str = "python3",
    ) -> None:
        self.model_path = model_path
        self.host = host
        self.port = port
        self.n_ctx = n_ctx
        self.n_gpu_layers = n_gpu_layers
        self.flash_attn = flash_attn
        self.startup_timeout = startup_timeout
        self.log_path = log_path
        self.python = python
        self._proc: subprocess.Popen[bytes] | None = None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def _argv(self) -> list[str]:
        argv = [
            self.python, "-m", "llama_cpp.server",
            "--model", self.model_path,
            "--host", self.host,
            "--port", str(self.port),
            "--n_ctx", str(self.n_ctx),
            "--n_gpu_layers", str(self.n_gpu_layers),
        ]
        if self.flash_attn:
            argv += ["--flash_attn", "true"]
        return argv

    def start(self) -> "LlamaCppServer":
        if self._proc is not None:
            return self
        log_fp: Any
        log_fp = open(self.log_path, "ab") if self.log_path else subprocess.DEVNULL
        self._proc = subprocess.Popen(
            self._argv(),
            stdout=log_fp,
            stderr=subprocess.STDOUT,
        )
        deadline = time.time() + self.startup_timeout
        while time.time() < deadline:
            if self._proc.poll() is not None:
                raise RuntimeError(
                    f"llama_cpp.server exited early rc={self._proc.returncode}"
                )
            try:
                r = httpx.get(f"{self.base_url}/v1/models", timeout=2.0)
                if r.status_code == 200:
                    return self
            except (httpx.HTTPError, OSError):
                pass
            time.sleep(1.0)
        self.stop()
        raise RuntimeError(
            f"llama_cpp.server did not become healthy within "
            f"{self.startup_timeout}s"
        )

    def stop(self, kill_timeout: float = 10.0) -> None:
        if self._proc is None:
            return
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=kill_timeout)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait(timeout=5.0)
        self._proc = None

    def __enter__(self) -> "LlamaCppServer":
        return self.start()

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.stop()
