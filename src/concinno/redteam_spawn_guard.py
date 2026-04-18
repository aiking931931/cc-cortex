"""concinno.redteam_spawn_guard — Cap red-team Opus spawns per event.

@module redteam_spawn_guard
@responsibility Prevent runaway red-team cascades (parent spawns red
    team, red team spawns sub-agents, sub-agents spawn more, …) by
    capping the total subagent count PER event and persisting spawn
    history to a JSONL ledger so retrospectives can prove a session
    stayed within budget.
@dependencies stdlib only. No BaseGuard coupling — this module is a
    helper invoked by the red-team dispatch site (subagent_fork / CLI
    glue), not a pipeline guard that runs every tool call.
@exports SpawnLimitExceeded, SpawnRecord, RedteamSpawnLedger,
    before_spawn_redteam, reset_ledger, DEFAULT_MAX_SPAWNS_PER_EVENT,
    ENV_MAX_SPAWNS, REDTEAM_GRANDCHILD_DIRECTIVE

Design notes
------------
- **Why spawn-count, not $USD cap?** CLI sessions run under the CC
  subscription, so a $ cap punishes the optimal path (≥3 red + 1 blue
  Opus for Chaotic-radius decisions). The real failure mode red team
  B attacked was **unbounded recursion**: a red-team subagent spawning
  more agents turns a 4-Opus event into a 16+ Opus cascade. Capping
  spawn *count* per event hits that vector directly without penalising
  the paid-in-full optimal dispatch pattern. See
  ``rules/L1/redteam.md`` 「有一說一」 section for the 2026-04-18
  hardening.

- **Why ledger?** Auditability. Post-event questions like "did this
  session actually stay inside the 5-spawn cap?" need a durable
  record. JSONL append-only is the same shape
  ``version_sync_guard`` uses for its skip log (see
  ``_EVENT_LEDGER_CANDIDATES`` for path resolution parity).

- **Grandchild ban**. The library cannot enforce "red-team subagent
  MUST NOT spawn more subagents" at the process layer — that is a CC
  L1 (subagent spawn unmonitored) limitation. Instead we expose
  ``REDTEAM_GRANDCHILD_DIRECTIVE`` for the dispatch site to inline in
  the red-team system prompt. Sancio runtime can upgrade this to hard
  deny once its subagent fork layer lands (MEMORY #36 stack).
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

# ── Constants ─────────────────────────────────────────────────────

#: Default spawn count cap per red-team event. 3 red + 1 blue + 1
#: safety buffer = 5 subagents fits the Chaotic-radius playbook
#: (``rules/L1/redteam.md`` "爆炸半徑分級" → Chaotic row). Callers
#: may override via ``CONCINNO_REDTEAM_MAX_SPAWNS_PER_EVENT`` env var.
DEFAULT_MAX_SPAWNS_PER_EVENT: int = 5

#: Environment variable read for the per-event cap. Parsed as ``int``;
#: any parse failure or non-positive value falls back to the default.
ENV_MAX_SPAWNS: str = "CONCINNO_REDTEAM_MAX_SPAWNS_PER_EVENT"

#: Ledger filename stored under the resolved ledger dir.
LEDGER_FILENAME: str = "redteam_ledger.jsonl"

#: Directive to inline into every red-team Opus system prompt so it
#: does not spawn further sub-agents. This is docs-level guidance —
#: the Agent Tool usage pattern enforces it at the dispatch site.
REDTEAM_GRANDCHILD_DIRECTIVE: str = (
    "⛔ 禁孫代理（2026-04-18 hardened）：You are a red-team subagent. "
    "You MUST NOT spawn further sub-agents (no Agent / Task tool "
    "calls). If you feel a downstream investigation is required, "
    "return that as a recommendation to the commander and stop. "
    "Depth > 1 red-team chains are a known runaway failure mode "
    "(see concinno.redteam_spawn_guard)."
)

# ── Exceptions ────────────────────────────────────────────────────


class SpawnLimitExceeded(RuntimeError):
    """Raised when a red-team dispatch would exceed the per-event cap."""

    def __init__(
        self,
        *,
        event_id: str,
        attempted: int,
        cap: int,
        ledger_path: Optional[Path] = None,
    ) -> None:
        self.event_id = event_id
        self.attempted = attempted
        self.cap = cap
        self.ledger_path = ledger_path
        msg = (
            f"red-team spawn limit exceeded for event={event_id!r}: "
            f"attempted={attempted} > cap={cap}."
        )
        if ledger_path is not None:
            msg += f" ledger={ledger_path}"
        super().__init__(msg)


# ── Ledger record ─────────────────────────────────────────────────


@dataclass(frozen=True)
class SpawnRecord:
    """A single red-team spawn event committed to the ledger.

    Schema v1. Adding fields later = append-only (old readers ignore
    unknown keys). Removing fields = breaking.
    """

    event_id: str
    spawn_count: int
    cap: int
    timestamp: str               # ISO-8601 UTC
    estimated_spawns: int        # what the caller announced
    role: str = "redteam"        # redteam | blueteam | commander | other
    extra: dict = field(default_factory=dict)

    def to_jsonl(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)


# ── Path resolution ───────────────────────────────────────────────


def _resolve_ledger_path(cache_dir: Optional[str]) -> Path:
    """Resolve the ledger file path.

    Priority (mirrors ``version_sync_guard``'s ``_CANDIDATES`` ladder):

    1. Explicit ``cache_dir`` argument (e.g. Concinno CLI ``ctx.cache_dir``).
    2. Workspace-level ``_AI_BRAIN/00_System/`` if the directory
       already exists (legacy AI King layout; library never **creates**
       this tree, only uses it when the operator already has it).
    3. ``~/.cache/concinno/redteam_ledger.jsonl`` — always writable
       for the running user, portable, matches ``version_sync_guard``.

    The chosen directory is created if missing (``~/.cache`` case),
    never for the workspace case (which would violate CCC "zero
    personal state" rule).
    """
    if cache_dir:
        base = Path(cache_dir)
        base.mkdir(parents=True, exist_ok=True)
        return base / LEDGER_FILENAME

    cwd_anchor = Path.cwd() / "_AI_BRAIN" / "00_System"
    if cwd_anchor.is_dir():
        return cwd_anchor / LEDGER_FILENAME

    fallback = Path.home() / ".cache" / "concinno"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback / LEDGER_FILENAME


# ── Cap resolution ────────────────────────────────────────────────


def _resolve_cap() -> int:
    """Return the configured per-event cap, honouring the env override."""
    raw = os.environ.get(ENV_MAX_SPAWNS)
    if raw is None:
        return DEFAULT_MAX_SPAWNS_PER_EVENT
    try:
        value = int(raw.strip())
    except (TypeError, ValueError):
        return DEFAULT_MAX_SPAWNS_PER_EVENT
    if value <= 0:
        return DEFAULT_MAX_SPAWNS_PER_EVENT
    return value


# ── Ledger accessor ───────────────────────────────────────────────


class RedteamSpawnLedger:
    """Append-only JSONL store of red-team spawn events.

    Thread-safety: the file is opened in ``"a"`` mode with line-buffered
    writes, so concurrent appends on POSIX keep each record atomic
    (``<4 KiB`` per line well below ``PIPE_BUF``). On Windows we accept
    the small race window — the ledger is audit evidence, not a hot
    critical section.
    """

    def __init__(self, cache_dir: Optional[str] = None) -> None:
        self.path = _resolve_ledger_path(cache_dir)

    def current_count(self, event_id: str) -> int:
        """Return how many spawns already fired for ``event_id``.

        Reads the whole ledger and filters by ``event_id``. Acceptable
        because red-team events are rare (minute-scale) and the ledger
        stays small (<1 MiB over months). If you need faster lookup,
        feed records into StateStore instead — the ledger is *audit*
        not *hot-path*.
        """
        if not self.path.exists():
            return 0
        count = 0
        try:
            with self.path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if record.get("event_id") == event_id:
                        count += 1
        except OSError:
            return 0
        return count

    def append(self, record: SpawnRecord) -> None:
        """Append ``record`` to the ledger."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(record.to_jsonl() + "\n")

    def reset(self, event_id: Optional[str] = None) -> int:
        """Drop records. ``event_id=None`` wipes the whole ledger.

        Returns the number of records removed. Used in tests; callers
        in production should almost never invoke ``reset()``.
        """
        if not self.path.exists():
            return 0
        if event_id is None:
            count = self.current_count_all()
            self.path.unlink()
            return count
        remaining: list[str] = []
        dropped = 0
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    record = json.loads(stripped)
                except json.JSONDecodeError:
                    remaining.append(line.rstrip("\n"))
                    continue
                if record.get("event_id") == event_id:
                    dropped += 1
                else:
                    remaining.append(line.rstrip("\n"))
        with self.path.open("w", encoding="utf-8") as fh:
            for line in remaining:
                fh.write(line + "\n")
        return dropped

    def current_count_all(self) -> int:
        """Return total record count across every event."""
        if not self.path.exists():
            return 0
        count = 0
        try:
            with self.path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    if line.strip():
                        count += 1
        except OSError:
            return 0
        return count


# ── Public helper ─────────────────────────────────────────────────


def before_spawn_redteam(
    event_id: str,
    estimated_spawns: int = 1,
    *,
    role: str = "redteam",
    cache_dir: Optional[str] = None,
    cap: Optional[int] = None,
    extra: Optional[dict] = None,
) -> bool:
    """Gate a red-team spawn. Raise if the event would exceed the cap.

    Called by the dispatch site **before** the Agent/Task tool fires.
    Appends a record to the ledger on success.

    Parameters
    ----------
    event_id:
        Stable identifier for the containing decision/event. Use the
        session id + a short slug, e.g. ``"cc_op47_1637#ziq-patent"``.
    estimated_spawns:
        How many subagents the caller intends to fire in this single
        request. Normally ``1``; the helper supports ``>1`` so batch
        dispatch (e.g. "3 red team Opus at once") can be validated
        atomically before any of them start.
    role:
        ``"redteam" | "blueteam" | "commander" | "other"``. Logged to
        the ledger for retrospectives.
    cache_dir:
        Optional override for ledger storage (see ``_resolve_ledger_path``).
    cap:
        Explicit cap override. ``None`` → resolve from env / default.
    extra:
        Arbitrary dict carried into the ledger record (e.g. prompt
        digest, model id).

    Returns
    -------
    bool
        ``True`` when the spawn was accepted and the ledger updated.

    Raises
    ------
    SpawnLimitExceeded
        When the aggregate spawn count for ``event_id`` would exceed
        ``cap``. The ledger is **not** updated in that case.
    ValueError
        When ``estimated_spawns <= 0``.
    """
    if estimated_spawns <= 0:
        raise ValueError(
            f"estimated_spawns must be positive, got {estimated_spawns}",
        )

    effective_cap = cap if cap is not None else _resolve_cap()
    ledger = RedteamSpawnLedger(cache_dir=cache_dir)
    already = ledger.current_count(event_id)
    new_total = already + estimated_spawns
    if new_total > effective_cap:
        raise SpawnLimitExceeded(
            event_id=event_id,
            attempted=new_total,
            cap=effective_cap,
            ledger_path=ledger.path,
        )

    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for _ in range(estimated_spawns):
        already += 1
        ledger.append(
            SpawnRecord(
                event_id=event_id,
                spawn_count=already,
                cap=effective_cap,
                timestamp=ts,
                estimated_spawns=estimated_spawns,
                role=role,
                extra=extra or {},
            ),
        )
    return True


def reset_ledger(
    event_id: Optional[str] = None,
    *,
    cache_dir: Optional[str] = None,
) -> int:
    """Convenience wrapper over ``RedteamSpawnLedger.reset``."""
    return RedteamSpawnLedger(cache_dir=cache_dir).reset(event_id)


__all__ = [
    "DEFAULT_MAX_SPAWNS_PER_EVENT",
    "ENV_MAX_SPAWNS",
    "LEDGER_FILENAME",
    "REDTEAM_GRANDCHILD_DIRECTIVE",
    "RedteamSpawnLedger",
    "SpawnLimitExceeded",
    "SpawnRecord",
    "before_spawn_redteam",
    "reset_ledger",
]
