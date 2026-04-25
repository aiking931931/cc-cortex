"""concinno.auto_update.tier2_self_update -- ``concinno self-update`` engine.

Tier 2 of the two-tier auto-update model (per
``_AI_BRAIN/05_Planning/sancio-gui-extension-auto-update-design-2026-04-25.md``
§4 Q4). Tier 1 = registry refresh only, no ``pip install``. Tier 2 = the
opt-in path that **actually upgrades the installed concinno package**
via ``pip install --upgrade``.

Architecture: detached helper process (R#7 commander verdict)
=================================================================

Running ``pip install --upgrade concinno`` from inside the very Python
process that imported ``concinno`` is hostile:

* **Windows**: ``concinno/*.py`` files are open as importer-held byte
  streams; pip's ``shutil.move`` of the new package over the old one
  raises ``PermissionError`` (file lock). 100% block.
* **POSIX**: file replacement succeeds (inode swap), but the live
  module objects in the parent's memory still reference the old code,
  so any subsequent import of a yet-unloaded submodule mixes versions.

Solution (per ``redteam.md`` R#7 + commander verdict line 49):

1. Detect installation method (pip-user / pip-system / pip-venv /
   editable). Editable = abort with helpful message.
2. Resolve target version (default = latest stable on PyPI).
3. Detect concinno-in-use (Windows: file-lock probe; POSIX: psutil walk
   if available, else best-effort by reading ``/proc/*/status``).
4. **Spawn a detached helper subprocess** that runs
   ``python -m pip install --upgrade concinno==<ver>`` with stdio
   completely redirected (DEVNULL or log file).
5. **Self-process exits immediately** with a "restart Claude Code to
   use new version" message. We do NOT wait for the helper.
6. Helper completes in the background. Next ``concinno`` invocation
   loads the upgraded package fresh.

Fail-soft contract:

* Editable install → return error, no spawn.
* Already on target version → return success, no spawn.
* No network / PyPI fetch fails → return error.
* In-use detected on Windows + no ``--skip-in-use-check`` → return
  error with "close Claude Code first" hint.
* In-use detected on POSIX + no ``--skip-in-use-check`` → warn and
  continue (POSIX file replacement is safe).

@module concinno.auto_update.tier2_self_update
@responsibility Drive ``pip install --upgrade`` from a detached helper
    so the parent process can exit cleanly without crashing itself on
    Windows file locks or POSIX module-version skew.
@dependencies stdlib only (urllib, subprocess, shutil, json, sys, os,
    pathlib, dataclasses, platform)
@exports SelfUpdateResult, detect_pip_install_method,
    get_latest_pypi_version, is_concinno_in_use, self_update
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

# ── Constants ──────────────────────────────────────────────────────

_PYPI_JSON_URL = "https://pypi.org/pypi/{pkg}/json"
_DEFAULT_TIMEOUT_S = 5
_DEFAULT_PKG = "concinno"

# Windows process-creation flags (subprocess module re-exports them on
# Windows but they are missing on POSIX).
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_NO_WINDOW = 0x08000000


# ── Public dataclass ───────────────────────────────────────────────


@dataclass
class SelfUpdateResult:
    """Outcome of one ``self_update()`` invocation."""

    current_version: str = ""
    latest_version: str | None = None
    target_version: str | None = None
    method: str = "unknown"
    helper_spawned: bool = False
    helper_pid: int | None = None
    in_use_warning: list[str] = field(default_factory=list)
    error: str | None = None
    dry_run: bool = False
    log_path: str | None = None


# ── Install-method detection ───────────────────────────────────────


def detect_pip_install_method() -> str:
    """Classify the current concinno install.

    Returns one of:
      * ``editable`` — package is installed in editable mode
        (pip ``-e`` / setuptools ``develop`` / hatchling editable).
      * ``pip-venv`` — installed inside a virtualenv (real_prefix or
        sys.prefix != sys.base_prefix).
      * ``pip-user`` — installed in user site-packages
        (``site.getusersitepackages()``).
      * ``pip-system`` — installed in system site-packages.
      * ``unknown`` — can't decide.

    Detection is best-effort. False classification only affects the
    user-facing report; the spawn step uses ``sys.executable -m pip``
    which works for every method except read-only system packages
    (which pip rejects on its own).
    """
    try:
        import concinno

        pkg_path = Path(concinno.__file__).resolve().parent
    except Exception:
        return "unknown"

    # Editable install markers:
    #   * file ``__editable__.<pkg>-<ver>.pth`` somewhere in sys.path
    #   * a sibling ``*.dist-info/direct_url.json`` with
    #     ``"dir_info": {"editable": true}``
    if _looks_like_editable(pkg_path):
        return "editable"

    # Venv check: sys.prefix differs from sys.base_prefix.
    if getattr(sys, "base_prefix", sys.prefix) != sys.prefix:
        return "pip-venv"

    # User install: pkg under site.getusersitepackages().
    try:
        import site

        user_site = site.getusersitepackages()
    except Exception:
        user_site = ""
    if user_site and str(pkg_path).startswith(user_site):
        return "pip-user"

    return "pip-system"


def _looks_like_editable(pkg_path: Path) -> bool:
    """Return True if ``pkg_path`` is part of an editable install."""
    # Strategy 1: scan parent dirs for __editable__*.pth files.
    parent = pkg_path.parent
    try:
        for entry in parent.iterdir():
            name = entry.name
            if name.startswith("__editable__") and name.endswith(".pth"):
                return True
            # PEP 660 also dumps an `_<pkg>_editable_finder.py` shim.
            if name.startswith("_") and name.endswith("_editable_finder.py"):
                return True
    except OSError:
        pass

    # Strategy 2: sibling ``*.dist-info/direct_url.json`` says editable.
    try:
        for sibling in parent.glob("concinno-*.dist-info"):
            durl = sibling / "direct_url.json"
            if durl.is_file():
                try:
                    data = json.loads(durl.read_text(encoding="utf-8"))
                except Exception:
                    continue
                dir_info = data.get("dir_info") or {}
                if dir_info.get("editable") is True:
                    return True
    except OSError:
        pass

    return False


# ── PyPI version fetch ─────────────────────────────────────────────


def get_latest_pypi_version(
    pkg: str = _DEFAULT_PKG,
    timeout_s: int = _DEFAULT_TIMEOUT_S,
    *,
    pre_release: bool = False,
) -> str | None:
    """Fetch latest version from ``https://pypi.org/pypi/<pkg>/json``.

    Returns ``None`` on network error, JSON-parse error, or empty
    release list. Does not raise.

    When ``pre_release`` is False (default), filters out versions
    containing alpha/beta/rc/dev markers (substring match against
    ``a``, ``b``, ``rc``, ``dev`` after the ``X.Y.Z`` numeric prefix).
    """
    url = _PYPI_JSON_URL.format(pkg=pkg)
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError):
        return None

    # Two ways to get the latest: ``info.version`` (PyPI's pick) or by
    # walking ``releases`` keys. ``info.version`` already filters
    # yanked but may include pre-releases. We use ``releases`` keys so
    # we can apply our own pre-release filter.
    releases = payload.get("releases") or {}
    if not releases:
        return None

    candidates: list[str] = []
    for ver, files in releases.items():
        if not files:
            # All artifacts deleted -> skip.
            continue
        # Skip yanked-only releases.
        all_yanked = all(f.get("yanked", False) for f in files)
        if all_yanked:
            continue
        if not pre_release and _is_pre_release(ver):
            continue
        candidates.append(ver)

    if not candidates:
        return None
    candidates.sort(key=_version_sort_key, reverse=True)
    return candidates[0]


def _is_pre_release(ver: str) -> bool:
    """True if version string contains alpha/beta/rc/dev marker."""
    s = ver.lower()
    for marker in ("a", "b", "rc", "dev", "pre"):
        # Look for digit-followed-by-marker (e.g. "2.36.0a1") or
        # "-rc" / "-dev" suffix.
        for sep in ("", ".", "-"):
            if f"{sep}{marker}" in s and any(
                c.isdigit() for c in s[: s.find(f"{sep}{marker}")]
            ):
                return True
    return False


def _version_sort_key(ver: str) -> tuple:
    """Best-effort numeric sort; falls back to string compare."""
    parts: list = []
    for chunk in ver.replace("-", ".").split("."):
        try:
            parts.append((0, int(chunk)))
        except ValueError:
            parts.append((1, chunk))
    return tuple(parts)


# ── In-use detection ───────────────────────────────────────────────


def is_concinno_in_use() -> tuple[bool, list[str]]:
    """Detect if other processes are currently importing concinno.

    Returns ``(in_use, [evidence])`` where ``evidence`` is a list of
    human-readable strings ("PID 1234: claude.exe", "file lock on
    foo.py"). Best-effort; never raises.

    Strategy:
      * **Windows**: probe a known concinno file with ``os.replace``
        in-place rename (which Windows blocks if any process holds
        the file open). If blocked → in_use=True. Always also try
        ``psutil`` walk if available.
      * **POSIX**: file replacement is always permitted (inode swap).
        Use ``psutil`` if available to walk live processes; otherwise
        return ``in_use=False`` (no cheap detection method).

    The current process (``os.getpid()``) is excluded from the
    evidence list — it's always "using" concinno but doesn't count.
    """
    evidence: list[str] = []
    in_use = False

    # File-lock probe (Windows-specific).
    if platform.system() == "Windows":
        locked, lock_evidence = _probe_windows_file_lock()
        if locked:
            in_use = True
            evidence.append(lock_evidence)

    # psutil walk (cross-platform; optional).
    psutil_evidence = _probe_psutil_walk()
    if psutil_evidence:
        in_use = True
        evidence.extend(psutil_evidence)

    return in_use, evidence


def _probe_windows_file_lock() -> tuple[bool, str]:
    """Try to rename a concinno file in-place; if it fails, it's locked.

    Windows ``MoveFileExW`` rejects rename if any process has an open
    handle to the file (concinno's own importer doesn't count, since
    Python opens .py files for read and closes the handle immediately;
    only .pyd/.pyc DLLs would be locked, but concinno is pure Python).

    Therefore a successful in-place rename means **no other process**
    has the file open with write/exclusive sharing — which is the
    relevant signal here.
    """
    try:
        import concinno

        target = Path(concinno.__file__).resolve()
    except Exception:
        return False, ""
    # Use a same-name same-dir replace (no actual change). Windows
    # still rejects if locked.
    try:
        os.replace(target, target)
        return False, ""
    except OSError as exc:
        return True, f"file lock probe failed on {target.name}: {exc}"


def _probe_psutil_walk() -> list[str]:
    """Walk live processes via psutil. Returns list of evidence strs.

    psutil is optional. If not installed, return [].
    """
    try:
        import psutil  # type: ignore[import-not-found]
    except ImportError:
        return []

    own_pid = os.getpid()
    evidence: list[str] = []

    try:
        my_concinno = Path(__import__("concinno").__file__).resolve().parent
    except Exception:
        my_concinno = None

    for proc in psutil.process_iter(["pid", "name"]):
        _scan_one_proc(proc, own_pid, my_concinno, evidence, psutil)

    return evidence


def _scan_one_proc(proc, own_pid, my_concinno, evidence, psutil_mod) -> None:
    """Inspect one process; append evidence strings if it uses concinno."""
    try:
        pid = proc.info["pid"]
        if pid == own_pid:
            return
        name = proc.info.get("name", "?")
        cmdline = " ".join(proc.cmdline() or [])
        if "concinno" in cmdline.lower():
            evidence.append(f"PID {pid} ({name}): cmdline mentions concinno")
            return
        if my_concinno is not None:
            _scan_open_files(proc, pid, name, my_concinno, evidence, psutil_mod)
    except (
        psutil_mod.NoSuchProcess,
        psutil_mod.AccessDenied,
        psutil_mod.ZombieProcess,
    ):
        return


def _scan_open_files(proc, pid, name, my_concinno, evidence, psutil_mod) -> None:
    """Inner helper: scan one proc's open files for concinno path overlap."""
    try:
        open_files = proc.open_files() or []
    except (psutil_mod.AccessDenied, psutil_mod.NoSuchProcess, OSError):
        return
    target = my_concinno.as_posix()
    for f in open_files:
        try:
            if target in Path(f.path).resolve().as_posix():
                evidence.append(
                    f"PID {pid} ({name}): has open handle to {f.path}"
                )
                return
        except OSError:
            continue


# ── Detached helper spawn ──────────────────────────────────────────


def _build_pip_argv(target_version: str) -> list[str]:
    """Build the pip invocation for the helper subprocess."""
    return [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--upgrade",
        "--disable-pip-version-check",
        f"{_DEFAULT_PKG}=={target_version}",
    ]


def _open_helper_log() -> tuple[Path, "object"]:
    """Open a log file for the helper subprocess output (POSIX path).

    Windows uses DEVNULL because we can't safely keep a file handle
    around once the parent exits. POSIX redirects to a log file under
    ``~/.concinno/self_update.log`` so the user can inspect outcome
    after restart.
    """
    log_dir = Path.home() / ".concinno"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "self_update.log"
    fp = open(log_path, "ab", buffering=0)  # noqa: SIM115 -- intentional
    return log_path, fp


def _spawn_detached(target_version: str) -> tuple[int, str | None]:
    """Spawn the pip helper, fully detached. Returns (pid, log_path).

    Never waits. The helper inherits no parent FDs; stdin/stdout/stderr
    are redirected to DEVNULL (Windows) or a log file (POSIX). Helper
    is in a new process group / new session so SIGINT to parent does
    not propagate.
    """
    argv = _build_pip_argv(target_version)
    is_windows = platform.system() == "Windows"

    if is_windows:
        flags = (
            _DETACHED_PROCESS
            | _CREATE_NEW_PROCESS_GROUP
            | _CREATE_NO_WINDOW
        )
        proc = subprocess.Popen(  # noqa: S603 -- argv constructed safely
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
            close_fds=True,
        )
        return proc.pid, None

    # POSIX: redirect to a log file the user can inspect.
    log_path, log_fp = _open_helper_log()
    try:
        proc = subprocess.Popen(  # noqa: S603 -- argv constructed safely
            argv,
            stdin=subprocess.DEVNULL,
            stdout=log_fp,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
        # Parent must close its handle so it isn't inherited if the
        # log_fp object lingers on the parent side.
        return proc.pid, str(log_path)
    finally:
        log_fp.close()


# ── Main entry point ───────────────────────────────────────────────


def self_update(
    *,
    target_version: str | None = None,
    pre_release: bool = False,
    skip_in_use_check: bool = False,
    dry_run: bool = False,
    pkg: str = _DEFAULT_PKG,
) -> SelfUpdateResult:
    """Drive the self-update flow.

    See module docstring for high-level architecture. This function
    never raises; all errors come back via ``SelfUpdateResult.error``.
    """
    try:
        import concinno

        current_version = getattr(concinno, "__version__", "unknown")
    except Exception:
        current_version = "unknown"

    result = SelfUpdateResult(
        current_version=current_version,
        method=detect_pip_install_method(),
        dry_run=dry_run,
    )

    # 1. Editable install -> abort with explanation.
    if result.method == "editable":
        result.error = (
            "editable install detected; self-update would clobber your "
            "working tree. Run `pip install --upgrade <pkg>` manually "
            "from outside this checkout, or `pip install -e .` again "
            "after pulling new code."
        )
        return result

    # 2. Resolve target version.
    if target_version is None:
        latest = get_latest_pypi_version(pkg, pre_release=pre_release)
        if latest is None:
            result.error = (
                f"failed to fetch latest version from PyPI "
                f"({_PYPI_JSON_URL.format(pkg=pkg)}). Check network."
            )
            return result
        result.latest_version = latest
        target = latest
    else:
        target = target_version
        # Try to fetch latest for display purposes; ignore failure.
        result.latest_version = get_latest_pypi_version(
            pkg, pre_release=pre_release
        )
    result.target_version = target

    # 3. Already on target -> success without spawn.
    if current_version == target:
        result.error = None
        return result

    # 4. In-use check.
    if not skip_in_use_check:
        in_use, evidence = is_concinno_in_use()
        result.in_use_warning = list(evidence)
        if in_use:
            if platform.system() == "Windows":
                result.error = (
                    "concinno is in use by another process; close Claude "
                    "Code (or pass --skip-in-use-check) before "
                    "self-update on Windows."
                )
                return result
            # POSIX: warn but continue.

    # 5. Dry-run -> stop before spawn.
    if dry_run:
        return result

    # 6. Spawn detached helper.
    try:
        pid, log_path = _spawn_detached(target)
    except OSError as exc:
        result.error = f"failed to spawn pip helper: {exc}"
        return result

    result.helper_spawned = True
    result.helper_pid = pid
    result.log_path = log_path
    return result


# ── pip presence sanity (used by CLI) ──────────────────────────────


def pip_available() -> bool:
    """True if ``python -m pip --version`` succeeds quickly."""
    try:
        proc = subprocess.run(  # noqa: S603 -- argv fully literal
            [sys.executable, "-m", "pip", "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
        return proc.returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


__all__ = [
    "SelfUpdateResult",
    "detect_pip_install_method",
    "get_latest_pypi_version",
    "is_concinno_in_use",
    "self_update",
    "pip_available",
]
