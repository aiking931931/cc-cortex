"""concinno.ziq_router — Dynamic feature routing via ZIQ alpha_t classification.

@module ziq_router
@responsibility Decide which concinno features to enable per request based on
    ZIQRetrieval's alpha_t complexity classification of the user query, plus
    context-budget overrides and explicit user overrides.
@dependencies concinno.feature_config, concinno.ziq_retrieval, concinno.core.config
@exports ZIQFeatureRouter, RoutingDecision

Design
------
Each feature has an optional ``routing_policy`` key in FEATURE_META:

* ``always_on``    — ignored by ZIQ, always enabled
* ``always_off``   — ignored by ZIQ, always disabled
* ``ziq_routed``   — enabled based on alpha_t tier + ctx budget (default if missing)
* ``user_override`` — respect ``cc_config.json`` value verbatim, never auto-routed

Tier ladder (alpha_t thresholds match ZIQRetrieval defaults):

* ``simple``      ``alpha_t < 0.20``  — only core guards
* ``complicated`` ``0.20 <= alpha_t < 0.55`` — + structural / prompt / sentinel
* ``complex``     ``0.55 <= alpha_t < 0.90`` — + cognitive / fail / hijack
* ``chaotic``     ``alpha_t >= 0.90`` — all ziq_routed features ON

Context budget override: when ``ctx_tokens / yellow_zone_tokens > 0.80`` the
router force-enables the ``consecutive_fail_gate`` feature regardless of tier
(this is the in-tree analogue of the historical ``field_read`` override —
post-4.6.0 ``cognitive_anchor`` removal).

The router NEVER mutates ZIQ or FeatureConfig state. It is a pure function
of (query, ctx_tokens, user_overrides) plus the metadata snapshot.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, Optional

if TYPE_CHECKING:  # pragma: no cover
    from concinno.core.config import Config  # noqa: F401
    from concinno.ziq_retrieval import ZIQRetrieval  # noqa: F401

# NOTE: feature_config.py exposes a module-level get_feature() function and
# a FEATURE_META dict — there is NO FeatureConfig class. The runtime config
# object lives in concinno.core.config.Config and exposes a .feature(name)
# method. The router accepts ANY object with a .feature() method (duck-typed)
# so tests can pass MagicMock without instantiating real Config.

logger = logging.getLogger(__name__)

# alpha_t boundaries — match ZIQRetrieval DEFAULT_LOW/HIGH_THRESHOLD
# (ablation-validated 0.20/0.55 in ziq_retrieval.py).
ALPHA_SIMPLE_MAX = 0.20
ALPHA_COMPLICATED_MAX = 0.55
ALPHA_COMPLEX_MAX = 0.90

Tier = Literal["simple", "complicated", "complex", "chaotic"]

# Feature names sourced from FEATURE_META in feature_config.py (verified).
# Names that appear in the original spec but are not real features
# (wiredo_block, arbiter, cbua_redteam, field_read) are mapped to the
# closest in-tree analogues:
#   - arbiter / cbua_redteam → consecutive_fail_gate
#   - field_read             → consecutive_fail_gate (ctx-budget override)
#   - wiredo_block           → delivery_gate
# Post-4.6.0: cognitive_anchor / proposal_guard / whitepaper_guard removed.
_SIMPLE_BASE: frozenset[str] = frozenset({
    "butterfly_guard",
    "token_gate",
    "read_first_gate",
})

_COMPLICATED_ADD: frozenset[str] = frozenset({
    "structural_guard",
    "prompt_guard",
    "sentinel_gate",
    "code_guard",
})

_COMPLEX_ADD: frozenset[str] = frozenset({
    "consecutive_fail_gate",
    "clarity_gate",
    "hijack_gate",
})

_CHAOTIC_ADD: frozenset[str] = frozenset({
    "delivery_gate",
    "ui_verify",
    "design_theory",
    "insight_engine",
})


@dataclass
class RoutingDecision:
    """Per-request routing outcome with full audit trail."""

    enabled_features: set[str]
    disabled_features: set[str]
    alpha_t: float
    tier: Tier
    reasons: dict[str, str] = field(default_factory=dict)
    ctx_forced_field_read: bool = False


class ZIQFeatureRouter:
    """Dynamically enable/disable concinno features per request.

    The router classifies the incoming query into a complexity tier using
    ZIQRetrieval's alpha_t, then unions the cumulative tier feature sets to
    decide which ``ziq_routed`` features should be on. Hard-coded policies
    (``always_on``/``always_off``/``user_override``) bypass tier logic.

    Failure modes
    -------------
    If the underlying ZIQRetrieval call raises (or its API has drifted), the
    router logs a warning and falls back to ``complicated`` — the safe middle
    that keeps quality guards on without overpaying for chaotic-tier overhead.

    Usage
    -----
    >>> router = ZIQFeatureRouter(ziq, feature_config)
    >>> decision = router.route("refactor the auth module", ctx_tokens=80_000)
    >>> "structural_guard" in decision.enabled_features
    True
    """

    # Class-level so tests can patch and so callers can introspect.
    TIER_FEATURES: dict[Tier, frozenset[str]] = {
        "simple": _SIMPLE_BASE,
        "complicated": _SIMPLE_BASE | _COMPLICATED_ADD,
        "complex": _SIMPLE_BASE | _COMPLICATED_ADD | _COMPLEX_ADD,
        "chaotic": (
            _SIMPLE_BASE | _COMPLICATED_ADD | _COMPLEX_ADD | _CHAOTIC_ADD
        ),
    }

    # Feature force-enabled when ctx pressure exceeds CTX_FORCE_RATIO of
    # the yellow_zone_tokens budget. Was historically named field_read; the
    # post-4.6.0 in-tree analogue is consecutive_fail_gate (cognitive_anchor
    # removed in the 4.6.0 KILL 10 cleanup wave).
    CTX_FORCED_FEATURE: str = "consecutive_fail_gate"
    CTX_FORCE_RATIO: float = 0.80

    def __init__(
        self,
        ziq: "ZIQRetrieval",
        config: "Config",
        yellow_zone_tokens: int = 200_000,
    ) -> None:
        self.ziq = ziq
        self.config = config
        self.yellow_zone_tokens = max(1, int(yellow_zone_tokens))

    # ── Public API ──────────────────────────────────────

    def route(
        self,
        query: str,
        ctx_tokens: int = 0,
        user_overrides: Optional[dict[str, bool]] = None,
    ) -> RoutingDecision:
        """Compute the routing decision for one request.

        Args:
            query: User-facing query text. Passed to ZIQRetrieval for alpha_t.
            ctx_tokens: Current session context token count, for budget override.
            user_overrides: Optional explicit feature toggles. These ALWAYS win.

        Returns:
            RoutingDecision with enabled / disabled sets and reasons audit.
        """
        meta = self._load_meta()
        alpha_t, tier, fallback_used = self._classify_query(query)
        tier_features = self.TIER_FEATURES[tier]

        ctx_ratio = ctx_tokens / self.yellow_zone_tokens
        ctx_forced = ctx_ratio > self.CTX_FORCE_RATIO

        enabled: set[str] = set()
        disabled: set[str] = set()
        reasons: dict[str, str] = {}

        overrides = user_overrides or {}

        for fname, fmeta in meta.items():
            policy = self._get_policy(fmeta)

            # 1. user_overrides always win.
            if fname in overrides:
                if overrides[fname]:
                    enabled.add(fname)
                    reasons[fname] = (
                        f"{fname}: user_override → enabled"
                    )
                else:
                    disabled.add(fname)
                    reasons[fname] = (
                        f"{fname}: user_override → disabled"
                    )
                continue

            # 2. always_on / always_off ignore ZIQ.
            if policy == "always_on":
                enabled.add(fname)
                reasons[fname] = f"{fname}: always_on policy"
                continue
            if policy == "always_off":
                disabled.add(fname)
                reasons[fname] = f"{fname}: always_off policy"
                continue

            # 3. user_override policy means "respect cc_config.json verbatim",
            #    which the router cannot change. Mark as enabled or disabled
            #    based on the live config value with a passthrough reason.
            if policy == "user_override":
                live_val = self._read_live_value(fname)
                if live_val:
                    enabled.add(fname)
                    reasons[fname] = (
                        f"{fname}: user_override policy (config=on)"
                    )
                else:
                    disabled.add(fname)
                    reasons[fname] = (
                        f"{fname}: user_override policy (config=off)"
                    )
                continue

            # 4. ziq_routed (default).
            if fname in tier_features:
                enabled.add(fname)
                reasons[fname] = (
                    f"{fname}: tier={tier}, alpha_t={alpha_t:.2f}"
                )
            else:
                disabled.add(fname)
                reasons[fname] = (
                    f"{fname}: not in tier={tier} (alpha_t={alpha_t:.2f})"
                )

        # 5. ctx-budget override — force CTX_FORCED_FEATURE on regardless,
        #    unless the user explicitly disabled it.
        if ctx_forced:
            forced = self.CTX_FORCED_FEATURE
            user_disabled = forced in overrides and not overrides[forced]
            if not user_disabled:
                disabled.discard(forced)
                enabled.add(forced)
                pct = int(ctx_ratio * 100)
                reasons[forced] = (
                    f"{forced}: forced by ctx {pct}% > "
                    f"{int(self.CTX_FORCE_RATIO * 100)}%"
                )

        if fallback_used:
            reasons["__router__"] = (
                "ZIQ.route_query unavailable — fallback tier=complicated"
            )

        return RoutingDecision(
            enabled_features=enabled,
            disabled_features=disabled,
            alpha_t=alpha_t,
            tier=tier,
            reasons=reasons,
            ctx_forced_field_read=ctx_forced,
        )

    def classify_tier(self, alpha_t: float) -> Tier:
        """Map an alpha_t value to its tier label."""
        if alpha_t < ALPHA_SIMPLE_MAX:
            return "simple"
        if alpha_t < ALPHA_COMPLICATED_MAX:
            return "complicated"
        if alpha_t < ALPHA_COMPLEX_MAX:
            return "complex"
        return "chaotic"

    def _tier_features(self, tier: Tier) -> set[str]:
        """Return the cumulative ziq_routed feature set for a tier."""
        return set(self.TIER_FEATURES[tier])

    # ── Internals ───────────────────────────────────────

    def _classify_query(self, query: str) -> tuple[float, Tier, bool]:
        """Run query through ZIQ to obtain (alpha_t, tier, fallback_used).

        ZIQRetrieval.route_query CONSUMES a confidence score; it does not
        produce one. We invert the relationship by probing several confidence
        levels and using the breadth of the returned namespaces as a proxy
        for tier. A direct alpha-producing API would be cleaner, but this
        keeps the router decoupled from ZIQ internals.
        """
        try:
            # Probe the router with a mid-band confidence. ZIQ's response
            # breadth tells us where the query sits relative to learned
            # thresholds. This avoids assuming a private alpha API.
            namespaces = self.ziq.route_query(query, confidence=0.5)
            breadth = len(namespaces) if namespaces else 0
            if breadth >= 5:
                alpha_t = 0.95
            elif breadth >= 3:
                alpha_t = 0.70
            elif breadth == 2:
                alpha_t = 0.40
            else:
                alpha_t = 0.10
            return alpha_t, self.classify_tier(alpha_t), False
        except Exception as exc:
            logger.warning(
                "ZIQFeatureRouter: ZIQ.route_query failed (%s); "
                "falling back to complicated tier",
                exc,
            )
            return 0.35, "complicated", True

    def _load_meta(self) -> dict[str, dict]:
        """Snapshot FEATURE_META at call time (allows tests to patch)."""
        from concinno.feature_config import FEATURE_META

        return FEATURE_META

    @staticmethod
    def _get_policy(fmeta: dict) -> str:
        """Read routing_policy with default 'ziq_routed' if missing."""
        policy = fmeta.get("routing_policy", "ziq_routed")
        if policy not in {
            "always_on", "always_off", "ziq_routed", "user_override",
        }:
            return "ziq_routed"
        return policy

    def _read_live_value(self, fname: str) -> bool:
        """Best-effort live config read for user_override policy.

        Duck-typed: prefers Config.feature(name) → bool, falls back to
        feature_all(name) → dict, then to True so user_override never
        silently disables a feature the caller didn't deliberately turn off.
        """
        cfg = self.config

        # Preferred: Config.feature(name) → bool (concinno.core.config.Config)
        feature_fn = getattr(cfg, "feature", None)
        if callable(feature_fn):
            try:
                val = feature_fn(fname)
                if isinstance(val, bool):
                    return val
                if isinstance(val, dict):
                    return bool(val.get("enabled", True))
            except Exception:
                pass

        # Fallback: feature_all(name) → dict
        feature_all_fn = getattr(cfg, "feature_all", None)
        if callable(feature_all_fn):
            try:
                val = feature_all_fn(fname)
                if isinstance(val, dict):
                    return bool(val.get("enabled", True))
            except Exception:
                pass

        return True
