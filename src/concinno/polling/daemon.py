"""concinno.polling.daemon — periodic poller running as a daemon thread.

The whole reason this module exists: the user's directive 2026-04-26
"設真定時輪巡 + ScheduleWakeup 自醒 query，不依賴 sub-agent 通知" —
the agent must NOT depend on sub-agent completion notifications to
keep tabs on async work. Real OS-timer polling is the answer.

Lifecycle:

* :func:`start_daemon` — spawn (idempotent) the daemon thread; safe
  to call from many places (e.g. every hook invocation) because the
  module-level singleton dedupes.
* :func:`stop_daemon` — sentinel + join; called from atexit by default.

The thread itself is a simple ``while not stop:`` loop that:

1. Sleeps ``interval_seconds`` (default 60s, env override
   ``CONCINNO_POLLING_INTERVAL``).
2. Calls :func:`wait_queue.purge_stale` every 30 minutes to drop
   abandoned records.
3. For every record in :func:`wait_queue.list_active`, calls
   :func:`wait_queue.check_wait`. Status changes are recorded as
   alerts inside ``check_wait`` itself.

Failure tolerance is paramount — this is a **best-effort observer**.
Any exception inside the loop is logged + swallowed so the daemon
never dies and starves alerts.
"""

from __future__ import annotations

import atexit
import logging
import os
import threading
import time
from typing import Optional

from . import wait_queue

logger = logging.getLogger("concinno.polling.daemon")


# ── Module-level singleton ────────────────────────────────────────────

_DEFAULT_INTERVAL_SECONDS = 60
_DEFAULT_PURGE_INTERVAL_SECONDS = 30 * 60
_DEFAULT_STALE_AGE_SECONDS = 24 * 3600

_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()
_lock = threading.Lock()
_atexit_registered = False


def _interval_seconds() -> int:
    raw = os.environ.get("CONCINNO_POLLING_INTERVAL", "")
    if raw.isdigit():
        return max(15, int(raw))
    return _DEFAULT_INTERVAL_SECONDS


def _is_enabled() -> bool:
    """Honour the FEATURE_META ``polling_watcher.enabled`` toggle.

    Lazy import so the daemon module stays importable in environments
    that haven't built the full Config singleton yet (early bootstrap,
    cold subprocess hooks)."""
    if os.environ.get("CONCINNO_POLLING_DISABLED") == "1":
        return False
    try:
        from concinno.core.config import get_config
        return bool(get_config().feature("polling_watcher", "enabled"))
    except Exception:
        return True  # fail-open — feature is productivity, not safety


def _loop() -> None:
    last_purge = time.monotonic()
    while not _stop_event.is_set():
        if _stop_event.wait(timeout=_interval_seconds()):
            return
        if not _is_enabled():
            continue
        try:
            now = time.monotonic()
            if now - last_purge >= _DEFAULT_PURGE_INTERVAL_SECONDS:
                try:
                    dropped = wait_queue.purge_stale(
                        max_age_seconds=_DEFAULT_STALE_AGE_SECONDS,
                    )
                    if dropped:
                        logger.info(
                            "polling daemon: purged %d stale wait(s)",
                            dropped,
                        )
                except Exception:
                    logger.exception("purge_stale failed")
                last_purge = now

            actives = wait_queue.list_active()
            for r in actives:
                try:
                    wait_queue.check_wait(r.id)
                except Exception:
                    logger.exception("check_wait failed for %s", r.id)
        except Exception:
            logger.exception("polling daemon tick failed")


def start_daemon() -> bool:
    """Spawn the daemon thread (idempotent). Returns True if spawned
    on this call, False if it was already running."""
    global _thread, _atexit_registered
    with _lock:
        if _thread is not None and _thread.is_alive():
            return False
        if not _is_enabled():
            return False
        _stop_event.clear()
        _thread = threading.Thread(
            target=_loop,
            name="concinno.polling",
            daemon=True,
        )
        _thread.start()
        if not _atexit_registered:
            atexit.register(stop_daemon)
            _atexit_registered = True
    logger.info(
        "polling daemon started (interval=%ds)", _interval_seconds(),
    )
    return True


def stop_daemon(*, timeout: float = 2.0) -> None:
    """Signal the daemon to exit + join. Idempotent."""
    global _thread
    with _lock:
        thr = _thread
    if thr is None:
        return
    _stop_event.set()
    try:
        thr.join(timeout=timeout)
    except Exception:
        pass
    with _lock:
        if _thread is thr:
            _thread = None


def is_running() -> bool:
    with _lock:
        return _thread is not None and _thread.is_alive()
