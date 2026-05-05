"""concinno.memory_relief.tray — system-tray right-click cleanup app.

@module memory_relief.tray
@responsibility Lightweight ``pystray``-backed tray icon that surfaces
    the cleanup tiers + a live RAM% colour-coded icon. The agent does
    not interact with this module — it exists for the human operator.
    Auto-trigger lives in ``process_guard.guard``; the tray is purely
    manual / opt-in.

@dependencies pystray + Pillow (lazy-imported, declared under the
    ``[memory-relief-tray]`` optional extras). On a base install the
    module is importable but :func:`main` raises a friendly hint.

Design choices justified by the red-team review:
* ``tray_enabled`` defaults to **False** (FEATURE_META) — opting in is
  explicit so a no-config Concinno install never adds a tray icon
  surprise.
* No auto-elevation / UAC prompt. Admin-required tiers are visible in
  the menu but skipped with ``skip_reason=needs_admin`` when the
  process token lacks the privilege; the menu label shows "(admin)" so
  the user knows why.
* Icon colour follows ``snapshot.used_percent``: green <70, amber
  70-85, red ≥85. Same scale the user already groks from Task Manager.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
from typing import TYPE_CHECKING

from . import core, engine

if TYPE_CHECKING:  # imported only for type hints — no runtime dep cost
    import pystray  # noqa: F401

logger = logging.getLogger("concinno.memory_relief.tray")

#: Refresh interval for the icon colour + tooltip (seconds). 5s strikes a
#: balance between snappy UI and CPU overhead from repeated
#: GetPerformanceInfo calls.
_REFRESH_INTERVAL_SECONDS = 5

_COLOUR_GREEN = (76, 175, 80)
_COLOUR_AMBER = (255, 152, 0)
_COLOUR_RED = (244, 67, 54)
_COLOUR_GREY = (158, 158, 158)


def _import_gui_deps():
    """Resolve pystray + Pillow lazily so the module imports cleanly
    on bare installs. Raises a friendly :class:`ImportError` listing
    the extras command when the dependency is missing."""
    try:
        import pystray  # noqa: PLC0415 — intentionally late
        from PIL import Image, ImageDraw  # noqa: PLC0415
    except ImportError as exc:
        raise ImportError(
            "memory_relief tray requires pystray + Pillow. "
            "Install with: pip install 'concinno[memory-relief-tray]'"
        ) from exc
    return pystray, Image, ImageDraw


# ── Icon rendering ────────────────────────────────────────────────────


def _render_icon(used_percent: float, draw_module, image_module):
    """Build a 64×64 PNG with the colour band and the percentage in
    white digits. Cheap enough to call on every refresh tick."""
    if used_percent >= 85:
        colour = _COLOUR_RED
    elif used_percent >= 70:
        colour = _COLOUR_AMBER
    elif used_percent > 0:
        colour = _COLOUR_GREEN
    else:
        colour = _COLOUR_GREY
    img = image_module.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = draw_module.Draw(img)
    draw.rounded_rectangle((4, 4, 60, 60), radius=10, fill=colour)
    label = f"{int(round(used_percent))}"
    # Default Pillow font is 11px; centre by hand to dodge truetype dep.
    text_x = 32 - (len(label) * 5)
    draw.text((text_x, 22), label, fill=(255, 255, 255))
    return img


def _format_tooltip(snapshot: core.MemorySnapshot) -> str:
    """One-line tray tooltip — Windows truncates at 127 chars."""
    return (
        f"RAM {snapshot.used_percent:.0f}% | "
        f"Standby {snapshot.standby_bytes // (1024 * 1024)} MB | "
        f"Commit {snapshot.commit_percent:.0f}%"
    )


# ── Cleanup actions ───────────────────────────────────────────────────


def _show_cleanup_summary(report: engine.CleanupReport) -> None:
    """Notify the user of the result. Tries Concinno's existing toast
    notifier first (consistent with the rest of the framework), falls
    back to stderr so headless / Avast-quarantined hosts still see it."""
    title = (
        f"Memory relief: {report.reclaimed_mb} MB reclaimed"
        if not report.dry_run
        else f"Memory relief preview: would reclaim ~{report.reclaimed_mb} MB"
    )
    body_lines = [
        f"Mode: {report.mode.value}",
        f"Stages: {len(report.stages)}",
        "Top trim: " + ", ".join(
            f"{t.name}({t.freed_bytes // (1024 * 1024)} MB)"
            for t in report.process_trims[:3]
        ) if report.process_trims else "no per-process trim",
    ]
    body = "\n".join(body_lines)

    try:
        from concinno.core.notify import show_toast

        show_toast(title=title, body=body)
        return
    except Exception as exc:  # noqa: BLE001 — fall through to stderr
        logger.debug("toast notifier unavailable (%s); falling back", exc)
    sys.stderr.write(f"[memory_relief] {title}\n{body}\n")


def _run_and_notify(mode: engine.CleanupMode, *, dry_run: bool) -> None:
    """Run cleanup off the tray's UI thread to keep the icon responsive.
    Pystray dispatches menu callbacks on its own worker so we are
    already off the GUI loop, but the cleanup itself can take a few
    hundred ms — wrap in a thread anyway so the menu redraws instantly."""

    def _worker() -> None:
        report = engine.run_cleanup(mode=mode, dry_run=dry_run)
        _show_cleanup_summary(report)

    threading.Thread(target=_worker, daemon=True).start()


def _show_status_console(_icon, _item) -> None:
    """Menu-callback: dump the current snapshot to stderr as JSON.
    Cheap diagnostic — avoids spawning a popup window which would need
    tk and bloat the install."""
    try:
        snapshot = core.get_memory_snapshot()
    except OSError as exc:
        sys.stderr.write(f"[memory_relief] snapshot failed: {exc}\n")
        return
    sys.stderr.write(
        "[memory_relief] snapshot:\n"
        + json.dumps(snapshot.as_dict(), indent=2)
        + "\n"
    )


def _build_menu(pystray_module):
    """Construct the right-click menu. Each item is a thin wrapper
    around :func:`_run_and_notify` so the dispatch path is identical
    across tiers."""
    Menu = pystray_module.Menu  # noqa: N806 — match upstream casing
    Item = pystray_module.MenuItem  # noqa: N806

    def _make(mode: engine.CleanupMode, *, dry_run: bool):
        return lambda _icon, _item: _run_and_notify(mode, dry_run=dry_run)

    return Menu(
        Item("Preview (dry-run)", _make(engine.CleanupMode.SAFE, dry_run=True)),
        Item("Clean — safe (no admin)", _make(engine.CleanupMode.SAFE, dry_run=False)),
        Menu.SEPARATOR,
        Item(
            "Standby purge — low priority (admin)",
            _make(engine.CleanupMode.STANDBY, dry_run=False),
        ),
        Item(
            "Aggressive — full standby + cache (admin)",
            _make(engine.CleanupMode.AGGRESSIVE, dry_run=False),
        ),
        Item(
            "Destructive — flush modified list (admin, IO)",
            _make(engine.CleanupMode.DESTRUCTIVE, dry_run=False),
        ),
        Menu.SEPARATOR,
        Item("Show status", _show_status_console),
        Item("Quit", lambda icon, _item: icon.stop()),
    )


# ── Refresh loop ──────────────────────────────────────────────────────


def _start_refresh_loop(icon, image_module, draw_module) -> None:
    """Background thread that re-renders the icon + tooltip every
    :data:`_REFRESH_INTERVAL_SECONDS` seconds. Daemon so it dies with
    the process when the user clicks Quit."""

    def _loop():
        while True:
            try:
                snapshot = core.get_memory_snapshot()
                icon.icon = _render_icon(
                    snapshot.used_percent, draw_module, image_module,
                )
                icon.title = _format_tooltip(snapshot)
            except Exception as exc:  # noqa: BLE001 — never crash UI loop
                logger.debug("refresh tick failed: %s", exc)
            time.sleep(_REFRESH_INTERVAL_SECONDS)

    threading.Thread(target=_loop, daemon=True).start()


# ── Entry point ───────────────────────────────────────────────────────


def main() -> int:
    """Console-script entry — registered as ``concinno-mem-tray`` in
    ``pyproject.toml``. Returns the conventional process exit code."""
    if sys.platform != "win32":
        sys.stderr.write(
            "concinno-mem-tray is Windows-only. "
            "Snapshot APIs degrade gracefully on Linux / macOS but the "
            "tray icon needs the Win32 message loop.\n"
        )
        return 1

    pystray, Image, ImageDraw = _import_gui_deps()

    # Initial snapshot for the first paint — refresh loop replaces it
    # within _REFRESH_INTERVAL_SECONDS but a blank icon flicker is ugly.
    try:
        snapshot = core.get_memory_snapshot()
        initial_pct = snapshot.used_percent
        initial_tooltip = _format_tooltip(snapshot)
    except OSError as exc:
        logger.warning("initial snapshot failed: %s", exc)
        initial_pct = 0.0
        initial_tooltip = "memory_relief (snapshot unavailable)"

    icon = pystray.Icon(
        name="concinno-memory-relief",
        icon=_render_icon(initial_pct, ImageDraw, Image),
        title=initial_tooltip,
        menu=_build_menu(pystray),
    )

    _start_refresh_loop(icon, Image, ImageDraw)
    icon.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
