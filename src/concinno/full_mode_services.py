"""concinno.full_mode_services — auto-launch / auto-stop services when
``set_handoff_mode("full")`` is called.

@module concinno.full_mode_services
@responsibility Bundle of background services that accompany ``full``
    handoff mode: config GUI (loopback), potentially more later
    (scheduled reflection monitor, cost-live view, etc.). Each service
    registers a ``launch(mode)`` + ``stop()`` pair; the module's
    :func:`ensure_services_for_mode` dispatches on mode transitions.

@responsibility_why User directive 2026-04-24: "full 模式裡面要含全部
    包含 gui" — full mode should be self-sufficient; the operator
    should not need to run ``concinno gui`` manually after flipping
    to full. Dropping back to ``phase`` / ``save-token`` tears the
    services down so ports don't stay bound.

@opt_out Set ``CONCINNO_FULL_MODE_AUTOLAUNCH_GUI=0`` to disable GUI
    auto-launch specifically. The generic opt-out is
    ``CONCINNO_FULL_MODE_SERVICES=off`` which disables every service
    this module starts (including future additions).

@dependencies stdlib only (subprocess, socket, os, sys, json,
    tempfile, atexit) — the GUI launcher defers FastAPI import to the
    child process so callers that don't install ``concinno[gui]`` just
    get a graceful skip.
@exports ensure_services_for_mode, launch_gui, stop_gui, gui_is_running
"""

from __future__ import annotations

import atexit
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

__all__ = [
    "ensure_services_for_mode",
    "launch_gui",
    "stop_gui",
    "gui_is_running",
    "GUI_DEFAULT_HOST",
    "GUI_DEFAULT_PORT",
]

GUI_DEFAULT_HOST = "127.0.0.1"
GUI_DEFAULT_PORT = 8400


def _state_dir() -> Path:
    d = Path.home() / ".concinno"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _gui_pidfile() -> Path:
    return _state_dir() / "gui.pid"


def _services_globally_disabled() -> bool:
    return os.environ.get("CONCINNO_FULL_MODE_SERVICES", "on").lower() in (
        "off", "0", "false", "no",
    )


def _gui_autolaunch_disabled() -> bool:
    return os.environ.get("CONCINNO_FULL_MODE_AUTOLAUNCH_GUI", "1").lower() in (
        "0", "false", "no", "off",
    )


def _port_bound(host: str, port: int, timeout: float = 0.2) -> bool:
    """Return True when ``(host, port)`` is already accepting connections."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _read_pidfile() -> Optional[dict]:
    p = _gui_pidfile()
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_pidfile(data: dict) -> None:
    _gui_pidfile().write_text(
        json.dumps(data, indent=2), encoding="utf-8",
    )


def _clear_pidfile() -> None:
    try:
        _gui_pidfile().unlink()
    except FileNotFoundError:
        pass


def _pid_alive(pid: int) -> bool:
    """Cheap cross-platform liveness probe."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=3,
            )
            return f"{pid}" in (out.stdout or "")
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def gui_is_running(host: str = GUI_DEFAULT_HOST, port: int = GUI_DEFAULT_PORT) -> bool:
    """Fast check: pidfile + port probe. Either signal on => running."""
    info = _read_pidfile()
    if info and isinstance(info.get("pid"), int) and _pid_alive(info["pid"]):
        return True
    return _port_bound(host, port)


def launch_gui(
    host: str = GUI_DEFAULT_HOST,
    port: int = GUI_DEFAULT_PORT,
    *,
    force: bool = False,
) -> dict:
    """Start the GUI as a detached child process.

    Returns a dict with ``status`` in {``"already-running"``, ``"launched"``,
    ``"skipped"``, ``"failed"``} plus diagnostic fields.
    """
    if _gui_autolaunch_disabled() and not force:
        return {"status": "skipped", "reason": "CONCINNO_FULL_MODE_AUTOLAUNCH_GUI=0"}
    if _services_globally_disabled() and not force:
        return {"status": "skipped", "reason": "CONCINNO_FULL_MODE_SERVICES=off"}
    if gui_is_running(host, port):
        return {"status": "already-running", "host": host, "port": port}
    # Spawn ``python -m concinno.gui`` as a daemon child — stdout/stderr
    # redirected to a rotating log under ~/.concinno so the launcher
    # doesn't inherit a console pipe.
    log_path = _state_dir() / "gui.log"
    try:
        log_fd = open(log_path, "ab")
    except Exception as err:  # pragma: no cover — filesystem edge
        return {"status": "failed", "reason": f"cannot open log: {err}"}
    cmd = [
        sys.executable, "-m", "concinno.cli.main",
        "gui", "--host", host, "--port", str(port),
    ]
    kwargs: dict = {
        "stdout": log_fd,
        "stderr": log_fd,
        "stdin": subprocess.DEVNULL,
        "close_fds": True,
    }
    if sys.platform == "win32":
        # Detach aggressively so the child survives the parent Python
        # exiting (common case: the launch call ran inside a short-lived
        # ``python -c "from concinno.full_mode_services import …"``):
        #   DETACHED_PROCESS (0x08)          — no console inheritance
        #   CREATE_NEW_PROCESS_GROUP (0x200) — own Ctrl-C group
        #   CREATE_NO_WINDOW (0x08000000)    — no flicker window
        #   CREATE_BREAKAWAY_FROM_JOB (0x01000000) — escape any job
        #     object (git-bash / pytest / IDE wrappers set one up so
        #     children die with the parent unless we opt out)
        kwargs["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000 | 0x01000000
    else:
        kwargs["start_new_session"] = True  # setsid
    try:
        proc = subprocess.Popen(cmd, **kwargs)
    except Exception as err:  # pragma: no cover
        log_fd.close()
        return {"status": "failed", "reason": str(err)}
    # Brief settle — let uvicorn bind the port before we declare success.
    for _ in range(20):
        if _port_bound(host, port, timeout=0.1):
            break
        time.sleep(0.1)
    else:
        return {"status": "failed", "reason": "port not bound within 2s",
                "pid": proc.pid}
    _write_pidfile({
        "pid": proc.pid, "host": host, "port": port,
        "started_at": time.time(),
    })
    return {"status": "launched", "pid": proc.pid, "host": host, "port": port,
            "url": f"http://{host}:{port}"}


def stop_gui() -> dict:
    """Terminate the daemon child tracked by the pidfile."""
    info = _read_pidfile()
    if not info:
        return {"status": "not-tracked"}
    pid = info.get("pid")
    if not isinstance(pid, int) or not _pid_alive(pid):
        _clear_pidfile()
        return {"status": "already-stopped"}
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/F"],
                capture_output=True, timeout=5,
            )
        else:
            os.kill(pid, 15)  # SIGTERM
            for _ in range(20):
                if not _pid_alive(pid):
                    break
                time.sleep(0.1)
            else:
                os.kill(pid, 9)  # SIGKILL fallback
    except Exception as err:
        return {"status": "error", "reason": str(err), "pid": pid}
    _clear_pidfile()
    return {"status": "stopped", "pid": pid}


def ensure_services_for_mode(mode: str) -> dict:
    """Launch or stop background services to match the requested mode.

    * ``full`` → launch GUI (unless opted out via env)
    * anything else → stop GUI if we own the pidfile

    Always returns a report dict so the caller can surface the outcome.
    The function is failure-soft — a broken GUI launch never prevents
    the mode change itself.
    """
    report: dict = {"mode": mode, "services": {}}
    if _services_globally_disabled():
        report["services"]["_note"] = "all services disabled via env"
        return report
    if mode == "full":
        report["services"]["gui"] = launch_gui()
    else:
        report["services"]["gui"] = stop_gui()
    return report


# Atexit hook — if we launched the GUI from this process and the parent
# is shutting down, best-effort tear it down so stale ports don't leak.
# We only stop services we started (pidfile matches our live record).
_launched_this_process: bool = False


def _atexit_cleanup() -> None:  # pragma: no cover — best-effort
    if _launched_this_process:
        try:
            stop_gui()
        except Exception:
            pass


atexit.register(_atexit_cleanup)
