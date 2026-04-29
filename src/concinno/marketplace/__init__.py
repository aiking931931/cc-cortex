"""concinno.marketplace — GUI Skill Marketplace substrate.

This package surfaces every ``concinno-skills-*`` distribution
discovered via :mod:`importlib.metadata`, regardless of whether the
package ships a ``SKILL.md`` directory. It powers the GUI Marketplace
tab (browse / install / uninstall) and the ``/api/skills/marketplace``
REST surface.

Bug 4b context (W4 carryover): the GUI ``Skills`` tab only walked
``~/.claude/skills/`` + ``cwd/.claude/skills/`` + the ``concinno.skills``
entry-points group. It silently missed any ``concinno-skills-*`` PyPI
sub-pkg that registers ``concinno.hooks.*`` entry-points but does not
ship a ``SKILL.md``. This package is the discovery substrate that
fixes the gap; the marketplace tab is the surface.

Public re-exports (lazy):

* :class:`MarketplaceRow` — wire shape returned by ``/api/skills/marketplace``
* :func:`list_installed_concinno_skills` — local ``importlib.metadata`` walk
* :func:`list_available_pypi` — PyPI JSON fetch + 1-hour disk cache
* :func:`install_pkg`, :func:`uninstall_pkg` — pip subprocess wrappers
* :func:`validate_dist_frontmatter` — delegates to HP1 validator

The package keeps imports cheap: heavyweight subprocess / urllib /
SQLite work happens only when the corresponding API route is invoked.
"""
from __future__ import annotations

from concinno.marketplace.discovery import (
    MarketplaceRow,
    list_available_pypi,
    list_installed_concinno_skills,
)
from concinno.marketplace.installer import (
    InstallError,
    install_pkg,
    uninstall_pkg,
)
from concinno.marketplace.pypi_client import (
    PyPIClient,
    PyPIUnreachableError,
)
from concinno.marketplace.validator import validate_dist_frontmatter

__all__ = [
    "MarketplaceRow",
    "PyPIClient",
    "PyPIUnreachableError",
    "InstallError",
    "list_installed_concinno_skills",
    "list_available_pypi",
    "install_pkg",
    "uninstall_pkg",
    "validate_dist_frontmatter",
]
