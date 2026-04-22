"""concinno.tools.mcp_bridge — MCP server adapter (fallback only).

@module tools.mcp_bridge
@responsibility Wrap an external MCP server as a set of Concinno
    :class:`Tool` protocol instances. Used only when a skill has no
    native Python library and we have to reach an MCP-speaking
    service. Native Python paths are strictly preferred (MEMORY #36:
    in-process ★★★★★ vs MCP ★).
@dependencies subprocess (stdlib); concinno.tool_executor.Tool
    (Protocol check only). Optional ``mcp`` PyPI package is *not*
    required — we speak raw JSON-RPC 2.0 over stdio to avoid a hard
    runtime dependency. Consumers who prefer the official SDK can
    wrap their own bridge around it.
@exports MCPBridge, MCPTool, bridge_mcp_server

Protocol: JSON-RPC 2.0 framed one-message-per-line over stdin/stdout
of the subprocess. This matches the stdio transport that every MCP
server supports. We implement the bare minimum: ``initialize``,
``tools/list``, ``tools/call``.

Subprocess lifecycle: the bridge starts the server on first
:meth:`MCPBridge.list_tools` (or first ``call`` on any adapter). It
shuts the subprocess down on :meth:`MCPBridge.close` / GC. One
subprocess per bridge — callers that want multiple MCP servers
instantiate multiple bridges.

Thread safety: a per-bridge ``threading.Lock`` serialises all RPC
exchanges so concurrent ``tool.call()`` from the daemon's per-
connection threads don't interleave requests on stdio.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading
from typing import Any

logger = logging.getLogger("concinno.tools.mcp_bridge")

_REQUEST_TIMEOUT_S = 30.0
_MAX_LINE_BYTES = 4 * 1024 * 1024


class MCPBridge:
    """Driver for one MCP server subprocess.

    Lazy-starts the process on first RPC; subsequent calls reuse the
    live connection. ``close()`` terminates the subprocess cleanly.
    """

    def __init__(
        self,
        server_cmd: list[str] | str,
        *,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        timeout_s: float = _REQUEST_TIMEOUT_S,
    ) -> None:
        if isinstance(server_cmd, str):
            server_cmd = server_cmd.split()
        self._cmd = list(server_cmd)
        self._env = env
        self._cwd = cwd
        self._timeout_s = timeout_s
        self._proc: subprocess.Popen[bytes] | None = None
        self._lock = threading.Lock()
        self._id_counter = 0
        self._initialized = False

    # ── Lifecycle ─────────────────────────────────────────────────

    def _ensure_started(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return
        logger.info("mcp_bridge: starting %s", self._cmd)
        self._proc = subprocess.Popen(
            self._cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self._env,
            cwd=self._cwd,
            bufsize=0,
        )
        self._initialized = False

    def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        result = self._rpc(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "concinno-mcp-bridge", "version": "0.1.0"},
            },
        )
        # Some servers expect a follow-up "initialized" notification.
        self._notify("notifications/initialized", {})
        self._initialized = True
        logger.debug("mcp_bridge: initialize result: %r", result)

    def _close_stdin(self) -> None:
        assert self._proc is not None
        if self._proc.stdin is None:
            return
        try:
            self._proc.stdin.close()
        except OSError:
            pass

    def _drain_or_kill(self) -> None:
        """Wait for graceful exit; escalate to terminate/kill on timeout."""
        assert self._proc is not None
        try:
            self._proc.wait(timeout=1.5)
            return
        except subprocess.TimeoutExpired:
            pass
        self._proc.terminate()
        try:
            self._proc.wait(timeout=1.5)
            return
        except subprocess.TimeoutExpired:
            pass
        self._proc.kill()

    def close(self) -> None:
        if self._proc is None:
            return
        try:
            if self._proc.poll() is None:
                self._close_stdin()
                self._drain_or_kill()
        finally:
            self._proc = None
            self._initialized = False

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:  # noqa: BLE001 — __del__ must not raise
            pass

    # ── RPC ───────────────────────────────────────────────────────

    def _next_id(self) -> int:
        self._id_counter += 1
        return self._id_counter

    def _send(self, payload: dict[str, Any]) -> None:
        assert self._proc is not None
        assert self._proc.stdin is not None
        data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        self._proc.stdin.write(data)
        self._proc.stdin.flush()

    def _recv_line(self) -> bytes:
        assert self._proc is not None
        assert self._proc.stdout is not None
        line = self._proc.stdout.readline()
        if not line:
            stderr = b""
            if self._proc.stderr is not None:
                try:
                    stderr = self._proc.stderr.read() or b""
                except OSError:
                    pass
            msg = f"mcp server closed stdout; stderr={stderr!r}"
            raise RuntimeError(msg)
        if len(line) > _MAX_LINE_BYTES:
            msg = "mcp server sent response exceeding max line size"
            raise RuntimeError(msg)
        return line

    def _rpc(self, method: str, params: dict[str, Any]) -> Any:
        with self._lock:
            self._ensure_started()
            req_id = self._next_id()
            self._send(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "method": method,
                    "params": params,
                }
            )
            # Drain lines until we see our response id. Notifications (no id)
            # or other ids are logged and skipped.
            while True:
                line = self._recv_line()
                try:
                    msg = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    logger.warning("mcp_bridge: dropping bad line: %s", exc)
                    continue
                if msg.get("id") != req_id:
                    logger.debug("mcp_bridge: skipping out-of-band message %r", msg)
                    continue
                if "error" in msg:
                    err = msg["error"]
                    raise RuntimeError(f"mcp error: {err!r}")
                return msg.get("result")

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        with self._lock:
            self._ensure_started()
            self._send(
                {
                    "jsonrpc": "2.0",
                    "method": method,
                    "params": params,
                }
            )

    # ── Public API ────────────────────────────────────────────────

    def list_tools(self) -> list[dict[str, Any]]:
        """Return the MCP server's advertised tools."""
        self._ensure_started()
        self._ensure_initialized()
        result = self._rpc("tools/list", {})
        if not isinstance(result, dict):
            return []
        tools = result.get("tools") or []
        return tools if isinstance(tools, list) else []

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """Invoke a tool on the MCP server, returning ``result.content``."""
        self._ensure_started()
        self._ensure_initialized()
        result = self._rpc(
            "tools/call",
            {"name": name, "arguments": arguments},
        )
        if isinstance(result, dict):
            return result.get("content", result)
        return result


class MCPTool:
    """Concinno Tool-protocol wrapper around one MCP server tool.

    ``is_concurrency_safe`` is always False because all MCP calls
    share a single subprocess pipe guarded by the bridge's lock —
    parallel dispatch would serialise inside the bridge anyway and
    would block other connections, so the executor should keep these
    serial from its point of view.
    """

    is_concurrency_safe: bool = False

    def __init__(
        self,
        bridge: MCPBridge,
        remote_name: str,
        name: str,
        description: str,
    ) -> None:
        self._bridge = bridge
        self._remote_name = remote_name
        self.name = name
        self.description = description

    def call(self, **kwargs: Any) -> Any:
        return self._bridge.call_tool(self._remote_name, kwargs)


def bridge_mcp_server(
    server_cmd: list[str] | str,
    *,
    prefix: str = "",
    env: dict[str, str] | None = None,
    cwd: str | None = None,
) -> tuple[MCPBridge, list[MCPTool]]:
    """Start an MCP server and wrap its tools.

    Args:
        server_cmd: Executable + args (list form preferred; string is
            shlex-split on whitespace).
        prefix: Prepended to each tool's local name to avoid registry
            collisions. Example: prefix ``"gh_"`` + remote ``"issues"``
            → local name ``"gh_issues"``.
        env: Optional subprocess environment override.
        cwd: Optional working directory for the subprocess.

    Returns:
        ``(bridge, tools)`` — keep a reference to the bridge (or pass
        it to :meth:`MCPBridge.close` explicitly) so the subprocess
        outlives the tool list. Tools share the bridge for RPC.
    """
    bridge = MCPBridge(server_cmd, env=env, cwd=cwd)
    remote_tools = bridge.list_tools()
    wrapped: list[MCPTool] = []
    for t in remote_tools:
        if not isinstance(t, dict):
            continue
        remote_name = t.get("name")
        if not isinstance(remote_name, str) or not remote_name:
            continue
        description = t.get("description") or f"MCP tool: {remote_name}"
        local_name = f"{prefix}{remote_name}" if prefix else remote_name
        wrapped.append(
            MCPTool(
                bridge=bridge,
                remote_name=remote_name,
                name=local_name,
                description=str(description),
            )
        )
    return bridge, wrapped


def _check_optional_mcp_sdk_available() -> bool:
    """Return True if the official ``mcp`` SDK is importable.

    The bridge itself does not need it — we speak raw JSON-RPC — but
    ecosystem code that expects the SDK can check here before calling.
    """
    try:
        import mcp  # type: ignore[import-not-found]  # noqa: F401
    except ImportError:
        return False
    return True


__all__ = [
    "MCPBridge",
    "MCPTool",
    "bridge_mcp_server",
]


if __name__ == "__main__":
    # Smoke-mode: start a server, list tools, print JSON.
    if len(sys.argv) < 2:
        print("usage: python -m concinno.tools.mcp_bridge <server-cmd...>", file=sys.stderr)
        raise SystemExit(2)
    br = MCPBridge(sys.argv[1:])
    try:
        tools = br.list_tools()
        print(json.dumps(tools, indent=2, ensure_ascii=False))
    finally:
        br.close()
