# SPDX-FileCopyrightText: 2025 Nous Research
# SPDX-License-Identifier: MIT

"""Substrate-grade destruction guard primitive.

Wraps the R0-R4 classification kernel from
``lyceum.sandbox.destruction_patterns`` with a per-tool ``evaluate()``
function and ``DestructionGuard`` class. This is the Lyceum substrate
layer — Concinno governance lib re-exports these symbols and adds its
own audit-trail / decorator / backup-orchestration on top.

Layering
--------
1. ``destruction_patterns`` (kernel): regex catalog + ``classify_command``.
2. ``destruction_guard`` (this module): per-tool evaluate + suggestion
   table + AskUserQuestion template builder + R0-R4 ladder envelope.
3. ``concinno.destruction_guard`` (governance shim): adds ``audit()``,
   ``destruction_gate`` decorator, backup orchestration, BaseGuard
   adapter — re-exports the substrate names so caller imports keep
   working.

Substrate vs governance split was settled in the Wave 2.7-F audit
(``_AI_BRAIN/05_Planning/2026-05-02-lyceum-api-surface-audit.md``):
classification + UX surface are SOTA primitive worth sharing across
harnesses; audit-trail (`~/.concinno/audit.log`) + decorator
(``_hook_context_permits``) are governance concerns that stay
Concinno-side.

Why no global state
-------------------
Substrate primitives must be importable into any harness without
side-effects. ``evaluate()`` here makes no I/O — it returns a hook
decision dict. Concinno wraps the call to add audit-log writes;
Lyceum-side callers can wrap differently.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Optional

from concinno._lyceum_vendor.sandbox.destruction_patterns import (
    VALID_REASON_KEYWORDS,
    RiskLevel,
    _strip_echo_content,
    check_destroy_confirmed,
    classify_command,
)

__all__ = [
    "DestructionGuard",
    "DestructionBlockedError",
    "EvaluateResult",
    "evaluate",
    "classify_bash",
    "classify_write",
    "block_message",
    "confirm_with_options",
    "suggest_safer_alternative",
    "is_reason_valid_r4",
    "RISK_ICONS",
    "RISK_LABELS",
    "R0",
    "R1",
    "R2",
    "R3",
    "R4",
]


# Backward-compat int aliases — Concinno callers used these before the
# RiskLevel enum existed. Keep so re-exports stay one-to-one.
R0, R1, R2, R3, R4 = 0, 1, 2, 3, 4

RISK_ICONS = {
    R0: "\U0001f7e2",  # green circle
    R1: "\U0001f7e1",  # yellow circle
    R2: "\U0001f7e0",  # orange circle
    R3: "\U0001f534",  # red circle
    R4: "⛔",  # no-entry
}
RISK_LABELS = {
    R0: "Safe",
    R1: "Low Risk",
    R2: "Medium Risk",
    R3: "High Risk",
    R4: "Forbidden",
}


class DestructionBlockedError(RuntimeError):
    """Raised when a destructive operation is blocked at the substrate.

    Substrate-side does not raise this directly inside ``evaluate()``
    (returns ``deny`` decision instead — non-throwing protocol). The
    exception exists so governance wrappers (Concinno's
    ``destruction_gate`` decorator) can raise it from the call sites
    that want exception-driven control flow.
    """


@dataclass(frozen=True)
class EvaluateResult:
    """Substrate-grade result of evaluating a tool call.

    Mirrors the Concinno ``evaluate()`` return-dict shape but as a
    typed dataclass so callers can pattern-match on attributes instead
    of stringly-typed dict keys. ``as_dict()`` keeps the legacy dict
    surface for hook-template emission.
    """

    permission_decision: str  # "allow" | "deny"
    risk: int = R0
    reason: str = ""
    block_message: str = ""
    safer_alternatives: Optional[list[dict]] = None
    ask_user_question_template: Optional[dict] = None

    def as_dict(self) -> dict:
        result: dict[str, Any] = {"permissionDecision": self.permission_decision}
        if self.permission_decision == "deny":
            result["reason"] = self.block_message or self.reason
            extras: dict[str, Any] = {}
            if self.ask_user_question_template:
                extras["ask_user_question_template"] = self.ask_user_question_template
            if self.safer_alternatives:
                extras["safer_alternatives"] = self.safer_alternatives
            if extras:
                result["additionalContext"] = extras
        return result


# ─── Risk classification (per-tool) ───────────────────────────────


def classify_bash(command: str) -> tuple[int, str]:
    """Classify a Bash command. Returns ``(risk_level, reason)``.

    Mirrors :func:`lyceum.sandbox.destruction_patterns.classify_command`
    but returns the legacy ``(int, formatted_reason)`` tuple so Concinno
    callers don't need to migrate.
    """
    match = classify_command(command)
    if match.risk == RiskLevel.SAFE:
        return R0, ""
    risk_int = int(match.risk)
    icon = RISK_ICONS[risk_int]
    return risk_int, f"{icon} {match.message}"


def classify_write(file_path: str, new_content: str) -> tuple[int, str]:
    """Detect dangerous Write overwrites (empty or massive reduction).

    R3 — empty overwrite of file >500 bytes.
    R2 — content reduced to <10% of original on file >1000 bytes.
    """
    if not file_path or not os.path.exists(file_path):
        return R0, ""
    try:
        existing_size = os.path.getsize(file_path)
    except OSError:
        return R0, ""

    new_size = len(new_content.encode("utf-8")) if new_content else 0

    if existing_size > 500 and new_size == 0:
        return R3, (
            f"{RISK_ICONS[R3]} Empty overwrite detected — "
            f"will erase {existing_size:,} bytes"
        )
    if existing_size > 1000 and new_size < existing_size * 0.1:
        pct = 100 - (new_size * 100 // existing_size)
        return R2, (
            f"{RISK_ICONS[R2]} Content reduction "
            f"{existing_size:,} -> {new_size:,} bytes ({pct}% loss)"
        )
    return R0, ""


def is_reason_valid_r4(reason: str) -> bool:
    """R4 requires a keyword from :data:`VALID_REASON_KEYWORDS`."""
    rl = (reason or "").lower()
    return any(kw in rl for kw in VALID_REASON_KEYWORDS)


# ─── Block message rendering ───────────────────────────────────────


def block_message(risk: int, command: str, reason: str) -> str:
    """Render a human-readable block message for ``risk`` level.

    Output is intended for the LLM-facing ``deny.reason`` channel so
    the next turn can paste an AskUserQuestion confirming the user's
    intent.
    """
    cmd_preview = command[:100] + ("..." if len(command) > 100 else "")
    sep = "━" * 20

    if risk == R2:
        return (
            f"\U0001f7e0 MEDIUM RISK — Confirmation Required\n{sep}\n"
            f"\U0001f4cb Command: `{cmd_preview}`\n"
            f"⚠️  Impact: {reason}\n\n"
            f"\U0001f527 Use AskUserQuestion to confirm with user:\n"
            f"   1. Explain what will be deleted and the impact scope\n"
            f"   2. If user agrees, retry with #DESTROY_CONFIRMED appended"
        )
    if risk == R3:
        return (
            f"\U0001f534 HIGH RISK — Confirmation + Reason Required\n{sep}\n"
            f"\U0001f4cb Command: `{cmd_preview}`\n"
            f"\U0001f480 Impact: {reason}\n\n"
            f"\U0001f527 Use AskUserQuestion to confirm with user:\n"
            f"   1. Explain target, impact scope, and irreversibility\n"
            f"   2. Ask user for their reason\n"
            f"   3. If user agrees, retry with #DESTROY_CONFIRMED:<reason>"
        )
    # R4
    return (
        f"⛔ CATASTROPHIC — Forbidden by Default\n{sep}\n"
        f"\U0001f4cb Command: `{cmd_preview}`\n"
        f"☠️  Highest risk level. "
        f"Potentially irreversible total loss.\n\n"
        f"\U0001f527 Only allowed with a valid justification keyword:\n"
        f"   abandon/bankrupt/migrate/transfer/decommission/sunset/EOL/\n"
        f"   廢棄/倒閉/轉移/終止/"
        f"放棄/廃止/倒産/폐기/폐업/"
        f"fermeture/stilllegung/cierre\n"
        f"   Use AskUserQuestion, then retry with "
        f"#DESTROY_CONFIRMED:<valid reason>"
    )


# ─── AskUserQuestion template builder ─────────────────────────────


def confirm_with_options(
    prompt: str,
    options: list[dict],
    default: str = "abort",
) -> dict:
    """Build an AskUserQuestion template payload.

    Hook subprocesses cannot raise AskUserQuestion directly — they can
    only return a deny decision. This helper packages a question
    template that the LLM is instructed to paste into a real
    AskUserQuestion call on the next turn.

    Args:
        prompt: Main question shown to the user.
        options: 2-4 ``{"label", "description", "preview"}`` entries.
        default: Label treated as default when user dismisses.

    Returns:
        ``{"question", "options", "default"}`` ready to embed under
        ``additionalContext.ask_user_question_template``.

    Raises:
        ValueError: If options count is not 2-4 or required keys missing.
    """
    if not 2 <= len(options) <= 4:
        raise ValueError(
            f"options must have 2-4 entries; got {len(options)}",
        )
    for opt in options:
        if "label" not in opt or "description" not in opt:
            raise ValueError(
                "each option requires 'label' and 'description' keys",
            )
    return {
        "question": prompt,
        "options": [
            {
                "label": opt["label"],
                "description": opt["description"],
                "preview": opt.get("preview"),
            }
            for opt in options
        ],
        "default": default,
    }


# ─── Safer-alternative lookup table ───────────────────────────────


_SAFER_ALTERNATIVES: list[tuple[re.Pattern[str], list[dict]]] = [
    (
        re.compile(r"\brm\s+-f\s+.*\.lock\b", re.IGNORECASE),
        [
            {
                "label": "python unlink(missing_ok=True)",
                "description": (
                    "Idempotent removal with stdlib. Doesn't invoke "
                    "shell rm so destruction guard doesn't gate it."
                ),
                "command": (
                    "python -c \"from pathlib import Path; "
                    "Path('<lockfile>').unlink(missing_ok=True)\""
                ),
            },
        ],
    ),
    (
        re.compile(r"\brm\s+-rf\s+(?!/)(?!~)(?!\$HOME)\S+", re.IGNORECASE),
        [
            {
                "label": "soft delete (move to trash)",
                "description": (
                    "mv to _trash/<timestamp>/ — reversible for TTL "
                    "window, cleanup handled by periodic /tidy."
                ),
                "command": "mv <path> _trash/$(date +%Y%m%d_%H%M%S)/",
            },
        ],
    ),
    (
        re.compile(r"\bgit\s+reset\s+--hard\b", re.IGNORECASE),
        [
            {
                "label": "git stash push -u",
                "description": (
                    "Stashes tracked + untracked; recoverable via "
                    "`git stash pop`. Use --hard only if stash fails."
                ),
                "command": (
                    "git stash push -u -m "
                    "'pre-reset $(date -Iseconds)'"
                ),
            },
        ],
    ),
    (
        re.compile(r"\bgit\s+push\s+(--force|-f)\b", re.IGNORECASE),
        [
            {
                "label": "git push --force-with-lease",
                "description": (
                    "Aborts if upstream moved — prevents clobbering "
                    "teammate commits invisibly."
                ),
                "command": "git push --force-with-lease",
            },
        ],
    ),
    (
        re.compile(
            r"\bgit\s+gc\s+.*--prune=(now|all|\d+\.\w+\.ago)\b",
            re.IGNORECASE,
        ),
        [
            {
                "label": "git gc --auto",
                "description": (
                    "Respects gc.auto + pack.deltaCacheLimit; only "
                    "repacks when thresholds hit. Non-destructive."
                ),
                "command": "git gc --auto",
            },
        ],
    ),
    (
        re.compile(r"\btwine\s+upload\b", re.IGNORECASE),
        [
            {
                "label": "twine upload --skip-existing",
                "description": (
                    "PyPI upload is irreversible — `--skip-existing` "
                    "lets retries be safe when a previous attempt "
                    "partially published."
                ),
                "command": "twine upload --skip-existing dist/*",
            },
        ],
    ),
    (
        re.compile(r"\bDROP\s+TABLE\s+(\w+)", re.IGNORECASE),
        [
            {
                "label": "rename to deprecated_<date>",
                "description": (
                    "Preserves data for audit + easy undo. Drop for "
                    "real only after verifying no consumers."
                ),
                "command": (
                    "ALTER TABLE <name> RENAME TO "
                    "<name>_deprecated_<YYYYMMDD>;"
                ),
            },
        ],
    ),
    (
        re.compile(
            r"\bkubectl\s+delete\s+(ns|namespace)\s+\S+",
            re.IGNORECASE,
        ),
        [
            {
                "label": "snapshot resources before delete",
                "description": (
                    "Dump all resources to YAML so the ns can be "
                    "rebuilt if the delete turns out to be wrong."
                ),
                "command": (
                    "kubectl get all -n <ns> -o yaml > backup-<ns>.yaml"
                ),
            },
        ],
    ),
    (
        re.compile(r"\bdocker\s+system\s+prune\s+-a\b", re.IGNORECASE),
        [
            {
                "label": "docker system prune (no -a)",
                "description": (
                    "Removes stopped containers + dangling images "
                    "only. `-a` additionally nukes all unused images "
                    "incl. base layers you'd pull again."
                ),
                "command": "docker system prune",
            },
        ],
    ),
    (
        re.compile(
            r"\baws\s+s3\s+rb\s+s3://\S+\s+--force\b",
            re.IGNORECASE,
        ),
        [
            {
                "label": "enable versioning first",
                "description": (
                    "Turn on bucket versioning so deletes leave "
                    "tombstones and versioned objects stay "
                    "recoverable for 30d."
                ),
                "command": (
                    "aws s3api put-bucket-versioning "
                    "--bucket <bucket> --versioning-configuration "
                    "Status=Enabled"
                ),
            },
        ],
    ),
    (
        re.compile(r"\bgit\s+filter-(branch|repo)\b", re.IGNORECASE),
        [
            {
                "label": "clone fresh + use bfg --no-blob-protection=false",
                "description": (
                    "History rewrites break every existing clone. "
                    "Do a fresh mirror clone, rewrite there, verify, "
                    "then force-with-lease push."
                ),
                "command": (
                    "git clone --mirror <repo> && cd <repo>.git && "
                    "bfg --strip-blobs-bigger-than 10M"
                ),
            },
        ],
    ),
]


def suggest_safer_alternative(command: str) -> Optional[list[dict]]:
    """Return safer alternatives for a destructive ``command``.

    Returns ``None`` (not an empty list) when no pattern matches —
    callers can ``if alternatives:`` cheaply.
    """
    if not command:
        return None
    cleaned = _strip_echo_content(command)
    for pattern, alternatives in _SAFER_ALTERNATIVES:
        if pattern.search(cleaned):
            return [dict(alt) for alt in alternatives]
    return None


# ─── Per-tool evaluate (substrate kernel) ─────────────────────────


def evaluate(tool_name: str, tool_input: dict) -> dict:
    """Evaluate a tool call and return a hook-decision dict.

    This is the substrate kernel — pure classification + suggestion
    rendering, no I/O, no audit log, no toast notification, no backup
    orchestration. Wrappers (Concinno's governance shim) layer those
    side-effects on top.

    Args:
        tool_name: ``"Bash"`` / ``"Write"`` / other (other tools allow).
        tool_input: ``{"command": "..."}`` for Bash, ``{"file_path", "content"}`` for Write.

    Returns:
        Hook-decision dict — ``{"permissionDecision": "allow"}`` or
        ``{"permissionDecision": "deny", "reason": "...", "additionalContext": {...}}``.
    """
    if tool_name == "Write":
        fp = tool_input.get("file_path", "")
        content = tool_input.get("content", "")
        risk, reason = classify_write(fp, content)
        if risk == R0:
            return {"permissionDecision": "allow"}
        msg_lines = [
            f"{RISK_ICONS[risk]} File Overwrite Protection",
            "━" * 20,
            f"\U0001f4c4 File: `{fp}`",
            f"⚠️  {reason}",
            "",
            "\U0001f527 Use AskUserQuestion to confirm overwrite with user",
        ]
        return {"permissionDecision": "deny", "reason": "\n".join(msg_lines)}

    if tool_name != "Bash":
        return {"permissionDecision": "allow"}

    command = tool_input.get("command", "")
    if not command:
        return {"permissionDecision": "allow"}

    risk, reason = classify_bash(command)

    if risk == R0:
        return {"permissionDecision": "allow"}
    if risk == R1:
        return {"permissionDecision": "allow"}

    confirmed, conf_reason = check_destroy_confirmed(command)
    if confirmed:
        if risk == R2:
            return {"permissionDecision": "allow"}
        if risk == R3:
            if conf_reason and len(conf_reason) > 3:
                return {"permissionDecision": "allow"}
            msg = (
                "\U0001f534 High risk requires a reason after "
                "#DESTROY_CONFIRMED:\n"
                "Format: #DESTROY_CONFIRMED:<user's reason>"
            )
            return {"permissionDecision": "deny", "reason": msg}
        if risk == R4:
            if conf_reason and is_reason_valid_r4(conf_reason):
                return {"permissionDecision": "allow"}
            msg = (
                "⛔ R4 requires a valid justification keyword.\n"
                "Accepted: abandon/migrate/decommission/sunset/EOL/"
                "廢棄/倒閉/轉移/終止 etc.\n"
                "Format: #DESTROY_CONFIRMED:<valid keyword reason>"
            )
            return {"permissionDecision": "deny", "reason": msg}

    msg = block_message(risk, command, reason)
    extras: dict[str, Any] = {}
    if risk >= R3:
        alternatives = suggest_safer_alternative(command) or []
        options: list[dict] = [
            {
                "label": "abort",
                "description": "Cancel — do not run the destructive command.",
                "preview": None,
            },
        ]
        for alt in alternatives[:2]:
            options.append(
                {
                    "label": alt["label"],
                    "description": alt["description"],
                    "preview": alt.get("command"),
                }
            )
        options.append(
            {
                "label": "proceed with #DESTROY_CONFIRMED:<reason>",
                "description": (
                    "Retry the original command with a valid reason "
                    "keyword appended (migrate/decommission/archive/...)."
                ),
                "preview": f"{command.strip()} #DESTROY_CONFIRMED:<reason>",
            }
        )
        try:
            template = confirm_with_options(
                prompt=(
                    f"{RISK_LABELS[risk]} destructive command intercepted. "
                    f"Choose how to proceed:"
                ),
                options=options[:4],
                default="abort",
            )
            extras["ask_user_question_template"] = template
        except ValueError:
            pass
        if alternatives:
            extras["safer_alternatives"] = alternatives

    result: dict[str, Any] = {"permissionDecision": "deny", "reason": msg}
    if extras:
        result["additionalContext"] = extras
    return result


# ─── DestructionGuard class (substrate-grade) ────────────────────


class DestructionGuard:
    """Substrate-grade destruction guard.

    Light Protocol-friendly class — ``evaluate(tool_name, tool_input)``
    is the single entry point. Concinno's governance shim wraps this
    with BaseGuard semantics + audit-trail. Other harnesses (Lyceum
    standalone, future Sancio runtime) can use the substrate class
    directly via composition.
    """

    name = "destruction_guard"

    def evaluate(self, tool_name: str, tool_input: dict) -> EvaluateResult:
        """Evaluate a tool call. Returns :class:`EvaluateResult`.

        Substrate variant — does not invoke audit / notify / backup
        side-effects. Wrappers add those.
        """
        result = evaluate(tool_name, tool_input)
        decision = result.get("permissionDecision", "allow")
        if decision == "allow":
            return EvaluateResult(permission_decision="allow")

        # Risk ladder retrieval — re-classify so callers don't need to
        # parse the formatted message back. classify_bash on the bash
        # branch, classify_write on Write — match upstream.
        if tool_name == "Bash":
            risk, reason = classify_bash(tool_input.get("command", ""))
        elif tool_name == "Write":
            risk, reason = classify_write(
                tool_input.get("file_path", ""),
                tool_input.get("content", ""),
            )
        else:
            risk, reason = R0, ""

        extras = result.get("additionalContext") or {}
        return EvaluateResult(
            permission_decision="deny",
            risk=risk,
            reason=reason,
            block_message=result.get("reason", ""),
            safer_alternatives=extras.get("safer_alternatives") if isinstance(extras, dict) else None,
            ask_user_question_template=(
                extras.get("ask_user_question_template")
                if isinstance(extras, dict)
                else None
            ),
        )

    def check(self, tool_name: str, tool_input: dict) -> Optional[EvaluateResult]:
        """BaseGuard-flavoured alias.

        Returns ``None`` when allow, :class:`EvaluateResult` when deny —
        the BaseGuard convention used by ``concinno.guards.base``.
        """
        result = self.evaluate(tool_name, tool_input)
        if result.permission_decision == "allow":
            return None
        return result
