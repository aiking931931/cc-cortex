"""concinno.auto_update -- Tier 1 / Tier 2 auto-update infrastructure.

Tier 1 (this module's :mod:`tier1_registry`) — registry refresh only,
no ``pip install``. Triggered from SessionStart hook to discover
newly-installed ``concinno-skills-*`` / ``concinno-features-*``
packages and atomic-write the registry caches under ``~/.concinno/``.

Tier 2 (separate module, lands in task #8) — opt-in ``concinno
self-update`` CLI that drives ``pip install --upgrade``.

Design source: ``_AI_BRAIN/05_Planning/sancio-gui-extension-auto-update-design-2026-04-25.md``
Commander amendments: R#5 (digest spec / race lock / 300ms budget /
500-skill stress) + R#10 (preserve user ``enabled`` state on
read-modify-write) per
``_AI_BRAIN/05_Planning/sancio-gui-extension-commander-verdict-2026-04-25.md``.
"""
from __future__ import annotations

from concinno.auto_update.tier1_registry import (
    RegistryCache,
    RegistryDigest,
    RegistryRefreshResult,
    refresh_tier1_registry,
)
from concinno.auto_update.tier2_self_update import (
    SelfUpdateResult,
    detect_pip_install_method,
    get_latest_pypi_version,
    is_concinno_in_use,
    self_update,
)

__all__ = [
    "RegistryCache",
    "RegistryDigest",
    "RegistryRefreshResult",
    "SelfUpdateResult",
    "detect_pip_install_method",
    "get_latest_pypi_version",
    "is_concinno_in_use",
    "refresh_tier1_registry",
    "self_update",
]
