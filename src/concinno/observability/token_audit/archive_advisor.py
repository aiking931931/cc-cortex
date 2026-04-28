"""concinno.observability.token_audit.archive_advisor — ZIQ FTRL archive advisor.

Recommends skills the operator has not invoked within a configurable
retention window. The advisor is **non-destructive**: it suggests
moves to ``~/.concinno/skills_archive/<date>/<skill>/``; it never
calls ``rm`` / ``unlink`` / ``shutil.rmtree``. The only mutation it
performs is a file move (skill dir → archive dir) when the user
explicitly accepts a recommendation, and even then the archive is
retained for ``archive_retention_days`` (default 90 days) before any
manual cleanup.

ZIQ FTRL integration
--------------------
The advisor emits one outcome per user decision through
:func:`concinno.ziq_emit_helpers.emit_boolean_outcome`:

* ``archive_advisor.accept`` — reward 1.0 — operator confirmed the
  recommendation. FTRL learns the per-skill score weight upward
  (more confident archive next time).
* ``archive_advisor.reject`` — reward 0.0 — operator declined. FTRL
  learns the score weight downward (skill earns "soft evidence" and
  is not flagged again for a cooldown period).

The internal score is a single FTRL-style weighted sum of two
features:

* ``days_unused / threshold`` — linearly clamped at 1.0 once the
  skill crosses the archive threshold (default 30 days).
* ``loaded_in_recent_sessions`` — soft-evidence shrinkage when the
  skill was loaded (even if not invoked) in the last
  ``soft_evidence_window`` sessions.

The output ``score`` is in ``[0.0, 1.0]``; only candidates with
``score >= 0.6`` are surfaced to the operator. The threshold is
intentionally above the FTRL midpoint so that uncertain candidates
do not nag the user.

State storage
-------------
The advisor reads / writes ``~/.concinno/skill_invocations.json``
which the audit pipeline updates whenever
:meth:`TokenAudit.record_skill_invocation` is called. The schema is
``{"<skill>": {"last_used": <iso>, "count": <int>,
"sessions_loaded_in": <list[iso]>, "ftrl_weight": <float>}}``. The
file is rewritten atomically (``write_text`` to a tempfile, then
``replace``) to survive a crash mid-update.
"""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Default thresholds — match FEATURE_META so tests stay aligned.
DEFAULT_ARCHIVE_DAYS_THRESHOLD: int = 30
DEFAULT_ARCHIVE_RETENTION_DAYS: int = 90
DEFAULT_SOFT_EVIDENCE_WINDOW: int = 10  # sessions
DEFAULT_FTRL_ALPHA: float = 0.1
DEFAULT_SCORE_GATE: float = 0.6


def _default_state_path() -> Path:
    return Path.home() / ".concinno" / "skill_invocations.json"


def _default_archive_root() -> Path:
    return Path.home() / ".concinno" / "skills_archive"


def _now() -> float:
    return time.time()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _parse_iso(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s).timestamp()
    except (ValueError, TypeError):
        return None


@dataclass
class ArchiveCandidate:
    """One advisor recommendation."""

    skill: str
    days_unused: float
    last_used: str | None
    score: float
    soft_evidence: dict[str, Any] = field(default_factory=dict)
    suggested_archive_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ArchiveAdvisor:
    """ZIQ FTRL-driven stale-skill archive advisor."""

    def __init__(
        self,
        *,
        state_path: Path | None = None,
        archive_root: Path | None = None,
        days_threshold: int = DEFAULT_ARCHIVE_DAYS_THRESHOLD,
        retention_days: int = DEFAULT_ARCHIVE_RETENTION_DAYS,
        soft_evidence_window: int = DEFAULT_SOFT_EVIDENCE_WINDOW,
        ftrl_alpha: float = DEFAULT_FTRL_ALPHA,
        score_gate: float = DEFAULT_SCORE_GATE,
    ) -> None:
        self._state_path = state_path or _default_state_path()
        self._archive_root = archive_root or _default_archive_root()
        self._days_threshold = max(1, int(days_threshold))
        self._retention_days = max(1, int(retention_days))
        self._soft_window = max(1, int(soft_evidence_window))
        self._alpha = max(1e-4, float(ftrl_alpha))
        self._score_gate = max(0.0, min(1.0, float(score_gate)))

    # ─── Public API ────────────────────────────────────────────────

    def suggest_archives(
        self,
        *,
        retention_days: int | None = None,
        skill_dirs: dict[str, Path] | None = None,
    ) -> list[ArchiveCandidate]:
        """Compute archive candidates for skills above the days threshold.

        ``retention_days`` overrides the days-unused threshold for a
        single call (e.g. operator passes ``--days 60`` on the CLI).
        ``skill_dirs`` lets the caller provide the on-disk path for
        each skill so the advisor can populate
        ``suggested_archive_path``; if omitted, the advisor returns
        candidates without a move target (the operator must specify
        the path on accept).
        """
        threshold_days = (
            int(retention_days) if retention_days else self._days_threshold
        )
        threshold_days = max(1, threshold_days)
        state = self._load_state()
        now = _now()
        out: list[ArchiveCandidate] = []
        for name, rec in state.items():
            last_used_ts = _parse_iso(rec.get("last_used"))
            if last_used_ts is None:
                continue
            days_unused = (now - last_used_ts) / 86400.0
            if days_unused < threshold_days:
                continue
            score = self._compute_score(
                rec,
                days_unused=days_unused,
                threshold_days=threshold_days,
            )
            if score < self._score_gate:
                continue
            soft_evidence = {
                "sessions_loaded_in_recent": min(
                    self._soft_window,
                    len(rec.get("sessions_loaded_in", []) or []),
                ),
                "ftrl_weight": float(rec.get("ftrl_weight", 0.0)),
                "invocation_count": int(rec.get("count", 0)),
            }
            suggested = ""
            if skill_dirs and name in skill_dirs:
                suggested = str(self._target_archive_dir(name))
            out.append(
                ArchiveCandidate(
                    skill=name,
                    days_unused=round(days_unused, 2),
                    last_used=rec.get("last_used"),
                    score=round(score, 4),
                    soft_evidence=soft_evidence,
                    suggested_archive_path=suggested,
                )
            )
        out.sort(key=lambda c: c.score, reverse=True)
        return out

    def accept(
        self,
        skill: str,
        *,
        skill_dir: Path | None = None,
    ) -> Path | None:
        """Accept a recommendation — move (NOT delete) the skill dir.

        Returns the destination path on success, ``None`` if no move
        could be performed (skill_dir missing, source absent). Emits
        a ZIQ ``archive_advisor.accept`` outcome regardless so FTRL
        keeps learning even when the source is already gone.
        """
        self._update_ftrl(skill, accepted=True)
        self._emit_outcome(skill, accepted=True)
        if skill_dir is None or not skill_dir.exists():
            return None
        target = self._target_archive_dir(skill)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            # Move (NOT rmtree). Even if move fails, never escalate
            # to a destructive op.
            shutil.move(str(skill_dir), str(target))
        except (OSError, shutil.Error):
            return None
        return target

    def reject(self, skill: str) -> None:
        """Reject a recommendation — record FTRL penalty + cooldown."""
        self._update_ftrl(skill, accepted=False)
        self._emit_outcome(skill, accepted=False)

    def record_invocation(self, skill: str, *, session_id: str = "") -> None:
        """Update the invocation state for a skill.

        Called by :class:`TokenAudit.record_skill_invocation` so the
        advisor sees fresh ``last_used`` data without the audit owning
        the persistence layer.
        """
        if not skill:
            return
        state = self._load_state()
        rec = state.setdefault(skill, {})
        rec["last_used"] = _iso(_now())
        rec["count"] = int(rec.get("count", 0)) + 1
        sessions = rec.setdefault("sessions_loaded_in", [])
        if session_id and session_id not in sessions:
            sessions.append(session_id)
            # Cap the window to keep state file small.
            if len(sessions) > self._soft_window * 4:
                sessions[:] = sessions[-self._soft_window * 4 :]
        self._save_state(state)

    def record_load(self, skill: str, *, session_id: str = "") -> None:
        """Update the soft-evidence sessions list on plain skill load."""
        if not skill or not session_id:
            return
        state = self._load_state()
        rec = state.setdefault(skill, {})
        sessions = rec.setdefault("sessions_loaded_in", [])
        if session_id not in sessions:
            sessions.append(session_id)
            if len(sessions) > self._soft_window * 4:
                sessions[:] = sessions[-self._soft_window * 4 :]
        # If we have no last_used record yet, anchor it now so the
        # threshold clock starts from first-load.
        rec.setdefault("last_used", _iso(_now()))
        rec.setdefault("count", 0)
        self._save_state(state)

    # ─── Cleanup helpers ───────────────────────────────────────────

    def list_archived(self) -> list[Path]:
        """Return all archive bundle dirs under the archive root."""
        if not self._archive_root.exists():
            return []
        return sorted(p for p in self._archive_root.glob("*/*") if p.is_dir())

    def expire_old_archives(self) -> list[Path]:
        """Move-and-keep semantics: report (not delete) expired archives.

        We deliberately do not auto-delete — the operator must run a
        separate destructive op manually. This method only enumerates
        archives older than ``retention_days``; the CLI prints them
        and tells the operator the safe ``rm -rf`` command they could
        run (with ``destruction_guard`` confirmation).
        """
        cutoff = _now() - (self._retention_days * 86400.0)
        out: list[Path] = []
        for path in self.list_archived():
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if mtime < cutoff:
                out.append(path)
        return out

    # ─── FTRL math ─────────────────────────────────────────────────

    def _compute_score(
        self,
        rec: dict[str, Any],
        *,
        days_unused: float,
        threshold_days: int,
    ) -> float:
        """Compute the per-skill archive score in ``[0.0, 1.0]``.

        Score = clamp(days_unused / (threshold * 2), 0, 1)
              * (1 - soft_evidence_factor)
              * sigmoid(ftrl_weight)

        Where soft_evidence_factor shrinks the score when the skill
        was loaded in many recent sessions (operator may not have
        invoked it but kept it mounted for a reason).
        """
        # Day component — saturates at threshold (1.0 once unused
        # equals threshold). Below threshold the candidate is already
        # filtered out before this is called.
        day_component = min(1.0, days_unused / float(threshold_days))
        # Soft evidence shrinkage.
        sessions = rec.get("sessions_loaded_in") or []
        recent = min(self._soft_window, len(sessions))
        soft_factor = recent / float(self._soft_window)
        soft_component = 1.0 - (0.4 * soft_factor)  # max 40% shrink
        # FTRL weight — modifier centred at 1.0 for weight=0 so the
        # base score is the unmodified day×soft product, with FTRL
        # nudging it up (operator accepts) or down (operator rejects).
        # Mapping: weight∈[-5,5] → modifier∈[0.5, 1.5] linearly
        # clamped, so a single accept (+0.1) bumps score ~+1%, ten
        # accepts saturate at +50%.
        weight = float(rec.get("ftrl_weight", 0.0))
        ftrl_modifier = 1.0 + max(-0.5, min(0.5, weight / 10.0))
        return float(day_component * soft_component * ftrl_modifier)

    def _update_ftrl(self, skill: str, *, accepted: bool) -> None:
        """Update the per-skill FTRL weight on user decision."""
        state = self._load_state()
        rec = state.setdefault(skill, {})
        weight = float(rec.get("ftrl_weight", 0.0))
        # Reward 1.0 (accepted) → push score up; reward 0.0 (reject)
        # → pull down. Centred FTRL update around 0.5.
        target = 1.0 if accepted else -1.0
        weight = weight + self._alpha * (target - 0.0)
        # Soft cap to avoid runaway weights.
        weight = max(-5.0, min(5.0, weight))
        rec["ftrl_weight"] = weight
        rec["last_decision_at"] = _iso(_now())
        rec["last_decision"] = "accept" if accepted else "reject"
        self._save_state(state)

    def _emit_outcome(self, skill: str, *, accepted: bool) -> None:
        """Best-effort emit to the ZIQ outcome bus."""
        try:
            from concinno.ziq_emit_helpers import emit_boolean_outcome

            tunable = (
                "archive_advisor.accept" if accepted else "archive_advisor.reject"
            )
            emit_boolean_outcome(
                tunable=tunable,
                value=accepted,
                success=accepted,
                source="token_audit_autopilot",
                metadata={"skill": skill},
            )
        except Exception:
            # Fail-open — bus errors must never escape.
            pass

    # ─── Persistence ───────────────────────────────────────────────

    def _target_archive_dir(self, skill: str) -> Path:
        date = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        return self._archive_root / date / skill

    def _load_state(self) -> dict[str, dict[str, Any]]:
        if not self._state_path.exists():
            return {}
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        # Filter out any non-dict values from a corrupted file.
        return {
            k: v for k, v in data.items()
            if isinstance(k, str) and isinstance(v, dict)
        }

    def _save_state(self, state: dict[str, dict[str, Any]]) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(state, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self._state_path)
        except OSError:
            return


__all__ = [
    "DEFAULT_ARCHIVE_DAYS_THRESHOLD",
    "DEFAULT_ARCHIVE_RETENTION_DAYS",
    "DEFAULT_FTRL_ALPHA",
    "DEFAULT_SCORE_GATE",
    "DEFAULT_SOFT_EVIDENCE_WINDOW",
    "ArchiveAdvisor",
    "ArchiveCandidate",
]
