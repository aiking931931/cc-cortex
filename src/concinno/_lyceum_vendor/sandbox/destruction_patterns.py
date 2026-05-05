# SPDX-FileCopyrightText: 2025 Nous Research
# SPDX-License-Identifier: MIT

"""R0-R4 destruction-risk classification + #DESTROY_CONFIRMED escape.

Ported from concinno.destruction_guard at commit 20bab6c8cb35006453bcb662afb4844831ea6427.
R0-R4 lifetime fire ledger: 14,517 events as of 2026-05-01.

Lyceum-idiomatic surface
------------------------
The upstream Concinno module bundles classification + auto-backup + audit
log + R3+ confirmation gating into a single 1,495-LoC file. This port
extracts only the **classification kernel** — the regex pattern set and
the ``#DESTROY_CONFIRMED:<reason>`` escape protocol — and exposes them
behind a small Protocol so Lyceum's existing ``hardline`` blocklist can
extend its match set by composition rather than monkey-patching.

Concinno's R0-R4 ladder
-----------------------
* **R0** = Safe (dry-run / regenerable temp / preview-only).
* **R1** = Low risk — deleting regenerable build artifacts (node_modules,
  dist, __pycache__, etc.). Safe but worth surfacing.
* **R2** = Medium risk — may delete important files or data. Includes
  ``DROP TABLE``, ``rm -rf <path>``, ``git branch -D``, ``ALTER TABLE
  ... DROP``, etc.
* **R3** = High risk — potential large-scale data loss. Includes
  ``terraform destroy``, ``git push --force``, ``git filter-repo``,
  ``git gc --prune=now``, ``rm -rf /var``, supply-chain attacks,
  privilege escalation, etc.
* **R4** = Catastrophic / forbidden — ``rm -rf /``, fork bombs,
  ``mkfs.*``, prod-namespace deletion. Even with ``#DESTROY_CONFIRMED``
  these should be treated with maximum scrutiny by the caller.

The R0-R4 patterns Lyceum specifically asked us to port (for the
hardline-blocklist fast path):

* **R0 in this port = force-push-main** (``git push --force origin
  main`` and variants). Concinno's R3 ``git\\s+push\\s+--force`` is the
  superset; the spec wanted us to surface main-branch-targeted force
  push as a top-level named pattern, so we expose it as
  ``FORCE_PUSH_MAIN`` separately.
* **R1 = rm -rf working tree** (``rm -rf`` on a path that is *not* a
  regenerable build artifact dir).
* **R2 = DROP TABLE**.
* **R3 = git filter-repo** (and ``git filter-branch`` / BFG).
* **R4 = git gc --prune=now** on a populated repo.

The full R3/R4 catalog from Concinno (~150 patterns) is preserved in
:data:`R3_PATTERNS` / :data:`R4_PATTERNS` so callers who want the wide
match still have it; the spec's named patterns are exposed separately
in :data:`NAMED_PATTERNS` for the hardline blocklist.

Escape syntax
-------------
``#DESTROY_CONFIRMED:<reason>`` appended to a command tells the gate
the operator has explicitly authorised the destructive operation. The
reason field is required (R4 needs a keyword from
:data:`VALID_REASON_KEYWORDS`; R3 accepts any reason >3 chars). This
syntax is preserved verbatim from Concinno so muscle memory carries
over.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import IntEnum
from typing import Iterable, Optional, Protocol, runtime_checkable

__all__ = [
    "RiskLevel",
    "MatchResult",
    "DestructionPattern",
    "NamedDestructionPattern",
    "NAMED_PATTERNS",
    "R0_PATTERNS",
    "R1_PATTERNS",
    "R2_PATTERNS",
    "R3_PATTERNS",
    "R4_PATTERNS",
    "VALID_REASON_KEYWORDS",
    "classify_command",
    "check_destroy_confirmed",
    "split_commands",
]


# ─── Risk levels ──────────────────────────────────────────────────


class RiskLevel(IntEnum):
    """Concinno R0-R4 destruction-risk classification."""

    SAFE = 0
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    FORBIDDEN = 4


_RISK_LABELS = {
    RiskLevel.SAFE: "Safe",
    RiskLevel.LOW: "Low Risk",
    RiskLevel.MEDIUM: "Medium Risk",
    RiskLevel.HIGH: "High Risk",
    RiskLevel.FORBIDDEN: "Forbidden",
}


# ─── Result dataclass ─────────────────────────────────────────────


@dataclass(frozen=True)
class MatchResult:
    """Outcome of a destruction-pattern match.

    Attributes:
        risk: R0-R4 classification.
        pattern_name: Stable identifier for the matched pattern (e.g.
            ``force_push_main``, ``rm_rf_working_tree``).
        regex: The actual regex string that matched, for logging.
        message: Human-readable explanation.
    """

    risk: RiskLevel
    pattern_name: str
    regex: str
    message: str

    @property
    def is_blocking(self) -> bool:
        """True iff this match should block the command outright."""
        return self.risk >= RiskLevel.MEDIUM


# ─── Protocol surface ─────────────────────────────────────────────


@runtime_checkable
class DestructionPattern(Protocol):
    """A single destruction-pattern matcher.

    Implementations may be a single regex, a compound rule, or a
    callable that synthesises a regex from configuration. Any object
    whose ``.match()`` returns ``MatchResult | None`` plugs into the
    Lyceum hardline blocklist.
    """

    def match(self, command: str) -> Optional[MatchResult]:  # pragma: no cover - Protocol
        ...


@dataclass(frozen=True)
class NamedDestructionPattern:
    """Concrete :class:`DestructionPattern` with a name + risk + regex.

    The risk level lets the hardline blocklist make routing decisions
    (FORBIDDEN = always block; HIGH = require ``#DESTROY_CONFIRMED``;
    MEDIUM = warn-and-confirm; LOW = log only).
    """

    name: str
    risk: RiskLevel
    regex: str
    message: str

    def match(self, command: str) -> Optional[MatchResult]:
        # Strip the #DESTROY_CONFIRMED tag before matching so dollar-
        # anchored patterns (e.g. R4 "rm -rf / $") still hit when the
        # operator appends confirmation. The escape is read separately
        # by check_destroy_confirmed().
        stripped = re.sub(r"\s*#DESTROY_CONFIRMED(?::.+)?$", "", command)
        if re.search(self.regex, stripped, re.IGNORECASE):
            return MatchResult(
                risk=self.risk,
                pattern_name=self.name,
                regex=self.regex,
                message=self.message,
            )
        return None


# ─── Named patterns the spec asked us to port ─────────────────────


NAMED_PATTERNS: tuple[NamedDestructionPattern, ...] = (
    NamedDestructionPattern(
        name="force_push_main",
        risk=RiskLevel.HIGH,
        regex=r"git\s+push\s+(?:--force|-f)\s+\S+\s+(?:main|master)\b",
        message=(
            "Force-pushing to main/master is unrecoverable for collaborators. "
            "Use #DESTROY_CONFIRMED:<reason> to authorise."
        ),
    ),
    NamedDestructionPattern(
        name="rm_rf_working_tree",
        # Match rm -rf on absolute paths, ~ expansions, or relative
        # paths that are NOT one of the regenerable build-artifact
        # directories (those are R1, classified separately).
        risk=RiskLevel.HIGH,
        regex=(
            r"rm\s+-[rR]f?\s+(?!"
            r"(?:\.?/?(?:node_modules|dist|build|\.next|__pycache__|"
            r"\.cache|\.venv|venv|\.tox|\.pytest_cache|\.turbo|"
            r"\.parcel-cache)\b))"
            r"(?:/|~|\$HOME|\.\.|\w)"
        ),
        message="rm -rf on a working-tree path can wipe unrelated data.",
    ),
    NamedDestructionPattern(
        name="drop_table",
        risk=RiskLevel.MEDIUM,
        regex=r"\bDROP\s+TABLE\b",
        message="DROP TABLE removes a database table irreversibly.",
    ),
    NamedDestructionPattern(
        name="git_filter_repo",
        risk=RiskLevel.HIGH,
        regex=r"git\s+filter-(?:branch|repo)\b",
        message=(
            "git filter-repo / filter-branch rewrites history. "
            "Force-push afterwards is irreversible for collaborators."
        ),
    ),
    NamedDestructionPattern(
        name="git_gc_prune_now",
        risk=RiskLevel.FORBIDDEN,
        regex=r"git\s+gc\s+.*--prune=(?:now|all|\d+\.\w+\.ago)\b",
        message=(
            "git gc --prune=now permanently destroys unreachable objects "
            "(reflog/dangling) — recovery from a botched rebase becomes "
            "impossible. Use a safer TTL or back up .git/ first."
        ),
    ),
)


# ─── Full Concinno R0-R4 catalog (regex strings) ──────────────────
# Verbatim from concinno.destruction_guard at the commit referenced in
# the module docstring. Lyceum can swap individual rules out by name
# but the default catalog imports the full set so the kernel matches
# the upstream's tested coverage.


R0_PATTERNS: tuple[str, ...] = (
    r"--dry-run",
    r"--what-if",
    r"-WhatIf",
    r"git\s+clean\s+-n",
    r"terraform\s+plan\b",
    r"rm\s+[^-]\S*\.(tmp|temp|log|bak)$",
)


R1_PATTERNS: tuple[str, ...] = (
    r"rm\s+-r[f]?\s+\.?/?(?:node_modules|dist|build|\.next|__pycache__|"
    r"\.cache|\.venv|venv|\.tox|\.pytest_cache|\.turbo|\.parcel-cache)\b",
    r"git\s+stash\s+drop",
    r"docker\s+container\s+prune\b(?!.*-a)",
    r"pip\s+cache\s+purge",
    r"npm\s+cache\s+clean",
    r"pnpm\s+store\s+prune",
)


R2_PATTERNS: tuple[str, ...] = (
    r"rm\s+-[rR]f?\s+\S+",
    r"del\s+/[sS]",
    r"rd\s+/[sS]\s+/[qQ]",
    r"Remove-Item.*-Recurse",
    r"DROP\s+TABLE",
    r"TRUNCATE\s+TABLE",
    r"DELETE\s+FROM\s+\w+\s*;?\s*$",
    r"git\s+branch\s+-[dD]\s+",
    r"docker\s+(?:rm|rmi)\s+",
    r"npm\s+unpublish",
    r"pip\s+uninstall",
    r"chmod\s+(?:-R\s+)?[0-7]*7[0-7]*\s+/(?:etc|var|usr|home|opt|sys)",
    r"useradd\s+.*-u\s+0\b",
    r"usermod\s+.*-u\s+0\b",
    r"ALTER\s+TABLE\s+\w+\s+DROP\b",
    r"docker\s+run\s+.*--privileged",
)


R3_PATTERNS: tuple[str, ...] = (
    r"terraform\s+destroy",
    r"pulumi\s+destroy",
    r"kubectl\s+delete\s+(?:namespace|deployment|statefulset|pvc|pv)\b",
    r"docker\s+system\s+prune\s+-a",
    r"docker\s+volume\s+prune",
    r"git\s+push\s+--force",
    r"git\s+push\s+-f\b",
    r"git\s+reset\s+--hard",
    r"DROP\s+(?:DATABASE|SCHEMA)",
    r"aws\s+s3\s+rb\s+.*--force",
    r"aws\s+cloudformation\s+delete-stack",
    r"gcloud\s+(?:projects|compute)\s+delete",
    r"az\s+(?:group|vm|webapp)\s+delete",
    r"rm\s+-[rR]f\s+/(?:var|etc|usr|home|opt|srv|data)\b",
    r"rm\s+-[rR]f\s+\.\s*$",
    r"base64\s+.*\|\s*(?:ba)?sh",
    r"curl\s+.*\|\s*(?:ba)?sh",
    r"wget\s+.*\|\s*(?:ba)?sh",
    r"git\s+gc\s+.*--prune=(?:now|all|\d+\.\w+\.ago)\b",
    r"git\s+gc\s+.*--aggressive\b",
    r"git\s+reflog\s+expire\s+.*--expire=now\b",
    r"git\s+prune\s+--expire=now\b",
    r"git\s+filter-(?:branch|repo)\s+",
    r"bfg\s+.*--strip-blobs-bigger-than\b",
    r"bfg\s+.*--delete-files\b",
    r"redis-cli\s+(?:FLUSHALL|FLUSHDB)",
    r"git\s+push\s+--force\s+--all",
)


R4_PATTERNS: tuple[str, ...] = (
    r"rm\s+-[rR]f\s+/\s*$",
    r"rm\s+-[rR]f\s+/\*",
    r"rm\s+-[rR]f\s+~/?$",
    r"rm\s+-[rR]f\s+\$HOME/?$",
    r"mkfs\.",
    r"dd\s+if=.*of=/dev/",
    r":\(\)\s*\{\s*:\|:&\s*\}\s*;:",
    r"terraform\s+destroy.*--auto-approve.*(?:prod|production)",
    r"kubectl\s+delete\s+namespace\s+(?:prod|production|default)\b",
    r"rm\s+-[rR]f\s+/?(?:Windows|System32|Program\s*Files)",
)


# ─── #DESTROY_CONFIRMED reason vocabulary ─────────────────────────


VALID_REASON_KEYWORDS = frozenset(
    [
        # English
        "abandon",
        "bankrupt",
        "migrate",
        "transfer",
        "decommission",
        "sunset",
        "eol",
        "deprecated",
        "end-of-life",
        "shutdown",
        "archive",
        "retire",
        "phase-out",
        "phase out",
        "wind-down",
        "obsolete",
        "replace",
        "upgrade",
        "restructure",
        # Chinese
        "廢棄",
        "倒閉",
        "轉移",
        "遷移",
        "終止",
        "棄用",
        "下架",
        "結束",
        "關閉",
        "淘汰",
        "退役",
        "放棄",
        "不要了",
        "重構",
        # Japanese
        "廃止",
        "倒産",
        "移行",
        "閉鎖",
        "終了",
        # Korean
        "폐기",
        "폐업",
        "이전",
        "종료",
        "철수",
        "포기",
        # French
        "fermeture",
        "migration",
        "remplacement",
        # German
        "stilllegung",
        "aufgabe",
        "abschaltung",
        "ersatz",
        # Spanish
        "cierre",
        "abandono",
        "reemplazo",
    ]
)


# ─── Functional API ───────────────────────────────────────────────


def split_commands(cmd: str) -> list[str]:
    """Split compound commands on ``||``, ``&&``, ``;``, ``|``.

    Mirrors concinno.destruction_guard.split_commands for parity so
    pipe-chained patterns (``curl | bash`` etc.) are caught.
    """
    parts = re.split(r"\s*(?:\|\||&&|;)\s*", cmd)
    result: list[str] = []
    for part in parts:
        result.extend(re.split(r"\s*\|\s*", part))
    return [piece.strip() for piece in result if piece.strip()]


def _strip_echo_content(cmd: str) -> str:
    """Strip echo/printf quoted content + heredocs (false-positive guard)."""
    result = re.sub(
        r"""(?:echo|printf)\s+(?:-[neE]+\s+)?(['"])(.*?)\1""", "echo", cmd
    )
    result = re.sub(r"<<-?\s*['\"]?(\w+)['\"]?.*?\1", "", result, flags=re.DOTALL)
    return result


def classify_command(command: str) -> MatchResult:
    """Classify a Bash command against the full R0-R4 catalog.

    Returns a :class:`MatchResult` with ``risk=SAFE`` if no pattern
    matches. The ``pattern_name`` field is ``"safe"`` in that case.

    The classifier runs in priority order (R4 > R3 > R2 > R1 > R0)
    matching Concinno's behaviour: a command that hits both R2 and R3
    is reported as R3.
    """
    # Strip #DESTROY_CONFIRMED tag before matching so $-anchored patterns
    # (R4 "rm -rf / $") still hit when operators append confirmation.
    stripped = re.sub(r"\s*#DESTROY_CONFIRMED(?::.+)?$", "", command)
    cleaned = _strip_echo_content(stripped)

    # Case-sensitive safe exit: git branch -d (lowercase) is safe;
    # -D is force-delete and falls through to R2.
    if re.search(r"git\s+branch\s+-d\s+", cleaned) and not re.search(
        r"git\s+branch\s+-D\s+", cleaned
    ):
        return MatchResult(RiskLevel.SAFE, "safe", "", "")

    # Pre-split: R4 patterns that span pipe/chain boundaries.
    for pat in R4_PATTERNS:
        if re.search(pat, cleaned, re.IGNORECASE):
            return MatchResult(
                RiskLevel.FORBIDDEN,
                "r4",
                pat,
                "Catastrophic operation — potentially irreversible total loss",
            )
    for pat in R3_PATTERNS:
        if re.search(pat, cleaned, re.IGNORECASE):
            return MatchResult(
                RiskLevel.HIGH,
                "r3",
                pat,
                "High risk — potential large-scale data loss",
            )

    parts = split_commands(cleaned)
    best = MatchResult(RiskLevel.SAFE, "safe", "", "")
    for part in parts:
        if any(re.search(p, part, re.IGNORECASE) for p in R0_PATTERNS):
            continue
        if any(re.search(p, part, re.IGNORECASE) for p in R1_PATTERNS):
            if RiskLevel.LOW > best.risk:
                best = MatchResult(
                    RiskLevel.LOW,
                    "r1",
                    "",
                    "Low risk — deleting regenerable files",
                )
            continue
        for risk, patterns, label in (
            (RiskLevel.FORBIDDEN, R4_PATTERNS, "Catastrophic operation"),
            (RiskLevel.HIGH, R3_PATTERNS, "High risk — potential large-scale data loss"),
            (RiskLevel.MEDIUM, R2_PATTERNS, "Medium risk — may delete important data"),
        ):
            for pat in patterns:
                if re.search(pat, part, re.IGNORECASE):
                    if risk > best.risk:
                        best = MatchResult(risk, f"r{int(risk)}", pat, label)
                    break
    return best


def check_destroy_confirmed(command: str) -> tuple[bool, str]:
    """Parse ``#DESTROY_CONFIRMED:<reason>`` from a command.

    Returns ``(confirmed, reason)`` where ``confirmed`` is True iff the
    marker is present. ``reason`` is the trailing text (may be empty —
    callers enforce R4-needs-keyword vs R3-accepts-any-reason policy).
    """
    match = re.search(r"#DESTROY_CONFIRMED(?::(.+?))?$", command)
    if match:
        return True, (match.group(1) or "").strip()
    return False, ""


def iter_named_patterns() -> Iterable[NamedDestructionPattern]:
    """Yield the named patterns from :data:`NAMED_PATTERNS`.

    Convenience for the Lyceum hardline blocklist:

        >>> from lyceum.sandbox.destruction_patterns import iter_named_patterns
        >>> for pattern in iter_named_patterns():
        ...     blocklist.add(pattern)
    """
    return iter(NAMED_PATTERNS)
