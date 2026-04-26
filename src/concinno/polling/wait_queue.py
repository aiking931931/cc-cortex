"""concinno.polling.wait_queue — atomic CRUD for ~/.concinno/state/wait_queue.json.

Thin wrapper over JSON file IO with the safety bits the daemon and the
hook surface both rely on:

* **Atomic write** (tmp + rename) so a crash mid-write never leaves a
  half-written file readers can choke on.
* **Cross-platform advisory lock** so two concurrent ``concinno``
  imports (e.g. main agent + a sub-agent) don't trample each other.
  ``msvcrt`` on Windows, ``fcntl`` on Unix; both gracefully no-op if
  the relevant module isn't importable so the wait queue still
  functions in stripped containers.
* **Tolerant load**: corrupt JSON → backup as ``.json.corrupt`` and
  start fresh, so a malformed file from a prior buggy version never
  bricks the agent on next boot.

State schema is a list of records:

.. code-block:: yaml

    - id: <stable hash of tool_name + tool_input + registered_at>
      kind: agent_dispatch | bash_background | upload | deploy
            | ci_check | long_op
      registered_at: <ISO-8601>
      check_cmd: <bash command to verify status>
      eta_seconds: <int>            # caller-supplied estimate
      status: pending | running | done | failed | timeout
      last_check: <ISO-8601>        # daemon's last update
      last_status: <str>            # tail of check_cmd output
      pid: <int|null>               # known background PID (if any)

Alerts (``poll_alerts.json``) are a separate file the daemon appends
to on status transitions — the inject hook pulls them on next prompt
submit.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

# ── State location ────────────────────────────────────────────────────

_STATE_DIR_ENV = "CONCINNO_STATE_DIR"
_DEFAULT_STATE_DIR = Path.home() / ".concinno" / "state"
_QUEUE_BASENAME = "wait_queue.json"
_ALERTS_BASENAME = "poll_alerts.json"
_LOCK_BASENAME = "wait_queue.lock"


def state_dir() -> Path:
    override = os.environ.get(_STATE_DIR_ENV)
    return Path(override) if override else _DEFAULT_STATE_DIR


def queue_path() -> Path:
    return state_dir() / _QUEUE_BASENAME


def alerts_path() -> Path:
    return state_dir() / _ALERTS_BASENAME


def _lock_path() -> Path:
    return state_dir() / _LOCK_BASENAME


# ── Records ───────────────────────────────────────────────────────────


@dataclass
class WaitRecord:
    id: str
    kind: str
    registered_at: str
    check_cmd: str
    eta_seconds: int = 600
    status: str = "pending"
    last_check: str = ""
    last_status: str = ""
    pid: Optional[int] = None
    extra: dict = field(default_factory=dict)


@dataclass
class AlertRecord:
    id: str
    kind: str
    from_status: str
    to_status: str
    at: str
    last_status: str = ""


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().strftime(
        "%Y-%m-%dT%H:%M:%S%z",
    )


# ── Lock primitive ────────────────────────────────────────────────────


@contextlib.contextmanager
def _file_lock(timeout: float = 5.0):
    """Best-effort cross-platform advisory lock on ``state_dir/wait_queue.lock``.

    Spins for up to ``timeout`` seconds before giving up. On giveup we
    yield anyway — the file is best-effort and we'd rather risk a rare
    racy write than block the agent. (The atomic rename inside
    ``_write_queue`` still gives us last-writer-wins consistency.)
    """
    state_dir().mkdir(parents=True, exist_ok=True)
    lock_path = _lock_path()
    try:
        fh = open(lock_path, "a+", encoding="utf-8")
    except Exception:
        yield
        return

    acquired = False
    deadline = time.monotonic() + max(0.1, float(timeout))
    try:
        if sys.platform == "win32":
            try:
                import msvcrt
                while time.monotonic() < deadline:
                    try:
                        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                        acquired = True
                        break
                    except OSError:
                        time.sleep(0.05)
            except ImportError:
                acquired = False
        else:
            try:
                import fcntl
                while time.monotonic() < deadline:
                    try:
                        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        acquired = True
                        break
                    except OSError:
                        time.sleep(0.05)
            except ImportError:
                acquired = False
        yield
    finally:
        if acquired:
            try:
                if sys.platform == "win32":
                    import msvcrt
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
        try:
            fh.close()
        except Exception:
            pass


# ── Read / write ──────────────────────────────────────────────────────


def _read_queue() -> list[WaitRecord]:
    p = queue_path()
    if not p.is_file():
        return []
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        try:
            p.rename(p.with_suffix(".json.corrupt"))
        except Exception:
            pass
        return []
    if not isinstance(raw, list):
        return []
    out: list[WaitRecord] = []
    for r in raw:
        if not isinstance(r, dict):
            continue
        try:
            out.append(WaitRecord(
                id=str(r.get("id", "")),
                kind=str(r.get("kind", "long_op")),
                registered_at=str(r.get("registered_at", _now_iso())),
                check_cmd=str(r.get("check_cmd", "")),
                eta_seconds=int(r.get("eta_seconds", 600)),
                status=str(r.get("status", "pending")),
                last_check=str(r.get("last_check", "")),
                last_status=str(r.get("last_status", ""))[:500],
                pid=r.get("pid"),
                extra=r.get("extra") if isinstance(r.get("extra"), dict) else {},
            ))
        except Exception:
            continue
    return out


def _write_queue(records: Iterable[WaitRecord]) -> None:
    p = queue_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = [asdict(r) for r in records]
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    tmp.replace(p)


def _read_alerts() -> list[AlertRecord]:
    p = alerts_path()
    if not p.is_file():
        return []
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(raw, list):
        return []
    out: list[AlertRecord] = []
    for r in raw:
        if not isinstance(r, dict):
            continue
        out.append(AlertRecord(
            id=str(r.get("id", "")),
            kind=str(r.get("kind", "")),
            from_status=str(r.get("from_status", "")),
            to_status=str(r.get("to_status", "")),
            at=str(r.get("at", "")),
            last_status=str(r.get("last_status", ""))[:500],
        ))
    return out


def _write_alerts(records: Iterable[AlertRecord]) -> None:
    p = alerts_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = [asdict(r) for r in records]
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    tmp.replace(p)


# ── Public API ────────────────────────────────────────────────────────


def make_task_id(tool_name: str, tool_input: dict, registered_at: str) -> str:
    """Stable hash so identical re-registrations dedupe instead of
    spawning forever-growing queue entries. ``registered_at`` is
    bucketed to the second to keep IDs stable across same-second
    invocations."""
    payload = json.dumps(
        {"t": tool_name, "i": tool_input, "r": registered_at[:19]},
        sort_keys=True, default=str,
    ).encode("utf-8")
    return hashlib.blake2b(payload, digest_size=10).hexdigest()


def register_wait(
    *,
    tool_name: str,
    tool_input: Optional[dict] = None,
    kind: str,
    check_cmd: str,
    eta_seconds: int = 600,
    pid: Optional[int] = None,
    extra: Optional[dict] = None,
) -> WaitRecord:
    """Record a new wait. Returns the created record (or an existing
    one if same task_id already in queue)."""
    if tool_input is None:
        tool_input = {}
    registered_at = _now_iso()
    task_id = make_task_id(tool_name, tool_input, registered_at)
    record = WaitRecord(
        id=task_id,
        kind=kind,
        registered_at=registered_at,
        check_cmd=check_cmd,
        eta_seconds=int(eta_seconds),
        status="pending",
        pid=pid,
        extra=extra or {},
    )
    with _file_lock():
        existing = _read_queue()
        if any(r.id == task_id for r in existing):
            for r in existing:
                if r.id == task_id:
                    return r
        existing.append(record)
        _write_queue(existing)
    return record


def list_waits(*, status_filter: Optional[Iterable[str]] = None) -> list[WaitRecord]:
    records = _read_queue()
    if status_filter is not None:
        wanted = set(status_filter)
        records = [r for r in records if r.status in wanted]
    return records


def list_active() -> list[WaitRecord]:
    """Convenience: pending or running only (the ones the inject hook
    needs to surface)."""
    return list_waits(status_filter={"pending", "running"})


def mark_done(task_id: str, *, final_status: str = "done", note: str = "") -> bool:
    """Mark a wait as terminal (done / failed / timeout). Emits an
    alert + drops the record from the queue. Returns True if the task
    was found and updated."""
    found = False
    with _file_lock():
        existing = _read_queue()
        kept: list[WaitRecord] = []
        for r in existing:
            if r.id == task_id:
                found = True
                _append_alert(AlertRecord(
                    id=r.id, kind=r.kind,
                    from_status=r.status, to_status=final_status,
                    at=_now_iso(),
                    last_status=note or r.last_status,
                ))
                continue
            kept.append(r)
        _write_queue(kept)
    return found


def _append_alert(alert: AlertRecord, *, max_alerts: int = 200) -> None:
    """Append one alert, trimming oldest when ``max_alerts`` exceeded.
    Caller already holds the wait_queue lock — alerts share it."""
    alerts = _read_alerts()
    alerts.append(alert)
    if len(alerts) > max_alerts:
        alerts = alerts[-max_alerts:]
    _write_alerts(alerts)


def read_alerts(*, drain: bool = True) -> list[AlertRecord]:
    """Pull all queued alerts; ``drain=True`` clears them after read."""
    with _file_lock():
        alerts = _read_alerts()
        if drain:
            _write_alerts([])
    return alerts


def check_wait(task_id: str, *, timeout_seconds: int = 10) -> Optional[WaitRecord]:
    """Run the wait's ``check_cmd`` once + persist the new status. Returns
    the updated record (or None when the task isn't in the queue).

    Status mapping:

    * exit 0 → ``done`` (and the record is removed via ``mark_done``)
    * exit non-zero, but command produced output → ``running``
    * timeout / OSError → ``timeout``
    * empty output AND exit 0 → still ``done`` (the cheap check
      command's only job is to return 0/non-0; we don't require
      stdout)
    """
    with _file_lock():
        records = _read_queue()
        target = next((r for r in records if r.id == task_id), None)
        if target is None:
            return None
        cmd = target.check_cmd
        prior_status = target.status
    # Run check command outside the lock — the lock is for queue
    # mutation, not for the (potentially) seconds-long check itself.
    if not cmd:
        return target
    try:
        proc = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            timeout=timeout_seconds,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        new_status = "done" if proc.returncode == 0 else "running"
        last_out = (proc.stdout or proc.stderr or "")[-300:]
    except subprocess.TimeoutExpired:
        new_status = "timeout"
        last_out = f"check_cmd timed out after {timeout_seconds}s"
    except Exception as exc:  # noqa: BLE001 — best-effort
        new_status = "running"
        last_out = f"check_cmd error: {exc}"

    with _file_lock():
        records = _read_queue()
        for i, r in enumerate(records):
            if r.id != task_id:
                continue
            r.last_check = _now_iso()
            r.last_status = last_out
            if r.status != new_status:
                _append_alert(AlertRecord(
                    id=r.id, kind=r.kind,
                    from_status=r.status, to_status=new_status,
                    at=r.last_check, last_status=last_out,
                ))
            r.status = new_status
            records[i] = r
            target = r
            break
        # Auto-drop terminal records so the queue stays bounded.
        if new_status in {"done", "timeout"}:
            records = [r for r in records if r.id != task_id]
        _write_queue(records)
    if prior_status != new_status:
        # caller may want to know transition; we already alerted.
        pass
    return target


def purge_stale(*, max_age_seconds: int = 24 * 3600) -> int:
    """Drop records older than ``max_age_seconds`` from the queue. Used
    by the daemon's startup sweep so a crash-left zombie record doesn't
    haunt the next session forever. Returns drop count."""
    cutoff = time.time() - max_age_seconds
    dropped = 0
    with _file_lock():
        records = _read_queue()
        kept: list[WaitRecord] = []
        for r in records:
            try:
                # ISO-8601 → epoch via datetime.fromisoformat (Py 3.7+).
                ts = datetime.fromisoformat(r.registered_at).timestamp()
            except Exception:
                ts = time.time()
            if ts < cutoff:
                dropped += 1
                _append_alert(AlertRecord(
                    id=r.id, kind=r.kind,
                    from_status=r.status, to_status="purged",
                    at=_now_iso(),
                    last_status=f"auto-purged after {max_age_seconds}s stale",
                ))
                continue
            kept.append(r)
        if dropped:
            _write_queue(kept)
    return dropped
