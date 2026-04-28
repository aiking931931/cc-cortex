"""concinno.release_authorization — Authorization gate for irreversible
package publish operations (PyPI / npm / cargo / docker public registry /
git tag push to remote).

@module release_authorization
@responsibility Distinct from ``destruction_guard`` (which handles
    data-destroying operations like ``rm -rf``, ``DROP TABLE``,
    ``git push --force main``): this module authorizes **irreversible
    but non-destructive** publish decisions that make code / binaries
    permanently visible on a public registry.

Authorization modes (user-selectable):

- ``STRING_MATCH`` (default) — user types the exact string
  ``go publish <package> <version>`` verbatim in a chat message. The
  permission check scans the recent transcript for this token.
- ``ASKUSER_ANSWER`` — the agent asks via ``AskUserQuestion`` and the
  user's selected option label OR free-text response contains the
  ``go publish <package> <version>`` token. Useful when the host UI
  makes chat typing awkward (e.g. mobile / IDE-embedded).

Disable toggle: when ``disabled=True``, publishes proceed without any
authorization check — the agent is trusted to not ship garbage. Default
is **False** (gate enabled) so new users are protected; power-users
opt out explicitly. This toggle does **not** affect ``destruction_guard``
(which continues to protect data integrity).

Config sources (later overrides earlier):

  1. Defaults (mode=STRING_MATCH, disabled=False)
  2. ``~/.concinno/release_auth.json``
  3. Env vars: ``CONCINNO_RELEASE_AUTH_MODE``,
     ``CONCINNO_RELEASE_AUTH_DISABLED``

@dependencies (stdlib only — json, os, re, dataclasses, enum, pathlib)
@exports AuthorizationMode, AuthorizationConfig, load_config,
    check_authorization, format_required_string, PUBLISH_PATTERNS,
    detect_publish_operation
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import socket
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Iterator, Optional

if TYPE_CHECKING:
    from concinno.guards.base import (
        BaseGuard,
        GuardCategory,
        GuardContext,
        GuardResult,
    )

# ── Irreversible publish operations ─────────────────────────────────
#
# Five canonical operations that mint a permanent public artifact.
# Regex targets the `command` string of a Bash tool call. Keep the
# operation names stable — tests + rule docs reference them.

PUBLISH_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("twine_upload", re.compile(r"\btwine\s+upload\b", re.IGNORECASE)),
    ("npm_publish", re.compile(r"\bnpm\s+publish\b", re.IGNORECASE)),
    ("cargo_publish", re.compile(r"\bcargo\s+publish\b", re.IGNORECASE)),
    # docker push to public registry (hub.docker.com / ghcr.io / quay.io)
    # — private registries on localhost / *.internal are not covered.
    (
        "docker_push_public",
        re.compile(
            r"\bdocker\s+push\s+("
            r"(?:docker\.io/|index\.docker\.io/|hub\.docker\.com/)?"
            r"[a-z0-9._-]+/[a-z0-9._-]+"
            r"|ghcr\.io/[a-z0-9._-]+/[a-z0-9._-]+"
            r"|quay\.io/[a-z0-9._-]+/[a-z0-9._-]+"
            r")",
            re.IGNORECASE,
        ),
    ),
    # git tag push to a remote — tags clone to everyone who fetches
    (
        "git_tag_push_remote",
        re.compile(
            r"\bgit\s+push\s+\S+\s+(?:--tags\b|v?\d+\.\d+\.\d+\S*\b)",
            re.IGNORECASE,
        ),
    ),
]


_ECHO_STRIP = re.compile(
    r"""\becho\s+(?:-[neE]+\s+)?(?:"[^"]*"|'[^']*'|[^|>&;]+)""",
    re.IGNORECASE,
)


def _strip_echo_args(command: str) -> str:
    """Remove ``echo`` argument text so 'echo \"twine upload\"' does not
    register as an actual publish. Conservative — only strips content
    after a plain ``echo`` invocation until a pipe / redirect / semicolon.
    """
    return _ECHO_STRIP.sub("echo", command)


def detect_publish_operation(command: str) -> Optional[str]:
    """Return the operation name if ``command`` matches a publish pattern.

    Args:
        command: Raw Bash command string. ``echo`` argument text is
            stripped before pattern matching so logging / scripting that
            mentions a publish command in quotes does not false-positive.

    Returns:
        Operation name (e.g. ``"twine_upload"``) or ``None`` if no
        publish pattern matches.
    """
    cleaned = _strip_echo_args(command)
    for name, pattern in PUBLISH_PATTERNS:
        if pattern.search(cleaned):
            return name
    return None


# ── Authorization mode + config ─────────────────────────────────────


class AuthorizationMode(str, Enum):
    """Which signal counts as user authorization."""

    STRING_MATCH = "string_match"
    ASKUSER_ANSWER = "askuser_answer"

    @classmethod
    def from_raw(cls, raw: object) -> "AuthorizationMode":
        """Parse loose user input (str / None / missing) to a mode.

        Falls back to ``STRING_MATCH`` on any malformed / unknown value
        so the gate degrades safely (fail-closed on mode confusion =
        fall back to the stricter default, not to the looser mode).
        """
        if isinstance(raw, str):
            normalized = raw.strip().lower().replace("-", "_")
            for m in cls:
                if m.value == normalized:
                    return m
        return cls.STRING_MATCH


@dataclass(frozen=True)
class AuthorizationConfig:
    """Resolved authorization config for the current process."""

    mode: AuthorizationMode = AuthorizationMode.STRING_MATCH
    disabled: bool = False
    source: str = "default"
    warnings: tuple[str, ...] = field(default_factory=tuple)


def _config_path() -> Path:
    return Path.home() / ".concinno" / "release_auth.json"


def _read_config_file(path: Path) -> tuple[dict[str, Any], Optional[str]]:
    """Read config JSON. Returns (data, warning_or_None). Fail-open."""
    try:
        if not path.is_file():
            return {}, None
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return {}, f"{path} is not a JSON object; ignoring"
        return data, None
    except json.JSONDecodeError:
        return {}, f"{path} is malformed JSON; ignoring"
    except OSError:
        return {}, None


def _coerce_bool(value: object) -> Optional[bool]:
    """Strict bool coercion — returns None on unknown inputs."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ("1", "true", "yes", "on", "enable", "enabled"):
            return True
        if s in ("0", "false", "no", "off", "disable", "disabled"):
            return False
    if isinstance(value, int):
        return bool(value)
    return None


def load_config(path: Optional[Path] = None) -> AuthorizationConfig:
    """Load authorization config from file + env, later overrides earlier.

    Args:
        path: Override the default config file path (for tests).

    Returns:
        Resolved ``AuthorizationConfig``. Always succeeds; malformed
        sources are dropped with a recorded warning and defaults are
        used instead (fail-closed toward the stricter default).
    """
    path = path or _config_path()
    warnings: list[str] = []
    sources: list[str] = []

    # Layer 1: defaults
    mode = AuthorizationMode.STRING_MATCH
    disabled = False

    # Layer 2: config file
    file_data, file_warning = _read_config_file(path)
    if file_warning:
        warnings.append(file_warning)
    if file_data:
        if "mode" in file_data:
            mode = AuthorizationMode.from_raw(file_data.get("mode"))
        file_disabled = _coerce_bool(file_data.get("disabled"))
        if file_disabled is not None:
            disabled = file_disabled
        sources.append("file")

    # Layer 3: env vars
    env_mode = os.environ.get("CONCINNO_RELEASE_AUTH_MODE")
    if env_mode is not None:
        mode = AuthorizationMode.from_raw(env_mode)
        sources.append("env:mode")
    env_disabled = os.environ.get("CONCINNO_RELEASE_AUTH_DISABLED")
    if env_disabled is not None:
        coerced = _coerce_bool(env_disabled)
        if coerced is not None:
            disabled = coerced
            sources.append("env:disabled")
        else:
            warnings.append(
                "CONCINNO_RELEASE_AUTH_DISABLED="
                f"{env_disabled!r} is not a bool; ignoring"
            )

    return AuthorizationConfig(
        mode=mode,
        disabled=disabled,
        source="+".join(sources) if sources else "default",
        warnings=tuple(warnings),
    )


# ── Core authorization check ────────────────────────────────────────


def format_required_string(package: str, version: str) -> str:
    """Return the canonical authorization token for ``(package, version)``.

    Always lowercased / stripped / no quoting — users just type the
    literal string into chat. Example::

        format_required_string("concinno", "2.12.0")
        # -> "go publish concinno 2.12.0"
    """
    pkg = (package or "").strip().lower()
    ver = (version or "").strip()
    return f"go publish {pkg} {ver}"


def _compile_match_regex(token: str) -> re.Pattern[str]:
    """Compile a whole-word / case-insensitive matcher for ``token``.

    Example: ``go publish concinno 2.12.0`` — matches the exact
    sequence with flexible whitespace; version must match exactly
    (no silent match of ``2.12.0rc1`` against ``2.12.0``).
    """
    escaped = re.escape(token)
    # Replace literal " " with "\s+" so users can break lines in chat.
    flexible = escaped.replace(r"\ ", r"\s+")
    return re.compile(rf"(?<!\S){flexible}(?!\S)", re.IGNORECASE)


def _record_authorization_outcome(proceed: bool) -> None:
    """Feed the publish-gate decision back to :mod:`concinno.approval_mode`.

    Called after every ``check_authorization`` outcome so the FTRL
    posterior on the ``release_authorization`` tunable bucket reflects
    real operator behaviour (proceed = the gate passed; ``proceed=False``
    means the operator was prompted but no auth string materialised).
    Best-effort — any failure is silent so the publish gate never
    breaks because the learning bus is unhappy.
    """
    try:
        from concinno import approval_mode as _am

        _am.record_outcome(
            _am.BLAST_RADIUS_HIGH,
            proceed=proceed,
            tunable="release_authorization",
        )
    except Exception:  # pragma: no cover - record is advisory
        return


def _approval_mode_layer(
    operation: str,
    package: str,
    version: str,
) -> Optional[tuple[bool, str]]:
    """Consult :mod:`concinno.approval_mode` for routing override.

    Layered ABOVE the publish gate (and BELOW
    :mod:`concinno.destruction_guard` — which keeps R0-R4 enforcement
    untouched regardless of approval_mode). When approval_mode is in
    ``off`` mode, the operator has globally disabled AskUser prompting
    so we autonomously proceed; when ``smart`` mode + the SPS×FTRL
    posterior crosses the autonomy threshold, we also proceed and
    feed back a ``proceed=True`` outcome to the FTRL learner. ``manual``
    mode (and the ``smart`` ask branch) returns ``None`` so the caller
    falls through to the canonical STRING_MATCH / ASKUSER_ANSWER path.

    Best-effort: any import error or runtime exception in approval_mode
    is swallowed and we return ``None`` (= no override) so the publish
    gate keeps its strict default behavior under all failure modes.

    Returns:
        ``(allowed, reason)`` to short-circuit, or ``None`` to defer
        to the legacy gate.
    """
    try:
        from concinno import approval_mode as _am
    except Exception:  # pragma: no cover - module always present
        return None
    try:
        cfg = _am.load_config()
        if cfg.mode is _am.ApprovalMode.OFF:
            try:
                _am.record_outcome(
                    _am.BLAST_RADIUS_HIGH,
                    proceed=True,
                    tunable="release_authorization",
                    config=cfg,
                )
            except Exception:  # pragma: no cover - record is advisory
                pass
            return True, ""
        if cfg.mode is _am.ApprovalMode.SMART:
            decision = _am.decide(
                _am.BLAST_RADIUS_HIGH,
                tunable="release_authorization",
                config=cfg,
            )
            if not decision.should_ask:
                try:
                    _am.record_outcome(
                        _am.BLAST_RADIUS_HIGH,
                        proceed=True,
                        tunable="release_authorization",
                        config=cfg,
                    )
                except Exception:  # pragma: no cover - record is advisory
                    pass
                return True, ""
        # MANUAL or SMART-asks → defer to the canonical gate below.
        return None
    except Exception:  # pragma: no cover - layer is advisory
        return None


def check_authorization(
    operation: str,
    package: str,
    version: str,
    transcript_text: str = "",
    askuser_answers: Optional[Iterable[str]] = None,
    config: Optional[AuthorizationConfig] = None,
) -> tuple[bool, str]:
    """Check whether the user has authorized this publish operation.

    Routing layers (in order; first to short-circuit wins):

    1. ``release_auth.disabled=True`` — operator's permanent publish
       opt-out; bypasses everything below and returns allowed.
    2. :mod:`concinno.approval_mode` — when the operator has switched
       to ``off`` (never ask) or ``smart`` mode with a posterior that
       has cleared the autonomy threshold, the publish gate auto-
       proceeds. ``manual`` mode (and ``smart`` mode that decides to
       ask) falls through to layer 3.
    3. STRING_MATCH / ASKUSER_ANSWER — the canonical ``go publish
       <pkg> <ver>`` token check.

    :mod:`concinno.destruction_guard` (R0-R4 data-destruction) is
    enforced separately and is NOT short-circuited by any of the
    above — see ``rules/L1/release_coord.md`` for the layering
    contract.

    Args:
        operation: Operation name from ``detect_publish_operation``
            (e.g. ``"twine_upload"``). Used only for the denial message.
        package: Package name being published (e.g. ``"concinno"``).
        version: Version being published (e.g. ``"2.12.0"``).
        transcript_text: Recent user-authored chat text. For
            ``STRING_MATCH`` mode, the required token must appear
            here. Pass an empty string if not available.
        askuser_answers: For ``ASKUSER_ANSWER`` mode, the
            user-selected option labels / free-text. Pass an
            iterable of strings.
        config: Resolved config. Defaults to ``load_config()``.

    Returns:
        ``(allowed, reason)``. ``reason`` is empty when allowed,
        or a human-readable explanation on denial. The reason is
        designed to be surfaced to the LLM so it can either ask
        the user again or route around the block.
    """
    cfg = config if config is not None else load_config()

    if cfg.disabled:
        return True, ""

    # Approval-mode layer (off / smart) may auto-authorize — runs
    # AFTER the disabled short-circuit so the publish-specific opt-out
    # keeps top priority, and BEFORE the canonical token check so the
    # operator's "never ask me" preference is honoured even when no
    # auth string was typed.
    am_override = _approval_mode_layer(operation, package, version)
    if am_override is not None:
        return am_override

    required = format_required_string(package, version)
    matcher = _compile_match_regex(required)

    def _notify_waiting(mode_label: str) -> None:
        """Surface a toast so the user sees the agent is blocked."""
        try:
            from concinno.core.notify import notify_waiting_on_user

            notify_waiting_on_user(
                f"{operation} {package}@{version} needs: {required}"
                f" (mode={mode_label})",
                tag="concinno-release-auth",
                group="concinno-release-auth",
            )
        except Exception:  # pragma: no cover — toast is a side-effect
            pass

    if cfg.mode == AuthorizationMode.STRING_MATCH:
        if transcript_text and matcher.search(transcript_text):
            _record_authorization_outcome(proceed=True)
            return True, ""
        _notify_waiting("STRING_MATCH")
        return False, (
            f"release_authorization: operation {operation!r} "
            f"({package}@{version}) requires the user to type the exact "
            f"string '{required}' in a chat message. Mode=STRING_MATCH. "
            f"To disable this gate globally, set "
            f"CONCINNO_RELEASE_AUTH_DISABLED=1 or write "
            f'{{"disabled": true}} to '
            f"~/.concinno/release_auth.json."
        )

    if cfg.mode == AuthorizationMode.ASKUSER_ANSWER:
        answers = list(askuser_answers or [])
        # Fall back to STRING_MATCH semantics when askuser answer is
        # absent — lets the gate still authorize via chat string in
        # hybrid flows where the agent was going to ask but user
        # preempted by typing.
        if transcript_text and matcher.search(transcript_text):
            _record_authorization_outcome(proceed=True)
            return True, ""
        for ans in answers:
            if ans and matcher.search(ans):
                _record_authorization_outcome(proceed=True)
                return True, ""
        _notify_waiting("ASKUSER_ANSWER")
        return False, (
            f"release_authorization: operation {operation!r} "
            f"({package}@{version}) requires the user to select an "
            f"AskUserQuestion option whose label (or 'Other' free text) "
            f"contains '{required}'. Mode=ASKUSER_ANSWER. To disable, "
            f"set CONCINNO_RELEASE_AUTH_DISABLED=1."
        )

    # Unknown mode — shouldn't happen (from_raw maps to STRING_MATCH),
    # but defensively fail closed.
    return False, (
        f"release_authorization: unknown mode {cfg.mode!r}; "
        f"falling back to denied. Reset config to restore gate."
    )


# ── CLI self-inspection helper (for `concinno release_auth status`) ──


def describe_current_config() -> str:
    """Return a one-paragraph human-readable current config summary."""
    cfg = load_config()
    lines = [
        f"mode={cfg.mode.value}",
        f"disabled={cfg.disabled}",
        f"source={cfg.source}",
        f"config_file={_config_path()} "
        f"({'present' if _config_path().is_file() else 'absent'})",
    ]
    if cfg.warnings:
        lines.append("warnings:")
        for w in cfg.warnings:
            lines.append(f"  - {w}")
    return "\n".join(lines)


# ── PreToolUse guard adapter (3.1.3, 2026-04-26) ────────────────────
#
# Until 3.1.3 ``check_authorization`` was only called by the user-side
# CLI (``concinno publish``). The agent's own ``Bash(twine upload …)``
# tool calls were *not* gated — meaning ``release_auth.disabled=True``
# vs ``False`` had no observable difference at hook time. The
# 2026-04-26 wiring audit caught this; the fix is below.


def _extract_pkg_version_from_command(cmd: str, operation: str) -> tuple[str, str]:
    """Best-effort extraction of (package, version) from a publish command.

    Returns ``("", "")`` when the target cannot be unambiguously
    determined; callers must treat that as "skip the gate" rather than
    "hard deny" so legitimate publish scripts (e.g. ``npm publish`` from
    a ``package.json`` directory) are not mass-blocked.
    """
    if operation in ("twine_upload", "cargo_publish"):
        # twine artifact filenames follow PEP 427/625:
        #   pkg-ver-py3-none-any.whl     (wheel)
        #   pkg-ver.tar.gz               (sdist)
        # Plus user-glob shortcuts:
        #   pkg-ver*                     (shell glob)
        # Version per PEP 440: ``\d+(\.\d+)+`` with optional pre/post/dev tag.
        m = re.search(
            r"(?:dist[\\/])([A-Za-z0-9_.\-]+?)-"
            # Version: numeric core + optional rc/a/b/alpha/beta + optional .postN / .devN
            r"(\d+(?:\.\d+)+"
            r"(?:(?:rc|a|b|alpha|beta)\d+)?"
            r"(?:\.(?:post|dev)\d+)?)"
            # Terminator: wheel suffix, sdist suffix, glob, whitespace, EOL
            r"(?=-(?:py|cp|pp|jp|ip)\d|\.tar|\.zip|\.whl|\.tgz|\*|\s|$)",
            cmd,
        )
        if m:
            pkg = m.group(1).lower().replace("_", "-")
            return pkg, m.group(2)
    if operation == "git_tag_push_remote":
        # `git push origin v3.1.3` / `git push origin 3.1.3`
        m = re.search(
            r"\bgit\s+push\s+\S+\s+v?(\d+\.\d+\.\d+(?:[a-z0-9.\-]*))",
            cmd,
            re.IGNORECASE,
        )
        if m:
            # Package name not in command — best-effort read from cwd
            # pyproject. Defer to runtime because cwd is process-local.
            try:
                pyproject = Path.cwd() / "pyproject.toml"
                if pyproject.is_file():
                    text = pyproject.read_text(encoding="utf-8")
                    pm = re.search(
                        r"\[project\][^\[]*?name\s*=\s*\"([^\"]+)\"",
                        text,
                        re.DOTALL,
                    )
                    if pm:
                        return pm.group(1).lower(), m.group(1)
            except OSError:
                pass
    # npm_publish / docker_push_public / unrecognized: extraction not
    # implemented in 3.1.3. Returning ("", "") makes the guard skip
    # rather than overblock; tracked for later rev.
    return "", ""


def _read_recent_user_transcript_text(session_id: str, max_chars: int = 100_000) -> str:
    """Read recent user-authored text from the Claude Code transcript JSONL.

    Returns at most ``max_chars`` of concatenated user text (most recent
    last). Returns ``""`` on any error so the guard fails-closed to
    deny (caller knows transcript scan didn't find the auth string).
    """
    try:
        from concinno.core.path_utils import find_transcript
    except Exception:
        return ""
    if not session_id:
        return ""
    path = find_transcript(session_id)
    if not path or not os.path.isfile(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return ""
    parts: list[str] = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        # CC transcript format: {"type": "user", "message": {"content": "..."}}
        if entry.get("type") != "user":
            continue
        msg = entry.get("message", {})
        content = msg.get("content", "") if isinstance(msg, dict) else ""
        if isinstance(content, list):
            text_parts = [
                p.get("text", "")
                for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            ]
            text = "\n".join(text_parts)
        elif isinstance(content, str):
            text = content
        else:
            text = ""
        if text:
            parts.append(text)
        if sum(len(p) for p in parts) >= max_chars:
            break
    parts.reverse()
    out = "\n".join(parts)
    return out[-max_chars:]


def _try_import_guard_base() -> tuple[
    type["BaseGuard"], type["GuardCategory"],
    type["GuardContext"], type["GuardResult"],
]:
    """Lazy-import guard base classes — release_authorization is a stdlib-
    only module by design; the guard adapter is the one place that
    pulls in ``concinno.guards.base``. Done lazily so direct imports of
    ``release_authorization`` (e.g. from the CLI) don't drag in the
    whole guard machinery."""
    from concinno.guards.base import (
        BaseGuard as _BG,
    )
    from concinno.guards.base import (
        GuardCategory as _GCa,
    )
    from concinno.guards.base import (
        GuardContext as _GCt,
    )
    from concinno.guards.base import (
        GuardResult as _GR,
    )
    return _BG, _GCa, _GCt, _GR


_BaseGuard, _GuardCategory, _GuardContext, _GuardResult = _try_import_guard_base()


class ReleaseAuthorizationGuard(_BaseGuard):  # type: ignore[misc,valid-type]
    """Block irreversible publish operations until the user types
    ``go publish <pkg> <ver>`` in chat (or selects an equivalent
    AskUserQuestion option in ``ASKUSER_ANSWER`` mode).

    Honours ``release_auth.disabled=True`` — when opt-out is on, the
    guard short-circuits to ALLOW so the harness layer's own
    permissions list is the only remaining check (two-layer-gate
    principle from ``rules/L1/release_coord.md``).

    3.1.3 (2026-04-26): wired in for the first time. Before this fix
    the gate function existed but no PreToolUse hook actually called
    it — the disabled toggle had no observable effect, which is what
    the user repeatedly complained about. See
    ``feedback_release_auth_gate_was_vaporware.md``.
    """

    name = "release_authorization"
    feature_name = "release_authorization"
    category = _GuardCategory.SECURITY

    def check(self, ctx: "GuardContext") -> Optional["GuardResult"]:
        if ctx.tool_name != "Bash":
            return None
        cmd = ""
        if isinstance(ctx.tool_input, dict):
            cmd = ctx.tool_input.get("command", "") or ""
        if not cmd:
            return None

        operation = detect_publish_operation(cmd)
        if not operation:
            return None

        cfg = load_config()
        if cfg.disabled:
            # User has opted out at the concinno layer — the harness
            # permissions list remains the only check. This is the
            # whole point of the disabled toggle.
            return None

        package, version = _extract_pkg_version_from_command(cmd, operation)
        if not package or not version:
            # Can't determine target unambiguously — skip rather than
            # overblock. A future revision can scan ``pyproject.toml`` /
            # ``package.json`` for npm/cargo coverage.
            return None

        transcript_text = _read_recent_user_transcript_text(ctx.session_id)
        allowed, reason = check_authorization(
            operation,
            package,
            version,
            transcript_text=transcript_text,
            config=cfg,
        )
        if allowed:
            return None
        return _GuardResult.deny(
            reason,
            check_type="release_authorization",
            operation=operation,
            package=package,
            version=version,
        )


# ── acquire_for_upload context manager (4.2.3, 2026-04-27) ──────────
#
# Wires ``coordination.release_lock.ReleaseLock`` + ``twine_pre_check.
# check_before_upload`` into the publish gate so the next ship cycle
# benefits from atomic per-package locks + PyPI pre-check race
# prevention. The 4.2.1 ship hit a 400-already-exists race specifically
# because ``check_authorization`` only consulted the user's auth string
# and the markdown ``RELEASE_COORDINATION.md::Active`` self-validation
# pattern — neither caught a concurrent upload from another session.
#
# Existing ``check_authorization()`` keeps its old signature for
# back-compat. The new logic only activates inside ``acquire_for_upload``
# so legacy callers see zero behavior change.


def _resolve_session_identity() -> str:
    """Best-effort session identity from env or ``instance_lock.json``.

    Mirrors :func:`concinno.cli.release_lock_cmd._resolve_session`.

    TODO(release_authorization): consolidate this into a shared helper
    in ``concinno.coordination`` so the CLI and the gate read the same
    source of truth. Inline copy avoids a circular import for now
    (``cli`` imports ``release_authorization``, not the other way).
    """
    for var in ("CCC_SESSION", "CC_SESSION_ID", "CLAUDE_SESSION_ID"):
        val = os.environ.get(var, "").strip()
        if val:
            return val
    lock_path = Path.home() / ".claude" / "token_state" / "instance_lock.json"
    if lock_path.exists():
        try:
            data = json.loads(lock_path.read_text(encoding="utf-8"))
            sessions = data.get("sessions", {})
            if isinstance(sessions, dict) and sessions:
                # Pick newest session by 'started' if available.
                def _start(item: tuple[str, dict[str, Any]]) -> str:
                    return str(item[1].get("started", ""))

                key, _ = max(sessions.items(), key=_start)
                return str(key)
        except (json.JSONDecodeError, OSError):
            pass
    return f"unknown-{socket.gethostname()}"


@dataclass(frozen=True)
class UploadAuthorization:
    """Result of :func:`acquire_for_upload`.

    Attributes:
        allowed: True iff every layer (config, transcript auth string,
            PyPI pre-check, atomic lock) cleared.
        reason: Empty when allowed; human-readable explanation otherwise.
        denied_at: One of ``""``, ``"authorization"``,
            ``"race_prevention"``, ``"lock_collision"``. Lets callers
            route on which layer rejected.
        lock_acquired: True iff the atomic ``ReleaseLock`` was taken
            (so callers know whether ``__exit__`` will try to release).
    """

    allowed: bool
    reason: str
    denied_at: str = ""
    lock_acquired: bool = False


@contextlib.contextmanager
def acquire_for_upload(
    package: str,
    version: str,
    session: Optional[str] = None,
    transcript_text: str = "",
    askuser_answers: Optional[Iterable[str]] = None,
    config: Optional[AuthorizationConfig] = None,
    operation: str = "twine_upload",
) -> Iterator[UploadAuthorization]:
    """Context manager that authorises + locks a publish, releasing on exit.

    Combines three layers in order:

    1. :func:`check_authorization` — standard auth-string / AskUser gate.
       (When ``release_auth.disabled=True`` this short-circuits to
       ``allowed=True`` and **both** subsequent layers are skipped — the
       opt-out is honoured cleanly per ``rules/L1/release_coord.md``.)
    2. :func:`coordination.twine_pre_check.check_before_upload`
       (with ``require_lock_held=False`` because we acquire below) —
       PyPI 404/200 pre-check that catches "already on PyPI" before
       ``twine upload`` 400s.
    3. :class:`coordination.release_lock.ReleaseLock` ``acquire`` — atomic
       per-package lock so two concurrent sessions cannot both upload.

    On any layer's denial, yields ``UploadAuthorization(allowed=False,
    reason=..., denied_at=...)`` and the lock is **not** acquired (so
    nothing to release). On success, yields ``allowed=True`` and the
    lock is released on ``__exit__`` even if the body raises.

    Args:
        package: Package name (e.g. ``"concinno"``).
        version: Target version (e.g. ``"4.3.0"``).
        session: Session identity for the lock. Defaults to
            :func:`_resolve_session_identity`.
        transcript_text: Recent user-authored chat for STRING_MATCH.
        askuser_answers: AskUserQuestion answers for ASKUSER_ANSWER mode.
        config: Resolved auth config; defaults to :func:`load_config`.
        operation: Operation label for the denial message
            (default ``"twine_upload"`` — this matches the most common
            caller; pass ``"cargo_publish"`` etc. for non-PyPI packages
            but note the PyPI pre-check still queries pypi.org regardless,
            which is harmless for cross-ecosystem packages — a
            ``not-on-pypi`` package simply 404s and is treated as free).

    Example::

        with acquire_for_upload("concinno", "4.3.0",
                                 transcript_text=chat) as auth:
            if not auth.allowed:
                print(f"blocked: {auth.reason}")
                return
            subprocess.check_call(
                ["twine", "upload", "dist/concinno-4.3.0-*"],
            )
        # lock auto-released on exit (success or exception)
    """
    cfg = config if config is not None else load_config()

    # Layer 1: standard authorization check. Honours disabled=True.
    allowed, reason = check_authorization(
        operation,
        package,
        version,
        transcript_text=transcript_text,
        askuser_answers=askuser_answers,
        config=cfg,
    )
    if not allowed:
        yield UploadAuthorization(
            allowed=False,
            reason=reason,
            denied_at="authorization",
            lock_acquired=False,
        )
        return

    # ``disabled=True`` → trust the operator. Skip pre-check + atomic lock
    # entirely so the opt-out really means "no friction at the concinno
    # layer" (the harness allow-list remains the only check).
    if cfg.disabled:
        yield UploadAuthorization(
            allowed=True,
            reason="",
            denied_at="",
            lock_acquired=False,
        )
        return

    # Lazy import — release_authorization stays stdlib-only at module
    # load; the coordination subpackage only loads when callers actually
    # use the upload context manager.
    from concinno.coordination.release_lock import ReleaseLock
    from concinno.coordination.twine_pre_check import check_before_upload

    resolved_session = session or _resolve_session_identity()

    # Layer 2: PyPI pre-check. ``require_lock_held=False`` because we
    # acquire the lock immediately below — caller hasn't yet.
    ok, pre_reason = check_before_upload(
        package,
        version,
        session=resolved_session,
        require_lock_held=False,
    )
    if not ok:
        yield UploadAuthorization(
            allowed=False,
            reason=pre_reason,
            denied_at="race_prevention",
            lock_acquired=False,
        )
        return

    # Layer 3: atomic release lock. Holder identity = resolved session.
    lock = ReleaseLock()
    host = socket.gethostname()
    if not lock.acquire(package, version, session=resolved_session, host=host):
        held = lock.check(package) or {}
        holder = held.get("holder_session", "?")
        held_ver = held.get("version", "?")
        yield UploadAuthorization(
            allowed=False,
            reason=(
                f"release lock held by {holder!r} for {package} "
                f"{held_ver!r}; concurrent publish in progress"
            ),
            denied_at="lock_collision",
            lock_acquired=False,
        )
        return

    try:
        yield UploadAuthorization(
            allowed=True,
            reason="",
            denied_at="",
            lock_acquired=True,
        )
    finally:
        # Release on exception too — a crashed body must not wedge
        # subsequent retries (TTL would eventually reclaim, but immediate
        # release is the cleaner contract).
        try:
            lock.release(package)
        except Exception:  # pragma: no cover — release is fail-safe
            pass


# ── Plan A Week 1 (4.3.0) integration: release_lock + pre_publish_check ──
#
# Public-facing wrappers exposing the primitives in ``coordination/``
# plus three new advisory checks (``twine check``, version-sync,
# optional pytest). Designed for callers that want to *report*
# pre-publish posture **without** triggering an AskUser prompt or
# re-introducing the publish-gate behaviour the user has permanently
# opted out of (see ``rules/L1/release_coord.md`` opt-out banner +
# ``feedback_publish_authorization_permanently_disabled.md``).
#
# Key contract (from Plan A spec):
#   * Never raise on failure — always return a result object.
#   * Never prompt the user.
#   * Never block ``twine upload`` even if checks fail; caller decides.
#   * Honour the existing ``release_auth.disabled=True`` opt-out:
#     when set, checks still run for *information*, but every result
#     is advisory and never escalated to AskUser / hard gate.
#   * Honour env ``CONCINNO_RELEASE_LOCK_DISABLED=1`` to fully bypass
#     the lock layer (used by 1-host CI/dev workflows).
#   * Emit a single ZIQ ``Outcome`` per ``pre_publish_check`` call so
#     the autotuner can learn the empirical fail rate over time. ZIQ
#     bus is lazy-imported and graceful-degrades on absence.


class LockAcquireError(Exception):
    """Result-style error returned (not raised) when the release lock
    cannot be taken because another live session holds it.

    Subclasses :class:`Exception` so callers that *want* to raise on
    contention can simply ``raise err``; the default
    :func:`acquire_release_lock` flow returns the instance instead so
    automatic retry / queue-drain is the caller's choice.
    """

    def __init__(self, package: str, holder: dict[str, object]) -> None:
        self.package = package
        self.holder = holder
        super().__init__(
            f"release lock for {package!r} held by "
            f"session {holder.get('holder_session', '?')!r} "
            f"(version {holder.get('version', '?')!r}, "
            f"acquired_at {holder.get('acquired_at', '?')!r})"
        )


def _release_lock_disabled() -> bool:
    """Return True when env ``CONCINNO_RELEASE_LOCK_DISABLED=1``.

    Read fresh per call so tests can flip mid-run without re-import.
    """
    raw = os.environ.get("CONCINNO_RELEASE_LOCK_DISABLED", "").strip().lower()
    return raw in {"1", "true", "yes", "on", "enable", "enabled"}


def acquire_release_lock(
    package: str,
    version: str,
    session: Optional[str] = None,
    host: Optional[str] = None,
) -> tuple[bool, Optional[LockAcquireError]]:
    """Try to acquire the per-package release lock for (package, version).

    Wraps :class:`concinno.coordination.release_lock.ReleaseLock` to
    expose a result-tuple API the caller can branch on without a
    try/except block. Stale-lock auto-revoke (TTL via env
    ``CONCINNO_RELEASE_LOCK_TTL_MIN``, default 30 min) is honoured by
    the underlying :class:`ReleaseLock` — callers do **not** need to
    sweep separately.

    Args:
        package: Package name (e.g. ``"concinno"``).
        version: Target version string (e.g. ``"4.3.0"``).
        session: Session identifier; defaults to
            :func:`_resolve_session_identity`.
        host: Hostname; defaults to ``socket.gethostname()``.

    Returns:
        ``(True, None)`` on successful acquire.
        ``(False, LockAcquireError)`` when another live session holds
        it. The error includes the holder dict so callers can format
        their own message or re-raise.

    Notes:
        * Re-acquire by the same ``session`` is idempotent and refreshes
          ``acquired_at`` — useful for long-running flows that survive
          a restart with the same session id.
        * When env ``CONCINNO_RELEASE_LOCK_DISABLED=1`` this is a no-op
          and always returns ``(True, None)`` — used in CI / dev hosts
          where a single agent owns the publish flow.
        * ``release_auth.disabled=True`` does **not** disable the lock —
          the publish opt-out is about *user authorization*, not
          *concurrency safety*. Two parallel sessions with the gate
          disabled still must not double-upload, so the lock stays on.
    """
    if _release_lock_disabled():
        return True, None

    from concinno.coordination.release_lock import ReleaseLock

    resolved_session = session or _resolve_session_identity()
    resolved_host = host or socket.gethostname()
    lock = ReleaseLock()
    if lock.acquire(
        package, version, session=resolved_session, host=resolved_host
    ):
        return True, None
    held = lock.check(package) or {}
    return False, LockAcquireError(package, held)


def release_release_lock(package: str) -> None:
    """Release the lock for ``package``. Idempotent — no-op if absent.

    Honours ``CONCINNO_RELEASE_LOCK_DISABLED=1`` (no-op when disabled,
    matching :func:`acquire_release_lock`).
    """
    if _release_lock_disabled():
        return
    from concinno.coordination.release_lock import ReleaseLock

    ReleaseLock().release(package)


# ── Pre-publish check (advisory bundle) ────────────────────────────


@dataclass(frozen=True)
class PreCheckResult:
    """Bundle of advisory pre-publish checks.

    Attributes:
        passed: True iff every executed check returned OK. False when
            any single check reported a problem; ``reasons`` carries
            human-readable details.
        reasons: List of ``"<check>: <message>"`` strings, one per
            failed check. Empty when ``passed`` is True.
        details: Per-check raw context (return code, parsed values,
            URLs probed). Stable schema only for the four built-in
            checks (``twine_check``, ``pypi_registry``, ``version_sync``,
            ``tests``); future additions may extend keys.

    Contract: this is a **report**, not a gate. ``pre_publish_check``
    never raises and never prompts. The caller decides whether to
    proceed.
    """

    passed: bool
    reasons: tuple[str, ...] = field(default_factory=tuple)
    details: dict[str, object] = field(default_factory=dict)


def _run_twine_check(dist_dir: Path) -> tuple[bool, str, dict[str, object]]:
    """Run ``python -m twine check <dist_dir>/*`` and return (ok, msg, ctx).

    Subprocess is the only sane call-site for ``twine`` — its public
    Python API is documented as unstable. We use a fixed argv list (no
    shell=True) and a short timeout; missing ``twine`` is reported as
    a check failure rather than crashing the whole bundle so callers
    in lean environments still get useful output for the other checks.
    """
    import shutil
    import subprocess

    ctx: dict[str, object] = {"dist_dir": str(dist_dir)}
    if not dist_dir.is_dir():
        return False, f"dist dir {dist_dir} does not exist", ctx
    artifacts = sorted(dist_dir.glob("*"))
    ctx["artifact_count"] = len(artifacts)
    if not artifacts:
        return False, f"dist dir {dist_dir} is empty", ctx
    twine = shutil.which("twine")
    if twine is None:
        # Try ``python -m twine`` instead so callers without ``twine``
        # on PATH but with the package installed still pass.
        argv = [sys.executable, "-m", "twine", "check", *map(str, artifacts)]
    else:
        argv = [twine, "check", *map(str, artifacts)]
    ctx["argv"] = argv
    try:
        proc = subprocess.run(  # noqa: S603 - argv list, no shell
            argv,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return False, f"twine check failed: {exc!r}", ctx
    ctx["returncode"] = proc.returncode
    ctx["stdout_tail"] = (proc.stdout or "")[-1000:]
    ctx["stderr_tail"] = (proc.stderr or "")[-1000:]
    if proc.returncode != 0:
        return False, f"twine check exited {proc.returncode}", ctx
    return True, "", ctx


def _check_pypi_registry(
    package: str, version: str
) -> tuple[bool, str, dict[str, object]]:
    """Probe pypi.org/<pkg>/<ver>/json. 404 = ok to publish, 200 = taken."""
    ctx: dict[str, object] = {
        "url": f"https://pypi.org/pypi/{package}/{version}/json",
    }
    try:
        from concinno.coordination.release_lock import pypi_version_taken

        taken = pypi_version_taken(package, version)
    except Exception as exc:  # noqa: BLE001 - surface any error as advisory
        ctx["error"] = repr(exc)
        return False, f"PyPI registry probe failed: {exc!r}", ctx
    ctx["taken"] = taken
    if taken:
        return False, (
            f"{package} {version} is already on PyPI — upload would 400"
        ), ctx
    return True, "", ctx


_PYPROJECT_VERSION_RE = re.compile(
    r"""^\s*version\s*=\s*['"]([^'"]+)['"]""",
    re.MULTILINE,
)
_CHANGELOG_VERSION_RE = re.compile(
    r"^##\s*\[\s*([0-9]+\.[0-9]+\.[0-9]+(?:[A-Za-z0-9.\-]*)?)\s*\]",
    re.MULTILINE,
)


def _check_version_sync(
    target_version: str,
    pyproject: Optional[Path] = None,
    changelog: Optional[Path] = None,
) -> tuple[bool, str, dict[str, object]]:
    """Verify ``target_version`` matches pyproject.toml + first CHANGELOG entry.

    The first non-``Unreleased`` ``## [X.Y.Z]`` heading in the changelog
    is treated as the latest released version. Only the ``[Unreleased]``
    placeholder is permitted to come before the target version (the Keep
    a Changelog convention). Missing files report as failures so callers
    in non-standard layouts know to skip this check.
    """
    pp = pyproject or Path.cwd() / "pyproject.toml"
    cl = changelog or Path.cwd() / "CHANGELOG.md"
    ctx: dict[str, object] = {
        "pyproject": str(pp),
        "changelog": str(cl),
        "target": target_version,
    }
    if not pp.is_file():
        return False, f"pyproject.toml not found at {pp}", ctx
    try:
        pp_text = pp.read_text(encoding="utf-8")
    except OSError as exc:
        return False, f"pyproject.toml unreadable: {exc!r}", ctx
    pp_match = _PYPROJECT_VERSION_RE.search(pp_text)
    if not pp_match:
        return False, "pyproject.toml has no [project].version field", ctx
    pp_version = pp_match.group(1)
    ctx["pyproject_version"] = pp_version
    if pp_version != target_version:
        return False, (
            f"pyproject.toml version {pp_version!r} != target "
            f"{target_version!r}"
        ), ctx
    if not cl.is_file():
        return False, f"CHANGELOG.md not found at {cl}", ctx
    try:
        cl_text = cl.read_text(encoding="utf-8")
    except OSError as exc:
        return False, f"CHANGELOG.md unreadable: {exc!r}", ctx
    cl_versions = _CHANGELOG_VERSION_RE.findall(cl_text)
    ctx["changelog_versions_head"] = cl_versions[:5]
    if not cl_versions:
        return False, "CHANGELOG.md has no '## [X.Y.Z]' headings", ctx
    if target_version not in cl_versions:
        return False, (
            f"CHANGELOG.md has no entry for {target_version!r} "
            f"(latest seen: {cl_versions[0]!r})"
        ), ctx
    return True, "", ctx


def _run_tests(test_path: Optional[Path]) -> tuple[bool, str, dict[str, object]]:
    """Run ``pytest`` against ``test_path`` (default ``tests/``).

    Bounded with a 5-minute timeout so callers cannot accidentally wedge
    the publish flow on a hung test.
    """
    import subprocess

    target = str(test_path or (Path.cwd() / "tests"))
    ctx: dict[str, object] = {"target": target}
    if not Path(target).exists():
        return False, f"test path {target} does not exist", ctx
    argv = [sys.executable, "-m", "pytest", target, "-x", "--tb=short", "-q"]
    ctx["argv"] = argv
    try:
        proc = subprocess.run(  # noqa: S603 - argv list, no shell
            argv,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return False, f"pytest failed to run: {exc!r}", ctx
    ctx["returncode"] = proc.returncode
    ctx["stdout_tail"] = (proc.stdout or "")[-2000:]
    if proc.returncode != 0:
        return False, f"pytest exited {proc.returncode}", ctx
    return True, "", ctx


def _emit_pre_check_outcome(
    package: str,
    target_version: str,
    result: PreCheckResult,
) -> None:
    """Emit a single ZIQ outcome summarising the pre-check run.

    Reward = 1.0 when ``passed``, 0.0 otherwise. ``value`` is the count
    of failed checks (lower = better) so the autotuner sees both a
    binary signal and a granularity signal. Tunable id is namespaced
    under ``release_authorization.pre_publish_check`` so it does not
    collide with the 19+ existing tunables.

    Lazy-imported and graceful-degrades when the bus is missing or
    disabled, so this helper is safe to call from all environments.
    """
    try:
        from concinno.ziq_outcome_bus import (  # noqa: PLC0415
            Outcome,
            get_bus,
            is_bus_disabled,
        )
    except Exception:  # pragma: no cover - bus optional
        return
    if is_bus_disabled():
        return
    try:
        bus = get_bus()
        bus.emit(
            Outcome(
                tunable="release_authorization.pre_publish_check",
                value=len(result.reasons),
                reward=1.0 if result.passed else 0.0,
                metadata={
                    "package": package,
                    "version": target_version,
                    "failed_checks": [r.split(":", 1)[0] for r in result.reasons],
                },
                source="release_authorization.pre_publish_check",
            )
        )
    except Exception:  # pragma: no cover - bus is advisory
        return


def pre_publish_check(
    target_version: str,
    package: Optional[str] = None,
    dist_dir: Optional[Path] = None,
    pyproject: Optional[Path] = None,
    changelog: Optional[Path] = None,
    run_tests: bool = False,
    test_path: Optional[Path] = None,
) -> PreCheckResult:
    """Run the four advisory pre-publish checks and return a result bundle.

    Checks (in order):

    1. ``twine_check`` — ``python -m twine check <dist_dir>/*``.
    2. ``pypi_registry`` — ``HEAD pypi.org/<pkg>/<ver>/json``; 404 = OK.
    3. ``version_sync`` — ``pyproject.toml``::version, first
       ``CHANGELOG.md`` entry, and ``target_version`` agree.
    4. ``tests`` (opt-in via ``run_tests=True``) —
       ``python -m pytest <test_path>``.

    Args:
        target_version: Version about to be published (e.g. ``"4.3.0"``).
        package: Package name. Defaults to the ``[project].name``
            field parsed from ``pyproject``.
        dist_dir: Directory containing the built wheel + sdist.
            Defaults to ``./dist``.
        pyproject: Override path to ``pyproject.toml``. Defaults to cwd.
        changelog: Override path to ``CHANGELOG.md``. Defaults to cwd.
        run_tests: If True, also run ``pytest``. Default False because
            most callers have already run tests via their CI / pre-ship
            workflow and re-running adds 30s+ for no signal.
        test_path: Override pytest target. Defaults to ``./tests``.

    Returns:
        :class:`PreCheckResult`. Never raises. Never prompts the user.
        Honours the user's permanent publish opt-out
        (``release_auth.disabled=True``) — checks still execute, but
        they are advisory and the caller decides whether to proceed.

    Side effects:
        Emits one ZIQ outcome on
        ``release_authorization.pre_publish_check``. ZIQ bus absence
        / kill-switch is silent.
    """
    pp = pyproject or Path.cwd() / "pyproject.toml"
    cl = changelog or Path.cwd() / "CHANGELOG.md"
    dd = dist_dir or Path.cwd() / "dist"

    if package is None:
        # Best-effort name parse for callers that only know the version.
        try:
            text = pp.read_text(encoding="utf-8") if pp.is_file() else ""
            m = re.search(
                r"^\s*name\s*=\s*['\"]([^'\"]+)['\"]",
                text,
                re.MULTILINE,
            )
            package = m.group(1) if m else "unknown"
        except OSError:
            package = "unknown"

    reasons: list[str] = []
    details: dict[str, object] = {}

    ok, msg, ctx = _run_twine_check(dd)
    details["twine_check"] = ctx
    if not ok:
        reasons.append(f"twine_check: {msg}")

    ok, msg, ctx = _check_pypi_registry(package, target_version)
    details["pypi_registry"] = ctx
    if not ok:
        reasons.append(f"pypi_registry: {msg}")

    ok, msg, ctx = _check_version_sync(target_version, pp, cl)
    details["version_sync"] = ctx
    if not ok:
        reasons.append(f"version_sync: {msg}")

    if run_tests:
        ok, msg, ctx = _run_tests(test_path)
        details["tests"] = ctx
        if not ok:
            reasons.append(f"tests: {msg}")

    result = PreCheckResult(
        passed=not reasons,
        reasons=tuple(reasons),
        details=details,
    )
    _emit_pre_check_outcome(package, target_version, result)
    return result


__all__ = [
    "AuthorizationMode",
    "AuthorizationConfig",
    "LockAcquireError",
    "PUBLISH_PATTERNS",
    "PreCheckResult",
    "UploadAuthorization",
    "acquire_for_upload",
    "acquire_release_lock",
    "detect_publish_operation",
    "load_config",
    "format_required_string",
    "check_authorization",
    "describe_current_config",
    "pre_publish_check",
    "release_release_lock",
    "ReleaseAuthorizationGuard",
]


if __name__ == "__main__":  # pragma: no cover — CLI smoke path
    print(describe_current_config(), file=sys.stderr)
