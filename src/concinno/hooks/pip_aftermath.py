"""concinno.hooks.pip_aftermath — detect pip operations on concinno itself.

When the agent runs ``pip install --upgrade concinno`` (or
``pip install -e ./projects/concinno`` etc.), the uninstall step
mid-flight deletes ``concinno/*.py`` from site-packages for a
sub-second window. Any other Python process that imports
``concinno.*`` during that window — typically the long-running
**Memoria** tray app — raises ``ImportError`` on its next tick and
its daemon thread silently dies. The user then sees Memoria's tray
icon "整個不見了" with no obvious cause.

This hook detects the trigger pattern (pip touching concinno) and:

1. Reads ``~/.memoria/heartbeat.json`` (written by Memoria's
   scheduler every tick — see ``~/.claude/scripts/memoria/
   scheduler.py::Scheduler._heartbeat``).
2. If the heartbeat is stale (>5 min default) OR missing, emits an
   ``additionalContext`` reminder so the agent surfaces the issue
   on its very next tool turn — it can choose to spawn ``pythonw -m
   memoria`` or relay to the user.

This is **detection + advice**, not auto-restart — the hook layer
intentionally does not spawn processes.

Sediment: see :file:`feedback_pip_concinno_kills_memoria.md` in
memory.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("concinno.hooks.pip_aftermath")

# Anchored at segment START (after splitting on shell operators
# ``&&`` ``||`` ``;`` ``|`` and stripping invocation prefixes like
# ``python -m`` / ``nohup`` / ``sudo``). Mirrors the polling
# classifier's same-day fix: regex anywhere inside the command would
# false-positive on commit messages like ``git commit -m "release:
# pip install concinno docs note"`` since the literal text contains
# ``pip install concinno`` as message body, not as an actual command.
_PIP_CONCINNO_RE = re.compile(
    r"^(?:python(?:[23](?:\.\d+)?)?\s+-m\s+)?"
    r"pip\s+(?:install|uninstall)"
    r"(?:[^|;&]*?)\bconcinno(?![\w-])",
    re.IGNORECASE,
)

_SHELL_SPLIT_RE = re.compile(r"\s*(?:&&|\|\||;|\|)\s*")
_INVOCATION_PREFIX_RE = re.compile(
    r"^(?:nohup\s+|time\s+|sudo\s+(?:-E\s+)?|env\s+\w+=\S+\s+)+",
    re.IGNORECASE,
)


def _command_matches_pip_concinno(cmd: str) -> bool:
    """True iff some segment of ``cmd``, after stripping an invocation
    prefix, starts with a real ``pip install/uninstall ... concinno``
    invocation. Quoted argument bodies don't make their own segments,
    so ``git commit -m "pip install concinno"`` returns False."""
    for segment in _SHELL_SPLIT_RE.split(cmd):
        head = _INVOCATION_PREFIX_RE.sub("", segment.strip(), count=1)
        if _PIP_CONCINNO_RE.match(head):
            return True
    return False

# Heartbeat freshness threshold. Memoria's scheduler ticks every
# 30 minutes by default, so 5 min stale = either daemon is dead or
# the file has never been written. Either case warrants the reminder.
_HEARTBEAT_STALE_SECONDS = 5 * 60

_HEARTBEAT_PATH = Path.home() / ".memoria" / "heartbeat.json"


def _feature_enabled() -> bool:
    try:
        from concinno.core.config import get_config
        return bool(get_config().feature("pip_aftermath_hint", "enabled"))
    except Exception:
        return True  # productivity feature — fail-open


def _heartbeat_age_seconds() -> Optional[float]:
    """Return the age of the heartbeat file in seconds, or ``None`` if
    the file doesn't exist (Memoria never ran on this machine, or the
    user hasn't upgraded to the heartbeat-aware version yet)."""
    try:
        if not _HEARTBEAT_PATH.is_file():
            return None
        mtime = _HEARTBEAT_PATH.stat().st_mtime
        return max(0.0, time.time() - mtime)
    except Exception:
        return None


def _heartbeat_pid() -> Optional[int]:
    try:
        if not _HEARTBEAT_PATH.is_file():
            return None
        data = json.loads(_HEARTBEAT_PATH.read_text(encoding="utf-8"))
        return int(data.get("pid")) if data.get("pid") is not None else None
    except Exception:
        return None


def detect_pip_concinno(tool_name: str, tool_input: dict) -> Optional[str]:
    """Return an ``additionalContext`` hint when:

    1. The just-run Bash command is a ``pip`` operation on concinno, AND
    2. Memoria's heartbeat file is missing or stale (>5 min old), AND
    3. The ``pip_aftermath_hint`` feature is enabled.

    Returns ``None`` otherwise.
    """
    if not _feature_enabled():
        return None
    if tool_name != "Bash":
        return None
    cmd = str(tool_input.get("command", ""))
    if not cmd:
        return None
    if not _command_matches_pip_concinno(cmd):
        return None

    age = _heartbeat_age_seconds()
    if age is None:
        # Heartbeat file doesn't exist — either Memoria has never run on
        # this machine (no advice needed) OR the running Memoria is on
        # a pre-heartbeat version. We can't distinguish without a pid
        # check, so we err on the side of advising — agent can ignore
        # if not relevant.
        return (
            "📦 pip touched concinno. If Memoria is running on this "
            "machine, its daemon thread may have died on the mid-install "
            "ImportError window. Restart with `pythonw -m memoria` or "
            "verify the tray icon is still alive. (No heartbeat file "
            "found — pre-heartbeat Memoria, or never installed.)"
        )

    if age <= _HEARTBEAT_STALE_SECONDS:
        # Memoria is alive and ticking. No reminder needed.
        return None

    pid = _heartbeat_pid()
    pid_clause = f"(stale heartbeat from PID {pid})" if pid else "(stale heartbeat)"
    return (
        f"📦 pip touched concinno → Memoria heartbeat is "
        f"{int(age)}s stale {pid_clause}. Daemon thread likely died "
        f"on mid-install ImportError. Restart: "
        f"`cd ~/.claude/scripts && pythonw -m memoria`. "
        f"After restart, heartbeat file `~/.memoria/heartbeat.json` "
        f"refreshes every scheduler tick."
    )
