#!/usr/bin/env python3
"""Generate text-based screenshots of CC Cortex demo output.

Captures terminal output from demo scenes and the status dashboard,
saving them as plain text files in docs/screenshots/.

These files can later be converted to SVG using tools like:
- termsvg (https://github.com/MrMarble/termsvg)
- terminal-to-svg
- carbon.now.sh (paste text)

Usage:
    python scripts/generate_screenshots.py
"""

from __future__ import annotations

import io
import os
import sys
from contextlib import redirect_stdout
from pathlib import Path

# Ensure package is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# Force color output for screenshots (even if not a TTY)
os.environ["FORCE_COLOR"] = "1"
os.environ.pop("NO_COLOR", None)

from cc_cortex.ui.colors import reset_color_cache

reset_color_cache()

SCREENSHOTS_DIR = Path(__file__).resolve().parent.parent / "docs" / "screenshots"


def _capture(func: object, *args: object, **kwargs: object) -> str:
    """Capture stdout from a function call."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        func(*args, **kwargs)  # type: ignore[operator]
    return buf.getvalue()


def _save(name: str, content: str) -> Path:
    """Save content to a screenshot file."""
    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    path = SCREENSHOTS_DIR / f"{name}.txt"
    path.write_text(content, encoding="utf-8")
    return path


def main() -> None:
    """Generate all screenshots."""
    print(f"Generating screenshots to {SCREENSHOTS_DIR}/")
    print()

    # Import after path setup
    # Avoid circular; import demo scenes directly
    import importlib.util

    from cc_cortex.ui.dashboard import render_dashboard

    demo_path = Path(__file__).resolve().parent / "demo.py"
    spec = importlib.util.spec_from_file_location("demo", demo_path)
    assert spec and spec.loader
    demo_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo_mod)  # type: ignore[union-attr]

    # Set fast mode for screenshots (no delays)
    demo_mod._fast_mode = True  # type: ignore[attr-defined]

    # 1. Dashboard
    output = render_dashboard()
    path = _save("dashboard", output)
    print(f"  Saved: {path.name}")

    # 2. Individual demo scenes
    for num, (title, func) in demo_mod.SCENES.items():  # type: ignore[attr-defined]
        output = _capture(func)
        path = _save(f"scene_{num}_{title.lower().replace(' ', '_')}", output)
        print(f"  Saved: {path.name}")

    # 3. Full demo
    output = _capture(demo_mod.run_all)  # type: ignore[attr-defined]
    path = _save("demo_full", output)
    print(f"  Saved: {path.name}")

    print()
    total = len(list(SCREENSHOTS_DIR.glob("*.txt")))
    print(f"Done! {total} screenshot(s) in {SCREENSHOTS_DIR}/")


if __name__ == "__main__":
    main()
