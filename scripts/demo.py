#!/usr/bin/env python3
"""Interactive demo for CC Cortex features.

Simulates real-world scenarios with typewriter effects to showcase
multi-instance coordination, injection detection, learning loops,
token warnings, and health checks.

Usage:
    python scripts/demo.py           # Run all scenarios
    python scripts/demo.py --scene 1 # Run specific scenario (1-5)
    python scripts/demo.py --fast    # Skip typewriter delays
    python scripts/demo.py --plain   # No color output
"""

from __future__ import annotations

import argparse
import os
import sys
import time

# Ensure package is importable when running from repo root
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent / "src"))

from cc_cortex.ui.colors import (
    BOLD,
    BRIGHT_BLACK,
    BRIGHT_CYAN,
    BRIGHT_GREEN,
    BRIGHT_RED,
    BRIGHT_YELLOW,
    DIM,
    c,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_fast_mode = False


def _delay(seconds: float) -> None:
    """Sleep unless in fast mode."""
    if not _fast_mode:
        time.sleep(seconds)


def _type(text: str, delay: float = 0.02) -> None:
    """Print text with typewriter effect."""
    if _fast_mode:
        print(text)
        return
    for ch in text:
        sys.stdout.write(ch)
        sys.stdout.flush()
        time.sleep(delay)
    print()


def _print_header(title: str) -> None:
    """Print a scene header."""
    print()
    print(c("=" * 60, DIM))
    print(c(f"  {title}", BOLD, BRIGHT_CYAN))
    print(c("=" * 60, DIM))
    print()
    _delay(0.5)


def _print_separator() -> None:
    """Print a thin separator."""
    print()
    print(c("-" * 40, DIM))
    print()


# ---------------------------------------------------------------------------
# Scenes
# ---------------------------------------------------------------------------


def scene_multi_instance() -> None:
    """Scene 1: Multi-instance file lock conflict."""
    _print_header("Scene 1: Multi-Instance Conflict Prevention")

    _type(c("[Session A]", BOLD, BRIGHT_GREEN) + " Writing to src/auth.ts...")
    _delay(0.3)

    _type(c("[Session A]", BOLD, BRIGHT_GREEN) + c(" Lock acquired", DIM))
    _delay(0.8)

    _type(c("[Session B]", BOLD, BRIGHT_YELLOW) + " Attempting to write src/auth.ts...")
    _delay(0.5)

    print()
    _type(
        c("BLOCKED:", BOLD, BRIGHT_RED)
        + " src/auth.ts is locked by Session A (since 14:21)"
    )
    _type(
        c("   -> Suggestion:", DIM)
        + " Work on a different file, or wait for Session A to finish"
    )
    _delay(0.5)

    print()
    _type(c("[Session A]", BOLD, BRIGHT_GREEN) + " Write complete. Lock released.")
    _delay(0.3)
    _type(c("[Session B]", BOLD, BRIGHT_YELLOW) + " Retrying... " + c("Success!", BRIGHT_GREEN))


def scene_injection() -> None:
    """Scene 2: Prompt injection detection."""
    _print_header("Scene 2: Prompt Injection Detection")

    _type(c("Scanning tool input...", DIM))
    _delay(0.3)

    # Simulate scanning animation
    patterns = ["checking context boundaries", "analyzing instruction flow", "MATCH FOUND"]
    for i, pat in enumerate(patterns):
        prefix = c(f"  [{i + 1}/3]", BRIGHT_BLACK)
        if i < 2:
            _type(f"{prefix} {c(pat, DIM)}")
        else:
            _type(f"{prefix} {c(pat, BOLD, BRIGHT_RED)}")
        _delay(0.4)

    print()
    _type(c("ALERT:", BOLD, BRIGHT_RED) + " Prompt injection detected!")
    _type(c("   Pattern:", DIM) + ' "ignore previous instructions"')
    _type(c("   Confidence:", DIM) + c(" 98%", BRIGHT_RED))
    _type(c("   Action:", DIM) + c(" BLOCKED", BOLD, BRIGHT_RED))
    print()
    _type(c("   Tool call rejected. Session continues safely.", BRIGHT_GREEN))


def scene_learning() -> None:
    """Scene 3: Auto-learning loop."""
    _print_header("Scene 3: Knowledge Auto-Learning Loop")

    _type(c("Scanning session transcript...", DIM))
    _delay(0.5)

    corrections = [
        "Don't use string concatenation for paths",
        "Don't use string concatenation for paths",
        "Don't use string concatenation for paths",
    ]

    for i, correction in enumerate(corrections):
        _type(f"  {c(f'Correction #{i + 1}:', DIM)} \"{correction}\"")
        _delay(0.3)

    print()
    _type(f"   Pattern count: {c('3', BOLD, BRIGHT_YELLOW)} (threshold reached)")
    _delay(0.5)

    _type(c("   Promoted to knowledge base", BOLD, BRIGHT_GREEN))
    print()
    _type(c("   Rule:", DIM) + ' "Use Path objects instead of string concatenation"')
    _type(c("   Applies to:", DIM) + " *.py, *.ts")
    _type(c("   Auto-enforced:", DIM) + c(" Yes", BRIGHT_GREEN) + " (next session onward)")


def scene_token_warning() -> None:
    """Scene 4: Token budget warning."""
    _print_header("Scene 4: Token Budget Guardian")

    tiers = [
        (50, "TIER 1", BRIGHT_YELLOW, "Consider wrapping up current task"),
        (70, "TIER 2", BRIGHT_YELLOW, "Start writing handoff notes"),
        (85, "TIER 3", BRIGHT_RED, "Wrap up and write handoff NOW"),
    ]

    total = 200_000
    for pct, tier_name, color, msg in tiers:
        used = int(total * pct / 100)
        remaining = total - used

        bar_width = 30
        filled = int(bar_width * pct / 100)
        empty = bar_width - filled
        bar = c("[" + "#" * filled, color) + c("." * empty + "]", DIM)

        _type(f"  Token usage: {c(f'{used:,}', color)} / {c(f'{total:,}', DIM)} ({pct}%)")
        _type(f"  {bar}")
        _type(f"  {c(tier_name, BOLD, color)}: {msg}")
        _type(f"  Estimated remaining: ~{remaining:,} tokens")
        _delay(0.8)
        if pct < 85:
            _print_separator()


def scene_doctor() -> None:
    """Scene 5: Health check (cc-cortex doctor)."""
    _print_header("Scene 5: cc-cortex doctor")

    _type(c("Running health check...", DIM))
    _delay(0.3)

    checks = [
        ("Hooks installed (4/4)", True, None),
        ("Config valid", True, None),
        ("Knowledge base accessible (47 entries)", True, None),
        ("No zombie sessions", True, None),
        ("All enabled modules responding", True, None),
        ("Sentinel latency: 1.2ms (target: <2ms)", True, None),
        ("Token budget configured", True, None),
    ]

    for label, ok, warning in checks:
        _delay(0.2)
        if ok and not warning:
            _type(f"  {c('OK', BRIGHT_GREEN)} {label}")
        elif warning:
            _type(f"  {c('WARN', BRIGHT_YELLOW)} {label}")
        else:
            _type(f"  {c('FAIL', BRIGHT_RED)} {label}")

    print()
    _type(c("  Overall: ", BOLD) + c("HEALTHY", BOLD, BRIGHT_GREEN))
    print()
    _type(c("  All systems operational. No action required.", DIM))


# ---------------------------------------------------------------------------
# Scene registry
# ---------------------------------------------------------------------------

SCENES = {
    1: ("Multi-Instance Conflict Prevention", scene_multi_instance),
    2: ("Prompt Injection Detection", scene_injection),
    3: ("Knowledge Auto-Learning Loop", scene_learning),
    4: ("Token Budget Guardian", scene_token_warning),
    5: ("Health Check (doctor)", scene_doctor),
}


def run_all() -> None:
    """Run all demo scenes."""
    print()
    print(c("CC CORTEX", BOLD, BRIGHT_CYAN) + c(" — Interactive Demo", DIM))
    print(c(f"  {len(SCENES)} scenarios demonstrating production-grade hooks", DIM))
    _delay(1.0)

    for _num, (_title, func) in SCENES.items():
        func()
        _delay(1.0)

    print()
    print(c("=" * 60, DIM))
    print(c("  Demo complete!", BOLD, BRIGHT_GREEN))
    print(c("  Install: ", DIM) + c("pip install cc-cortex", BOLD))
    print(c("  Setup:   ", DIM) + c("cc-cortex init", BOLD))
    print(c("  Docs:    ", DIM) + c("https://github.com/anthropics-community/cc-cortex", BOLD))
    print(c("=" * 60, DIM))
    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    """Entry point for demo script."""
    global _fast_mode

    parser = argparse.ArgumentParser(description="CC Cortex interactive demo")
    parser.add_argument("--scene", type=int, choices=list(SCENES.keys()), help="Run specific scene")
    parser.add_argument("--fast", action="store_true", help="Skip typewriter delays")
    parser.add_argument("--plain", action="store_true", help="Disable colors")
    args = parser.parse_args()

    if args.plain:
        os.environ["NO_COLOR"] = "1"
        from cc_cortex.ui.colors import reset_color_cache

        reset_color_cache()

    _fast_mode = args.fast

    if args.scene:
        _title, func = SCENES[args.scene]
        func()
    else:
        run_all()


if __name__ == "__main__":
    main()
