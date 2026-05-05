"""concinno.polling — auto-detect & periodically poll wait states.

@module polling
@responsibility Make every long-running async operation (sub-agent
    dispatch, background bash, upload, deploy, CI watch) self-monitor
    without depending on sub-agent notifications. Built around three
    layers:

    1. Detection (``classifier``): inspect ``PostToolUse`` payload,
       decide if the tool call implies a wait + emit a check command.
    2. Persistence (``wait_queue``): atomic JSON file
       ``~/.concinno/state/wait_queue.json`` lists active waits +
       per-wait status.
    3. Daemon (``daemon``): a daemon thread that re-runs each wait's
       check command every ``interval_seconds``, updates status,
       logs alerts on transitions. Runs **independently of agent
       invocations** — that's the whole point.

@dependencies stdlib only. JSON read/write uses ``json`` +
    atomic temp + rename. File locking via ``msvcrt`` (Windows) /
    ``fcntl`` (Linux/macOS) — the wrapper falls back to a no-op lock
    if neither is available so polling still works in stripped-down
    environments.

The agent surface (hook integration) is in
:mod:`concinno.hooks.wait_watcher` (PostToolUse register) and
:mod:`concinno.hooks.wait_inject` (UserPromptSubmit / SessionStart
status fan-in). This module is pure I/O + scheduling — it knows
nothing about Claude Code hooks.

Public entry points:

* :func:`register_wait` — record a new wait task.
* :func:`list_waits` — current active waits (for UX).
* :func:`check_wait` — run one wait's check_cmd, return updated record.
* :func:`mark_done` — explicit removal (e.g. agent confirmed done).
* :func:`start_daemon` / :func:`stop_daemon` — control the timer.

Per the 4.0.0 default-off directive, this entire feature is gated by
``polling_watcher`` in FEATURE_META. ``polling_watcher`` is **NOT**
in ``DEFAULT_OFF_4_0_0`` — it ships default-ON because it's a
productivity feature, not a deny gate.
"""

from __future__ import annotations

from .classifier import classify_wait
from .daemon import start_daemon, stop_daemon
from .wait_queue import (
    check_wait,
    list_active,
    list_waits,
    mark_done,
    purge_stale,
    read_alerts,
    register_wait,
    state_dir,
)

__all__ = [
    "classify_wait",
    "check_wait",
    "list_active",
    "list_waits",
    "mark_done",
    "purge_stale",
    "read_alerts",
    "register_wait",
    "state_dir",
    "start_daemon",
    "stop_daemon",
]
