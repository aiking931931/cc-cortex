"""A2A JSON-RPC server for CCC Guard Agent.

Implements the Google A2A protocol (JSON-RPC binding)
with SendMessage, GetTask, CancelTask endpoints.
"""

from __future__ import annotations

import json
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from cc_cortex.a2a.agent import GuardAgent

# ---------------------------------------------------------------------------
# In-memory task store
# ---------------------------------------------------------------------------
_tasks: dict[str, dict] = {}
_agent: GuardAgent | None = None


def _get_agent() -> GuardAgent:
    global _agent
    if _agent is None:
        _agent = GuardAgent()
    return _agent


# ---------------------------------------------------------------------------
# Agent Card
# ---------------------------------------------------------------------------
AGENT_CARD = {
    "name": "CC-Cortex Guard Agent",
    "description": (
        "Four-layer AI safety agent: L1 CCC 55+ guard pipeline (0ms, "
        "regex + heuristic, SECURITY/QUALITY/COGNITIVE layers), "
        "L2 LLM semantic judge, L3 safe task execution, "
        "L4 CCC post-tool output check. Zero-dep core, optional LLM."
    ),
    "version": "1.5.1",
    "supportedInterfaces": [
        {
            "url": "http://localhost:8420/rpc",
            "protocolBinding": "JSONRPC",
            "protocolVersion": "1.0",
        }
    ],
    "provider": {
        "organization": "CC-Cortex",
        "url": "https://github.com/anthropics-community/cc-cortex",
    },
    "capabilities": {
        "streaming": False,
        "pushNotifications": False,
        "stateTransitionHistory": True,
        "extendedAgentCard": False,
    },
    "defaultInputModes": ["text/plain", "application/json"],
    "defaultOutputModes": ["text/plain", "application/json"],
    "skills": [
        {
            "id": "guard-eval",
            "name": "Four-Layer Safety Evaluation",
            "description": (
                "Evaluate input through regex + LLM safety judge + "
                "task execution + output filtering. Handles jailbreak, "
                "injection, exfiltration, and policy violations."
            ),
            "tags": ["security", "safety", "guard", "jailbreak", "injection"],
            "inputModes": ["text/plain", "application/json"],
            "outputModes": ["text/plain", "application/json"],
        }
    ],
}


# ---------------------------------------------------------------------------
# JSON-RPC handlers
# ---------------------------------------------------------------------------

def _handle_send_message(params: dict) -> dict:
    """Handle SendMessage — run guard pipeline on incoming message."""
    agent = _get_agent()
    msg = params.get("message", {})
    parts = msg.get("parts", [])
    msg_id = msg.get("messageId", str(uuid.uuid4()))
    context_id = msg.get("contextId", str(uuid.uuid4()))
    task_id = msg.get("taskId") or str(uuid.uuid4())

    result = agent.handle_a2a_message(parts)

    response_msg = {
        "messageId": str(uuid.uuid4()),
        "role": "ROLE_AGENT",
        "parts": result["response_parts"],
        "metadata": {
            "blocked": result["blocked"],
            "guard_count": result["guard_count"],
            "status": result["status"],
        },
    }

    task = {
        "id": task_id,
        "contextId": context_id,
        "status": {
            "state": "TASK_STATE_COMPLETED",
            "message": response_msg,
            "timestamp": _iso_now(),
        },
        "artifacts": [
            {
                "artifactId": str(uuid.uuid4()),
                "name": "Guard Evaluation Result",
                "parts": [
                    {"data": result["details"], "mediaType": "application/json"},
                    *result["response_parts"],
                ],
            }
        ],
        "history": [
            {"messageId": msg_id, "role": "ROLE_USER", "parts": parts},
            response_msg,
        ],
    }

    _tasks[task_id] = task
    return {"task": task}


def _handle_get_task(params: dict) -> dict:
    """Handle GetTask — return stored task by ID."""
    task_id = params.get("id", "")
    task = _tasks.get(task_id)
    if task is None:
        return {"error": {"code": -32001, "message": f"Task not found: {task_id}"}}
    return {"task": task}


def _handle_list_tasks(params: dict) -> dict:
    """Handle ListTasks."""
    tasks = list(_tasks.values())
    context_id = params.get("contextId")
    if context_id:
        tasks = [t for t in tasks if t.get("contextId") == context_id]
    return {"tasks": tasks, "nextPageToken": ""}


def _handle_cancel_task(params: dict) -> dict:
    """Handle CancelTask."""
    task_id = params.get("id", "")
    task = _tasks.get(task_id)
    if task is None:
        return {"error": {"code": -32001, "message": f"Task not found: {task_id}"}}
    terminal = ("TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "TASK_STATE_CANCELED")
    if task["status"]["state"] in terminal:
        return {"error": {"code": -32002, "message": "Task already in terminal state"}}
    task["status"]["state"] = "TASK_STATE_CANCELED"
    task["status"]["timestamp"] = _iso_now()
    return {"task": task}


_RPC_METHODS = {
    "SendMessage": _handle_send_message,
    "GetTask": _handle_get_task,
    "ListTasks": _handle_list_tasks,
    "CancelTask": _handle_cancel_task,
}


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())


# ---------------------------------------------------------------------------
# HTTP Server
# ---------------------------------------------------------------------------

class A2AHandler(BaseHTTPRequestHandler):
    """Handles A2A JSON-RPC and Agent Card discovery."""

    def do_GET(self) -> None:
        if self.path == "/.well-known/agent-card.json":
            body = json.dumps(AGENT_CARD, indent=2).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        if self.path not in ("/rpc", "/a2a"):
            self.send_error(404)
            return

        content_length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(content_length)

        try:
            req = json.loads(raw)
        except json.JSONDecodeError:
            self._json_response(400, {"error": "Invalid JSON"})
            return

        method = req.get("method", "")
        params = req.get("params", {})
        req_id = req.get("id", 1)

        handler = _RPC_METHODS.get(method)
        if handler is None:
            self._json_response(200, {
                "jsonrpc": "2.0", "id": req_id,
                "error": {"code": -32601, "message": f"Unknown method: {method}"},
            })
            return

        result = handler(params)

        if "error" in result and "task" not in result:
            self._json_response(200, {
                "jsonrpc": "2.0", "id": req_id, "error": result["error"],
            })
        else:
            self._json_response(200, {
                "jsonrpc": "2.0", "id": req_id, "result": result,
            })

    def _json_response(self, status: int, body: dict) -> None:
        data = json.dumps(body, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: Any) -> None:
        """Suppress default stderr logging for cleaner output."""
        pass


def create_app(host: str = "0.0.0.0", port: int = 8420) -> HTTPServer:
    """Create the A2A HTTP server (stdlib, zero external deps)."""
    server = HTTPServer((host, port), A2AHandler)
    return server


def main() -> None:
    """Entry point for standalone server."""
    import os
    port = int(os.environ.get("A2A_PORT", "8420"))
    server = create_app(port=port)
    print(f"🛡️ CC-Cortex Guard Agent running on http://0.0.0.0:{port}")
    print(f"   Agent Card: http://localhost:{port}/.well-known/agent-card.json")
    print(f"   JSON-RPC:   http://localhost:{port}/rpc")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n⏹ Shutting down.")
        server.shutdown()


if __name__ == "__main__":
    main()
