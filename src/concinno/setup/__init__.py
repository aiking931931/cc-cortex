"""concinno.setup — interactive profile recommender.

@module concinno.setup
@responsibility Tailor a starting Concinno feature configuration for the
    five canonical Claude Code user types (senior / junior / benchmark /
    production / researcher) so newcomers do not have to learn 30+
    feature flags before their first session.
@exports Profile, PROFILES, recommend, apply
"""

from __future__ import annotations

from concinno.setup.recommender import PROFILES, Profile, apply, recommend

__all__ = ["PROFILES", "Profile", "apply", "recommend"]
