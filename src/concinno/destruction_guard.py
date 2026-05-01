"""concinno.destruction_guard — Governance shim over Lyceum substrate.

@module destruction_guard
@responsibility Concinno-side governance integration for the
    substrate-grade destruction guard primitive that lives in
    ``lyceum.sandbox.destruction_guard``. Adds: append-only audit log
    rotation, ``destruction_gate`` decorator wired to
    ``_hook_context_permits``, auto-backup orchestration before
    destructive Bash/Write ops, ``BaseGuard`` adapter, toast notify on
    deny.

Wave 2.7-F (2026-05-02): the classification kernel, ``evaluate``-style
hook decision, ``confirm_with_options`` AskUser template builder,
``suggest_safer_alternative`` lookup table, and ``block_message``
formatter were ported to ``lyceum.sandbox.destruction_guard`` so other
harnesses (Lyceum standalone, future Sancio runtime) can reuse the
SOTA primitive. Concinno keeps the audit-trail (``~/.claude/destruction_audit.log``),
backup orchestration (``.destruction_backups/``), the ``destruction_gate``
decorator wired to ``_hook_context_permits``, and the ``BaseGuard``
adapter so callers don't churn.

@dependencies concinno.guards.base, lyceum.sandbox.destruction_guard
@exports evaluate, classify_bash, classify_write, backup_targets, backup_file,
    audit, list_backups, cleanup_backups, restore_backup, DestructionGuard,
    confirm_with_options, suggest_safer_alternative, destruction_gate,
    DestructionBlockedError, block_message, check_confirmed, split_commands,
    is_reason_valid_r4, R0, R1, R2, R3, R4, RISK_ICONS, RISK_LABELS,
    VALID_REASON_KEYWORDS, R0_PATTERNS, R1_PATTERNS, R2_PATTERNS, R3_PATTERNS,
    R4_PATTERNS, _strip_echo_content, load_config
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

# ─── Substrate re-exports (Lyceum) ───────────────────────────────
# These names form the public API surface that callers across Concinno
# (cli, hooks, tools, tests) import from this module. They live in
# ``lyceum.sandbox.destruction_guard`` since Wave 2.7-F so Lyceum
# standalone harnesses can use the SOTA classification kernel without
# pulling Concinno governance code. The re-export keeps every existing
# ``from concinno.destruction_guard import X`` callsite working.
from lyceum.sandbox.destruction_guard import (  # noqa: F401 — public API
    R0,
    R1,
    R2,
    R3,
    R4,
    RISK_ICONS,
    RISK_LABELS,
    DestructionBlockedError,
    EvaluateResult,
    block_message,
    classify_bash,
    classify_write,
    confirm_with_options,
    is_reason_valid_r4,
    suggest_safer_alternative,
)
from lyceum.sandbox.destruction_guard import evaluate as _lyceum_evaluate
from lyceum.sandbox.destruction_patterns import (  # noqa: F401 — public API
    R0_PATTERNS,
    R1_PATTERNS,
    R2_PATTERNS,
    R3_PATTERNS,
    R4_PATTERNS,
    VALID_REASON_KEYWORDS,
    _strip_echo_content,
    check_destroy_confirmed,
    split_commands,
)

from concinno.guards.base import BaseGuard, GuardCategory, GuardContext, GuardResult


# Concinno-specific alias — older callers used ``check_confirmed``;
# substrate uses ``check_destroy_confirmed``. Keep both so nothing
# churns.
def check_confirmed(text: str) -> tuple[bool, str]:
    """Concinno alias for :func:`lyceum.sandbox.destruction_patterns.check_destroy_confirmed`."""
    return check_destroy_confirmed(text)


# ─── Concinno-side governance config ─────────────────────────────


DEFAULT_CONFIG = {
    "enabled": True,
    "backup": {
        "enabled": True,
        "dir": ".destruction_backups",
        "max_file_mb": 50,
        "max_total_mb": 500,
        "cleanup_keep_days": 7,
        "exclude_patterns": [
            "node_modules",
            "__pycache__",
            ".git",
            "dist",
            "build",
            ".next",
            ".cache",
            ".venv",
            "venv",
        ],
    },
    "risk_overrides": {},
    "notify_on_deny": False,
}


def _find_config() -> Path:
    """Find config: check hooks dir first, then project dir."""
    hooks_cfg = Path.home() / ".claude" / "hooks" / "destruction_guard_config.json"
    if hooks_cfg.exists():
        return hooks_cfg
    proj = Path(os.environ.get("CLAUDE_PROJECT_DIR", "."))
    proj_cfg = proj / ".claude" / "hooks" / "destruction_guard_config.json"
    if proj_cfg.exists():
        return proj_cfg
    return hooks_cfg  # default location


def load_config() -> dict:
    """Load config with defaults for missing keys."""
    cfg_path = _find_config()
    if cfg_path.exists():
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            for k, v in DEFAULT_CONFIG.items():
                cfg.setdefault(k, v)
                if isinstance(v, dict):
                    for kk, vv in v.items():
                        cfg[k].setdefault(kk, vv)
            return cfg
        except Exception:
            pass
    return DEFAULT_CONFIG.copy()


def _project_dir() -> Path:
    return Path(os.environ.get("CLAUDE_PROJECT_DIR", ".")).resolve()


def _audit_log_path() -> Path:
    return Path.home() / ".claude" / "destruction_audit.log"


# ─── Backup orchestration (Concinno-specific) ────────────────────


def _backup_dir(config: dict) -> Path:
    raw = config.get("backup", {}).get("dir", ".destruction_backups")
    p = Path(raw)
    if not p.is_absolute():
        p = _project_dir() / p
    return p


def backup_targets(command: str, config: dict) -> Optional[str]:
    """Auto-backup targets before destructive Bash op. Returns backup ID or None."""
    if not config.get("backup", {}).get("enabled", True):
        return None

    backup_base = _backup_dir(config)
    max_bytes = config["backup"].get("max_file_mb", 50) * 1024 * 1024
    exclude = set(config["backup"].get("exclude_patterns", []))

    targets: list[str] = []
    for m in re.finditer(r"rm\s+(?:-[rRfiv]+\s+)*(.+?)(?:\s*[;|&#]|$)", command):
        for t in m.group(1).split():
            if not t.startswith("-") and not t.startswith("#"):
                targets.append(t)
    for m in re.finditer(
        r"(?:del|rd)\s+(?:/[sS]\s+)?(?:/[qQ]\s+)?(.+?)(?:\s*[;|&#]|$)",
        command,
    ):
        targets.append(m.group(1).strip().strip('"'))

    if not targets:
        return None

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    bid = f"{ts}_{hashlib.md5(command.encode()).hexdigest()[:6]}"
    bpath = backup_base / bid
    backed: list[str] = []
    project = _project_dir()

    for target in targets:
        tp = Path(target)
        if not tp.is_absolute():
            tp = project / tp
        tp = tp.resolve()
        if not tp.exists() or tp.name in exclude:
            continue
        try:
            if tp.is_file() and tp.stat().st_size <= max_bytes:
                bpath.mkdir(parents=True, exist_ok=True)
                shutil.copy2(tp, bpath / tp.name)
                backed.append(str(tp))
            elif tp.is_dir():
                total = sum(
                    f.stat().st_size
                    for f in tp.rglob("*")
                    if f.is_file() and f.name not in exclude
                )
                if total <= max_bytes:
                    shutil.copytree(
                        tp, bpath / tp.name, ignore=shutil.ignore_patterns(*exclude)
                    )
                    backed.append(str(tp))
        except Exception:
            pass

    if backed:
        bpath.mkdir(parents=True, exist_ok=True)
        with open(bpath / "manifest.json", "w", encoding="utf-8") as f:
            json.dump(
                {
                    "id": bid,
                    "timestamp": datetime.now().isoformat(),
                    "command": command[:500],
                    "targets": backed,
                    "type": "auto",
                    "pinned": False,
                },
                f,
                indent=2,
                ensure_ascii=False,
            )
        return bid
    return None


def backup_file(file_path: str, config: dict) -> Optional[str]:
    """Backup a single file before Write overwrite."""
    if not config.get("backup", {}).get("enabled", True):
        return None
    tp = Path(file_path)
    if not tp.exists():
        return None
    max_bytes = config["backup"].get("max_file_mb", 50) * 1024 * 1024
    if tp.stat().st_size > max_bytes:
        return None

    backup_base = _backup_dir(config)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    bid = f"write_{ts}_{hashlib.md5(file_path.encode()).hexdigest()[:6]}"
    bpath = backup_base / bid
    try:
        bpath.mkdir(parents=True, exist_ok=True)
        shutil.copy2(tp, bpath / tp.name)
        with open(bpath / "manifest.json", "w", encoding="utf-8") as f:
            json.dump(
                {
                    "id": bid,
                    "timestamp": datetime.now().isoformat(),
                    "file": str(tp),
                    "original_size": tp.stat().st_size,
                    "type": "write_overwrite",
                    "pinned": False,
                },
                f,
                indent=2,
                ensure_ascii=False,
            )
        return bid
    except Exception:
        return None


# ─── Audit log (Concinno-specific) ───────────────────────────────


_AUDIT_ROTATE_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
_AUDIT_ROTATE_MAX_AGE_DAYS = 90
_AUDIT_ROTATE_KEEP = 3  # .log.1.gz .. .log.3.gz


def _rotate_audit_log(log_path: Path) -> None:
    """Rotate audit log when it exceeds size or age threshold."""
    import gzip

    try:
        stat = log_path.stat()
    except OSError:
        return

    needs_rotate = False
    if stat.st_size >= _AUDIT_ROTATE_MAX_BYTES:
        needs_rotate = True
    else:
        age_seconds = time.time() - stat.st_mtime
        if age_seconds >= _AUDIT_ROTATE_MAX_AGE_DAYS * 86400:
            needs_rotate = True

    if not needs_rotate:
        return

    for idx in range(_AUDIT_ROTATE_KEEP, 0, -1):
        src = log_path.with_name(f"{log_path.name}.{idx}.gz")
        if idx == _AUDIT_ROTATE_KEEP and src.exists():
            try:
                src.unlink()
            except OSError:
                pass
            continue
        dst = log_path.with_name(f"{log_path.name}.{idx + 1}.gz")
        if src.exists():
            try:
                src.replace(dst)
            except OSError:
                pass

    try:
        with open(log_path, "rb") as src_f:
            archive_path = log_path.with_name(f"{log_path.name}.1.gz")
            with gzip.open(archive_path, "wb") as dst_f:
                shutil.copyfileobj(src_f, dst_f)
    except OSError:
        return

    try:
        log_path.unlink()
    except OSError:
        pass


def audit(cmd: str, risk: int, decision: str, detail: str = "") -> None:
    """Append to audit log with size/age-based rotation.

    Rotation is handled inline (no background thread / cron). Cheap
    ``stat()`` on every append keeps the common path fast.
    """
    try:
        log_path = _audit_log_path()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        _rotate_audit_log(log_path)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "ts": datetime.now().isoformat(),
                        "cmd": cmd[:200],
                        "risk": f"R{risk}",
                        "decision": decision,
                        "detail": detail,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    except Exception:
        pass


def _notify_deny(reason: str, config: dict) -> None:
    """Notify user when a destructive operation is denied."""
    if not config.get("notify_on_deny", True):
        return
    import sys

    print(f"\033[91m\U0001f6e1️ BLOCKED: {reason}\033[0m", file=sys.stderr)
    try:
        from concinno.core.notify import show_toast

        show_toast(
            "Claude Code",
            f"\U0001f6e1️ Blocked | {reason[:80]}",
            enabled=True,
            tag="destruction-guard",
            group="concinno",
        )
    except Exception:
        pass


# ─── Hook entry point: governance wrapper over substrate evaluate ─


def evaluate(tool_name: str, tool_input: dict) -> dict:
    """Concinno governance wrapper over the Lyceum substrate evaluate.

    Adds: feature-flag honor, audit-log writes, auto-backup before R2+
    operations, toast-notify on deny. Substrate kernel handles
    classification + AskUserQuestion template + safer-alternatives.

    Returns the same hook-decision dict shape as the substrate
    (``{"permissionDecision": ..., "reason": ..., "additionalContext": ...}``).
    """
    config = load_config()
    if not config.get("enabled", True):
        return {"permissionDecision": "allow"}

    # Substrate decision (no I/O)
    substrate_result = _lyceum_evaluate(tool_name, tool_input)

    if tool_name == "Write":
        fp = tool_input.get("file_path", "")
        content = tool_input.get("content", "")
        risk, _reason = classify_write(fp, content)
        if substrate_result.get("permissionDecision") == "deny":
            bid = backup_file(fp, config)
            audit(f"Write:{fp}", risk, "block", _reason)
            msg = substrate_result.get("reason", "")
            if bid:
                msg = msg.replace(
                    "Use AskUserQuestion to confirm overwrite with user",
                    f"\U0001f4be Backed up: {bid}\n\n"
                    "\U0001f527 Use AskUserQuestion to confirm overwrite with user",
                )
            _notify_deny(f"Write overwrite: {fp}", config)
            substrate_result["reason"] = msg
        return substrate_result

    if tool_name != "Bash":
        return substrate_result

    command = tool_input.get("command", "")
    if not command:
        return substrate_result

    risk, reason = classify_bash(command)

    decision = substrate_result.get("permissionDecision", "allow")
    if decision == "allow":
        # R0 (silent) / R1 (auto-backup low risk) / R2+confirmed / R3+confirmed+reason
        if risk == R1:
            backup_targets(command, config)
            audit(command, risk, "allow", "low risk")
        elif risk >= R2:
            confirmed, conf_reason = check_confirmed(command)
            if confirmed:
                bid = backup_targets(command, config)
                detail_bits = []
                if conf_reason:
                    detail_bits.append(f"reason:{conf_reason}")
                else:
                    detail_bits.append("confirmed")
                if bid:
                    detail_bits.append(f"backup:{bid}")
                audit(command, risk, "allow", "|".join(detail_bits))
        return substrate_result

    # decision == "deny" — substrate already produced reason +
    # ask_user_question_template + safer_alternatives. Add Concinno
    # audit + notify side-effects.
    audit(command, risk, "block", reason)
    _notify_deny(substrate_result.get("reason", ""), config)
    return substrate_result


# ─── CLI helpers (Concinno-specific) ─────────────────────────────


def list_backups() -> str:
    """List all backups. Returns formatted string."""
    config = load_config()
    bdir = _backup_dir(config)
    if not bdir.exists():
        return "No backups found."

    lines = ["Destruction Guard Backups:", ""]
    count = 0
    for entry in sorted(bdir.iterdir(), reverse=True):
        manifest = entry / "manifest.json"
        if not manifest.exists():
            continue
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except Exception:
            continue
        count += 1
        pin = " \U0001f4cc" if data.get("pinned") else ""
        ts = data.get("timestamp", "?")[:19]
        btype = data.get("type", "?")
        targets = data.get("targets", data.get("file", "?"))
        if isinstance(targets, list):
            targets = ", ".join(Path(t).name for t in targets[:3])
        lines.append(f"  {data['id']}{pin}  [{ts}]  {btype}  {targets}")

    if count == 0:
        return "No backups found."
    lines.insert(1, f"  Total: {count}")
    return "\n".join(lines)


def cleanup_backups(keep_days: Optional[int] = None) -> str:
    """Remove expired backups. Returns summary."""
    config = load_config()
    bdir = _backup_dir(config)
    if not bdir.exists():
        return "No backups directory."

    days = keep_days or config["backup"].get("cleanup_keep_days", 7)
    cutoff = datetime.now().timestamp() - days * 86400
    removed = 0

    for entry in list(bdir.iterdir()):
        manifest = entry / "manifest.json"
        if not manifest.exists():
            continue
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except Exception:
            continue
        if data.get("pinned"):
            continue
        ts = datetime.fromisoformat(data["timestamp"]).timestamp()
        if ts < cutoff:
            shutil.rmtree(entry, ignore_errors=True)
            removed += 1

    return f"Cleaned {removed} backup(s) older than {days} days."


def _restore_single_target(src: Path, dst: Path) -> None:
    """Copy one backup entry to its original location."""
    if src.is_dir():
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
        return
    shutil.copy2(src, dst)


def restore_backup(backup_id: str) -> str:
    """Restore a backup by ID."""
    config = load_config()
    bdir = _backup_dir(config) / backup_id
    manifest = bdir / "manifest.json"
    if not manifest.exists():
        return f"Backup not found: {backup_id}"

    data = json.loads(manifest.read_text(encoding="utf-8"))
    restored: list[str] = []

    if data.get("type") == "write_overwrite":
        orig_file = data.get("file", "")
        for f in bdir.iterdir():
            if f.name == "manifest.json":
                continue
            shutil.copy2(f, orig_file)
            restored.append(orig_file)
        return f"Restored {len(restored)} item(s) from {backup_id}: {restored}"

    targets = data.get("targets", [])
    name_to_target = {Path(t).name: t for t in targets}
    for f in bdir.iterdir():
        if f.name == "manifest.json":
            continue
        dst = name_to_target.get(f.name)
        if dst is None:
            continue
        _restore_single_target(f, Path(dst))
        restored.append(dst)

    return f"Restored {len(restored)} item(s) from {backup_id}: {restored}"


def set_pin(backup_id: str, pinned: bool) -> str:
    """Pin or unpin a backup."""
    config = load_config()
    bdir = _backup_dir(config) / backup_id
    manifest = bdir / "manifest.json"
    if not manifest.exists():
        return f"Backup not found: {backup_id}"

    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
        data["pinned"] = pinned
        manifest.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        action = "Pinned" if pinned else "Unpinned"
        return f"{action}: {backup_id}"
    except Exception as e:
        return f"Failed to update backup: {e}"


# ─── @destruction_gate decorator (Concinno-specific) ─────────────


def _hook_context_permits(op_name: str) -> bool:
    """Decide whether we're running inside a trusted hook call.

    We treat the combination of ``CLAUDE_PROJECT_DIR`` + a per-op
    escape env flag as "hook context" — the stop/tool hook pipeline
    itself gates the operation upstream. Outside that combo, the
    gate fires.
    """
    if not os.environ.get("CLAUDE_PROJECT_DIR"):
        return False
    escape_map = {
        "squash_auto_commits": "CONCINNO_INLINE_SQUASH",
        "git_gc": "CONCINNO_GIT_GC",
        "prune": "CONCINNO_BACKUP_PRUNE",
        "rollback": "CONCINNO_GIT_ROLLBACK",
        "cleanup_stale_files": "CONCINNO_STALE_CLEANUP",
        "rotate_log_files": "CONCINNO_LOG_ROTATE",
    }
    flag = escape_map.get(op_name)
    if not flag:
        return False
    return os.environ.get(flag) == "1"


def destruction_gate(risk: str, op_name: str):
    """Decorator — intercept destructive calls outside hook context.

    Usage::

        @destruction_gate(risk="R3", op_name="git_gc")
        def git_gc(...): ...

    Behavior:
      * **Hook context** (``CLAUDE_PROJECT_DIR`` set + op-specific
        escape env flag = ``"1"``): pass-through.
      * **Direct call**: require ``reason=`` kwarg with a keyword
        from :data:`VALID_REASON_KEYWORDS` (>3 chars). Missing or
        invalid → raise :class:`DestructionBlockedError`. Audited
        either way.
    """

    def _decorator(fn):
        def _wrapper(*args, **kwargs):
            reason = kwargs.pop("reason", "")
            if _hook_context_permits(op_name):
                audit(
                    f"gated:{op_name}",
                    R2,
                    "allow",
                    f"hook_context|risk={risk}",
                )
                return fn(*args, **kwargs)

            reason_str = (reason or "").strip().lower()
            has_keyword = any(kw in reason_str for kw in VALID_REASON_KEYWORDS)
            if reason_str and len(reason_str) > 3 and has_keyword:
                audit(
                    f"gated:{op_name}",
                    R3,
                    "allow",
                    f"reason={reason_str[:80]}|risk={risk}",
                )
                return fn(*args, **kwargs)

            audit(
                f"gated:{op_name}",
                R3,
                "block",
                f"missing_reason|risk={risk}",
            )
            raise DestructionBlockedError(
                f"{op_name} is gated ({risk}). Pass reason=<keyword> "
                f"from: migrate/decommission/archive/redact/retire/... "
                f"Or run inside the hook pipeline with the op-specific "
                f"escape env flag."
            )

        _wrapper.__name__ = getattr(fn, "__name__", "_wrapper")
        _wrapper.__doc__ = fn.__doc__
        _wrapper.__wrapped__ = fn
        return _wrapper

    return _decorator


# ─── BaseGuard adapter (Concinno-specific) ───────────────────────


class DestructionGuard(BaseGuard):
    """Risk-based destructive operation interception + auto-backup."""

    name = "destruction_guard"
    category = GuardCategory.SECURITY

    def check(self, ctx: GuardContext) -> GuardResult | None:
        """Block destructive Bash/Write operations without user confirmation.

        Args:
            ctx: Guard context with tool_name and tool_input.

        Returns:
            GuardResult.deny with risk details and backup ID, or None if safe.
        """
        result = evaluate(ctx.tool_name, ctx.tool_input)
        if result.get("permissionDecision") == "deny":
            additional = result.get("additionalContext", "")
            if isinstance(additional, dict):
                additional_context = json.dumps(additional, ensure_ascii=False)
            else:
                additional_context = str(additional) if additional else ""
            return GuardResult.deny(
                result.get("reason", self.name),
                context=additional_context,
            )
        return None


# Substrate dataclass is part of the public API — silence unused import
# warning by exposing it explicitly.
__all__ = [
    "DEFAULT_CONFIG",
    "DestructionBlockedError",
    "DestructionGuard",
    "EvaluateResult",
    "R0",
    "R1",
    "R2",
    "R3",
    "R4",
    "R0_PATTERNS",
    "R1_PATTERNS",
    "R2_PATTERNS",
    "R3_PATTERNS",
    "R4_PATTERNS",
    "RISK_ICONS",
    "RISK_LABELS",
    "VALID_REASON_KEYWORDS",
    "_strip_echo_content",
    "audit",
    "backup_file",
    "backup_targets",
    "block_message",
    "check_confirmed",
    "check_destroy_confirmed",
    "classify_bash",
    "classify_write",
    "cleanup_backups",
    "confirm_with_options",
    "destruction_gate",
    "evaluate",
    "is_reason_valid_r4",
    "list_backups",
    "load_config",
    "restore_backup",
    "set_pin",
    "split_commands",
    "suggest_safer_alternative",
]
