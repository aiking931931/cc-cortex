"""BaseGuard adapter for delivery gate.

@module delivery.adapter
@responsibility DeliveryGuard stop-hook integration
@dependencies delivery.wiredo, concinno.guards.base
"""

from __future__ import annotations

from concinno.guards.base import BaseGuard, GuardCategory, GuardContext, GuardResult

from .wiredo import auto_delivery_gate


class DeliveryGuard(BaseGuard):
    """Stop hook: run CCC's own Delivery Gate at session end.

    Uses auto_delivery_gate() which walks the full D1→D6 pipeline:
    define_done → verify → report → retry/rollback → audit → gate_check.
    """

    name = "delivery_guard"
    category = GuardCategory.QUALITY

    def check(self, ctx: GuardContext) -> GuardResult | None:
        """No-op PreToolUse — this guard only acts on Stop."""
        return None  # Stop hook only

    def on_stop(self, ctx: GuardContext) -> GuardResult | None:
        """Run auto Delivery Gate at session end."""
        report = auto_delivery_gate(ctx.cache_dir, ctx.session_id)
        if report:
            return GuardResult.allow(context=report)
        return None
