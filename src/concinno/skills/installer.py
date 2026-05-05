"""
concinno.skills.installer — Install skill templates into .claude/skills/.

Usage:
    python -m concinno.skills.installer [--target ~/.claude/skills]
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

SKILLS_DIR = Path(__file__).parent

# Legacy single-file Skills (wrapped into cortex-<name>/SKILL.md).
# Kept at the skills-root level for backwards compat.
SKILL_FILES = ["guard.md", "schedule.md", "hooks.md"]


def _ensure_junction(link_path: str, target_path: str) -> bool:
    """Create a directory junction / symlink at ``link_path`` → ``target_path``.

    Cross-platform: uses ``mklink /J`` on Windows (no admin needed) and
    ``os.symlink`` elsewhere. Returns True on success, False if the
    link already points at the right target (no-op) or a real directory
    is in the way (refuses to clobber user data).
    """
    # No-op if an existing link already points at the right target.
    if os.path.islink(link_path):
        try:
            if os.path.realpath(link_path) == os.path.realpath(target_path):
                return False
        except OSError:
            pass
        os.unlink(link_path)
    elif os.path.isdir(link_path):
        # Real directory — don't clobber user data. Caller warns.
        return False

    if sys.platform == "win32":
        # Directory junction: no admin needed, unlike symlinks.
        subprocess.run(
            ["cmd.exe", "/c", "mklink", "/J", link_path, target_path],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        os.symlink(target_path, link_path, target_is_directory=True)
    return True


def install_skills(target_dir: str | None = None) -> list[str]:
    """Copy skill templates / Skill directories to target directory.

    Layout created in ``target_dir``::

        <target>/
            public/
                <skill-name>/          # actual Skill files live here
                    SKILL.md
                    ...
            private/                   # empty — user's personal Skills
            <skill-name>                → junction → public/<skill-name>

    The top-level junctions keep Claude Code's flat-scan auto-discovery
    happy while the public/private split enforces the pip-share contract:
    anything under ``public/`` is what ships to other consumers via
    ``concinno[windows-full]`` — no per-skill "should this be pip'd?"
    decision needed.

    Args:
        target_dir: Destination (default: ``~/.claude/skills/``).

    Returns:
        List of installed skill paths (file paths for legacy single-file
        Skills, directory paths for directory-bundled Skills).
    """
    if not target_dir:
        target_dir = os.path.join(os.path.expanduser("~"), ".claude", "skills")

    public_dir = os.path.join(target_dir, "public")
    private_dir = os.path.join(target_dir, "private")
    os.makedirs(public_dir, exist_ok=True)
    os.makedirs(private_dir, exist_ok=True)

    installed: list[str] = []

    # Legacy .md-only Skills — wrapped into cortex-<name>/SKILL.md
    # under public/ (they are universal by definition).
    for skill_file in SKILL_FILES:
        src = SKILLS_DIR / skill_file
        if not src.exists():
            continue
        skill_name = skill_file.replace(".md", "")
        dest_dir = os.path.join(public_dir, f"cortex-{skill_name}")
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, "SKILL.md")
        shutil.copy2(str(src), dest)
        installed.append(dest)
        # Top-level junction for CC flat-scan discovery.
        junction = os.path.join(target_dir, f"cortex-{skill_name}")
        try:
            _ensure_junction(junction, dest_dir)
        except (OSError, subprocess.CalledProcessError):
            pass  # CC still works via public/ if flat scan supports it

    # Directory-bundled Skills — copied whole from the bundled public/
    # tree (src/concinno/skills/public/<name>/) into the consumer's
    # <target>/public/<name>/, then junctioned from the skills root.
    #
    # F1 (2.7.1): Previously this called ``shutil.rmtree(dest_dir)``
    # when a destination existed, which on POSIX follows symlinks and
    # on Windows follows junctions. If a user had manually symlinked
    # ``<target>/public/<name>`` to their personal workspace repo
    # (e.g. for live Skill development) the rmtree would wipe the
    # target. We now unlink links and only recurse into real dirs.
    src_public = SKILLS_DIR / "public"
    if src_public.is_dir():
        for entry in sorted(src_public.iterdir()):
            if not entry.is_dir():
                continue
            dest_dir = os.path.join(public_dir, entry.name)
            if os.path.islink(dest_dir) or _is_windows_junction(dest_dir):
                # Unlink the link itself. os.unlink handles symlinks
                # on POSIX and Windows (NTFS junction) alike.
                try:
                    os.unlink(dest_dir)
                except OSError:
                    # Best-effort: skip this skill rather than clobber
                    # whatever the link resolves to.
                    continue
            elif os.path.isdir(dest_dir):
                shutil.rmtree(dest_dir)
            shutil.copytree(str(entry), dest_dir)
            installed.append(dest_dir)
            junction = os.path.join(target_dir, entry.name)
            try:
                _ensure_junction(junction, dest_dir)
            except (OSError, subprocess.CalledProcessError) as exc:
                # F5 (2.7.1): surface the failure instead of silent-pass.
                sys.stderr.write(
                    f"warning: failed to create junction at "
                    f"{junction!r} → {dest_dir!r}: {exc}\n",
                )

    return installed


def _is_windows_junction(path: str) -> bool:
    """Return True if ``path`` is a Windows NTFS directory junction.

    Junctions are reparse points that behave like directory symlinks
    but are NOT detected by :func:`os.path.islink` on Windows Python
    (see https://bugs.python.org/issue37834). We probe via
    :func:`os.lstat` for the reparse-point attribute on Windows,
    returning False everywhere else.
    """
    if sys.platform != "win32":
        return False
    try:
        st = os.lstat(path)
    except (OSError, ValueError):
        return False
    # FILE_ATTRIBUTE_REPARSE_POINT = 0x400 on Windows.
    return bool(getattr(st, "st_file_attributes", 0) & 0x400)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Install concinno skill templates")
    parser.add_argument("--target", help="Target directory (default: ~/.claude/skills)")
    args = parser.parse_args()

    installed = install_skills(args.target)
    for path in installed:
        print(f"Installed: {path}")
    print(f"Total: {len(installed)} skills")


if __name__ == "__main__":
    main()
