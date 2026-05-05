# SPDX-FileCopyrightText: 2025 Nous Research
# SPDX-License-Identifier: MIT

"""Vendored Lyceum substrate primitives.

This subpackage contains a frozen copy of the Lyceum substrate modules
that Concinno's governance shims (``concinno.destruction_guard``,
``concinno.approval_mode``, ``concinno.security.ssrf_guard``) delegate
to. They were copied here in Concinno 5.2.0 to ship-without-Lyceum on
PyPI; the upstream import name ``lyceum`` is squatted on PyPI by an
unrelated educational package, so we cannot pull it as a runtime
dependency.

Do not import from this subpackage in user code. Use the public
``concinno.destruction_guard`` / ``concinno.approval_mode`` /
``concinno.security.ssrf_guard`` APIs — they re-export the symbols.

Vendor source: ``projects/lyceum/lyceum/{sandbox,governance,security}/``
at AI King monorepo, Lyceum agent v0.1.0.
"""
from __future__ import annotations

__all__ = ["sandbox", "governance", "security"]
