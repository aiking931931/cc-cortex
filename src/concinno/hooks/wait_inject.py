"""concinno.hooks.wait_inject — UserPromptSubmit polling status fan-in.

Reads ``~/.concinno/state/wait_queue.json`` + ``poll_alerts.json``
and assembles a single ``additionalContext`` block summarising:

* Active waits (kind, status, ETA remaining).
* Status transitions since the last prompt (drained alerts).

The agent thus sees, at every prompt submission, exactly what async
work is pending and what just completed — independent of whether
sub-agent notifications fired.

Lazy import + best-effort throughout — a corrupt state file or a
missing concinno install must never block the hook from completing.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("concinno.hooks.wait_inject")


def _feature_enabled() -> bool:
    try:
        from concinno.core.config import get_config
        return bool(get_config().feature("polling_watcher", "enabled"))
    except Exception:
        return True  # productivity feature — fail-open


def _now() -> datetime:
    return datetime.now(timezone.utc).astimezone()


def _eta_remaining(record) -> int:
    try:
        registered = datetime.fromisoformat(record.registered_at)
    except Exception:
        return record.eta_seconds
    elapsed = (_now() - registered).total_seconds()
    return max(0, int(record.eta_seconds - elapsed))


def build_context() -> Optional[str]:
    """Return the ``additionalContext`` string for the prompt-submit
    hook, or ``None`` when there's nothing to surface."""
    if not _feature_enabled():
        return None
    try:
        from concinno.polling import list_active, read_alerts
    except Exception:
        return None

    try:
        actives = list_active()
    except Exception:
        logger.exception("list_active failed")
        actives = []
    try:
        alerts = read_alerts(drain=True)
    except Exception:
        logger.exception("read_alerts failed")
        alerts = []

    if not actives and not alerts:
        return None

    lines: list[str] = []

    if actives:
        lines.append("📡 Active polling waits:")
        for r in actives:
            preview = (r.extra or {}).get("preview", r.kind) if hasattr(r, "extra") else r.kind
            lines.append(
                f"  [{r.id}] kind={r.kind} status={r.status} "
                f"eta_remaining={_eta_remaining(r)}s — {preview}"
            )

    if alerts:
        lines.append("📡 Recent status changes:")
        for a in alerts:
            lines.append(
                f"  [{a.id}] {a.from_status} → {a.to_status} "
                f"at {a.at[:19]} — {a.last_status[:120]}"
            )

    if actives:
        lines.append(
            "Per polling 鐵律: any wait still pending should be "
            "covered by ScheduleWakeup(delaySeconds<=1800) — daemon "
            "polls in background but you decide when to act on alerts."
        )

    return "\n".join(lines)
