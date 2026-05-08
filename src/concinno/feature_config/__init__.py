"""concinno.feature_config — Feature risk metadata, validation, and safe get/set.

@module feature_config
@responsibility Define risk metadata for all configurable features, validate parameter
    changes with min/max/recommended bounds, and provide safe get/set API
    with risk warnings.
@dependencies (none — self-contained metadata)
@exports list_features, get_feature, set_feature, validate_value, FEATURE_META,
    get_severity_tier

Schema additions (2.36.0a1 — all optional, backward-compatible):

* ``recommended`` (bool, default ``False``) — surfaced as a "Recommended ON"
  badge in the GUI; advisory, never overrides explicit user state.
* ``severity_if_off`` (Literal[``"none","minor","major","critical"]``,
  default ``"none"``) — drives 4-tier confirm UX in the GUI and gates
  whether ``set_feature`` writes to ``~/.concinno/critical_changes.log``.
  Invariant: every ``category == "hard_gate"`` entry MUST declare
  ``severity_if_off >= "major"``. Enforced by
  ``tests/test_feature_meta_schema_v2_36.py``.
* ``consequences_if_off`` (str, ≤120 chars zh-TW; default ``""``) — one-line
  plain-language consequence shown next to the toggle.
* ``consequences_if_off_en`` — English mirror; falls back to
  ``consequences_if_off`` when absent.

Wiring status (2.7.0 — every feature in this table is now live):

  Centralized wiring
  ------------------
  ``concinno.guards.pipeline.Pipeline._feature_enabled`` consults
  ``cfg.feature(name, "enabled")`` for every ``BaseGuard`` at every
  check/on_post_tool/on_stop call. Guard classes whose ``name``
  differs from their feature key declare ``feature_name =`` on the
  class (e.g. ``ReadFirstGuard.feature_name = "read_first_gate"``).

  Hook-level wiring
  -----------------
  Features that gate module functions rather than ``BaseGuard``
  subclasses read ``cfg.feature(..., "enabled")`` at the hook entry
  point. Current hook-level wirings (beyond the pipeline dispatch):

    * ``clarity_gate``       — on_prompt_submit.py
    * ``prompt_guard``       — on_prompt_submit.py (multi-question)
    * ``insight_engine``     — on_prompt_submit.py
    * ``streak_ux``          — on_post_tool.py (_run_streak_ux)
    * ``session_summary``    — on_stop.py (_session_summary)
    * ``delivery_gate``      — on_stop.py (_build_auto_delivery)
    * ``bash_background_gate`` / ``python_c_gate``
                              — pre_tool_guards.py (BashPythonGuard)

  Metadata-only
  -------------
  ``typescript``, ``language_enforce``,
  ``deny_marker``, ``token_display``, ``handoff_format``,
  ``pipeline_mode``, ``handoff_required_guard``, ``identity_guard``,
  ``butterfly_guard``, ``code_guard``, ``boundary_guard``,
  ``agent_cap``, ``design_theory``,
  ``token_gate``, ``structural_guard``, ``ui_verify``,
  ``publish_scan``, ``sentinel_gate``,
  ``consecutive_fail_gate``, ``hijack_gate`` — every one has either
  a ``BaseGuard`` subclass picked up by the pipeline dispatch or a
  direct ``cfg.feature()`` call at its hook entry point. Use
  ``concinno config set <name> enabled false`` and the guard stops
  running at runtime without a code change or session restart.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, Optional, cast

# Sub-module imports for FEATURE_META partitions. Kept at the top so
# ruff E402 does not fire — these modules contain only data dicts and
# do not import from ``concinno.feature_config`` themselves, so there
# is no circular-import risk.
from concinno.feature_config._meta_part1_gates import _FEATURE_META_PART_1
from concinno.feature_config._meta_part2_security import _FEATURE_META_PART_2
from concinno.feature_config._meta_part3_release_gaia import (
    _FEATURE_META_PART_3,
)
from concinno.feature_config._meta_part4_core_behav import (
    _FEATURE_META_PART_4,
)
from concinno.feature_config._meta_part5_observ import _FEATURE_META_PART_5
from concinno.feature_config._meta_part6_hooks import _FEATURE_META_PART_6
from concinno.feature_config._meta_part7_universal import (
    _FEATURE_META_PART_7,
)

if TYPE_CHECKING:
    from pathlib import Path

# ── Fail-mode taxonomy (4.3.0 — Plan B Step 1) ──────────────────────
#
# A feature whose runtime check fails (or whose policy gate fires) can
# react in one of four escalating ways. Profiles + per-feature user
# overrides + ZIQ FTRL all converge on this same 4-value Literal so the
# downstream :class:`concinno.security.policy_gate.PolicyGate` can
# dispatch without speculation.
#
# silent     — log nothing, take no action (research / shadow mode)
# warn       — stderr warn once per session, still allow the action
# warn+log   — stderr warn + persist to ~/.concinno/audit.jsonl
# hard_deny  — raise / PreToolUse deny (only profile that blocks)
#
# The literal is canonical: the validator below rejects anything else.
# Storing the four values as a frozenset makes ``in`` lookups O(1) and
# saves a tuple-construction on every validate call.

FailMode = Literal["silent", "warn", "warn+log", "hard_deny"]

VALID_FAIL_MODES: frozenset[str] = frozenset({
    "silent", "warn", "warn+log", "hard_deny",
})

# ── 4.0.0 default-off catalogue ───────────────────────────
#
# Per AI King 2026-04-26 directive: every blocking feature except
# ``DestructionGuard`` (R0-R4 hardcoded data-deletion patterns) ships
# default-OFF in 4.0.0. ``pip install concinno`` then yields a permissive
# install — the user opts into individual gates via ``concinno features
# set <name> enabled true`` or the bulk ``concinno features set-profile
# strict`` shortcut.
#
# This frozenset is the *single source of truth* — keeping it in one
# place avoids the 26-edit scatter pattern and makes future audits
# (which features ship default-on?) one ``DEFAULT_OFF_4_0_0`` lookup.
#
# Senior-dev rationale: see
# ``feedback_default_off_gates_for_senior_devs.md`` (MEMORY index)
# and the CHANGELOG ``[4.0.0]`` entry.
#
# **NOT in this set** = ships default-ON. Currently every other
# FEATURE_META entry (UX, behavioural, context, hard_quality
# enforcement, ZIQ infra, etc.) — these are observability /
# coordination / rendering features that don't deny tool calls or
# block agent flow.
# 5.0.0 BREAKING — Default-off vaporware resurrection (audit 2026-04-29).
# 24 of the 27 D-class features previously here were promoted to default-on
# per the 8-axis evidence-driven audit (zero production trace despite being
# major-wave work product). The remaining 3 entries are deliberate retains:
#
# - ``release_authorization``: sovereign user opt-out via
#   ``~/.concinno/release_auth.json`` (publish-authorization permanent
#   opt-out directive 2026-04-27 — >10 user corrections).
# - ``dspy_prompt_optimization``: cost-bearing API op — must remain
#   explicit opt-in until budget guard ships.
# - ``premise_gate``: external module (no FEATURE_META entry) honoured
#   via this fallback. Retained at user discretion; flip via
#   ``cfg.feature('premise_gate', 'enabled', True)`` or env var.
#
# Senior-dev rationale: see ``feedback_default_off_features_become_vaporware.md``
# (MEMORY #4s) and the CHANGELOG ``[5.0.0]`` entry.
DEFAULT_OFF_4_0_0: frozenset[str] = frozenset({
    "release_authorization",
    "dspy_prompt_optimization",
    "premise_gate",
})

# 5.0.0 — D-class promotions from default-off to default-on.
#
# These 27 features were the work product of major feature waves
# (security guards, CBUA gates, skill emergence pipeline, operational
# guards). All shipped default-off in 4.0.0 and accumulated zero
# production trace by the 2026-04-29 8-axis audit. Promoted to
# default-on in 5.0.0 (BREAKING).
#
# This frozenset exists so users who relied on 4.x default-off
# behaviour can bulk-disable in one CLI call (``concinno features
# disable-all-d-class`` → :data:`FEATURE_TOGGLE_PROFILES["4-x-compat"]`)
# without typing 27 individual ``cfg.feature(..., 'enabled', False)``
# overrides.
#
# The set is intentionally frozen — adding entries here implies
# another semver-major bump because it changes the meaning of
# "4-x-compat" mid-release.
D_CLASS_5_0_0: frozenset[str] = frozenset({
    # Security guards (9)
    "http_client_guard", "rce_injection_guard", "sql_injection_guard",
    "circuit_breaker_guard", "publish_scan", "publish_scan_guard",
    "semver_gate", "identity_guard", "boundary_guard",
    # CBUA gates (10)
    "butterfly_guard", "sentinel_gate", "consecutive_fail_gate",
    "hijack_gate", "token_gate", "agent_cap", "clarity_gate",
    "prompt_guard", "delivery_gate", "read_first_gate",
    # Skill audit (2)
    "token_audit_autopilot", "wiredo_subagent_verify",
    # Operational guards (5)
    "bash_background_gate", "python_c_gate", "handoff_required_guard",
    "handoff_claim_guard", "ui_verify",
})


def meta_enabled_default(name: str) -> bool:
    """Single source of truth for ship-level default-enabled.

    Lookup order:

    1. ``DEFAULT_OFF_4_0_0`` membership → returns ``False`` (the 4.0.0
       senior-dev permissive baseline). Includes ``premise_gate``
       even though it has no FEATURE_META entry — the lookup happens
       before the meta probe so this works.
    2. ``FEATURE_META[name]["enabled"]`` if explicitly declared.
    3. ``True`` (legacy default for entries that pre-date 4.0.0).

    Used by :meth:`concinno.core.config.Config.feature`,
    :func:`list_features`, and :func:`get_feature` so all three read
    paths agree on the same default — eliminates the GUI-vs-runtime
    divergence flagged by the 4.0.0 red/blue review verdict #6.

    Lookup is name-canonical only (no legacy alias resolution); the
    caller of :meth:`Config.feature` already canonicalises the name
    before consulting this helper.
    """
    if name in DEFAULT_OFF_4_0_0:
        return False
    meta = FEATURE_META.get(name)
    if meta is None:
        return True
    return bool(meta.get("enabled", True))


# ── Risk Metadata / FEATURE_META aggregate ────────────────────────────────
#
# Single source of truth for all 116 feature entries. Split across
# ``_meta_part{1..7}_*.py`` to keep each file ≤1500 LoC (per the
# repo-wide max_file_lines convention) and to give git-bisect a
# narrow blast radius when a single category regresses.
#
# Boundaries between parts were chosen at "},\n    # ── header ──"
# transitions in the legacy 6169-line ``feature_config.py`` so each
# part holds complete entries plus their preceding comment headers
# — never mid-entry. Re-split at any time via
# ``python _tmp/split_feature_config.py``.
#
# Public API (re-exported at the end of this module) is unchanged:
# ``from concinno.feature_config import FEATURE_META`` still works
# byte-equivalent.

FEATURE_META: dict[str, dict[str, Any]] = {
    **_FEATURE_META_PART_1,
    **_FEATURE_META_PART_2,
    **_FEATURE_META_PART_3,
    **_FEATURE_META_PART_4,
    **_FEATURE_META_PART_5,
    **_FEATURE_META_PART_6,
    **_FEATURE_META_PART_7,
}



# ── Back-compat aliases (legacy feature names → canonical) ──────
#
# Each entry is ``<old_name>: <canonical_name>``. When the config layer
# (``concinno.core.config.Config.feature``) sees a user-set value
# under ``<old_name>`` it transparently treats it as a value on
# ``<canonical_name>`` and emits a one-time stderr deprecation warning
# of the form
# ``concinno: feature '<old_name>' renamed to '<canonical_name>' (drops 2026-07)``.
#
# Drop policy: aliases stay for one minor version after introduction
# so existing user configs keep working through one upgrade cycle.
LEGACY_ALIASES: dict[str, str] = {
    # 2026-04-26 — leakage-suspect names removed; behavior unchanged
    # (these always only toggled the LANCZOS upscale gate, no prompt
    # injection lived under these flags).
    "bassclef_wordreverse": "gaia_music_image_upscale",
    "polygon_counting_hint": "gaia_polygon_image_upscale",
}


def resolve_alias(name: str) -> str:
    """Map a legacy feature name to its canonical replacement.

    Returns ``name`` unchanged when no alias exists. Pure lookup —
    deprecation warnings are emitted by the config layer (which has
    the per-session dedup state), not here.
    """
    return LEGACY_ALIASES.get(name, name)


# ── 2.36.0a1 schema-extension constants ────────────────────────

#: Severity tiers, ordered low->high. Index used for invariant comparisons.
_SEVERITY_ORDER: tuple[str, ...] = ("none", "minor", "major", "critical")


def get_severity_tier(name: str) -> str:
    """Return the ``severity_if_off`` tier for ``name``.

    Falls back to ``"none"`` for unknown features or entries that did
    not migrate to the 2.36.0a1 schema. Drives the GUI 4-tier confirm
    UX (none -> direct toggle / minor -> info banner / major -> 2-click
    warn / critical -> typed-feature-name confirm + audit log).
    """
    meta = FEATURE_META.get(name) or {}
    sev = meta.get("severity_if_off", "none")
    return sev if sev in _SEVERITY_ORDER else "none"


def _severity_at_or_above(name: str, threshold: str) -> bool:
    """True iff ``name`` has ``severity_if_off >= threshold``."""
    sev = get_severity_tier(name)
    try:
        return _SEVERITY_ORDER.index(sev) >= _SEVERITY_ORDER.index(threshold)
    except ValueError:
        return False


def _audit_log_path() -> Path:
    """Where high-severity feature mutations are recorded.

    Append-only, line-delimited; one record per mutation. Path is
    stable across versions so external tooling can tail it. Returns
    a :class:`pathlib.Path` (lazily imported to keep the module's
    zero-dep top-level namespace).
    """
    from pathlib import Path

    return Path.home() / ".concinno" / "critical_changes.log"


def _record_critical_change(
    name: str, key: str, value: Any, *, origin: tuple[str, ...],
) -> None:
    """Append a record to the critical-changes audit log.

    Fail-soft — observability only, never blocks ``set_feature`` if the
    audit log can't be written (filesystem read-only / disk full /
    permission denied). Format::

        <ISO-8601 UTC>  <severity>  <feature>.<key> -> <value>  origin=<...>
    """
    import datetime as _dt

    path = _audit_log_path()
    sev = get_severity_tier(name)
    timestamp = _dt.datetime.now(tz=_dt.timezone.utc).isoformat(timespec="seconds")
    origin_str = ":".join(origin) if origin else "manual"
    line = f"{timestamp}  {sev}  {name}.{key} -> {value!r}  origin={origin_str}\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        # Observability must never break set_feature.
        pass


# ── Public API ────────────────────────────────────────────


def _merge_feature_meta(
    sources: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], str]:
    """Merge one feature's meta across the three layers.

    Precedence by field (per 2.31.0 spec v2 amendment A4):

    * ``description`` / ``category`` / ``cosmetic`` / ``ziq_autotunable``
      / ``description_zh``: highest-precedence source wins (shipped >
      user > plugin).
    * ``enabled``: low-to-high cascade (plugin default -> user override
      -> shipped override). Library integrity wins for the final gate.
    * ``params``: per-param merge. Shipped params are the baseline and
      define the ``type`` / ``default`` / ``min`` / ``max``. User may
      override the effective ``value`` (but not redefine the schema).
      Plugin may introduce new params not in shipped.

    Returns ``(merged_meta, origin_label)``. ``origin_label`` is a
    single source name for the 1-source case, else a
    ``"merged:shipped+user"``-style label.
    """
    shipped = sources.get("official")
    user = sources.get("user")
    plugin = sources.get("plugin")

    merged: dict[str, Any] = {}

    # High-precedence-wins fields.
    for field in ("description", "description_zh", "category",
                  "cosmetic", "ziq_autotunable"):
        for layer in (shipped, user, plugin):
            if layer is not None and field in layer:
                merged[field] = layer[field]
                break

    # enabled cascade: plugin default -> user override -> shipped override.
    enabled = True  # ultimate default
    for layer in (plugin, user, shipped):
        if layer is not None and "enabled" in layer:
            enabled = layer["enabled"]
    merged["enabled"] = enabled

    # params per-param merge.
    shipped_params = dict(shipped.get("params", {})) if shipped else {}
    plugin_params = dict(plugin.get("params", {})) if plugin else {}
    user_params = dict(user.get("params", {})) if user else {}
    merged_params: dict[str, Any] = {}
    all_param_names = set(shipped_params) | set(plugin_params) | set(user_params)
    for pname in sorted(all_param_names):
        if pname in shipped_params:
            # Shipped defines schema; user may override value fields.
            p = dict(shipped_params[pname])
            if pname in user_params:
                for k, v in user_params[pname].items():
                    if k in ("default", "value", "recommended"):
                        p[k] = v
            merged_params[pname] = p
        elif pname in plugin_params:
            p = dict(plugin_params[pname])
            if pname in user_params:
                for k, v in user_params[pname].items():
                    if k in ("default", "value", "recommended"):
                        p[k] = v
            merged_params[pname] = p
        else:
            merged_params[pname] = dict(user_params[pname])
    merged["params"] = merged_params

    # Preserve schema_version on plugin-originated rows for downstream
    # GUI rendering / forward-compat warnings.
    if plugin is not None and "schema_version" in plugin:
        merged["schema_version"] = plugin["schema_version"]

    # Origin label. "official" is the legacy backward-compat name for
    # the shipped layer (pre-2.31.0 used this label). Keep it to avoid
    # breaking consumers that compare origin strings.
    present = [name for name in ("official", "user", "plugin") if name in sources]
    if len(present) == 1:
        origin = present[0]
    else:
        origin = "merged:" + "+".join(present)
    # Plugin origin includes the package name for GUI surfacing.
    if "plugin" in sources and "_plugin_pkg" in sources:
        pkg = sources["_plugin_pkg"]
        if origin == "plugin":
            origin = f"plugin:{pkg}"
        else:
            origin = origin + f":{pkg}"

    return merged, origin


def iter_all_features_with_origin() -> list[tuple[str, dict[str, Any], str]]:
    """Yield every feature known to this process as
    ``(name, meta, origin)`` tuples.

    Three-layer merge per 2.31.0 spec v2 amendment A4:

    * ``"shipped"`` -- entries from :data:`FEATURE_META` (always
      wins on core schema / library-integrity fields)
    * ``"user"`` -- entries from
      ``~/.concinno/user_features.json`` (may override ``enabled``
      and param values; cannot redefine shipped schema)
    * ``"plugin:<pkg>"`` -- entries from installed
      ``concinno-skills-*`` packages via the ``concinno.features``
      entry-points group (lowest precedence; user-features override
      plugin defaults by name collision)

    ``origin`` labels:

    * Single layer: ``"shipped"`` / ``"user"`` / ``"plugin:<pkg>"``
    * Multi-layer merged: ``"merged:shipped+user"`` /
      ``"merged:user+plugin:<pkg>"`` etc.

    When two sources collide on a name
    :func:`concinno.user_features.record_collision` is called so the
    GUI's collision-bar can surface the shadow.

    Originally added in 2.30.1 (shipped+user only); plugin layer
    added in 2.31.0.
    """
    try:
        from concinno.user_features import (
            clear_collision_warnings,
            load_user_features,
            record_collision,
        )
        clear_collision_warnings()
        user_feats = load_user_features()
    except Exception:
        user_feats = {}
        record_collision = None  # type: ignore[assignment]

    # Plugin layer (2.31.0). Import is lazy + failure-tolerant so a
    # broken plugin does not take down feature enumeration.
    plugin_by_name: dict[str, tuple[dict[str, Any], str]] = {}
    try:
        from concinno.plugins import iter_valid_feature_plugins

        for name, meta, pkg in iter_valid_feature_plugins():
            if name in plugin_by_name:
                # Same name from two plugin packages — first-wins,
                # mirror ToolRegistry.load_plugins behaviour.
                if record_collision is not None:
                    record_collision(
                        name,
                        f"plugin collision: also in package {pkg!r}",
                    )
                continue
            plugin_by_name[name] = (meta, pkg)
    except Exception:
        plugin_by_name = {}

    shipped_names = set(FEATURE_META.keys())
    user_names = set(user_feats.keys())
    plugin_names = set(plugin_by_name.keys())
    all_names = shipped_names | user_names | plugin_names

    rows: list[tuple[str, dict[str, Any], str]] = []
    for name in sorted(all_names):
        sources: dict[str, Any] = {}
        if name in shipped_names:
            sources["official"] = FEATURE_META[name]
        if name in user_names:
            sources["user"] = user_feats[name]
        if name in plugin_names:
            plugin_meta, pkg = plugin_by_name[name]
            sources["plugin"] = plugin_meta
            sources["_plugin_pkg"] = pkg

        # Emit collisions (anything more than one real layer).
        real_layers = [k for k in ("official", "user", "plugin") if k in sources]
        if len(real_layers) > 1 and record_collision is not None:
            winner = real_layers[0]  # official > user > plugin by iter order
            shadowed = real_layers[1:]
            for s in shadowed:
                tag = s if s != "plugin" else f"plugin:{sources.get('_plugin_pkg', '?')}"
                record_collision(
                    name,
                    f"{tag} shadowed by {winner} (merged fields preserved)",
                )

        merged_meta, origin = _merge_feature_meta(sources)
        rows.append((name, merged_meta, origin))

    return rows


def list_features(lang: str = "en") -> list[dict[str, Any]]:
    """List all features (shipped + user-registered) with current
    config values."""
    try:
        from concinno.core.config import get_config

        cfg = get_config()
    except Exception:
        cfg = None

    result = []
    for name, meta, origin in iter_all_features_with_origin():
        desc = meta.get(f"description_{lang}", meta["description"])
        # Bug fix v5.5.1 (W1B audit F2): per-key cfg.feature() lookup so the
        # CLI listing reflects env var overrides. feature_all() returns the
        # raw cc_config dict and silently drops env (CONCINNO_<FEATURE>_<PARAM>)
        # overrides — that made `concinno features list` echo state that
        # disagreed with runtime behaviour when env vars were set.
        if cfg:
            enabled_val = cfg.feature(name, "enabled")
            params_dict = {}
            for k, v in meta.get("params", {}).items():
                cfg_val = cfg.feature(name, k)
                params_dict[k] = {
                    "value": cfg_val if cfg_val is not None else v.get("default"),
                    "default": v.get("default"),
                    "recommended": v.get("recommended"),
                }
        else:
            enabled_val = meta_enabled_default(name)
            params_dict = {
                k: {
                    "value": v.get("default"),
                    "default": v.get("default"),
                    "recommended": v.get("recommended"),
                }
                for k, v in meta.get("params", {}).items()
            }
        result.append({
            "name": name,
            "category": meta["category"],
            "description": desc,
            "enabled": enabled_val,
            "source": origin,
            "params": params_dict,
        })
    return result


def get_feature(name: str, lang: str = "en") -> Optional[dict[str, Any]]:
    """Get feature info with full risk metadata."""
    meta = FEATURE_META.get(name)
    if not meta:
        return None

    # Bug fix v5.5.1 (W1B audit F2): use cfg.feature() per-key so env var
    # overrides surface here too. feature_all() returned the raw cc_config
    # dict and silently swallowed CONCINNO_<FEATURE>_<PARAM> env overrides.
    try:
        from concinno.core.config import get_config

        cfg = get_config()
    except Exception:
        cfg = None

    desc = meta.get(f"description_{lang}", meta["description"])
    params = {}
    for k, v in meta.get("params", {}).items():
        risk_suffix = f"_{lang}" if lang != "en" else ""
        cfg_val = cfg.feature(name, k) if cfg else None
        params[k] = {
            "value": cfg_val if cfg_val is not None else v.get("default"),
            **v,
        }
        # Add localized risk text if available
        for risk_key in ("risk_low", "risk_high", "risk_off"):
            localized = v.get(f"{risk_key}{risk_suffix}")
            if localized:
                params[k][risk_key] = localized

    enabled_val = cfg.feature(name, "enabled") if cfg else meta_enabled_default(name)
    return {
        "name": name,
        "category": meta["category"],
        "description": desc,
        "enabled": enabled_val,
        "params": params,
    }


def _validate_numeric(
    name: str, key: str, value: Any, param: dict[str, Any], ptype: str,
) -> list[str]:
    """Validate int or float param. Returns warnings list."""
    expected = int if ptype == "int" else (int, float)
    if not isinstance(value, expected):
        return [f"{name}.{key} must be {ptype}, got {type(value).__name__}"]
    if ptype == "float":
        value = float(value)
    warnings: list[str] = []
    if "min" in param and value < param["min"]:
        warnings.append(
            f"⚠ {name}.{key}={value} below minimum {param['min']}. "
            f"{param.get('risk_low', '')}"
        )
    elif "max" in param and value > param["max"]:
        warnings.append(
            f"⚠ {name}.{key}={value} above maximum {param['max']}. "
            f"{param.get('risk_high', '')}"
        )
    if ptype == "int":
        rec = param.get("recommended")
        if rec is not None and value != rec:
            warnings.append(
                f"ℹ Recommended: {name}.{key}={rec} (you set {value})"
            )
    return warnings


def _validate_str_or_bool(
    name: str, key: str, value: Any, param: dict[str, Any], ptype: str,
) -> list[str]:
    """Validate str or bool param. Returns warnings or errors."""
    if ptype == "str":
        if not isinstance(value, str):
            return [f"{name}.{key} must be str, got {type(value).__name__}"]
        options = param.get("options")
        if options and value not in options:
            return [f"{name}.{key}={value!r} not in {options}"]
    elif ptype == "bool":
        if not isinstance(value, bool):
            return [f"{name}.{key} must be bool, got {type(value).__name__}"]
        if not value and "risk_off" in param:
            return [f"⚠ {param['risk_off']}"]
    return []


def validate_value(name: str, key: str, value: Any) -> list[str]:
    """Validate a value change and return risk warnings (empty = safe)."""
    meta = FEATURE_META.get(name)
    if not meta:
        return [f"Unknown feature: {name}"]

    if key == "enabled":
        if not isinstance(value, bool):
            return [f"enabled must be bool, got {type(value).__name__}"]
        return []

    # GUI-managed sidecar keys (2.23.0+):
    #   ``ziq_opt_out``      — feature-level ZIQ toggle
    #   ``<param>__pinned``  — per-param manual lock (ZIQ skip)
    # Both are bool, neither lives in FEATURE_META params — accept them
    # unconditionally so the GUI can write them without every feature
    # needing a schema update.
    if key == "ziq_opt_out" or key.endswith("__pinned"):
        if not isinstance(value, bool):
            return [f"{key} must be bool, got {type(value).__name__}"]
        return []

    param = meta.get("params", {}).get(key)
    if not param:
        return [f"Unknown param: {name}.{key}"]

    ptype = param.get("type", "int")
    if ptype in ("int", "float"):
        return _validate_numeric(name, key, value, param, ptype)
    return _validate_str_or_bool(name, key, value, param, ptype)


def set_feature(
    name: str,
    key: str,
    value: Any,
    *,
    force: bool = False,
    origin: tuple[str, ...] = ("manual",),
) -> list[str]:
    """Set a feature config value. Returns risk warnings.

    Args:
        name: FEATURE_META key.
        key: Param name (or ``"enabled"``).
        value: New value.
        force: When False and validation produces warnings, change is NOT
            applied. When True, applied regardless of warnings.
        origin: Provenance tuple recorded in the preset-cascade origin
            sidecar (``~/.concinno/preset_origins.json``) — examples:
            ``("manual",)``, ``("preset", "benchmark")``,
            ``("ziq", "autotune", "full")``. Wired for narrower-scope v4
            so ``concinno preset show`` can explain why a value is
            what it is.

    Returns:
        Risk-warning strings (empty on safe change).
    """
    warnings = validate_value(name, key, value)

    if warnings and not force:
        warnings.append("→ Use force=True to apply anyway.")
        return warnings

    try:
        from concinno.core.config import get_config

        cfg = get_config()
        cfg.set_feature(name, key, value)
    except Exception as e:
        return [f"Failed to set: {e}"]

    # Record origin sidecar for preset-cascade inspection. Fail-soft —
    # origin tracking is observability, not gating.
    try:
        from concinno.preset_cascade import _record_origin

        _record_origin(name, key, origin)
    except Exception:  # pragma: no cover — optional sidecar
        pass

    # 2.36.0a1: append to ~/.concinno/critical_changes.log when the
    # feature carries severity_if_off >= "major". Drives the redteam-
    # mandated audit trail for GUI-initiated config mutations of
    # high-impact gates (R#6 acceptance per commander verdict).
    if _severity_at_or_above(name, "major"):
        _record_critical_change(name, key, value, origin=origin)

    return warnings


def list_autotunable() -> list[str]:
    """Return FEATURE_META names that ZIQ may auto-tune (non-cosmetic).

    Used by :class:`concinno.ziq_autotune_loop.ZIQAutoTuneLoop.tick` to
    short-circuit the walk over safety-only / cosmetic entries.
    """
    return sorted(
        name
        for name, meta in FEATURE_META.items()
        if meta.get("ziq_autotunable") and not meta.get("cosmetic", False)
    )


# ── Preset Profiles ──────────────────────────────────────

PROFILES: dict[str, dict[str, Any]] = {
    "minimal": {
        "description": "Lightweight — core guards only, no ARBITER, no skill routing",
        "settings": {
            "guard_count": 7,
            "arbiter": False,
            "skill_routing": False,
            "silent": False,
        },
    },
    "standard": {
        "description": "Full guards, skill routing enabled, no ARBITER overhead",
        "settings": {
            "guard_count": 55,
            "arbiter": False,
            "skill_routing": True,
            "silent": False,
        },
    },
    "paranoid": {
        "description": "Maximum safety — all guards + ARBITER post-check",
        "settings": {
            "guard_count": 55,
            "arbiter": True,
            "skill_routing": True,
            "silent": False,
        },
    },
    "competition": {
        "description": "Competition mode — all guards, ARBITER, eager skill loading, silent",
        "settings": {
            "guard_count": 55,
            "arbiter": True,
            "skill_routing": "eager",
            "silent": True,
        },
    },
    "ziq_adaptive": {
        "description": (
            "ZIQ-routed — features dynamically enabled per-request via "
            "ZIQFeatureRouter (α_t tier + ctx budget)"
        ),
        "settings": {
            "guard_count": 55,
            "arbiter": "ziq_routed",
            "skill_routing": True,
            "silent": False,
            "dynamic_routing": True,
        },
    },
}


# ── Routing Policy Convention ────────────────────────────
#
# Features may declare a ``routing_policy`` key in FEATURE_META to control
# how ZIQFeatureRouter (see ``concinno.ziq_router``) treats them:
#
#   "always_on"     → ignore ZIQ, always enabled (critical safety gates)
#   "always_off"    → ignore ZIQ, always disabled (opt-in only)
#   "ziq_routed"    → enabled based on α_t tier + ctx budget  (DEFAULT)
#   "user_override" → respect the persisted config value verbatim
#
# Absence of the key is equivalent to ``"ziq_routed"``. The router returns
# a RoutingDecision with a ``reasons`` audit trail per feature.

ROUTING_POLICY_VALUES = frozenset({
    "always_on", "always_off", "ziq_routed", "user_override",
})


def get_routing_policy(name: str) -> str:
    """Return the effective routing_policy for a feature (default ziq_routed)."""
    meta = FEATURE_META.get(name)
    if not meta:
        return "ziq_routed"
    policy = meta.get("routing_policy", "ziq_routed")
    return policy if policy in ROUTING_POLICY_VALUES else "ziq_routed"


def list_with_routing(lang: str = "en") -> list[dict[str, Any]]:
    """Like ``list_features`` but also includes effective ``routing_policy``."""
    features = list_features(lang=lang)
    for f in features:
        f["routing_policy"] = get_routing_policy(f["name"])
    return features


def list_profiles() -> dict[str, str]:
    """Return {name: description} for all available profiles."""
    return {k: v["description"] for k, v in PROFILES.items()}


def get_active_profile() -> str:
    """Return the currently active profile name.

    Resolution order (first hit wins):
      1. ``CONCINNO_PROFILE`` environment variable (if it names a
         known profile)
      2. ``active_profile`` key in the live config file (set by
         ``apply_profile``)
      3. Fallback to ``"standard"``

    Fail-soft: any exception is swallowed and ``"standard"`` returned.
    This helper drives the competition-mode advisory silencer in
    ``GuardPipeline`` — it must never raise, because raising inside
    the hook path would break every tool call.
    """
    import os as _os

    try:
        env_name = _os.environ.get("CONCINNO_PROFILE", "").strip()
        if env_name and env_name in PROFILES:
            return env_name
    except Exception:
        pass

    try:
        from concinno.core.config import get_config

        cfg = get_config()
        active = cfg.raw("active_profile", "")
        if isinstance(active, str) and active in PROFILES:
            return active
    except Exception:
        pass

    return "standard"


def apply_profile(name: str) -> list[str]:
    """Apply a preset profile. Returns list of changes made.

    Individual feature toggles can still override after applying a profile.
    Use ``/hook profile <name>`` to switch profiles.
    """
    if name not in PROFILES:
        return [f"Unknown profile: {name}. Available: {', '.join(PROFILES)}"]

    profile = PROFILES[name]
    changes: list[str] = []

    try:
        from concinno.core.config import get_config

        cfg = get_config()
        # Write directly to cc_config.json (not through set_feature validation
        # — "profile" is a meta-concept above individual features)
        cfg.update_file("active_profile", name)
        cfg.update_file("profile_settings", profile["settings"])
        for key, value in profile["settings"].items():
            changes.append(f"  {key} = {value}")
    except Exception as e:
        return [f"Failed to apply profile: {e}"]

    return [f"Applied profile '{name}': {profile['description']}"] + changes


# ── Per-feature toggle profiles (4.2.x, set-profile shortcut) ────
#
# These profiles toggle individual features in ``DEFAULT_OFF_4_0_0``
# en-masse, so an operator restoring strict mode after the 4.0.0
# permissive baseline can do it in one command instead of running
# ``concinno config set features.<name>.enabled true`` 27 times.
#
# Distinct from the legacy :data:`PROFILES` dict above — that one
# stores high-level settings (guard_count / arbiter / skill_routing
# / silent / dynamic_routing) under a meta ``profile_settings`` key,
# while this one writes per-feature ``enabled`` flags through
# :meth:`Config.set_feature` so the existing 6-source resolution
# chain (env > user > project > FEATURE_META) keeps working.
#
# Each profile maps a name to two specs (``enable`` / ``disable``).
# Each spec is either a frozenset of feature names, or the string
# sentinel ``"DEFAULT_OFF_4_0_0"`` which expands to the live frozenset
# at apply time. The ``permissive`` profile *disables* every feature
# in ``DEFAULT_OFF_4_0_0`` so a previously-applied ``strict`` can be
# rolled back in one command.
#
# ZIQ note: profile choice is operator preference, not auto-tunable
# (cosmetic=False, ziq_autotunable=False). ZIQ does not auto-flip
# operator-applied profiles; user明示 wins.

# ── Profile fail-mode override defaults (4.3.0 — Plan B Step 1) ────
#
# Each profile carries a ``fail_mode_overrides: dict[feature_name,
# FailMode]`` that the policy gate consults when a feature reports a
# failure. Resolution chain (later wins):
#
#   1. profile default (this dict)
#   2. user override in ``~/.concinno/<feature>.json::fail_mode``
#   3. env ``CONCINNO_<FEATURE>_FAIL_MODE``
#   4. ZIQ auto-tune (future — registered ``ziq_autotunable=False``
#      for 4.3.0; flips to True once outcome bus signals are wired)
#
# A feature absent from a profile's overrides falls through to the
# profile's "default for everything else" implicit category — encoded
# in the docstring per profile, not the dict, because the four
# profiles disagree on what "everything else" should mean (lite =
# silent, mainstream = warn, strict = warn+log, paranoid = hard_deny).
# :func:`get_fail_mode` materialises that fallback.

# 4.3.0 schema additions are layered on top of the existing 3 profile
# names (strict / permissive / dev) — `permissive` is now an alias to
# `lite` for backward-compat (3-month migration window per CHANGELOG).

FEATURE_TOGGLE_PROFILES: dict[str, dict[str, Any]] = {
    "lite": {
        "description": (
            "Lite default (4.3.0+) — minimal blocking, only "
            "DestructionGuard hard-denies. Other guards default to "
            "silent / warn. Aliased from ``permissive``; intended for "
            "senior-dev daily driver and pre-shipping prototypes."
        ),
        "enable": frozenset(),
        "disable": "DEFAULT_OFF_4_0_0",
        "fail_mode_overrides": {
            "destruction_guard": "hard_deny",
            "butterfly_guard": "warn",
        },
        "fail_mode_default": "silent",
    },
    "mainstream": {
        "description": (
            "Mainstream profile (4.3.0+) — production-ready balance. "
            "Hard-deny on data-loss + secrets, warn+log on quality "
            "gates, warn on the rest."
        ),
        "enable": frozenset(),
        "disable": "DEFAULT_OFF_4_0_0",
        "fail_mode_overrides": {
            "destruction_guard": "hard_deny",
            "pii_guard": "warn",
            "butterfly_guard": "warn+log",
        },
        "fail_mode_default": "warn",
    },
    "strict": {
        "description": (
            "Strict profile (pre-4.0.0 paranoid baseline) — enable "
            "all 27 default-off guards. Most checks warn+log, "
            "destruction / pii / deserialize hard-deny."
        ),
        "enable": "DEFAULT_OFF_4_0_0",  # sentinel — expanded at apply time
        "disable": frozenset(),
        "fail_mode_overrides": {
            "destruction_guard": "hard_deny",
            "pii_guard": "hard_deny",
            "deserialize_guard": "hard_deny",
            "circuit_breaker_guard": "hard_deny",
        },
        "fail_mode_default": "warn+log",
    },
    "paranoid": {
        "description": (
            "Paranoid profile (4.3.0+) — every guard hard-denies "
            "except cosmetic/observability features which stay warn. "
            "Intended for security-sensitive deployments and CI."
        ),
        "enable": "DEFAULT_OFF_4_0_0",
        "disable": frozenset(),
        "fail_mode_overrides": {
            "destruction_guard": "hard_deny",
            "pii_guard": "hard_deny",
            "deserialize_guard": "hard_deny",
            "circuit_breaker_guard": "hard_deny",
            "butterfly_guard": "hard_deny",
        },
        "fail_mode_default": "hard_deny",
    },
    "permissive": {
        "description": (
            "DEPRECATED — alias for ``lite`` since 4.3.0. Will be "
            "removed in 5.0.0. Existing CLI/tests keep working "
            "transparently via :func:`_resolve_profile_alias`."
        ),
        "enable": frozenset(),
        "disable": "DEFAULT_OFF_4_0_0",
        "fail_mode_overrides": {
            "destruction_guard": "hard_deny",
            "butterfly_guard": "warn",
        },
        "fail_mode_default": "silent",
        "alias_of": "lite",
    },
    "dev": {
        "description": (
            "Solo-dev daily driver — enable productivity features "
            "(dspy_prompt_optimization, polling_watcher, "
            "pip_aftermath_hint) only. Leaves DEFAULT_OFF_4_0_0 guards "
            "off. Inherits ``lite`` fail-mode defaults."
        ),
        "enable": frozenset({
            "dspy_prompt_optimization",
            "polling_watcher",
            "pip_aftermath_hint",
        }),
        "disable": frozenset(),
        "fail_mode_overrides": {
            "destruction_guard": "hard_deny",
            "butterfly_guard": "warn",
        },
        "fail_mode_default": "silent",
    },
    "4-x-compat": {
        "description": (
            "5.0.0 BREAKING — restore 4.x default-off behaviour for the "
            "27 D-class features promoted to default-on in 5.0.0. "
            "Idempotent. Apply once via ``concinno features set-profile "
            "4-x-compat`` (or the explicit ``concinno features "
            "disable-all-d-class`` alias) to keep 4.x trust baseline. "
            "Inherits ``lite`` fail-mode defaults so DestructionGuard "
            "still hard-denies."
        ),
        "enable": frozenset(),
        "disable": "D_CLASS_5_0_0",
        "fail_mode_overrides": {
            "destruction_guard": "hard_deny",
            "butterfly_guard": "warn",
        },
        "fail_mode_default": "silent",
    },
}


# Build-time validation: every fail_mode value across every profile
# must be a member of :data:`VALID_FAIL_MODES`. Catches typos at
# import time instead of at policy-gate dispatch (the original Plan B
# spec called for runtime validation; module-level catches the
# regression earlier and costs nothing).
def _validate_profile_fail_modes() -> None:
    """Module-import gate — raises ``ValueError`` on any bad fail_mode.

    Examined keys:
      * ``fail_mode_default`` (per-profile fallback)
      * Every value in ``fail_mode_overrides``
    """
    for name, prof in FEATURE_TOGGLE_PROFILES.items():
        default = prof.get("fail_mode_default")
        if default is not None and default not in VALID_FAIL_MODES:
            raise ValueError(
                f"Profile {name!r} has invalid fail_mode_default "
                f"{default!r}. Valid: {sorted(VALID_FAIL_MODES)}"
            )
        overrides = prof.get("fail_mode_overrides") or {}
        if not isinstance(overrides, dict):
            raise ValueError(
                f"Profile {name!r} fail_mode_overrides must be a "
                f"dict, got {type(overrides).__name__}"
            )
        for feat, mode in overrides.items():
            if mode not in VALID_FAIL_MODES:
                raise ValueError(
                    f"Profile {name!r} fail_mode_overrides[{feat!r}] "
                    f"= {mode!r} is invalid. Valid: "
                    f"{sorted(VALID_FAIL_MODES)}"
                )


_validate_profile_fail_modes()


# Profile alias map — single resolver used by both
# :func:`apply_feature_toggle_profile` (Plan B carry-over) and
# :func:`get_fail_mode` (new in 4.3.0).
_PROFILE_ALIASES: dict[str, str] = {
    name: target
    for name, prof in FEATURE_TOGGLE_PROFILES.items()
    if isinstance(target := prof.get("alias_of"), str)
}


def _resolve_profile_alias(name: str) -> str:
    """Map ``permissive`` → ``lite`` (and any future alias). Pass
    through any non-aliased name verbatim, including unknown names —
    the caller's existing "Unknown profile" error path stays
    authoritative.
    """
    return _PROFILE_ALIASES.get(name, name)


def get_fail_mode(
    feature_name: str,
    profile: str = "lite",
    *,
    cfg: Any = None,
) -> FailMode:
    """Return the effective fail-mode for ``feature_name`` under ``profile``.

    Resolution chain (later wins, mirrors :meth:`Config.feature`):

      1. Profile per-feature override (``fail_mode_overrides[feat]``)
      2. Profile catch-all (``fail_mode_default``)
      3. User override on disk (``cfg.feature(feat, "fail_mode")``)
      4. Env var ``CONCINNO_<FEATURE>_FAIL_MODE`` (handled by
         :meth:`Config.feature`'s 6-source chain — no extra work here)

    The ``cfg`` argument is optional — when ``None`` we skip user/env
    overrides and return the pure profile default. This keeps the
    function trivially callable from policy-gate hot paths that do not
    want a singleton lookup on every check.

    Raises:
        ValueError: if ``profile`` does not exist in
            :data:`FEATURE_TOGGLE_PROFILES` (after alias resolution).
            Returning a silent default would mask config bugs.
    """
    canonical = _resolve_profile_alias(profile)
    if canonical not in FEATURE_TOGGLE_PROFILES:
        raise ValueError(
            f"Unknown profile {profile!r}. Available: "
            f"{', '.join(sorted(FEATURE_TOGGLE_PROFILES))}"
        )
    prof = FEATURE_TOGGLE_PROFILES[canonical]

    # User override beats profile when cfg is supplied. Honours the
    # full 6-source chain inside Config.feature, so env vars and
    # per-project cc_config.json work for free.
    if cfg is not None:
        try:
            override = cfg.feature(feature_name, "fail_mode")
        except Exception:
            override = None
        if isinstance(override, str) and override in VALID_FAIL_MODES:
            return _coerce_fail_mode(override)

    overrides: dict[str, str] = prof.get("fail_mode_overrides") or {}
    if feature_name in overrides:
        return _coerce_fail_mode(overrides[feature_name])

    default = prof.get("fail_mode_default", "warn")
    return _coerce_fail_mode(default)


def _coerce_fail_mode(value: str) -> FailMode:
    """Narrow ``str`` → ``FailMode`` after a ``VALID_FAIL_MODES`` check.

    The runtime check has already happened at module import (or at the
    Config.feature override step), so this is purely a typing helper —
    mypy strict requires the cast to bridge ``str`` → ``Literal``.
    """
    if value not in VALID_FAIL_MODES:
        # Defence in depth — should never trigger after the import-time
        # validator, but a corrupted cc_config.json could feed a junk
        # string through Config.feature.
        raise ValueError(
            f"Invalid fail_mode {value!r}. Valid: "
            f"{sorted(VALID_FAIL_MODES)}"
        )
    # mypy needs the explicit cast — Literal narrowing from a frozenset
    # membership check is not currently inferred.
    return cast("FailMode", value)


def list_feature_toggle_profiles() -> dict[str, str]:
    """Return ``{name: description}`` for the per-feature toggle profiles."""
    return {k: v["description"] for k, v in FEATURE_TOGGLE_PROFILES.items()}


def _resolve_profile_features(
    spec: "frozenset[str] | str",
) -> frozenset[str]:
    """Expand the ``"DEFAULT_OFF_4_0_0"`` / ``"D_CLASS_5_0_0"``
    sentinels to their actual frozensets; pass through real frozensets
    verbatim."""
    if spec == "DEFAULT_OFF_4_0_0":
        return DEFAULT_OFF_4_0_0
    if spec == "D_CLASS_5_0_0":
        return D_CLASS_5_0_0
    if isinstance(spec, frozenset):
        return spec
    return frozenset()


def apply_feature_toggle_profile(
    name: str,
    cfg: Any = None,
) -> dict[str, Any]:
    """Apply a per-feature toggle profile by name.

    Returns a dict with ``profile`` / ``enabled`` / ``disabled`` /
    ``unchanged`` / ``error`` keys so callers (CLI, GUI) can render
    structured output.

    The function is idempotent — re-running ``apply_feature_toggle_profile
    ("strict")`` after the first invocation yields ``unchanged ==``
    full set, ``enabled == disabled == []``.

    The optional ``cfg`` parameter accepts a pre-built
    :class:`concinno.core.config.Config` instance (used by tests to
    isolate writes to a tmp ``cc_config.json``). When ``None``, the
    process-wide singleton from ``get_config()`` is used.
    """
    canonical = _resolve_profile_alias(name)
    if canonical not in FEATURE_TOGGLE_PROFILES:
        return {
            "profile": name,
            "error": (
                f"Unknown profile: {name!r}. Available: "
                f"{', '.join(sorted(FEATURE_TOGGLE_PROFILES))}"
            ),
            "enabled": [],
            "disabled": [],
            "unchanged": [],
        }

    profile = FEATURE_TOGGLE_PROFILES[canonical]
    to_enable = _resolve_profile_features(profile["enable"])
    to_disable = _resolve_profile_features(profile["disable"])

    enabled: list[str] = []
    disabled: list[str] = []
    unchanged: list[str] = []
    errors: list[str] = []

    if cfg is None:
        try:
            from concinno.core.config import get_config

            cfg = get_config()
        except Exception as exc:  # pragma: no cover — bootstrap is solid
            return {
                "profile": name,
                "error": f"Failed to load config: {exc}",
                "enabled": [],
                "disabled": [],
                "unchanged": [],
            }

    for feat in sorted(to_enable):
        try:
            current = bool(cfg.feature(feat, "enabled"))
        except Exception:
            current = False
        if current:
            unchanged.append(feat)
            continue
        try:
            cfg.set_feature(feat, "enabled", True)
            enabled.append(feat)
        except Exception as exc:
            errors.append(f"{feat}: {exc}")

    for feat in sorted(to_disable):
        try:
            current = bool(cfg.feature(feat, "enabled"))
        except Exception:
            current = True
        if not current:
            unchanged.append(feat)
            continue
        try:
            cfg.set_feature(feat, "enabled", False)
            disabled.append(feat)
        except Exception as exc:
            errors.append(f"{feat}: {exc}")

    result: dict[str, Any] = {
        "profile": name,
        "description": profile["description"],
        "enabled": enabled,
        "disabled": disabled,
        "unchanged": sorted(unchanged),
    }
    if errors:
        result["errors"] = errors
    return result
