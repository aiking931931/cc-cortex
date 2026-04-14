"""cc_cortex.tools.builtin.shell — Bash execution tool (Aegis P0.2).

@module tools.builtin.shell
@responsibility Execute a bash command under the full CCC defensive stack:
    (1) :class:`BashValidator` (24 validators from bash_validators.py) gates
    shell-injection shapes, (2) :func:`destruction_guard.evaluate` applies
    the R0-R4 destructive-operation classifier with auto-backup, (3) a
    two-stage classifier (deterministic stage-1 rules + LLM-stub stage-2)
    marks the command for audit, (4) a single :mod:`subprocess` run with
    a 15-second auto-background flip mirrors Claude Code's BashTool
    foreground/background behavior, (5) output is middle-truncated to
    30k characters with a clear marker. State for background jobs is
    persisted via :class:`StateStore` under the ``shell_background``
    namespace so long-running jobs survive process restarts.

    This module is the Aegis drop-in replacement for CC's BashTool. The
    public ``name`` is ``"Bash"`` so callers that key off CC's tool
    registry find it unchanged; the security posture is equal to or
    stronger than CC's own (CCC 1.15.0 validators port the full
    ``bashSecurity.ts`` Zsh bypass blocklist + dangerous-variable
    checks — see the docstring of that module for the list of 24
    validators and attack classes they close).

@dependencies cc_cortex.security.bash_validators (hard),
    cc_cortex.destruction_guard (hard),
    cc_cortex.core.state_store (hard),
    stdlib: subprocess, tempfile, pathlib, time, uuid, logging, json.
@exports Shell, ShellSecurityError, ShellDestructionError,
    ShellTimeoutError, classify_stage1, classify_stage2.

CC source cross-reference (read, not copied):
  - tools/BashTool/BashTool.tsx           (top-level Tool class + 15s rule)
  - tools/BashTool/bashSecurity.ts L45-74 (Zsh module blocklist — already
    covered by CCC ``validate_zsh_dangerous_commands`` in
    bash_validators.py)
  - tools/BashTool/bashPermissions.ts     (safe wrapper stripping — already
    covered by CCC ``strip_safe_wrappers``)

Gap audit (2026-04-14) — things CC's BashTool does that this wrapper
does NOT yet do, recorded here so the next round can address them:
  * tree-sitter shell parser — CCC uses regex + state machine
    (bash_validators.py docstring explains the tradeoff).
  * Stage-2 LLM classifier — currently a stub that always returns
    "unknown". A real escalator call should be wired when the aegis
    cost/latency budget for stage-2 is decided.
  * Sandbox manager — not in scope for P0.2; DestructionGuard's
    auto-backup is the current mitigation.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Literal

from cc_cortex.core.state_store import StateStore
from cc_cortex.destruction_guard import evaluate as destruction_evaluate
from cc_cortex.security.bash_validators import BashValidator

logger = logging.getLogger("cc_cortex.tools.builtin.shell")


# --------------------------------------------------------------------------- #
# Public constants
# --------------------------------------------------------------------------- #


DEFAULT_TIMEOUT_MS: int = 120_000
"""Foreground timeout matching CC's BashTool default (2 minutes)."""

AUTO_BACKGROUND_SECONDS: float = 15.0
"""Wall-clock threshold after which a foreground command auto-flips to
background mode. Mirrors CC's BashTool 15-second rule."""

OUTPUT_TRUNCATION_CAP: int = 30_000
"""Maximum stdout/stderr length before middle-truncation kicks in."""

_STATE_STORE_NAMESPACE: str = "shell_background"
"""StateStore namespace for tracking spawned background jobs."""


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #


class ShellSecurityError(Exception):
    """Raised when :class:`BashValidator` rejects the command.

    Attributes:
        command: The original command string.
        validator: The validator name that caught the bypass.
        bypass_class: The attack class (see ``BYPASS_CLASSES`` in
            ``bash_validators.py``).
    """

    def __init__(
        self,
        command: str,
        *,
        validator: str,
        bypass_class: str,
        reason: str,
    ) -> None:
        self.command = command
        self.validator = validator
        self.bypass_class = bypass_class
        self.reason = reason
        super().__init__(
            f"BashValidator rejected command (validator={validator!r}, "
            f"bypass_class={bypass_class!r}): {reason}"
        )


class ShellDestructionError(Exception):
    """Raised when :func:`destruction_guard.evaluate` denies the command."""

    def __init__(self, command: str, *, reason: str) -> None:
        self.command = command
        self.reason = reason
        super().__init__(f"DestructionGuard denied command: {reason}")


class ShellTimeoutError(Exception):
    """Raised when a foreground command exceeds ``timeout_ms``.

    Note:
        The 15-second auto-background flip is a *separate* mechanism —
        it does not raise this error. Only the explicit ``timeout_ms``
        ceiling triggers :class:`ShellTimeoutError`.
    """

    def __init__(self, command: str, *, timeout_ms: int) -> None:
        self.command = command
        self.timeout_ms = timeout_ms
        super().__init__(
            f"Command exceeded timeout_ms={timeout_ms}: {command!r}"
        )


# --------------------------------------------------------------------------- #
# Stage-1 classifier (deterministic)
# --------------------------------------------------------------------------- #


# Safe command whitelist. A command counts as "safe" iff the first
# executable token is in this set AND no shell metacharacter appears
# between it and the end of the string. This is intentionally narrow —
# false-negative is OK (falls through to "unknown"), false-positive is
# expensive (would bypass the stage-2 gate).
_SAFE_COMMAND_PREFIXES: frozenset[str] = frozenset(
    {
        "ls",
        "cat",
        "grep",
        "find",
        "pwd",
        "which",
        "echo",
        "printf",
        "head",
        "tail",
        "wc",
        "file",
        "stat",
        "date",
        "whoami",
        "hostname",
        "true",
        "false",
    }
)

# Safe two-token git subcommands. Any git invocation outside this list
# falls through to "unknown" and is subject to stage-2 review.
_SAFE_GIT_SUBCOMMANDS: frozenset[str] = frozenset(
    {
        "status",
        "log",
        "diff",
        "show",
        "branch",
        "remote",
        "config",
        "rev-parse",
        "describe",
        "ls-files",
        "ls-tree",
        "cat-file",
    }
)

# Risky patterns — re-uses the aegis/guard.py ``_DESTRUCTIVE_BASH`` list
# verbatim so the two layers stay in sync. Each entry is (regex, label).
# NOTE: this is defence-in-depth on top of the destruction_guard which
# has already run before stage-1. A match here flags the command for
# stage-2 audit logging even if destruction_guard missed an edge case.
_STAGE1_RISKY_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"rm\s+-rf\s+/", "Recursive deletion from root"),
    (r"rm\s+-rf\s+~", "Recursive deletion of home directory"),
    (r"rm\s+-rf\s+\.\s", "Recursive deletion of current directory"),
    (r"mkfs\.", "Filesystem format command"),
    (r"dd\s+if=.+of=/dev/", "Direct disk overwrite"),
    (r":\(\)\s*\{\s*:\|:&\s*\}", "Fork bomb"),
    (r"chmod\s+-R\s+777\s+/", "World-writable root permissions"),
    (r">\s*/dev/sd[a-z]", "Direct write to block device"),
    (r"git\s+push\s+.*--force\s+.*main", "Force push to main branch"),
    (r"git\s+push\s+.*--force\s+.*master", "Force push to master branch"),
    (r"git\s+reset\s+--hard", "Hard reset (potential data loss)"),
    (r"DROP\s+(TABLE|DATABASE)", "SQL destructive operation"),
    (r"TRUNCATE\s+TABLE", "SQL table truncation"),
)

# Pre-compile for performance — stage-1 is on every Bash call hot path.
import re as _re  # noqa: E402

_STAGE1_RISKY_COMPILED: tuple[tuple[Any, str], ...] = tuple(
    (_re.compile(pat, _re.IGNORECASE), label)
    for pat, label in _STAGE1_RISKY_PATTERNS
)

# Characters that immediately disqualify a command from the safe
# whitelist. Any of these leaks pipelines / redirection / substitution
# / backgrounding / command chaining beyond a trivial read.
_UNSAFE_METACHARS: frozenset[str] = frozenset(
    {"|", ";", "&", "`", "$", ">", "<", "\n"}
)


Stage1Label = Literal["safe", "risky", "unknown"]


def classify_stage1(command: str) -> Stage1Label:
    """Deterministic stage-1 classifier.

    Returns one of ``"safe"`` / ``"risky"`` / ``"unknown"``:
      * ``"safe"`` — pure read command from the whitelist with no shell
        metacharacters. Stage-2 is skipped.
      * ``"risky"`` — matches one of the destructive patterns from
        ``_STAGE1_RISKY_PATTERNS``. Stage-2 is consulted (currently a
        stub that logs but does not block — see :func:`classify_stage2`).
      * ``"unknown"`` — everything else. Stage-2 is consulted and the
        command is allowed pending future LLM review.

    This is the first sieve of the Aegis 2-stage classifier. It is
    deliberately cheap (~microseconds) and never blocks on its own —
    blocking is the job of :class:`BashValidator` and
    ``destruction_guard`` which have already run by the time we reach
    here. Classification output is used for audit logging and to decide
    whether stage-2 needs to be consulted.
    """
    if not command or not command.strip():
        return "unknown"

    # 1. Risky patterns are checked FIRST — even a command that looks
    #    "safe" can be hiding a destructive payload in a chain.
    for regex, _label in _STAGE1_RISKY_COMPILED:
        if regex.search(command):
            return "risky"

    stripped = command.strip()

    # 2. Safe whitelist — only applies if there are no shell metachars.
    if any(ch in stripped for ch in _UNSAFE_METACHARS):
        return "unknown"

    tokens = stripped.split()
    if not tokens:
        return "unknown"

    first = tokens[0]

    # Special case: `git <safe-subcommand>` is safe.
    if first == "git" and len(tokens) >= 2 and tokens[1] in _SAFE_GIT_SUBCOMMANDS:
        return "safe"

    if first in _SAFE_COMMAND_PREFIXES:
        return "safe"

    return "unknown"


def classify_stage2(command: str) -> str:
    """Stage-2 LLM-based classifier. **Currently a stub.**

    TODO(aegis P0.3): Wire this to an LLM escalator that can reason
    about unknown / risky commands. The escalator should return
    ``"safe"`` / ``"risky"`` / ``"unknown"`` matching stage-1, and
    the caller in :meth:`Shell.call` should promote a ``"risky"``
    verdict here to an audit log entry (not a hard block — hard
    blocking is the job of :class:`BashValidator` and
    ``destruction_guard``).

    For now the stub always returns ``"unknown"`` and emits a warning
    so the operator can see every call site that would benefit from
    stage-2 once it ships.
    """
    logger.warning(
        "stage2 classifier not yet wired — returning 'unknown' for command: %s",
        command[:120],
    )
    return "unknown"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _truncate_output(text: str, cap: int = OUTPUT_TRUNCATION_CAP) -> tuple[str, bool]:
    """Middle-truncate ``text`` to at most ``cap`` characters.

    Keeps the first ``cap // 2`` and last ``cap // 2`` characters with a
    ``... [truncated N chars] ...`` marker in between. Returns
    ``(truncated_text, was_truncated)``. If ``text`` already fits the
    cap the original string is returned unchanged.
    """
    if text is None:
        return "", False
    if len(text) <= cap:
        return text, False
    half = cap // 2
    head = text[:half]
    tail = text[-half:]
    dropped = len(text) - (len(head) + len(tail))
    marker = f"\n... [truncated {dropped} chars] ...\n"
    return head + marker + tail, True


def _make_background_log_path(job_id: str) -> Path:
    """Return ``~/.cc_cortex_cache/shell_logs/{job_id}.log``.

    The parent directory is created on demand. ``job_id`` is inserted
    into the filename verbatim; callers must generate it via
    :func:`uuid.uuid4` or equivalent so that filesystem collisions are
    impossible even under heavy parallelism.
    """
    base = Path(os.environ.get("CC_CORTEX_CACHE_DIR", str(Path.home() / ".cc_cortex_cache")))
    log_dir = base / "shell_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / f"{job_id}.log"


# --------------------------------------------------------------------------- #
# Main Tool
# --------------------------------------------------------------------------- #


class Shell:
    """Bash execution tool — Aegis drop-in for CC's BashTool.

    Pipeline on every :meth:`call`:
      1. :class:`BashValidator` — 24-validator shell-injection gate
         (raises :class:`ShellSecurityError` on reject).
      2. :func:`destruction_guard.evaluate` — R0-R4 destructive-op
         classifier with auto-backup (raises
         :class:`ShellDestructionError` on deny).
      3. :func:`classify_stage1` — deterministic safe/risky/unknown
         classification for audit.
      4. :func:`classify_stage2` — LLM stub for risky/unknown commands
         (currently logs and returns ``"unknown"`` unconditionally).
      5. Execute via :class:`subprocess.Popen`:
         * ``run_in_background=False`` — foreground run with 15-second
           auto-background flip. If the process has not exited after
           ``AUTO_BACKGROUND_SECONDS`` the job is registered with
           :class:`StateStore` and a ``background_job_id`` is returned
           alongside whatever output has been captured so far.
         * ``run_in_background=True`` — spawn and return immediately
           with ``background_job_id`` set.
      6. Middle-truncate stdout/stderr if they exceed
         ``OUTPUT_TRUNCATION_CAP``.

    Attributes:
        name: Matches CC's tool name so Aegis is a drop-in replacement
            (``"Bash"``).
        description: Short description for the tool registry.
        is_concurrency_safe: Always ``False`` — shell execution is
            stateful and must be serialized per session.
    """

    name: str = "Bash"
    description: str = (
        "Execute a bash command. Subject to destruction_guard, "
        "bash_validators, and 2-stage classifier."
    )
    is_concurrency_safe: bool = False

    def __init__(
        self,
        *,
        validator: BashValidator | None = None,
        state_store: StateStore | None = None,
        cache_dir: str | None = None,
    ) -> None:
        """Construct a Shell tool instance.

        Args:
            validator: Optional pre-configured :class:`BashValidator`.
                If ``None``, a default-config instance is created.
            state_store: Optional pre-configured :class:`StateStore`.
                If ``None``, one is created rooted at ``cache_dir`` or
                ``~/.cc_cortex_cache``.
            cache_dir: Base directory for StateStore + background logs.
                Defaults to ``$CC_CORTEX_CACHE_DIR`` or
                ``~/.cc_cortex_cache``.
        """
        self._validator = validator or BashValidator()
        if cache_dir is None:
            cache_dir = os.environ.get(
                "CC_CORTEX_CACHE_DIR", str(Path.home() / ".cc_cortex_cache")
            )
        self._cache_dir = cache_dir
        self._store = state_store or StateStore(cache_dir)

    # ------------------------------------------------------------------ #
    # Public entrypoint
    # ------------------------------------------------------------------ #

    def call(
        self,
        *,
        command: str,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        description: str = "",
        run_in_background: bool = False,
    ) -> dict[str, Any]:
        """Run ``command`` through the full pipeline.

        Args:
            command: The bash command string.
            timeout_ms: Hard ceiling in milliseconds for foreground
                runs. Default 120_000 (2 minutes) matching CC.
            description: Optional LLM-supplied description for audit.
                Currently logged only; not persisted.
            run_in_background: If ``True``, spawn the process and
                return immediately with a ``background_job_id``.

        Returns:
            A dict with keys ``stdout``, ``stderr``, ``exit_code``,
            ``duration_ms``, ``background_job_id``,
            ``stage1_classification``, ``truncated``.

        Raises:
            ShellSecurityError: The :class:`BashValidator` pipeline
                rejected the command.
            ShellDestructionError: ``destruction_guard`` denied the
                command.
            ShellTimeoutError: Foreground run exceeded ``timeout_ms``.
        """
        # -------- Stage A: bash_validators -------------------------- #
        vr = self._validator.validate(command)
        if not vr.ok:
            raise ShellSecurityError(
                command,
                validator=vr.validator,
                bypass_class=vr.bypass_class,
                reason=vr.reason,
            )

        # -------- Stage B: destruction_guard ------------------------ #
        dg = destruction_evaluate("Bash", {"command": command})
        if dg.get("permissionDecision") == "deny":
            raise ShellDestructionError(
                command, reason=str(dg.get("reason", "destruction_guard denied"))
            )

        # -------- Stage C: 2-stage classifier ----------------------- #
        stage1 = classify_stage1(command)
        if stage1 != "safe":
            # "risky" and "unknown" both consult stage-2. Stub logs
            # a warning and returns "unknown"; we do not block on
            # stage-2 output — audit only.
            stage2 = classify_stage2(command)
            logger.info(
                "shell classify stage1=%s stage2=%s desc=%r command=%r",
                stage1, stage2, description, command[:120],
            )
        else:
            logger.debug("shell classify stage1=safe command=%r", command[:120])

        # -------- Stage D: execute --------------------------------- #
        if run_in_background:
            return self._spawn_background(command, stage1=stage1)

        return self._run_foreground(
            command, timeout_ms=timeout_ms, stage1=stage1
        )

    # ------------------------------------------------------------------ #
    # Foreground execution with 15s auto-background flip
    # ------------------------------------------------------------------ #

    def _run_foreground(
        self,
        command: str,
        *,
        timeout_ms: int,
        stage1: Stage1Label,
    ) -> dict[str, Any]:
        start = time.monotonic()
        hard_timeout = max(timeout_ms / 1000.0, 0.001)

        proc = subprocess.Popen(  # noqa: S602 - shell=True is the whole point
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
        )

        # First wait: 15s auto-background flip.
        try:
            stdout, stderr = proc.communicate(
                timeout=min(AUTO_BACKGROUND_SECONDS, hard_timeout)
            )
        except subprocess.TimeoutExpired:
            # 15 seconds elapsed without exit. If the user's hard
            # timeout is already past, raise. Otherwise, auto-flip
            # to background: leave the process running, register
            # it with the state store, and return the job_id.
            elapsed = time.monotonic() - start
            if elapsed >= hard_timeout:
                proc.kill()
                try:
                    proc.communicate(timeout=1.0)
                except Exception:  # noqa: BLE001 - best-effort cleanup
                    pass
                raise ShellTimeoutError(command, timeout_ms=timeout_ms) from None

            # Auto-background flip.
            job_id = self._register_background_job(
                command, proc=proc, started_at=start
            )
            logger.info(
                "shell auto-backgrounded after %.1fs: job_id=%s command=%r",
                elapsed, job_id, command[:120],
            )
            return {
                "stdout": "",
                "stderr": "",
                "exit_code": None,
                "duration_ms": int(elapsed * 1000),
                "background_job_id": job_id,
                "stage1_classification": stage1,
                "truncated": False,
            }

        duration_ms = int((time.monotonic() - start) * 1000)

        # Truncate oversized output.
        out_trunc, out_was_truncated = _truncate_output(stdout or "")
        err_trunc, err_was_truncated = _truncate_output(stderr or "")

        return {
            "stdout": out_trunc,
            "stderr": err_trunc,
            "exit_code": proc.returncode,
            "duration_ms": duration_ms,
            "background_job_id": None,
            "stage1_classification": stage1,
            "truncated": out_was_truncated or err_was_truncated,
        }

    # ------------------------------------------------------------------ #
    # Background execution
    # ------------------------------------------------------------------ #

    def _spawn_background(
        self, command: str, *, stage1: Stage1Label
    ) -> dict[str, Any]:
        start = time.monotonic()
        job_id = uuid.uuid4().hex
        log_path = _make_background_log_path(job_id)
        log_fh = open(log_path, "w", encoding="utf-8", errors="replace")
        proc = subprocess.Popen(  # noqa: S602 - shell=True is the whole point
            command,
            shell=True,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self._store.write(
            _STATE_STORE_NAMESPACE,
            job_id,
            {
                "pid": proc.pid,
                "command": command,
                "started_at": start,
                "log_path": str(log_path),
            },
        )
        return {
            "stdout": "",
            "stderr": "",
            "exit_code": None,
            "duration_ms": 0,
            "background_job_id": job_id,
            "stage1_classification": stage1,
            "truncated": False,
        }

    def _register_background_job(
        self,
        command: str,
        *,
        proc: subprocess.Popen[str],
        started_at: float,
    ) -> str:
        """Register an already-running process as a background job.

        Used by the 15-second auto-background flip: the process was
        started in the foreground, we hit the flip threshold, and we
        hand it off to StateStore so the caller can poll later. The
        process keeps its existing stdout/stderr pipes — they are
        NOT redirected to a log file because doing so would require
        stopping and restarting the process, losing state.

        Note:
            Output buffered in the pipes after auto-background is
            intentionally lost. Callers who need full output must
            pass ``run_in_background=True`` from the start so the
            log file is wired up before ``Popen`` runs.
        """
        job_id = uuid.uuid4().hex
        self._store.write(
            _STATE_STORE_NAMESPACE,
            job_id,
            {
                "pid": proc.pid,
                "command": command,
                "started_at": started_at,
                "log_path": None,  # pipes not redirected — see docstring
                "auto_backgrounded": True,
            },
        )
        return job_id
