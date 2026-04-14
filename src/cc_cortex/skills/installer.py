"""
cc_cortex.skills.installer — Install skill templates into .claude/skills/.

Usage:
    python -m cc_cortex.skills.installer [--target ~/.claude/skills]
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

SKILLS_DIR = Path(__file__).parent
SKILL_FILES = ["guard.md", "schedule.md", "hooks.md"]


def install_skills(target_dir: str | None = None) -> list[str]:
    """Copy skill templates to target directory.

    Args:
        target_dir: Destination (default: ~/.claude/skills/).

    Returns:
        List of installed skill paths.
    """
    if not target_dir:
        target_dir = os.path.join(os.path.expanduser("~"), ".claude", "skills")

    installed = []
    for skill_file in SKILL_FILES:
        src = SKILLS_DIR / skill_file
        if not src.exists():
            continue

        skill_name = skill_file.replace(".md", "")
        dest_dir = os.path.join(target_dir, f"cortex-{skill_name}")
        os.makedirs(dest_dir, exist_ok=True)

        dest = os.path.join(dest_dir, "SKILL.md")
        shutil.copy2(str(src), dest)
        installed.append(dest)

    return installed


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Install cc-cortex skill templates")
    parser.add_argument("--target", help="Target directory (default: ~/.claude/skills)")
    args = parser.parse_args()

    installed = install_skills(args.target)
    for path in installed:
        print(f"Installed: {path}")
    print(f"Total: {len(installed)} skills")


if __name__ == "__main__":
    main()
