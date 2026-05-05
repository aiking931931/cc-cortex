"""Tests for concinno.tools.mcp_bridge — MCP subprocess wrapper.

We spin up a tiny fake MCP server written in Python that speaks
JSON-RPC 2.0 over stdio. This verifies the raw wire format without
requiring the upstream ``mcp`` SDK.
"""

from __future__ import annotations

import sys
import textwrap

import pytest

from concinno.tools.mcp_bridge import (
    MCPBridge,
    MCPTool,
    bridge_mcp_server,
)

# ── Fake MCP server ────────────────────────────────────────────────────


_FAKE_SERVER = textwrap.dedent(
    """
    import json, sys

    def write(msg):
        sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\\n")
        sys.stdout.flush()

    TOOLS = [
        {"name": "echo", "description": "Echo the input string."},
        {"name": "add",  "description": "Return a + b."},
    ]

    while True:
        line = sys.stdin.readline()
        if not line:
            break
        try:
            req = json.loads(line)
        except Exception:
            continue
        method = req.get("method")
        req_id = req.get("id")
        # Notifications (no id) are silently accepted.
        if req_id is None:
            continue
        if method == "initialize":
            write({"jsonrpc": "2.0", "id": req_id, "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "serverInfo": {"name": "fake-mcp", "version": "0.0.1"},
            }})
        elif method == "tools/list":
            write({"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            params = req.get("params", {})
            name = params.get("name")
            args = params.get("arguments", {})
            if name == "echo":
                out = {"content": args.get("text", "")}
            elif name == "add":
                out = {"content": args.get("a", 0) + args.get("b", 0)}
            else:
                write({"jsonrpc": "2.0", "id": req_id, "error":
                    {"code": -32601, "message": f"no such tool: {name}"}})
                continue
            write({"jsonrpc": "2.0", "id": req_id, "result": out})
        else:
            write({"jsonrpc": "2.0", "id": req_id, "error":
                {"code": -32601, "message": f"unknown method: {method}"}})
    """
).strip()


@pytest.fixture
def fake_server_cmd(tmp_path) -> list[str]:
    server_script = tmp_path / "fake_mcp.py"
    server_script.write_text(_FAKE_SERVER, encoding="utf-8")
    return [sys.executable, str(server_script)]


# ── list_tools / call_tool ─────────────────────────────────────────────


class TestMCPBridge:
    def test_list_tools(self, fake_server_cmd):
        br = MCPBridge(fake_server_cmd)
        try:
            tools = br.list_tools()
            names = [t["name"] for t in tools]
            assert "echo" in names
            assert "add" in names
        finally:
            br.close()

    def test_call_tool_echo(self, fake_server_cmd):
        br = MCPBridge(fake_server_cmd)
        try:
            br.list_tools()  # triggers init
            result = br.call_tool("echo", {"text": "hello"})
            assert result == "hello"
        finally:
            br.close()

    def test_call_tool_add(self, fake_server_cmd):
        br = MCPBridge(fake_server_cmd)
        try:
            br.list_tools()
            result = br.call_tool("add", {"a": 2, "b": 40})
            assert result == 42
        finally:
            br.close()

    def test_call_unknown_raises(self, fake_server_cmd):
        br = MCPBridge(fake_server_cmd)
        try:
            br.list_tools()
            with pytest.raises(RuntimeError, match="no such tool"):
                br.call_tool("nope", {})
        finally:
            br.close()

    def test_string_cmd_is_split(self, fake_server_cmd):
        # Pass the command as space-separated string — should split OK.
        cmd_str = " ".join(fake_server_cmd)
        br = MCPBridge(cmd_str)
        try:
            tools = br.list_tools()
            assert len(tools) >= 1
        finally:
            br.close()

    def test_idempotent_close(self, fake_server_cmd):
        br = MCPBridge(fake_server_cmd)
        br.close()
        br.close()  # no raise

    def test_init_happens_once(self, fake_server_cmd):
        br = MCPBridge(fake_server_cmd)
        try:
            br.list_tools()
            # Second list should reuse the initialized subprocess.
            tools2 = br.list_tools()
            assert len(tools2) >= 1
        finally:
            br.close()


# ── bridge_mcp_server wrapper ──────────────────────────────────────────


class TestBridgeMCPServer:
    def test_wraps_tools_as_MCPTool(self, fake_server_cmd):
        bridge, tools = bridge_mcp_server(fake_server_cmd)
        try:
            assert all(isinstance(t, MCPTool) for t in tools)
            # Name carried through, default no prefix.
            names = {t.name for t in tools}
            assert {"echo", "add"}.issubset(names)
            # is_concurrency_safe must be False.
            assert all(t.is_concurrency_safe is False for t in tools)
        finally:
            bridge.close()

    def test_prefix_applied(self, fake_server_cmd):
        bridge, tools = bridge_mcp_server(fake_server_cmd, prefix="fake_")
        try:
            names = {t.name for t in tools}
            assert "fake_echo" in names
            assert "fake_add" in names
            # Original remote name preserved for dispatch (private attr OK in test).
            echo = next(t for t in tools if t.name == "fake_echo")
            assert echo._remote_name == "echo"
        finally:
            bridge.close()

    def test_mcp_tool_call_roundtrip(self, fake_server_cmd):
        bridge, tools = bridge_mcp_server(fake_server_cmd, prefix="x_")
        try:
            echo = next(t for t in tools if t.name == "x_echo")
            assert echo.call(text="roundtrip") == "roundtrip"
            addt = next(t for t in tools if t.name == "x_add")
            assert addt.call(a=10, b=5) == 15
        finally:
            bridge.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
