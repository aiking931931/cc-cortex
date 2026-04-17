"""Example: LLM-as-Judge via prompt_hooks (1.4.0 C6).

Demonstrates the `concinno.prompt_hooks` module by generating a
Claude Code settings.json fragment for the three shipped judges —
HallucinationJudge, ExcuseScannerJudge, CodeQualityJudge — and
showing the install / list / uninstall round-trip.

CCC never calls an LLM itself. The judges run inside Claude Code's
built-in `type: "prompt"` hook runtime (Haiku 4.5 by default). This
example writes settings into a temp directory so it cannot touch
your real config.

Run::

    python examples/prompt_hooks_example.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from concinno import (
    ALL_JUDGES,
    CODE_QUALITY_JUDGE,
    EXCUSE_SCANNER_JUDGE,
    HALLUCINATION_JUDGE,
    build_hook_config,
    install_prompt_hooks,
    list_installed_judges,
    uninstall_prompt_hooks,
)


def _header(title: str) -> None:
    print(f"\n── {title} ──")


def main() -> None:
    print("concinno 1.4.0 — prompt_hooks (LLM-as-Judge) example")

    _header("1. Shipped judges")
    for judge in ALL_JUDGES:
        print(f"  {judge.name}")
        print(f"    event    : {judge.event}")
        print(f"    matcher  : {judge.matcher or '(none)'}")
        print(f"    model    : {judge.model}")
        print(f"    purpose  : {judge.description}")

    _header("2. Hook config fragment (for settings.json)")
    cfg = build_hook_config([HALLUCINATION_JUDGE])
    print(json.dumps({"hooks": cfg}, indent=2, ensure_ascii=False))

    _header("3. Install into a sandbox settings.json")
    with tempfile.TemporaryDirectory() as tmp:
        settings_path = Path(tmp) / "settings.json"
        install_prompt_hooks(settings_path)
        print(f"  wrote {settings_path}")

        installed = list_installed_judges(settings_path)
        print(f"  installed judges: {installed}")

        _header("4. Idempotent reinstall (no duplication)")
        install_prompt_hooks(settings_path)
        disk = json.loads(settings_path.read_text(encoding="utf-8"))
        post = disk["hooks"]["PostToolUse"][0]["hooks"]
        print(f"  PostToolUse/Write|Edit spec count: {len(post)} (expected 2)")

        _header("5. Uninstall only the excuse scanner")
        uninstall_prompt_hooks(settings_path, judges=[EXCUSE_SCANNER_JUDGE])
        remaining = list_installed_judges(settings_path)
        print(f"  remaining: {remaining}")

        _header("6. Full uninstall — settings restored to empty")
        uninstall_prompt_hooks(
            settings_path,
            judges=[HALLUCINATION_JUDGE, CODE_QUALITY_JUDGE],
        )
        disk = json.loads(settings_path.read_text(encoding="utf-8"))
        print(f"  final settings.json: {json.dumps(disk)}")


if __name__ == "__main__":
    main()
