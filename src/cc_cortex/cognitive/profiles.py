"""Product cognitive profiles — per-product configuration for CBUA.

@module cognitive.profiles
@responsibility Define which cognitive levels and action phases each product uses.
    Avoids one-size-fits-all overhead by letting products opt into what they need.
@dependencies cognitive.router
@exports ProductProfile, get_profile, PROFILES
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ProductProfile:
    """Cognitive configuration for a specific product.

    Attributes:
        name: Product identifier.
        cognitive_levels: Which C-levels are active (e.g. ["c0","c2","c3"]).
        action_phases: Which A-phases are active (e.g. ["a2","a3","a5"]).
        wiredo_enabled: Whether WIREDO verification is active.
        wiredo_asset_types: Which asset types to verify.
        special_notes: Product-specific behavior notes.
    """

    name: str
    cognitive_levels: tuple[str, ...] = ("c0", "c1", "c2", "c4")
    action_phases: tuple[str, ...] = ("a2", "a3", "a5")
    wiredo_enabled: bool = True
    wiredo_asset_types: tuple[str, ...] = ("code",)
    special_notes: str = ""
    extra: dict = field(default_factory=dict)


# ── Built-in profiles ────────────────────────────────────

PROFILES: dict[str, ProductProfile] = {
    "ccc": ProductProfile(
        name="CC Cortex",
        cognitive_levels=("c0", "c1", "c2", "c3", "c4", "c5"),
        action_phases=("a0", "a1", "a2", "a3", "a4", "a5"),
        wiredo_enabled=True,
        wiredo_asset_types=("code", "document"),
        special_notes="Reference implementation. Full cognitive stack.",
    ),
    "security-app": ProductProfile(
        name="Security App",
        cognitive_levels=("c0", "c1", "c2", "c3", "c4", "c5"),
        action_phases=("a0", "a1", "a2", "a3", "a4", "a5"),
        wiredo_enabled=True,
        wiredo_asset_types=("code",),
        special_notes="Example profile for security-focused apps with guard pipelines.",
    ),
    "multimodal-app": ProductProfile(
        name="Multimodal App",
        cognitive_levels=("c0", "c1", "c2", "c4"),
        action_phases=("a2", "a3", "a5"),
        wiredo_enabled=True,
        wiredo_asset_types=("code", "image", "video", "audio"),
        special_notes="Example profile for apps with multimedia assets.",
    ),
    "infinite-agent": ProductProfile(
        name="Infinite Agent",
        cognitive_levels=("c0", "c2", "c3", "c4", "c5"),
        action_phases=("a0", "a1", "a2", "a3", "a4", "a5"),
        wiredo_enabled=True,
        wiredo_asset_types=("code", "document"),
        special_notes="Cross-agent orchestration. Aggregates WIREDO across agents.",
    ),
    "default": ProductProfile(
        name="Default",
        cognitive_levels=("c0", "c1", "c2", "c4"),
        action_phases=("a2", "a3", "a5"),
        wiredo_enabled=True,
        wiredo_asset_types=("code",),
        special_notes="Minimal viable cognitive stack for any product.",
    ),
}


def get_profile(product: str = "") -> ProductProfile:
    """Get the cognitive profile for a product.

    Falls back to 'default' if product not found.
    Auto-detects from CLAUDE_PROJECT_DIR if product not specified.
    """
    if product:
        return PROFILES.get(product.lower(), PROFILES["default"])

    # Auto-detect: not implemented yet, return default
    return PROFILES["default"]
