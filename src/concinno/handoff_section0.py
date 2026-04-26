"""concinno.handoff_section0 — FieldRead populator for handoff §0 block.

@module handoff_section0
@responsibility Pull live values for the handoff template's §0 (auto-revival)
    placeholders so a new session does not waste 20-50k tokens
    re-discovering pod SSH / vault aliases / env vars / last commit.
@dependencies stdlib only at import time. Optional ``concinno_skills_auth``
    and external ``runpod_pod_current`` script are loaded lazily so the
    module imports clean even on systems where neither is installed.
@exports SECTION0_FIELDS, populate_section_0, render_section_0,
    SourceFailure

The §0 contract (matches ``handoff_templates/generic_v2.md``):

    | Item              | Description                                       |
    |-------------------|---------------------------------------------------|
    | pod_id            | Active RunPod pod id, or "<n/a>" for non-bench    |
    | ssh_cmd           | Full ``ssh -i ... -p ... root@...`` line          |
    | vault_aliases     | Comma-joined credential aliases relevant to proj  |
    | env_vars          | Comma-joined env var names ($KEY only, never val) |
    | last_commit       | Short hash + subject of HEAD                      |
    | last_session_id   | Read from ``~/.concinno/session_lock.json``       |
    | last_token_usage  | Placeholder until Sancio state_store ships        |
    | populated_at      | ISO-8601 UTC timestamp                            |

Source-failure semantics (rule from spec):
  Each source is wrapped in a guarded helper. On any error the helper
  returns ``"<unknown — source failed: <reason>>"`` (a literal string)
  and never raises. ``populate_section_0`` therefore *cannot* crash —
  the worst case is a §0 full of ``<unknown ...>`` markers, which is
  still a valid handoff and tells the next session what's broken.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

# ── Field schema ───────────────────────────────────────────

SECTION0_FIELDS: tuple[str, ...] = (
    "pod_id",
    "ssh_cmd",
    "vault_aliases",
    "env_vars",
    "last_commit",
    "last_session_id",
    "last_token_usage",
    "populated_at",
)
"""Ordered tuple of field names that ``populate_section_0`` returns.

Tests assert against this constant so adding a new placeholder requires
extending here AND extending the bundled template AND the docstring
above. Triple-source alignment by design.
"""

# ── Source failure marker ──────────────────────────────────


def _failed(reason: str) -> str:
    """Format the failed-source placeholder string.

    Single helper so callers don't accidentally drift the prefix
    (downstream parsers grep for the leading ``"<unknown — "``).
    """
    return f"<unknown — source failed: {reason}>"


class SourceFailure(Exception):
    """Internal sentinel — never escapes ``populate_section_0``.

    Helper functions raise this to signal "this source is unavailable;
    record a placeholder". The top-level populator catches and converts
    to the failed-string format. Subclasses don't add value because
    the only consumer is the populator itself.
    """


# ── Source: vault aliases ─────────────────────────────────


def _list_vault_aliases(project_name: str) -> str:
    """Return comma-joined vault aliases plausibly relevant to ``project_name``.

    Strategy: list everything in the vault, then keyword-match alias
    substrings against the project name (case-insensitive). Heuristic
    only — when in doubt include, because false positives just clutter
    a row in the §0 table; false negatives cost a session.

    Always-included aliases (regardless of project): ``anthropic-main``,
    ``openai-main``. These are the universally needed credentials for
    almost any agent flow.
    """
    base_aliases = ["anthropic-main", "openai-main"]
    try:
        from concinno_skills_auth import (  # type: ignore[import-untyped]
            CredentialAuth,
        )
    except Exception as exc:  # pragma: no cover (env-specific)
        raise SourceFailure(f"concinno_skills_auth import: {exc}") from exc
    try:
        resp = CredentialAuth().call(action="list")
    except Exception as exc:  # pragma: no cover (vault locked etc.)
        raise SourceFailure(f"vault list call: {exc}") from exc

    entries = resp.get("entries") if isinstance(resp, dict) else None
    if not isinstance(entries, (list, tuple)):
        raise SourceFailure("vault list response missing 'entries' list")

    needle = project_name.lower()
    matched: list[str] = []
    for ent in entries:
        if not isinstance(ent, dict):
            continue
        alias = ent.get("alias")
        if not isinstance(alias, str):
            continue
        if any(tok and tok in alias.lower() for tok in needle.split("-")):
            matched.append(alias)

    out = list(dict.fromkeys(base_aliases + matched))  # de-dup, keep order
    return ", ".join(out) if out else "(none matched)"


# ── Source: pod info ──────────────────────────────────────


def _read_pod_cache() -> dict:
    """Read ``~/.concinno/pod_current.json`` if present; else raise SourceFailure."""
    path = Path.home() / ".concinno" / "pod_current.json"
    if not path.is_file():
        raise SourceFailure("pod_current.json not present (run pod_current.py)")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SourceFailure(f"pod_current.json parse: {exc}") from exc


def _pod_id_from_cache(cache: dict) -> str:
    pid = cache.get("pod_id") or cache.get("id")
    if not isinstance(pid, str) or not pid:
        raise SourceFailure("pod_current.json missing pod_id")
    return pid


def _ssh_cmd_from_cache(cache: dict) -> str:
    ssh = cache.get("ssh_cmd")
    if isinstance(ssh, str) and ssh:
        return ssh
    # Fallback: synthesize from ip + port if present.
    ip = cache.get("ip")
    port = cache.get("port")
    if ip and port:
        return f"ssh -i ~/.ssh/runpod_ed25519 -p {port} root@{ip}"
    raise SourceFailure("pod_current.json missing ssh_cmd / ip+port")


# ── Source: env vars (names only, never values) ───────────


_ALWAYS_RELEVANT_ENV_NAMES: tuple[str, ...] = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "HF_TOKEN",
    "RUNPOD_API_KEY",
)


def _list_env_var_names(project_name: str) -> str:
    """Return comma-joined names of env vars set in current process.

    Filters to: always-relevant set ∪ env vars whose name contains
    the project name (uppercased). Values are never read.
    """
    matches = [n for n in _ALWAYS_RELEVANT_ENV_NAMES if n in os.environ]
    needle = project_name.upper().replace("-", "_")
    for name in os.environ:
        if needle and needle in name and name not in matches:
            matches.append(name)
    return ", ".join(matches) if matches else "(none set)"


# ── Source: last git commit ────────────────────────────────


def _last_git_commit(project_root: Path) -> str:
    """Short hash + subject of HEAD, for ``project_root`` git repo."""
    if not shutil.which("git"):
        raise SourceFailure("git binary not on PATH")
    try:
        proc = subprocess.run(
            ["git", "log", "-1", "--format=%h %s"],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception as exc:  # pragma: no cover
        raise SourceFailure(f"git log subprocess: {exc}") from exc
    if proc.returncode != 0:
        # Likely "not a git repo" or "no HEAD yet".
        msg = (proc.stderr or "").strip().splitlines()[:1]
        raise SourceFailure(f"git log failed: {msg[0] if msg else proc.returncode!r}")
    out = (proc.stdout or "").strip()
    if not out:
        raise SourceFailure("git log returned empty output")
    return out


# ── Source: last session id ───────────────────────────────


def _last_session_id() -> str:
    """Read ``~/.concinno/session_lock.json`` for last known session id."""
    path = Path.home() / ".concinno" / "session_lock.json"
    if not path.is_file():
        raise SourceFailure("session_lock.json absent")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SourceFailure(f"session_lock.json parse: {exc}") from exc
    sid = data.get("session_id") if isinstance(data, dict) else None
    if not isinstance(sid, str) or not sid:
        raise SourceFailure("session_lock.json missing session_id")
    return sid


# ── Public API ────────────────────────────────────────────


def populate_section_0(
    project_name: str,
    *,
    project_root: Path | str | None = None,
) -> dict[str, str]:
    """Build the §0 placeholder dict for ``project_name``.

    Args:
        project_name: Handoff project key. Used for keyword-matching
            vault aliases and env var names.
        project_root: Optional override of the git repo root used for
            the ``last_commit`` lookup. Defaults to current working
            directory.

    Returns:
        Dict whose keys are exactly :data:`SECTION0_FIELDS` and whose
        values are either the live value as a string OR a literal
        ``"<unknown — source failed: <reason>>"`` placeholder.

    Side effects: none (reads only). Network call only if the lazy
    pod-cache helper triggers it — current implementation reads the
    cached JSON, never re-hits RunPod API, so the function is offline-
    safe.

    Always returns; never raises.
    """
    if project_root is None:
        root = Path.cwd()
    else:
        root = Path(project_root)

    out: dict[str, str] = {}

    # vault aliases
    try:
        out["vault_aliases"] = _list_vault_aliases(project_name)
    except SourceFailure as exc:
        out["vault_aliases"] = _failed(str(exc))

    # pod (cache-only)
    try:
        cache = _read_pod_cache()
        try:
            out["pod_id"] = _pod_id_from_cache(cache)
        except SourceFailure as exc:
            out["pod_id"] = _failed(str(exc))
        try:
            out["ssh_cmd"] = _ssh_cmd_from_cache(cache)
        except SourceFailure as exc:
            out["ssh_cmd"] = _failed(str(exc))
    except SourceFailure as exc:
        marker = _failed(str(exc))
        out["pod_id"] = marker
        out["ssh_cmd"] = marker

    # env var names
    try:
        out["env_vars"] = _list_env_var_names(project_name)
    except SourceFailure as exc:  # pragma: no cover (helper never raises today)
        out["env_vars"] = _failed(str(exc))

    # last commit
    try:
        out["last_commit"] = _last_git_commit(root)
    except SourceFailure as exc:
        out["last_commit"] = _failed(str(exc))

    # last session id
    try:
        out["last_session_id"] = _last_session_id()
    except SourceFailure as exc:
        out["last_session_id"] = _failed(str(exc))

    # token usage — leave placeholder (Sancio state_store dependency)
    out["last_token_usage"] = (
        "<deferred — Sancio state_store integration pending>"
    )

    out["populated_at"] = datetime.now(timezone.utc).isoformat()

    # Defensive: ensure every contracted field is present even if
    # somebody re-orders the helpers above and forgets one.
    for field_name in SECTION0_FIELDS:
        out.setdefault(field_name, _failed("populator skipped this field"))

    return out


def render_section_0(values: Mapping[str, str], template: str) -> str:
    """Render a §0 template by substituting ``{{key}}`` placeholders.

    Args:
        values: Output of :func:`populate_section_0` (or a hand-built
            dict with the same keys).
        template: Markdown template containing ``{{field_name}}``
            placeholders. Unknown placeholders are left as-is so the
            user notices something is missing.

    Returns:
        Rendered markdown string.
    """
    out = template
    for key in SECTION0_FIELDS:
        out = out.replace("{{" + key + "}}", values.get(key, _failed("not provided")))
    return out


__all__ = [
    "SECTION0_FIELDS",
    "SourceFailure",
    "populate_section_0",
    "render_section_0",
]
