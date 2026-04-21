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

import json
import os
import re
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Iterable, Optional

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


def _read_config_file(path: Path) -> tuple[dict, Optional[str]]:
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


def check_authorization(
    operation: str,
    package: str,
    version: str,
    transcript_text: str = "",
    askuser_answers: Optional[Iterable[str]] = None,
    config: Optional[AuthorizationConfig] = None,
) -> tuple[bool, str]:
    """Check whether the user has authorized this publish operation.

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

    required = format_required_string(package, version)
    matcher = _compile_match_regex(required)

    if cfg.mode == AuthorizationMode.STRING_MATCH:
        if transcript_text and matcher.search(transcript_text):
            return True, ""
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
            return True, ""
        for ans in answers:
            if ans and matcher.search(ans):
                return True, ""
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


__all__ = [
    "AuthorizationMode",
    "AuthorizationConfig",
    "PUBLISH_PATTERNS",
    "detect_publish_operation",
    "load_config",
    "format_required_string",
    "check_authorization",
    "describe_current_config",
]


if __name__ == "__main__":  # pragma: no cover — CLI smoke path
    print(describe_current_config(), file=sys.stderr)
