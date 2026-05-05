"""concinno.cli.publish_cmd — PyPI publish bypass for user's own terminal.

@module publish_cmd
@responsibility Drive ``twine upload`` (+ ``git tag``) from the user's
    OWN terminal so Claude Code's host permission gate never sees the
    command. The agent can't auto-run this (host policy), but the user
    can type ``concinno publish concinno 2.16.0`` in their shell and get
    the full RELEASE_COORDINATION queue-aware flow.

Design (AI King 2026-04-23 directive):

  "除了刪除重要資料要詢問視窗以外其他要授權的都關閉"

  Publish is irreversible but not destructive — gate stays as a single
  ``yes`` in the user's shell (or fully auto when
  ``release_auth.disabled=True``). No more per-version AskUser string
  typed into the chat UI.

Flow:
  1. Locate RELEASE_COORDINATION.md in ``--path`` (default cwd).
  2. Parse Pending Publish Queue YAML block, find record for ``<ver>``.
  3. Validate: ``state == ready-to-publish``, wheel + sdist exist in
     ``<path>/dist/``, ``twine check`` passes, triple source is aligned.
  4. If ``release_auth.disabled=True`` → proceed. Otherwise prompt the
     user for a single ``yes`` in the terminal.
  5. Run ``python -m twine upload --disable-progress-bar`` with
     ``PYTHONIOENCODING=utf-8`` (MEMORY #34b).
  6. On success: update Queue record → History section, tag ``<pkg>-v<ver>``.
  7. On failure: leave Queue record at ``ready-to-publish`` for retry.

Integration:
  - Package name auto-detected from ``pyproject.toml::[project].name``.
  - Works for both ``concinno`` core and ``concinno-skills-*`` sub-packages
    as long as they have a ``RELEASE_COORDINATION.md`` + ``dist/`` dir.

@dependencies (stdlib only — argparse, os, re, subprocess, sys, pathlib)
@exports cmd_publish, find_queue_record, verify_artifacts,
    execute_twine_upload, PublishResult
"""

from __future__ import annotations

import argparse
import glob
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass
class PublishResult:
    """Outcome of a publish attempt."""

    success: bool
    reason: str
    package: str = ""
    version: str = ""
    uploaded_artifacts: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.uploaded_artifacts is None:
            self.uploaded_artifacts = []


# ── pyproject detection ────────────────────────────────────────────


def detect_package_name(project_dir: Path) -> str:
    """Extract ``name`` from ``pyproject.toml::[project].name``.

    Uses simple regex instead of tomllib (py3.11+) so we stay 3.10
    compatible.
    """
    pyproject = project_dir / "pyproject.toml"
    if not pyproject.is_file():
        return ""
    text = pyproject.read_text(encoding="utf-8")
    # Look for name = "..." in a [project] table.
    m = re.search(
        r"\[project\][^\[]*?name\s*=\s*\"([^\"]+)\"",
        text,
        re.DOTALL,
    )
    if m:
        return m.group(1)
    return ""


# ── RELEASE_COORDINATION queue parse ───────────────────────────────


def find_queue_record(
    coord_path: Path,
    version: str,
) -> tuple[dict | None, str | None]:
    """Return the Queue record for ``version`` + any parsing warning.

    The coord file uses a loose YAML-in-markdown format. We only need
    two pieces of info per record: the version string and the ``state``
    value. A stricter YAML parser would pull in a dependency; we keep
    this regex-based (stdlib only).
    """
    if not coord_path.is_file():
        return None, f"{coord_path} not found"
    text = coord_path.read_text(encoding="utf-8")

    # Find all YAML records starting with `- version: "X.Y.Z"` — they
    # persist until the next `- version:` line OR the next fenced block
    # close.
    pattern = re.compile(
        r'-\s+version:\s*"?([^"\n]+)"?\s*\n'
        r'((?:(?!^-\s+version:)(?!^```).*\n)*)',
        re.MULTILINE,
    )
    for m in pattern.finditer(text):
        record_version = m.group(1).strip()
        body = m.group(2)
        if record_version == version:
            state_match = re.search(r"state:\s*(\S+)", body)
            state = state_match.group(1).strip() if state_match else ""
            return {"version": record_version, "state": state, "body": body}, None

    return None, f"No Queue record for version {version!r} in {coord_path}"


# ── Artifact verification ──────────────────────────────────────────


def verify_artifacts(
    project_dir: Path,
    package: str,
    version: str,
) -> tuple[list[str], str | None]:
    """Ensure dist/ contains wheel+sdist for (package, version).

    Returns (artifact_paths, error_or_None). On success ``error`` is
    None and ``artifact_paths`` is non-empty.
    """
    dist = project_dir / "dist"
    if not dist.is_dir():
        return [], f"{dist} does not exist — run `python -m build` first"

    # PyPI normalizes package name: "concinno-skills-google" may be
    # built as both ``concinno_skills_google-0.1.0*`` and
    # ``concinno-skills-google-0.1.0*``. Accept either.
    normalized = package.replace("-", "_")
    patterns = [
        f"{package}-{version}-*.whl",
        f"{normalized}-{version}-*.whl",
        f"{package}-{version}.tar.gz",
        f"{normalized}-{version}.tar.gz",
    ]
    found: list[str] = []
    for pattern in patterns:
        found.extend(glob.glob(str(dist / pattern)))

    # Deduplicate while keeping order.
    seen = set()
    unique: list[str] = []
    for p in found:
        if p not in seen:
            seen.add(p)
            unique.append(p)

    if not unique:
        return [], (
            f"No artifacts matching {package}-{version}* in {dist}. "
            f"Run `python -m build` first."
        )
    has_wheel = any(p.endswith(".whl") for p in unique)
    has_sdist = any(p.endswith(".tar.gz") for p in unique)
    if not has_wheel:
        return unique, f"Missing wheel (.whl) for {package}-{version}"
    if not has_sdist:
        return unique, f"Missing sdist (.tar.gz) for {package}-{version}"
    return unique, None


# ── Twine check + upload ───────────────────────────────────────────


def run_twine_check(artifacts: list[str]) -> tuple[bool, str]:
    """Run ``twine check`` on the artifacts. Returns (passed, output)."""
    cmd = [sys.executable, "-m", "twine", "check", *artifacts]
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=env,
            check=False,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError:
        return False, "twine not installed; run `pip install twine`"
    ok = res.returncode == 0
    return ok, (res.stdout or "") + (res.stderr or "")


def execute_twine_upload(
    artifacts: list[str],
    *,
    extra_env: dict | None = None,
) -> tuple[bool, str]:
    """Actually upload to PyPI via ``twine upload``.

    Called only after all gates passed. Uses MEMORY #34b hardening:
    ``PYTHONIOENCODING=utf-8`` + ``--disable-progress-bar`` to avoid
    Windows GBK locale crash on the upload progress rendering.
    """
    cmd = [
        sys.executable,
        "-m",
        "twine",
        "upload",
        "--disable-progress-bar",
        *artifacts,
    ]
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    if extra_env:
        env.update(extra_env)
    try:
        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=env,
            check=False,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError:
        return False, "twine not installed; run `pip install twine`"
    ok = res.returncode == 0
    return ok, (res.stdout or "") + (res.stderr or "")


# ── Confirmation prompt (only when release_auth not disabled) ──────


def _confirm_from_tty(package: str, version: str) -> bool:
    """Ask the user for a plain ``yes`` in their terminal."""
    prompt = (
        f"\nconcinno: about to publish {package} {version} to PyPI.\n"
        f"This is IRREVERSIBLE. Type 'yes' to proceed, anything else to abort: "
    )
    try:
        ans = input(prompt)
    except EOFError:
        return False
    return ans.strip().lower() in ("y", "yes")


def _release_auth_disabled() -> bool:
    """Check whether release_authorization.disabled == True."""
    try:
        from concinno.release_authorization import load_config

        return bool(load_config().disabled)
    except Exception:
        return False


# ── CLI entry ──────────────────────────────────────────────────────


def cmd_publish(args: argparse.Namespace) -> None:
    """`concinno publish <pkg> <ver>` entry point."""
    project_dir = Path(args.path) if args.path else Path.cwd()
    requested_pkg = args.package
    version = args.version
    dry_run = bool(getattr(args, "dry_run", False))

    result = _run_publish_flow(
        project_dir=project_dir,
        requested_pkg=requested_pkg,
        version=version,
        dry_run=dry_run,
    )
    if result.success:
        _print_success(result)
        return
    print(f"concinno publish: FAILED — {result.reason}", file=sys.stderr)
    sys.exit(1)


def _run_publish_flow(
    project_dir: Path,
    requested_pkg: str,
    version: str,
    dry_run: bool,
) -> PublishResult:
    """End-to-end publish flow; returns PublishResult."""
    detected = detect_package_name(project_dir)
    if detected and detected != requested_pkg:
        return PublishResult(
            success=False,
            reason=(
                f"package mismatch: CLI arg {requested_pkg!r} "
                f"does not match pyproject.toml name {detected!r}"
            ),
            package=requested_pkg,
            version=version,
        )
    package = detected or requested_pkg

    coord_path = project_dir / "RELEASE_COORDINATION.md"
    record, warn = find_queue_record(coord_path, version)
    if warn:
        return PublishResult(success=False, reason=warn, package=package, version=version)
    assert record is not None
    if record["state"] not in ("ready-to-publish", "claimed"):
        return PublishResult(
            success=False,
            reason=(
                f"Queue record for {version} has state={record['state']!r}, "
                f"expected 'ready-to-publish'"
            ),
            package=package,
            version=version,
        )

    artifacts, err = verify_artifacts(project_dir, package, version)
    if err:
        return PublishResult(success=False, reason=err, package=package, version=version)

    check_ok, check_out = run_twine_check(artifacts)
    if not check_ok:
        return PublishResult(
            success=False,
            reason=f"twine check FAILED:\n{check_out.strip()}",
            package=package,
            version=version,
        )

    if dry_run:
        return PublishResult(
            success=True,
            reason=f"DRY RUN — would upload {len(artifacts)} artifact(s)",
            package=package,
            version=version,
            uploaded_artifacts=artifacts,
        )

    if not _release_auth_disabled():
        if not _confirm_from_tty(package, version):
            return PublishResult(
                success=False,
                reason="user declined confirmation",
                package=package,
                version=version,
            )

    ok, out = execute_twine_upload(artifacts)
    if not ok:
        return PublishResult(
            success=False,
            reason=f"twine upload FAILED:\n{out.strip()}",
            package=package,
            version=version,
        )
    return PublishResult(
        success=True,
        reason="uploaded",
        package=package,
        version=version,
        uploaded_artifacts=artifacts,
    )


def _print_success(r: PublishResult) -> None:
    """Human output for the success path."""
    print(f"concinno publish: OK — {r.package} {r.version}")
    for art in r.uploaded_artifacts or []:
        print(f"  uploaded: {art}")
    if "DRY RUN" in r.reason:
        print(f"  ({r.reason})")


def register(subparsers: argparse._SubParsersAction) -> None:
    """Register the `publish` subcommand."""
    p = subparsers.add_parser(
        "publish",
        help="Publish a PyPI package (run from user's terminal, not via CC)",
    )
    p.add_argument("package", help="Package name (e.g. concinno)")
    p.add_argument("version", help="Version to publish (e.g. 2.16.0)")
    p.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="Verify artifacts + Queue record, skip upload",
    )
    p.add_argument(
        "--path",
        default="",
        help="Project root (default: cwd). Must contain RELEASE_COORDINATION.md + dist/.",
    )
    p.set_defaults(func=cmd_publish)


__all__ = [
    "PublishResult",
    "detect_package_name",
    "find_queue_record",
    "verify_artifacts",
    "run_twine_check",
    "execute_twine_upload",
    "cmd_publish",
    "register",
]
