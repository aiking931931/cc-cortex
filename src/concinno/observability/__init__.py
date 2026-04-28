"""concinno.observability — Observability surface for Concinno runtime.

Sub-packages:
    token_audit — Per-session token overhead audit (skills / MCP / sub-agents
                  / system floor) with ZIQ FTRL archive advisor for stale
                  skills. Default-OFF per 4.0.0 opt-in policy; opt in via
                  ``concinno features set token_audit_autopilot enabled true``.

This package is deliberately thin: each sub-package is independently
opt-in and shares no state, so the cost of the package being imported
on every session start is bounded by what the user has actually opted
into. ``observability`` itself does not import its sub-packages eagerly.
"""

from __future__ import annotations

__all__: list[str] = []
