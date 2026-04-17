"""Shared constants, models, and platform helpers for process_guard.

@module process_guard._base
@responsibility Constants, data models, cross-platform primitives
@dependencies (none — stdlib only)
"""

from __future__ import annotations

import logging
import os
import platform
import signal
import subprocess
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

logger = logging.getLogger("concinno.process_guard")

# ── Constants ──────────────────────────────────────────────

IDLE_MINUTES = 30
STALE_MINUTES = 120
CLAUDE_MAX_MB = 8192
MEMORY_CRITICAL_PERCENT = 95

SYSTEM = platform.system()  # "Windows", "Darwin", "Linux"
_CREATE_NO_WINDOW = 0x08000000


# ── Models ─────────────────────────────────────────────────


class Tier(Enum):
    ALIVE = "ALIVE"
    STALE = "STALE"
    ORPHAN = "ORPHAN"
    SCHEDULED = "SCHEDULED"


@dataclass
class ClaudeProcess:
    pid: int
    name: str
    mem_mb: int = 0
    start_time: Optional[float] = None
    cmdline: str = ""
    tier: Tier = Tier.ALIVE
    kill_reason: str = ""


@dataclass
class GuardResult:
    scanned: int = 0
    killed: int = 0
    freed_mb: int = 0
    warnings: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    lock_cleaned: int = 0
    user_notification: str = ""


@dataclass
class RegistrationResult:
    cli_pid: int = 0
    vscode_pid: int = 0
    session_key: str = ""
    suspects: list[int] = field(default_factory=list)
    stale_cleaned: int = 0
    actions: list[str] = field(default_factory=list)


# ── Platform helpers ───────────────────────────────────────


def _run(cmd: list[str], timeout: int = 10) -> str:
    """Run a command and return stdout, empty string on failure."""
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=_CREATE_NO_WINDOW if SYSTEM == "Windows" else 0,
        )
        return r.stdout
    except Exception:
        return ""


def _pid_alive(pid: int) -> bool:
    """Check if a PID is still running."""
    if pid <= 0:
        return False
    try:
        if SYSTEM == "Windows":
            out = _run(["tasklist", "/FI", f"PID eq {pid}", "/NH"])
            return str(pid) in out
        else:
            os.kill(pid, 0)
            return True
    except (ProcessLookupError, PermissionError):
        return SYSTEM != "Windows"
    except Exception:
        return False


def _kill_process(pid: int) -> bool:
    """Kill a process (and its tree on Windows)."""
    try:
        if SYSTEM == "Windows":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        else:
            os.kill(pid, signal.SIGTERM)
            time.sleep(0.5)
            try:
                os.kill(pid, signal.SIGKILL)  # type: ignore[attr-defined]
            except ProcessLookupError:
                pass
        return True
    except Exception:
        return False


def _get_system_memory_percent() -> float:
    """Get system RAM usage percentage (0-100)."""
    if SYSTEM != "Windows":
        try:
            with open("/proc/meminfo") as f:
                lines = f.readlines()
            info = {}
            for line in lines:
                parts = line.split()
                if len(parts) >= 2:
                    info[parts[0].rstrip(":")] = int(parts[1])
            total = info.get("MemTotal", 0)
            avail = info.get("MemAvailable", 0)
            if total > 0:
                return (1.0 - avail / total) * 100.0
        except Exception:
            pass
        return 0.0
    try:
        import ctypes.wintypes

        class _MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.wintypes.DWORD),
                ("dwMemoryLoad", ctypes.wintypes.DWORD),
                ("ullTotalPhys", ctypes.c_uint64),
                ("ullAvailPhys", ctypes.c_uint64),
                ("ullTotalPageFile", ctypes.c_uint64),
                ("ullAvailPageFile", ctypes.c_uint64),
                ("ullTotalVirtual", ctypes.c_uint64),
                ("ullAvailVirtual", ctypes.c_uint64),
                ("ullAvailExtendedVirtual", ctypes.c_uint64),
            ]

        stat = _MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
        return float(stat.dwMemoryLoad)
    except Exception:
        return 0.0
