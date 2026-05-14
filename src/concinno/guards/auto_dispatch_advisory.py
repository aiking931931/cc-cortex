"""concinno.guards.auto_dispatch_advisory — Detect irreversible commits with no recent red-team.

@module auto_dispatch_advisory
@responsibility Inspect the most recent ``git commit`` performed by a Bash
    tool call. When the commit message contains an irreversible-keyword
    (release / publish / pine deploy / prod / db schema / migration /
    force push) AND no manual red/blue/green ledger record exists in the
    last ``advisory_window_seconds`` window, emit:
      1. an audit JSONL line under
         ``~/.concinno/audit/auto_dispatch_advisory.jsonl``
      2. a single-line stderr warning (per ``MEMORY 4r5`` advisory not
         block + ``CC L6`` PostToolUse cannot deny side effects).
@dependencies
    concinno.redteam_spawn_guard.RedteamSpawnLedger (for recent role
    counts), concinno.core.config (feature kill-switch + params).
@exports detect_irreversible_commit, on_post_tool_bash, IrreversibleHit,
    AdvisoryRecord, AUDIT_JSONL_PATH, IRREVERSIBLE_KEYWORDS

Design
------
This module is **advisory only** — never deny, never raise. CC L6
(GitHub anthropics/claude-code#32105) means PostToolUse cannot block
side effects from a tool call that has already run; the commit is
already on disk by the time we see it. The only useful action is to
nudge the operator that a manual red/blue dispatch was skipped on a
high-blast-radius event so the next session can reconcile.

Rate limit
----------
Per-commit-sha, 24 h cooldown. The rate-limit ledger lives under
``~/.concinno/audit/auto_dispatch_advisory_seen.jsonl`` and tracks
``{sha, timestamp}`` pairs. We choose JSONL over JSON-dict for the
same reason the redteam spawn guard does: append-only is concurrency-
safe across overlapping CC sessions.

Switch wiring
-------------
``cfg.feature("auto_dispatch_chaotic_advisory", "enabled")`` — default
True since 2026-05-14 (advisory-only never deny per L6 + MEMORY 4zn
warn-don't-deny on opt-out). Prior to 2026-05-14 default was False
(opt-in per L0 鐵律 #6 + MEMORY 4zn).

Params (per FEATURE_META):
    advisory_window_seconds (int, default 7200 = 2 h)
    rate_limit_seconds      (int, default 86400 = 24 h)

Production canary
-----------------
After shipping this feature, verify wiring really fires in a real
session (not just pytest mocks) by checking the audit jsonl 1 week
after ship::

    test -s ~/.concinno/audit/auto_dispatch_advisory.jsonl && \\
        wc -l ~/.concinno/audit/auto_dispatch_advisory.jsonl

If size > 0 / line count > 0 → wiring is live and production sessions
have hit the advisory path. If size == 0 after 1 week of normal use
including ≥1 irreversible-keyword commit, the wiring is healthy in
tests but never fires in prod — investigate the same way MEMORY 4zm
+ 2h diagnosed the 5-system production-not-fired pattern (payload
metadata missing, short-circuit, switch source-chain default, etc.).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

# ── Constants ─────────────────────────────────────────────────────

#: Keyword set indicating an irreversible / release / production-touching
#: commit. Lowercase compare; conservative — false positives only mean
#: an extra audit line, not a hard deny.
IRREVERSIBLE_KEYWORDS: tuple[str, ...] = (
    "release",
    "publish",
    "pine deploy",
    "pine v",          # pine v3 / pine v4 deploy commits
    "deploy prod",
    "prod deploy",
    " prod",           # leading-space-anchored to avoid ' product '
    "db schema",
    "schema migration",
    "migration",
    "force push",
    "force-push",
    "twine upload",
    "npm publish",
    "cargo publish",
    "docker push",
    "git tag",
)

#: Path to the audit JSONL file. Per-user (~/.concinno) so multi-project
#: sessions on the same machine share a single audit trail.
AUDIT_JSONL_PATH: Path = Path.home() / ".concinno" / "audit" / "auto_dispatch_advisory.jsonl"

#: Path to the rate-limit ledger. Same dir, separate file so retention /
#: rotation can treat them independently if a future audit module wants
#: to.
_SEEN_JSONL_PATH: Path = (
    Path.home() / ".concinno" / "audit" / "auto_dispatch_advisory_seen.jsonl"
)

#: Default lookback window for "recent manual redteam" check.
DEFAULT_ADVISORY_WINDOW_S: int = 7200  # 2 hours

#: Default rate-limit (per-commit-sha) cooldown.
DEFAULT_RATE_LIMIT_S: int = 86400  # 24 hours


# ── Dataclasses ───────────────────────────────────────────────────


@dataclass(frozen=True)
class IrreversibleHit:
    """Result of inspecting a commit message for irreversible keywords."""

    sha: str
    matched_keyword: str
    subject: str

    @property
    def is_hit(self) -> bool:
        return bool(self.matched_keyword)


@dataclass
class AdvisoryRecord:
    """Audit JSONL schema v1.

    Adding fields later = append-only (old readers ignore unknown).
    """

    timestamp: str            # ISO-8601 UTC
    commit_sha: str
    matched_keyword: str
    commit_subject: str
    redteam_count_window: int
    advisory_window_seconds: int
    suggestion: str
    session_id: str = ""
    extra: dict = field(default_factory=dict)

    def to_jsonl(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)


# ── Commit detection ──────────────────────────────────────────────


_GIT_COMMIT_PATTERNS = (
    re.compile(r"\bgit\s+commit\b", re.IGNORECASE),
    re.compile(r"\bgit\s+-c\s+\S+\s+commit\b", re.IGNORECASE),
)


def _is_git_commit_command(command: str) -> bool:
    """True when the bash command line looks like a git commit invocation."""
    if not command:
        return False
    for pat in _GIT_COMMIT_PATTERNS:
        if pat.search(command):
            return True
    return False


def _git_log_head(cwd: Optional[str] = None) -> Optional[tuple[str, str]]:
    """Return (sha, subject) of HEAD, or None if git fails."""
    no_window = 0x08000000 if sys.platform == "win32" else 0
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%H%n%s%n%b"],
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=5,
            creationflags=no_window,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    output = (result.stdout or "").strip().splitlines()
    if not output:
        return None
    sha = output[0].strip()
    # Combine subject + body so keyword matches can hit either.
    subject_lines = output[1:]
    subject = "\n".join(subject_lines).strip()
    if not sha:
        return None
    return sha, subject


def detect_irreversible_commit(
    commit_subject: str,
    commit_sha: str,
    keywords: Iterable[str] = IRREVERSIBLE_KEYWORDS,
) -> IrreversibleHit:
    """Match commit subject (lowercased) against irreversible keywords.

    Returns ``IrreversibleHit(matched_keyword="")`` when no keyword hits.
    """
    if not commit_subject:
        return IrreversibleHit(sha=commit_sha, matched_keyword="", subject="")
    lowered = commit_subject.lower()
    for kw in keywords:
        kw_norm = kw.lower()
        if kw_norm and kw_norm in lowered:
            return IrreversibleHit(
                sha=commit_sha,
                matched_keyword=kw,
                subject=commit_subject,
            )
    return IrreversibleHit(sha=commit_sha, matched_keyword="", subject=commit_subject)


# ── Recent redteam check ──────────────────────────────────────────


def _count_recent_redteam(window_seconds: int) -> int:
    """Count red/blue/green ledger records inside the lookback window.

    Uses ``RedteamSpawnLedger`` so ledger storage stays unified. Failures
    (missing ledger / parse errors / OS errors) → return 0 (treat as
    "no recent dispatch", which is the conservative path that emits
    advisory).
    """
    try:
        from concinno.redteam_spawn_guard import RedteamSpawnLedger
    except Exception:
        return 0

    try:
        ledger = RedteamSpawnLedger()
        ledger_path = ledger.path
    except Exception:
        return 0

    if not ledger_path.exists():
        return 0

    cutoff = time.time() - max(window_seconds, 0)
    count = 0
    try:
        with ledger_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    record = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                role = str(record.get("role", "")).lower()
                if role not in (
                    "redteam", "blueteam", "greenteam",
                    "red", "blue", "green", "verifier",
                ):
                    continue
                ts_raw = record.get("timestamp", "")
                ts_epoch = _parse_iso_timestamp(ts_raw)
                if ts_epoch is None:
                    continue
                if ts_epoch >= cutoff:
                    count += 1
    except OSError:
        return 0
    return count


def _parse_iso_timestamp(raw: str) -> Optional[float]:
    """Parse ISO-8601 timestamp → epoch seconds. Returns None on failure."""
    if not raw:
        return None
    try:
        # Tolerate both Z-suffix and +00:00 forms.
        normalized = raw.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


# ── Rate limit ────────────────────────────────────────────────────


def _seen_recently(sha: str, cooldown_s: int, *, seen_path: Optional[Path] = None) -> bool:
    """True if a record for ``sha`` exists within ``cooldown_s``.

    Used to suppress duplicate advisories for the same commit (e.g. when
    the agent re-runs a verification step).
    """
    target = seen_path or _SEEN_JSONL_PATH
    if not target.exists():
        return False
    cutoff = time.time() - max(cooldown_s, 0)
    try:
        with target.open("r", encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    record = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if record.get("sha") != sha:
                    continue
                ts = float(record.get("timestamp_epoch", 0))
                if ts >= cutoff:
                    return True
    except OSError:
        return False
    return False


def _record_seen(sha: str, *, seen_path: Optional[Path] = None) -> None:
    """Append a ``{sha, timestamp_epoch}`` entry to the seen ledger."""
    target = seen_path or _SEEN_JSONL_PATH
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            {"sha": sha, "timestamp_epoch": time.time()},
            ensure_ascii=False,
            sort_keys=True,
        )
        with target.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        return


# ── Audit emission ────────────────────────────────────────────────


def _append_audit(record: AdvisoryRecord, *, audit_path: Optional[Path] = None) -> None:
    """Append the advisory record to the audit JSONL."""
    target = audit_path or AUDIT_JSONL_PATH
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(record.to_jsonl() + "\n")
    except OSError:
        return


def _emit_stderr(record: AdvisoryRecord) -> None:
    """Single-line stderr warning. Per MEMORY 4r5 advisory-not-block."""
    msg = (
        "\033[93m[Concinno: auto_dispatch_chaotic_advisory] "
        f"irreversible commit {record.commit_sha[:8]} "
        f"({record.matched_keyword!r}) — "
        "no manual red/blue/green dispatch in window. "
        f"{record.suggestion}\033[0m"
    )
    try:
        sys.stderr.write(msg + "\n")
    except OSError:
        return


# ── Public hook entry-point ───────────────────────────────────────


def on_post_tool_bash(
    tool_name: str,
    tool_input: dict[str, Any],
    *,
    session_id: str = "",
    cwd: Optional[str] = None,
    cfg_override: Optional[dict[str, Any]] = None,
    audit_path: Optional[Path] = None,
    seen_path: Optional[Path] = None,
) -> Optional[AdvisoryRecord]:
    """PostToolUse hook entry. Returns the emitted record (or ``None``).

    Parameters
    ----------
    tool_name:
        Should be ``"Bash"`` for the hook to fire. Other tools no-op.
    tool_input:
        The Bash tool input dict. Reads ``command`` field.
    session_id:
        Session ID for the audit record (best-effort identifier).
    cwd:
        Optional working directory for the ``git log`` lookup. Defaults
        to the current process cwd (which CC sets to the project dir).
    cfg_override:
        Test seam: when supplied, bypasses the live ``get_config()``
        lookup and uses this dict instead. Keys: ``enabled`` (bool),
        ``advisory_window_seconds`` (int), ``rate_limit_seconds`` (int).
    audit_path / seen_path:
        Test seams. Override the on-disk file locations.

    Returns
    -------
    AdvisoryRecord | None
        The record actually written. ``None`` when the feature is off,
        the tool isn't Bash, the command isn't a git commit, the commit
        message doesn't match keywords, recent redteam exists, or the
        rate-limit gate trips.
    """
    if tool_name != "Bash":
        return None

    command = ""
    if isinstance(tool_input, dict):
        command = str(tool_input.get("command", ""))
    if not _is_git_commit_command(command):
        return None

    enabled, window_s, rate_limit_s = _resolve_config(cfg_override)
    if not enabled:
        return None

    head = _git_log_head(cwd=cwd)
    if head is None:
        return None
    sha, subject = head

    hit = detect_irreversible_commit(subject, sha)
    if not hit.is_hit:
        return None

    if _seen_recently(sha, rate_limit_s, seen_path=seen_path):
        return None

    redteam_count = _count_recent_redteam(window_s)
    if redteam_count > 0:
        # Manual dispatch already happened — no advisory needed.
        return None

    record = AdvisoryRecord(
        timestamp=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        commit_sha=sha,
        matched_keyword=hit.matched_keyword,
        commit_subject=subject[:240],  # bound subject growth in audit log
        redteam_count_window=0,
        advisory_window_seconds=window_s,
        suggestion=(
            "Suggest: `concinno redteam record-manual --event-id "
            "<radius-name> --role red|blue|green` or dispatch Agent "
            "tool with role=red|blue per rules/L1/redteam.md."
        ),
        session_id=session_id or _resolve_session_id(),
    )
    _append_audit(record, audit_path=audit_path)
    _record_seen(sha, seen_path=seen_path)
    _emit_stderr(record)
    return record


# ── Helpers ───────────────────────────────────────────────────────


def _resolve_config(
    override: Optional[dict[str, Any]] = None,
) -> tuple[bool, int, int]:
    """Resolve (enabled, window_s, rate_limit_s) honouring the override seam."""
    if override is not None:
        return (
            bool(override.get("enabled", False)),
            int(override.get("advisory_window_seconds", DEFAULT_ADVISORY_WINDOW_S)),
            int(override.get("rate_limit_seconds", DEFAULT_RATE_LIMIT_S)),
        )

    enabled = False
    window_s = DEFAULT_ADVISORY_WINDOW_S
    rate_limit_s = DEFAULT_RATE_LIMIT_S
    try:
        from concinno.core.config import get_config

        cfg = get_config()
        enabled = bool(cfg.feature("auto_dispatch_chaotic_advisory", "enabled"))
        raw_w = cfg.feature("auto_dispatch_chaotic_advisory", "advisory_window_seconds")
        if raw_w is not None:
            try:
                window_s = int(raw_w)
            except (TypeError, ValueError):
                pass
        raw_r = cfg.feature("auto_dispatch_chaotic_advisory", "rate_limit_seconds")
        if raw_r is not None:
            try:
                rate_limit_s = int(raw_r)
            except (TypeError, ValueError):
                pass
    except Exception:
        pass
    return enabled, window_s, rate_limit_s


def _resolve_session_id() -> str:
    """Best-effort session id pull from CC env vars."""
    return (
        os.environ.get("CLAUDE_SESSION_ID", "")
        or os.environ.get("CC_SESSION_ID", "")
        or ""
    )[:32]
