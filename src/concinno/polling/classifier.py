"""concinno.polling.classifier — detect "agent is waiting on X" patterns.

The classifier inspects a ``PostToolUse`` payload (tool_name +
tool_input) and decides:

1. Whether this tool call implies a wait state (returns ``None`` if not).
2. What ``kind`` of wait it is (used for UX labels + ETA defaults).
3. What ``check_cmd`` will tell the daemon "is it done yet?".
4. A reasonable ``eta_seconds`` estimate so the inject hook can show
   "X expected to finish in N min".

Detection patterns ordered most-specific → most-generic so a
``Bash(twine upload)`` invocation classifies as ``upload``, not
``bash_background``.

Conservative on purpose — false negatives (skip a real wait) are
better than false positives (spam the queue with bogus checks). The
classifier defers to the agent's own judgement: if the agent
explicitly calls ``concinno.polling.register_wait`` from inside a
script, that wins over heuristic detection.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

# ── Detection result ──────────────────────────────────────────────────


@dataclass
class WaitClassification:
    kind: str                  # human-readable category
    check_cmd: str             # bash command to verify status
    eta_seconds: int           # caller-supplied ETA estimate
    pid: Optional[int] = None  # when known (e.g. background bash)


# ── Pattern table ─────────────────────────────────────────────────────
#
# Each entry: (regex pattern, kind label, default ETA seconds).
# Patterns are designed to be matched against the LEADING token of a
# shell segment (via ``pat.match``), not searched anywhere in the
# command — that's how we avoid the false positive where a commit
# message like ``git commit -m "twine upload dist/foo"`` would
# masquerade as an actual twine invocation.
_BASH_PATTERNS: list[tuple[re.Pattern[str], str, int]] = [
    # PyPI / npm / cargo publish — irreversible upload, expect 30s-2min
    (re.compile(r"twine\s+upload\b", re.IGNORECASE), "upload", 180),
    (re.compile(r"npm\s+publish\b", re.IGNORECASE), "upload", 180),
    (re.compile(r"cargo\s+publish\b", re.IGNORECASE), "upload", 240),
    # Docker push — large layer transfer, can run several minutes
    (re.compile(r"docker\s+push\b", re.IGNORECASE), "upload", 600),
    # File transfer (assume LAN/WAN mix)
    (re.compile(r"scp\b", re.IGNORECASE), "upload", 300),
    (re.compile(r"rsync\b", re.IGNORECASE), "upload", 300),
    # GitHub release upload
    (re.compile(r"gh\s+release\s+upload\b", re.IGNORECASE), "upload", 240),
    # CI watch — gh waits on PR / workflow checks
    (re.compile(r"gh\s+(?:pr\s+checks|run\s+watch)\b", re.IGNORECASE), "ci_check", 600),
    # Deploy
    (re.compile(r"(?:python\s+)?deploy\.py\b", re.IGNORECASE), "deploy", 600),
    (re.compile(r"ansible-playbook\b", re.IGNORECASE), "deploy", 600),
    # Long-running build / install
    (re.compile(r"npm\s+install\b", re.IGNORECASE), "long_op", 300),
    (re.compile(r"cargo\s+build(?:\s+--release)?\b", re.IGNORECASE), "long_op", 600),
    (re.compile(r"pytest\b.*\b--timeout\b", re.IGNORECASE), "long_op", 1800),
    # ``pytest --runslow`` toggles slow-marked tests (common idiom across
    # Concinno + many OSS Python repos); 4-trigger expansion 2026-04-27.
    # NB: ``\b`` before ``--`` does NOT match (space + dash are both
    # non-word), so we anchor on whitespace then literal ``--flag``.
    (re.compile(r"pytest\b.*\s--runslow\b", re.IGNORECASE), "long_op", 1800),
    (re.compile(r"git\s+clone\b", re.IGNORECASE), "long_op", 300),
    # ``git fetch --all --prune`` on a multi-remote repo can take a
    # while; treat as long_op so the watcher surfaces ETA.
    (re.compile(r"git\s+fetch\b.*\s--all\b.*\s--prune\b", re.IGNORECASE), "long_op", 300),
    # RunPod / cloud orchestration
    (re.compile(r"runpod\s+", re.IGNORECASE), "deploy", 600),
]


# Shell-operator splitter. Cheap regex — does NOT parse quotes, so a
# command containing a quoted ``;`` or ``&&`` will split where it
# shouldn't, but we then re-anchor at the segment START which makes
# this conservative (false negative on rare quoted-operator cases is
# fine; false positive on commit messages was the actual bug).
_SHELL_SPLIT_RE = re.compile(r"\s*(?:&&|\|\||;|\|)\s*")

# Common invocation prefixes we strip before pattern matching, so
# ``python -m twine upload`` and ``twine upload`` both classify the
# same way.
_INVOCATION_PREFIX_RE = re.compile(
    r"^(?:python(?:[23](?:\.\d+)?)?\s+-m\s+|nohup\s+|time\s+|sudo\s+(?:-E\s+)?|env\s+\w+=\S+\s+)+",
    re.IGNORECASE,
)


def _shell_segments(cmd: str) -> list[str]:
    """Split a command on common shell operators. Best-effort, no
    quote parsing — see ``_SHELL_SPLIT_RE`` rationale."""
    return [s.strip() for s in _SHELL_SPLIT_RE.split(cmd) if s.strip()]


def _strip_invocation_prefix(segment: str) -> str:
    """Drop leading ``python -m`` / ``nohup`` / ``time`` / ``sudo`` /
    ``env VAR=val`` so the pattern table can match the underlying
    command word directly."""
    return _INVOCATION_PREFIX_RE.sub("", segment.lstrip(), count=1)


# ── Public entry ──────────────────────────────────────────────────────


def classify_wait(
    tool_name: str,
    tool_input: dict,
) -> Optional[WaitClassification]:
    """Return a :class:`WaitClassification` if the tool call should be
    polled, ``None`` otherwise.

    Recognised patterns:

    * ``Agent`` tool dispatch → ``agent_dispatch`` (sub-agent runs
      until it returns; check_cmd is a sentinel that always fails so
      the daemon shows it as ``running`` until the user explicitly
      marks it done — sub-agent completion arrives via the normal
      tool result, not via this poll).
    * ``Bash`` with ``run_in_background=True`` → ``bash_background``;
      check_cmd inspects the stored task output file for a completion
      marker.
    * ``Bash`` with a known long-running command (``twine upload``,
      ``docker push``, ``scp``, ``deploy.py`` etc.) → mapped via
      ``_BASH_PATTERNS``. check_cmd is a "is the foreground bash still
      hanging?" probe — for synchronous Bash calls the call has
      already returned by PostToolUse, so the daemon flips to ``done``
      on first check, but the alert serves as a "this just happened"
      record for the user-prompt inject.
    """
    if not isinstance(tool_input, dict):
        return None

    if tool_name == "Agent":
        # The Agent tool call returns synchronously to the *parent* but
        # the spawned sub-agent runs in its own context. Without
        # PostToolUse-level visibility into "did the sub-agent finish"
        # we record it for surfacing in the inject hook; the agent
        # itself can mark_done() once the sub-agent's tool result lands.
        # Description is preserved on the queue record via
        # ``register_wait``'s ``extra`` dict — see wait_watcher hook.
        return WaitClassification(
            kind="agent_dispatch",
            check_cmd=":",  # always-pass sentinel; agent explicitly mark_done
            eta_seconds=900,  # 15 min default for sub-agent ETA
        )

    if tool_name == "Bash":
        cmd = str(tool_input.get("command", ""))
        if not cmd:
            return None
        # Background bash explicitly requested
        if tool_input.get("run_in_background") is True:
            return WaitClassification(
                kind="bash_background",
                check_cmd=":",  # task notification handles real completion
                eta_seconds=int(tool_input.get("timeout", 600000)) // 1000 or 600,
            )
        # Match against pattern table — but only against the LEADING
        # token(s) of each shell segment, so a ``git commit -m "twine
        # upload …"`` commit message doesn't masquerade as an actual
        # twine upload. We split on common shell operators (``&&``
        # ``||`` ``;`` ``|``) without parsing quotes, which is fine
        # because the property we want is "the keyword must START a
        # segment", not "the keyword appears anywhere".
        for segment in _shell_segments(cmd):
            head = _strip_invocation_prefix(segment).lstrip()
            for pat, kind, eta in _BASH_PATTERNS:
                # Anchored search: pattern must match at start of head.
                if pat.match(head):
                    return WaitClassification(
                        kind=kind,
                        check_cmd=":",
                        eta_seconds=eta,
                    )

    # Other tools (Read, Edit, Write, Grep, Glob etc.) are synchronous
    # by construction — no wait state.
    return None
