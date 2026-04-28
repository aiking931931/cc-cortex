"""concinno.setup.recommender — five-profile starter configuration.

@module concinno.setup.recommender
@responsibility Map a single profile name to a ready-to-merge ``features``
    block plus a human-readable summary so ``concinno setup`` can hand
    a new user a sensible default without inflicting a 30-question
    interactive wizard.
@dependencies Pure stdlib only (``dataclasses``, ``json``, ``os``,
    ``pathlib``, ``tempfile``). No imports from ``concinno.core.config``
    so this module stays test-friendly and avoids circular imports.
@exports Profile, PROFILES, recommend, apply

Profile feature_overrides intentionally use the on-disk ``features``
schema understood by ``concinno.core.config`` (each entry is a dict of
parameters merged into the existing block, rather than a raw boolean,
so we can flip ``enabled`` and tune knobs in one record). Callers that
want the legacy ``modules`` toggle should layer their own translator
on top — the recommender stays focused on the modern feature surface.
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Profile:
    """One named starter configuration for a Claude Code user type.

    Attributes:
        name: Short slug used as the CLI ``--profile`` value.
        description: Human-readable one-liner shown by ``--list``.
        feature_overrides: Mapping of feature key → parameter dict
            merged into the on-disk ``features`` block. Each value is
            itself a dict (not a bare boolean) so callers can flip
            ``enabled`` and tune knobs in a single record.
        notes: Free-form bullets surfaced after ``apply`` so the user
            understands which trade-offs they just opted into.
    """

    name: str
    description: str
    feature_overrides: dict[str, dict[str, Any]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Catalogue
# ---------------------------------------------------------------------------

PROFILES: dict[str, Profile] = {
    "senior": Profile(
        name="senior",
        description=(
            "Senior dev / power user — minimal blocking gates, only "
            "DestructionGuard hard-deny, full autonomy."
        ),
        feature_overrides={
            "destruction_guard": {"enabled": True},
            "release_authorization": {"enabled": False, "disabled": True},
            "sedimentation_gate": {"enabled": False},
            "handoff_required_guard": {"enabled": False},
            "premise_gate": {"enabled": False},
            "consecutive_fail_gate": {"enabled": False},
            "wiredo": {"enabled": False},
        },
        notes=[
            "Only DestructionGuard (rm -rf / DROP TABLE / force push main) blocks.",
            "Publish/release authorization is opted out; twine/npm fire freely.",
            "No sedimentation, handoff, premise or consecutive-fail gates.",
        ],
    ),
    "junior": Profile(
        name="junior",
        description=(
            "Junior dev / learning — extra safety nets, sedimentation "
            "and handoff guards on, premise gate enforces ceiling lookups."
        ),
        feature_overrides={
            "destruction_guard": {"enabled": True},
            "sedimentation_gate": {"enabled": True},
            "handoff_required_guard": {"enabled": True},
            "premise_gate": {"enabled": True},
            "consecutive_fail_gate": {"enabled": True, "max_fails": 2},
            "butterfly_guard": {"enabled": True},
            "wiredo": {"enabled": True},
            "code_guard": {"enabled": True},
        },
        notes=[
            "SedimentationGate stops Stop until corrections are sedimented.",
            "PremiseGate forces an external-docs check before citing limits.",
            "ConsecutiveFailGuard trips at 2 fails (stricter than default 3).",
        ],
    ),
    "benchmark": Profile(
        name="benchmark",
        description=(
            "Benchmark / leaderboard runner — competition mode, all "
            "blocking gates off, scoreboard-friendly telemetry only."
        ),
        feature_overrides={
            "destruction_guard": {"enabled": True},
            "release_authorization": {"enabled": False, "disabled": True},
            "sedimentation_gate": {"enabled": False},
            "handoff_required_guard": {"enabled": False},
            "premise_gate": {"enabled": False},
            "consecutive_fail_gate": {"enabled": False},
            "butterfly_guard": {"enabled": False},
            "wiredo": {"enabled": False},
            "code_guard": {"enabled": False},
            "publish_scan": {"enabled": False},
        },
        notes=[
            "All non-destruction gates off so benchmark runs do not stall.",
            "DestructionGuard kept on — leaderboards rarely need rm -rf.",
            "Re-enable individual gates after the score is locked.",
        ],
    ),
    "production": Profile(
        name="production",
        description=(
            "Production / regulated env — release authorization on, "
            "publish scan on, audit-friendly defaults."
        ),
        feature_overrides={
            "destruction_guard": {"enabled": True},
            "release_authorization": {"enabled": True, "disabled": False},
            "publish_scan": {"enabled": True},
            "secret_scan": {"enabled": True},
            "exfil_guard": {"enabled": True},
            "identity_guard": {"enabled": True},
            "git_safety": {"enabled": True},
            "dep_audit": {"enabled": True},
            "premise_gate": {"enabled": True},
            "sedimentation_gate": {"enabled": True},
            "handoff_required_guard": {"enabled": True},
            "wiredo": {"enabled": True},
        },
        notes=[
            "Release authorization is ON — publish requires the gate string.",
            "Secret / dep-audit / exfil / identity guards all hard-on.",
            "Pair with a release_coord.md before twine upload to prod.",
        ],
    ),
    "researcher": Profile(
        name="researcher",
        description=(
            "Researcher / experimenter — ZIQ autotune on, observability "
            "heavy, gates relaxed to keep ablations flowing."
        ),
        feature_overrides={
            "destruction_guard": {"enabled": True},
            "release_authorization": {"enabled": False, "disabled": True},
            "ziq_autotune": {"enabled": True},
            "token_audit_autopilot": {"enabled": True},
            "skill_emergence_guard": {"enabled": True},
            "skill_disclosure": {"enabled": True},
            "premise_gate": {"enabled": False},
            "consecutive_fail_gate": {"enabled": False},
            "wiredo": {"enabled": False},
        },
        notes=[
            "ZIQ autotune + token_audit_autopilot on for telemetry-rich runs.",
            "SkillEmergenceGuard + SkillDisclosure on so emergent patterns stage.",
            "Premise/consecutive-fail gates off to keep multi-trial loops moving.",
        ],
    ),
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _config_path() -> Path:
    """Resolve the cc_config.json path, honouring CONCINNO_HOOKS_DIR."""
    base_env = os.environ.get("CONCINNO_HOOKS_DIR")
    base = Path(base_env) if base_env else Path.home() / ".claude" / "hooks"
    return base / "cc_config.json"


def recommend(profile_name: str) -> dict[str, Any]:
    """Render a profile to a serializable preview dict.

    Args:
        profile_name: Name of a profile in :data:`PROFILES`.

    Returns:
        A dict with keys ``profile``, ``description``, ``features``,
        and ``notes`` ready to merge into ``cc_config.json``.

    Raises:
        ValueError: If ``profile_name`` is not registered.
    """
    if profile_name not in PROFILES:
        valid = ", ".join(sorted(PROFILES.keys()))
        raise ValueError(
            f"unknown profile {profile_name!r}; valid profiles: {valid}"
        )
    profile = PROFILES[profile_name]
    return {
        "profile": profile.name,
        "description": profile.description,
        "features": copy.deepcopy(profile.feature_overrides),
        "notes": list(profile.notes),
    }


def apply(
    profile_name: str,
    *,
    dry_run: bool = True,
    config_path: Path | None = None,
) -> dict[str, Any]:
    """Compute (and optionally persist) the diff for a profile.

    Args:
        profile_name: Name of a profile in :data:`PROFILES`.
        dry_run: If True (default), do not write to disk.
        config_path: Override the target ``cc_config.json`` path; used
            in tests to avoid touching the real user config.

    Returns:
        A dict with keys ``profile``, ``dry_run``, ``path``, ``before``,
        ``after``, and ``changed`` (list of touched feature keys).

    Raises:
        ValueError: If ``profile_name`` is not registered.
    """
    preview = recommend(profile_name)
    target = config_path or _config_path()

    existing: dict[str, Any] = {}
    if target.is_file():
        try:
            with target.open("r", encoding="utf-8") as fh:
                existing = json.load(fh)
        except (OSError, json.JSONDecodeError):
            existing = {}

    before_features = copy.deepcopy(existing.get("features", {}))
    after_features = copy.deepcopy(before_features)
    for key, overrides in preview["features"].items():
        slot = after_features.get(key)
        if not isinstance(slot, dict):
            slot = {}
        slot.update(overrides)
        after_features[key] = slot

    changed = sorted(
        k for k in after_features if before_features.get(k) != after_features[k]
    )

    if not dry_run:
        new_data = copy.deepcopy(existing)
        new_data["features"] = after_features
        target.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write: tmp file in same dir + os.replace.
        fd, tmp = tempfile.mkstemp(
            prefix="cc_config.", suffix=".tmp", dir=str(target.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(new_data, fh, indent=2, ensure_ascii=False)
            os.replace(tmp, target)
        except Exception:
            # Best-effort cleanup if replace failed mid-flight.
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    return {
        "profile": profile_name,
        "dry_run": dry_run,
        "path": str(target),
        "before": before_features,
        "after": after_features,
        "changed": changed,
    }
