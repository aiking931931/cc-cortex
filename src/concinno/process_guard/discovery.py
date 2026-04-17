"""Process discovery — find and identify Claude processes.

@module process_guard.discovery
@responsibility Cross-platform process listing, Claude process identification
@dependencies process_guard._base
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import os
import re
from datetime import datetime

from ._base import SYSTEM, ClaudeProcess, _run

# Windows API constants for CreateToolhelp32Snapshot
_TH32CS_SNAPPROCESS = 0x00000002


class _PROCESSENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_ulong),
        ("cntUsage", ctypes.c_ulong),
        ("th32ProcessID", ctypes.c_ulong),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", ctypes.c_ulong),
        ("cntThreads", ctypes.c_ulong),
        ("th32ParentProcessID", ctypes.c_ulong),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", ctypes.c_ulong),
        ("szExeFile", ctypes.c_char * 260),
    ]


def _get_all_processes() -> list[dict]:
    """Get all running processes as list of dicts."""
    if SYSTEM == "Windows":
        return _get_all_processes_windows()
    return _get_all_processes_unix()


def _snapshot_processes_windows() -> list[dict]:
    """Phase A: Fast process list via Windows API (no wmic, no console flash)."""
    kernel32 = ctypes.windll.kernel32
    snapshot = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if snapshot == ctypes.wintypes.HANDLE(-1).value:
        return []

    pe = _PROCESSENTRY32()
    pe.dwSize = ctypes.sizeof(pe)
    processes = []

    if kernel32.Process32First(snapshot, ctypes.byref(pe)):
        while True:
            processes.append(
                {
                    "pid": pe.th32ProcessID,
                    "ppid": pe.th32ParentProcessID,
                    "name": pe.szExeFile.decode("utf-8", errors="replace"),
                    "cmdline": "",
                    "mem_kb": 0,
                    "start_time": None,
                }
            )
            if not kernel32.Process32Next(snapshot, ctypes.byref(pe)):
                break

    kernel32.CloseHandle(snapshot)
    return processes


def _enrich_claude_processes(processes: list[dict]) -> list[dict]:
    """Phase B: Enrich Claude-candidate processes with cmdline + mem via wmic."""
    claude_names = {"code.exe", "node.exe", "claude.exe", "claude", "bash.exe"}
    candidate_pids = [p["pid"] for p in processes if p["name"].lower() in claude_names]

    if not candidate_pids:
        return processes

    cmdline_map: dict[int, dict] = {}
    try:
        out = _run(
            [
                "wmic",
                "process",
                "where",
                "name='Code.exe' OR name='node.exe' OR name='claude.exe'",
                "get",
                "ProcessId,CommandLine,WorkingSetSize",
                "/FORMAT:CSV",
            ],
            timeout=15,
        )
        for line in out.strip().splitlines():
            line = line.strip()
            if not line or "ProcessId" in line or line.startswith("Node"):
                continue
            parts = line.split(",")
            if len(parts) < 4:
                continue
            try:
                cmdline = ",".join(parts[1:-2])
                pid = int(parts[-2])
                ws = int(parts[-1]) if parts[-1].strip() else 0
                cmdline_map[pid] = {"cmdline": cmdline, "mem_kb": ws // 1024}
            except (ValueError, IndexError):
                continue
    except Exception:
        pass

    for p in processes:
        if p["pid"] in cmdline_map:
            p["cmdline"] = cmdline_map[p["pid"]]["cmdline"]
            p["mem_kb"] = cmdline_map[p["pid"]]["mem_kb"]

    return processes


def _get_all_processes_windows() -> list[dict]:
    """Two-phase Windows process discovery."""
    processes = _snapshot_processes_windows()
    if not processes:
        return _get_all_processes_wmic_legacy()
    return _enrich_claude_processes(processes)


def _get_all_processes_wmic_legacy() -> list[dict]:
    """Legacy fallback: full wmic query."""
    out = _run(
        [
            "wmic",
            "process",
            "get",
            "ProcessId,ParentProcessId,Name,CommandLine,WorkingSetSize",
            "/FORMAT:CSV",
        ],
        timeout=15,
    )
    processes = []
    for line in out.strip().splitlines():
        parts = line.split(",")
        if len(parts) < 5 or "ProcessId" in line:
            continue
        try:
            cmdline = parts[1] if len(parts) >= 6 else ""
            name = parts[2] if len(parts) >= 6 else parts[1]
            ppid = int(parts[3]) if len(parts) >= 6 else 0
            pid = int(parts[4]) if len(parts) >= 6 else int(parts[2])
            ws = int(parts[5]) if len(parts) >= 6 else 0
            processes.append(
                {
                    "pid": pid,
                    "ppid": ppid,
                    "name": name,
                    "cmdline": cmdline,
                    "mem_kb": ws // 1024,
                    "start_time": None,
                }
            )
        except (ValueError, IndexError):
            continue
    return processes


def _get_all_processes_unix() -> list[dict]:
    """Use ps on macOS/Linux."""
    out = _run(["ps", "-eo", "pid,ppid,rss,lstart,comm,args"], timeout=10)
    processes = []
    for line in out.strip().splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        match = re.match(
            r"\s*(\d+)\s+(\d+)\s+(\d+)\s+"
            r"(\w+\s+\w+\s+\d+\s+[\d:]+\s+\d+)\s+"
            r"(\S+)\s*(.*)",
            line,
        )
        if not match:
            continue
        try:
            pid = int(match.group(1))
            ppid = int(match.group(2))
            rss_kb = int(match.group(3))
            lstart_str = match.group(4)
            comm = match.group(5)
            args = match.group(6) if match.group(6) else comm

            start_epoch = None
            try:
                dt = datetime.strptime(lstart_str, "%a %b %d %H:%M:%S %Y")
                start_epoch = dt.timestamp()
            except ValueError:
                pass

            processes.append(
                {
                    "pid": pid,
                    "ppid": ppid,
                    "name": os.path.basename(comm),
                    "cmdline": args,
                    "mem_kb": rss_kb,
                    "start_time": start_epoch,
                }
            )
        except (ValueError, IndexError):
            continue
    return processes


def _find_claude_processes(all_procs: list[dict]) -> list[ClaudeProcess]:
    """Find Claude-related processes from the full process list."""
    result = []
    seen_pids: set[int] = set()

    for p in all_procs:
        pid = p["pid"]
        if pid in seen_pids:
            continue
        name = p.get("name", "").lower()
        cmdline = p.get("cmdline", "")

        is_claude = False
        display_name = ""

        if name in ("claude", "claude.exe"):
            is_claude = True
            display_name = "claude"
        elif name in ("node", "node.exe") and "claude" in cmdline.lower():
            if "process-guard" not in cmdline.lower():
                is_claude = True
                display_name = "node(claude)"
        elif name in ("code", "code.exe") and re.search(r"cli\.js.*--output-format", cmdline):
            is_claude = True
            display_name = "code(claude-cli)"

        if is_claude:
            seen_pids.add(pid)
            result.append(
                ClaudeProcess(
                    pid=pid,
                    name=display_name,
                    mem_mb=p.get("mem_kb", 0) // 1024,
                    start_time=p.get("start_time"),
                    cmdline=cmdline,
                )
            )

    return sorted(result, key=lambda x: x.start_time or 0)
