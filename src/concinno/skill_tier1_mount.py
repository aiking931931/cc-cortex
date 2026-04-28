"""concinno.skill_tier1_mount — SessionStart Tier1 skill auto-mount.

@module skill_tier1_mount
@responsibility Discover the curated set of "Tier1" high-value Skills and
    surface them to the agent on SessionStart via
    ``hookSpecificOutput.additionalContext`` so the model sees them in
    primacy position rather than waiting for the user to type a slash
    command. Hard 500 ms wall-clock budget — over-budget = drop the
    inject silently rather than block ``SessionStart``.
@dependencies stdlib only; consumes ``~/.concinno/skills.json`` written
    by :mod:`concinno.auto_update.tier1_registry` plus a static fallback
    list when the registry cache is missing or empty.
@exports DEFAULT_TIER1_SKILLS, Tier1MountResult, build_tier1_inject,
    mount_tier1_skills

The "auto-mount" is **advisory**, not invocation: this module never
invokes a Skill on the agent's behalf. It writes a short additional-
context block (``📌 Tier1 skills active this session: ...``) so the
agent's primacy bias surfaces the right skill names when the user's
first prompt arrives.

Why this lives in its own module rather than inside
``auto_update.tier1_registry``:

* Registry refresh is about *cache freshness*, this is about *agent
  context*. Different concern, different failure mode (registry
  contention is fine, mount inject failure should also be silent).
* The mount call has its own budget (500 ms vs 300 ms registry) and
  its own opt-out path (FEATURE_META ``skill_tier1_mount`` flag),
  separate from the existing tier1 toggles.

Design constraints:

* Pure stdlib, never imports ``anthropic``. Tier1 mount is a static
  read of ``skills.json`` and a static list — no LLM judge here. The
  proactive router (:mod:`concinno.skill_proactive_router`) is the
  module that talks to Haiku.
* ``skip_if_already_mounted`` = same-session debounce so a hook that
  fires twice within one SessionStart (rare but possible during
  ``compact + resume`` flows) does not stack two injects.
* All public functions accept a ``home`` keyword to redirect the
  ``~/.concinno`` root in tests.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

__all__ = [
    "DEFAULT_TIER1_SKILLS",
    "Tier1MountResult",
    "build_tier1_inject",
    "load_tier1_skill_list",
    "mount_tier1_skills",
]

# Hard wall-clock budget per Plan line 65: SessionStart never blocks.
DEFAULT_TIMEOUT_MS = 500

# Maximum number of Tier1 skills surfaced in a single mount call.
# Plan line 65 caps the curated set at 10. Anything beyond is noise and
# pushes the additionalContext payload past the primacy-bias budget.
MAX_TIER1_SKILLS = 10

# Curated default Tier1 list: high-value Skills that an AI King session
# should always have within reach. The list lives in source so a fresh
# install (no ``~/.concinno/tier1_skills.json``) still mounts something
# useful. Operators override by writing the override file.
DEFAULT_TIER1_SKILLS: tuple[str, ...] = (
    "memoria",
    "kb_handoff",
    "kb_runpod",
    "claude-api",
    "awareness",
    "judgment",
    "precise-fix",
    "kb_benchmark",
    "handoff-tick",
    "credentials",
)


# ── result dataclass ───────────────────────────────────────


@dataclass
class Tier1MountResult:
    """One-shot result from :func:`mount_tier1_skills`.

    Mirrors the shape of :class:`concinno.auto_update.RegistryRefreshResult`
    so SessionStart hook telemetry can format both with one branch.

    ``additional_context`` is the rendered string that the caller should
    embed inside ``hookSpecificOutput.additionalContext``. Empty string
    means "no inject this turn" (skipped, debounced, or no skills).
    """

    elapsed_ms: float = 0.0
    mounted_count: int = 0
    skipped_already_mounted: bool = False
    timed_out: bool = False
    error: Optional[str] = None
    warnings: list[str] = field(default_factory=list)
    additional_context: str = ""


# ── path helpers (test seams) ──────────────────────────────


def _concinno_home(home: Optional[Path] = None) -> Path:
    if home is not None:
        return Path(home)
    return Path.home() / ".concinno"


def _skills_cache_path(home: Optional[Path] = None) -> Path:
    return _concinno_home(home) / "skills.json"


def _tier1_override_path(home: Optional[Path] = None) -> Path:
    return _concinno_home(home) / "tier1_skills.json"


def _mount_marker_path(home: Optional[Path] = None) -> Path:
    """Per-session debounce marker.

    Holds the most recent mount timestamp; ``mount_tier1_skills`` skips
    when the marker is younger than ``debounce_window_s``. Living under
    ``~/.concinno/state/`` keeps it out of the user-facing config dir.
    """
    return _concinno_home(home) / "state" / "tier1_mount.marker"


# ── pure helpers ──────────────────────────────────────────


def load_tier1_skill_list(
    *,
    home: Optional[Path] = None,
) -> list[str]:
    """Return the operator-curated Tier1 skill names, or the default.

    Override file format: a JSON array of strings under
    ``~/.concinno/tier1_skills.json``. Anything else (missing file,
    malformed JSON, non-list, non-string entries) falls back to
    :data:`DEFAULT_TIER1_SKILLS`.

    Cap honoured here so callers cannot accidentally inject a wall of
    advisory text by writing a 200-skill file.
    """
    override = _tier1_override_path(home)
    skills: list[str]
    try:
        if override.is_file():
            raw = json.loads(override.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                skills = [s for s in raw if isinstance(s, str) and s]
            else:
                skills = list(DEFAULT_TIER1_SKILLS)
        else:
            skills = list(DEFAULT_TIER1_SKILLS)
    except Exception:
        skills = list(DEFAULT_TIER1_SKILLS)

    # Deduplicate while preserving operator order — first occurrence wins.
    seen: set[str] = set()
    deduped: list[str] = []
    for name in skills:
        if name in seen:
            continue
        seen.add(name)
        deduped.append(name)
        if len(deduped) >= MAX_TIER1_SKILLS:
            break
    return deduped


def _load_installed_skills(
    *,
    home: Optional[Path] = None,
) -> dict[str, dict[str, Any]]:
    """Return ``{name: entry}`` map from ``~/.concinno/skills.json``.

    The file is written by :func:`concinno.auto_update.refresh_tier1_registry`
    on SessionStart; if it is missing this is the first run before the
    registry has been refreshed and we return ``{}``. Callers gracefully
    degrade to "Tier1 list as plain names with no metadata".
    """
    path = _skills_cache_path(home)
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, dict)}


def build_tier1_inject(
    skill_names: list[str],
    *,
    installed: Optional[dict[str, dict[str, Any]]] = None,
) -> str:
    """Render the additionalContext block for the given Tier1 skill set.

    Returns an empty string when ``skill_names`` is empty so callers can
    do ``if text: emit(text)`` without an extra branch.

    ``installed`` is the optional ``~/.concinno/skills.json`` map; when
    a name appears in both the Tier1 list and the installed map we
    annotate it with the package source so the agent can disambiguate
    duplicate skill names across plugins.
    """
    if not skill_names:
        return ""
    installed = installed or {}
    rows: list[str] = []
    for name in skill_names:
        meta = installed.get(name)
        if isinstance(meta, dict):
            pkg = meta.get("package") or ""
            if pkg:
                rows.append(f"- /{name}  (from {pkg})")
            else:
                rows.append(f"- /{name}")
        else:
            rows.append(f"- /{name}")
    body = "\n".join(rows)
    return (
        "📌 Tier1 skills active this session — invoke via slash command "
        f"if the situation matches:\n{body}\n"
    )


def _check_debounce(
    marker: Path,
    debounce_window_s: float,
) -> bool:
    """Return True iff a mount within the debounce window already ran."""
    if not marker.is_file():
        return False
    try:
        text = marker.read_text(encoding="utf-8").strip()
        ts = float(text)
    except Exception:
        return False
    return (time.time() - ts) < debounce_window_s


def _stamp_marker(marker: Path) -> None:
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(f"{time.time():.3f}", encoding="utf-8")
    except Exception:
        # Marker write failure is non-fatal — worst case we mount twice.
        pass


# ── orchestrator ───────────────────────────────────────────


def mount_tier1_skills(
    *,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    debounce_window_s: float = 30.0,
    skip_if_already_mounted: bool = True,
    home: Optional[Path] = None,
) -> Tier1MountResult:
    """Build the Tier1 mount inject block under a hard wall-clock budget.

    Steps:

    1. Honour the opt-out env / debounce marker before doing real work.
    2. Read the operator override list (or fall back to the default).
    3. Read ``~/.concinno/skills.json`` for package annotations.
    4. Render the inject body.
    5. Stamp the debounce marker so the next call within
       ``debounce_window_s`` short-circuits to skipped.

    The function never raises — every failure mode populates ``error``
    or ``warnings`` and returns a valid :class:`Tier1MountResult`.

    Args:
        timeout_ms: Hard budget. 500 ms by default per Plan line 65.
        debounce_window_s: Suppress repeat mounts inside this window.
        skip_if_already_mounted: Honour the marker. Tests pass ``False``
            to force a fresh mount without unsetting the file.
        home: Override ``~/.concinno`` (test seam).
    """
    t0 = time.monotonic()
    result = Tier1MountResult()
    deadline = t0 + (timeout_ms / 1000.0)

    def _elapsed_ms() -> float:
        return (time.monotonic() - t0) * 1000.0

    def _over_budget() -> bool:
        return time.monotonic() >= deadline

    try:
        # 1. env opt-out (zero work fast path).
        if os.environ.get("CONCINNO_SKILL_TIER1_MOUNT_DISABLED") in {"1", "true", "yes", "on"}:
            result.warnings.append("disabled via env")
            result.elapsed_ms = _elapsed_ms()
            return result

        marker = _mount_marker_path(home)
        if skip_if_already_mounted and _check_debounce(marker, debounce_window_s):
            result.skipped_already_mounted = True
            result.elapsed_ms = _elapsed_ms()
            return result

        if _over_budget():
            result.timed_out = True
            result.elapsed_ms = _elapsed_ms()
            return result

        # 2. operator-curated list (or default fallback).
        skill_names = load_tier1_skill_list(home=home)

        if _over_budget():
            result.timed_out = True
            # Still try to render with what we have — partial inject is
            # better than nothing.
            installed: dict[str, dict[str, Any]] = {}
        else:
            # 3. annotate via skills.json if present.
            installed = _load_installed_skills(home=home)

        # 4. render.
        text = build_tier1_inject(skill_names, installed=installed)
        result.additional_context = text
        result.mounted_count = len(skill_names)

        # 5. stamp marker for next debounce window.
        if text:
            _stamp_marker(marker)

        if _over_budget():
            result.timed_out = True

        result.elapsed_ms = _elapsed_ms()
        return result

    except Exception as exc:  # noqa: BLE001
        result.error = f"unexpected: {exc}"
        result.elapsed_ms = _elapsed_ms()
        return result
