"""concinno.notify_health — Self-healing for Windows toast regression modes.

@module notify_health
@responsibility Detect and auto-fix Win11 AUMID reputation demote
    (``PeriodicNotificationCount`` exceeds banner-suppression threshold
    so toasts degrade to Action Center only, losing banner pop-up).
    Wired into ``on_session_start`` so every session starts with a
    reset counter — user's previous "早上才修好一下又沒了" regression
    gets root-caused.
@dependencies (stdlib only — subprocess, sys, os)
@exports reset_aumid_counter, is_demoted, auto_reset_on_session_start,
    get_counter, AUMID_VSCODE, AUMID_CURSOR

Background (kb_notify_health Mode 1):
    Windows 11 tracks toast reputation per AppUserModelID. When
    PeriodicNotificationCount exceeds ~3 per 24h with low user
    interaction ratio, the AUMID is demoted — toasts still dispatch
    successfully but are suppressed from the banner overlay and only
    appear in Action Center. User perceives "notification broken".

Fix: reset counter to 0 on every session start. No prevention of
future accumulation (that would require per-call throttle), but
guarantees banner availability at session start which is when the
user actually works.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Optional

_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# ── Known AUMIDs ────────────────────────────────────────────────────
#
# VS Code is the default AUMID — Claude Code runs inside VS Code or
# Cursor host process, so toasts are sent under the host IDE's identity.
# If we're in Cursor, the AUMID is different. Track both for safety.

AUMID_VSCODE = "Microsoft.VisualStudioCode"
AUMID_CURSOR = "Cursor.Cursor"

_REG_BASE = (
    r"HKCU:\Software\Microsoft\Windows\CurrentVersion"
    r"\Notifications\Settings"
)


def _hidden_startupinfo():
    if sys.platform != "win32":
        return None
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0
    return si


def _run_powershell(cmd: str, timeout_s: float = 10.0) -> Optional[str]:
    """Run a PowerShell command silently. Returns stdout or None on error."""
    if sys.platform != "win32":
        return None
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", cmd],
            capture_output=True,
            timeout=timeout_s,
            encoding="utf-8",
            errors="replace",
            creationflags=_CREATE_NO_WINDOW,
            startupinfo=_hidden_startupinfo(),
        )
        if result.returncode == 0:
            return result.stdout.strip()
        return None
    except Exception:
        return None


def get_counter(aumid: str = AUMID_VSCODE) -> Optional[int]:
    """Read ``PeriodicNotificationCount`` for an AUMID.

    Returns:
        int counter value, or ``None`` if the registry key does not
        exist (AUMID never registered) or on Windows-only platform
        mismatch.
    """
    if sys.platform != "win32":
        return None
    cmd = (
        f"$p='{_REG_BASE}\\{aumid}'; "
        "if (Test-Path $p) { "
        "(Get-ItemProperty -Path $p -Name PeriodicNotificationCount "
        "-ErrorAction SilentlyContinue).PeriodicNotificationCount "
        "} else { '' }"
    )
    out = _run_powershell(cmd, timeout_s=5.0)
    if not out:
        return None
    try:
        return int(out.strip())
    except (ValueError, TypeError):
        return None


def is_demoted(aumid: str = AUMID_VSCODE, threshold: int = 3) -> bool:
    """Return True if the AUMID counter is at/past banner-demote threshold.

    Windows 11's exact threshold is undocumented but empirically ~3 per
    24h window triggers banner suppression (app moves to Action-Center-
    only mode). Default ``threshold=3`` = treat equal-to as demoted so
    we reset pre-emptively.
    """
    c = get_counter(aumid)
    return c is not None and c >= threshold


def reset_aumid_counter(aumid: str = AUMID_VSCODE) -> bool:
    """Reset ``PeriodicNotificationCount`` to 0 and ensure ``Enabled=1``.

    Idempotent — safe to call every session start. No-op on non-Windows.

    Returns:
        True if reset succeeded (or was unnecessary), False on error.
    """
    if sys.platform != "win32":
        return True
    # Create the key if missing (`New-Item -Force` is idempotent), then
    # set both properties. Use `-ErrorAction SilentlyContinue` so a
    # transient permissions issue doesn't crash the session start.
    cmd = (
        f"$p='{_REG_BASE}\\{aumid}'; "
        "if (-not (Test-Path $p)) { New-Item -Path $p -Force | Out-Null }; "
        "Set-ItemProperty -Path $p -Name PeriodicNotificationCount "
        "-Value 0 -Type DWord -ErrorAction SilentlyContinue; "
        "Set-ItemProperty -Path $p -Name Enabled "
        "-Value 1 -Type DWord -ErrorAction SilentlyContinue"
    )
    out = _run_powershell(cmd, timeout_s=5.0)
    return out is not None


def auto_reset_on_session_start(
    aumids: tuple[str, ...] = (AUMID_VSCODE, AUMID_CURSOR),
    verbose: bool = False,
) -> dict[str, bool]:
    """Reset counters for all known host-IDE AUMIDs.

    Called from ``concinno.hooks.on_session_start``. Fail-open: any
    error is silently swallowed so a broken AV / missing PowerShell
    / locked registry doesn't block session start.

    Args:
        aumids: Tuple of AUMID strings to reset. Default resets both
            VS Code and Cursor (user may switch hosts mid-day).
        verbose: Emit a one-line summary to stderr listing pre-reset
            counter values. Default False to keep hooks quiet.

    Returns:
        Mapping of AUMID → True if reset succeeded.
    """
    results: dict[str, bool] = {}
    pre_reset_counters: dict[str, Optional[int]] = {}
    for aumid in aumids:
        try:
            pre_reset_counters[aumid] = get_counter(aumid)
            results[aumid] = reset_aumid_counter(aumid)
        except Exception:
            results[aumid] = False
            pre_reset_counters[aumid] = None
    if verbose:
        try:
            parts: list[str] = []
            for aumid in aumids:
                c = pre_reset_counters.get(aumid)
                ok = results.get(aumid, False)
                if c is not None and c >= 1:
                    flag = "✅" if ok else "❌"
                    parts.append(f"{flag} {aumid.split('.')[-1]}={c}→0")
            if parts:
                print(
                    f"🔔 notify_health: AUMID counter reset ({', '.join(parts)})",
                    file=sys.stderr,
                )
        except Exception:
            pass
    return results


__all__ = [
    "AUMID_VSCODE",
    "AUMID_CURSOR",
    "auto_reset_on_session_start",
    "get_counter",
    "is_demoted",
    "reset_aumid_counter",
]
