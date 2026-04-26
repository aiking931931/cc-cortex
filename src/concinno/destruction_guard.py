"""concinno.destruction_guard — Risk-based destructive operation interception + auto-backup.

@module destruction_guard
@responsibility Five-level (R0-R4) risk classification for destructive operations,
    auto-backup before dangerous commands, user confirmation gates, and
    append-only audit logging. Used as library and via CLI.
@dependencies concinno.guards.base
@exports evaluate, classify_bash, classify_write, backup_targets, backup_file,
    audit, list_backups, cleanup_backups, restore_backup, DestructionGuard,
    confirm_with_options, suggest_safer_alternative,
    destruction_gate, DestructionBlockedError
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

from concinno.guards.base import BaseGuard, GuardCategory, GuardContext, GuardResult

# ─── Constants ────────────────────────────────────────────────────

R0, R1, R2, R3, R4 = 0, 1, 2, 3, 4

RISK_ICONS = {R0: "\U0001f7e2", R1: "\U0001f7e1", R2: "\U0001f7e0", R3: "\U0001f534", R4: "\u26d4"}
RISK_LABELS = {R0: "Safe", R1: "Low Risk", R2: "Medium Risk", R3: "High Risk", R4: "Forbidden"}

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

# ─── Valid Reason Keywords (R4: must contain one; R3: any >3 chars) ───

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
        "\u5ee2\u68c4",
        "\u5012\u9589",
        "\u8f49\u79fb",
        "\u9077\u79fb",
        "\u7d42\u6b62",
        "\u68c4\u7528",
        "\u4e0b\u67b6",
        "\u7d50\u675f",
        "\u95dc\u9589",
        "\u6dd8\u6c70",
        "\u9000\u5f79",
        "\u653e\u68c4",
        "\u4e0d\u8981\u4e86",
        "\u91cd\u69cb",
        # Japanese
        "\u5ec3\u6b62",
        "\u5012\u7523",
        "\u79fb\u884c",
        "\u9589\u9396",
        "\u7d42\u4e86",
        # Korean
        "\ud3d0\uae30",
        "\ud3d0\uc5c5",
        "\uc774\uc804",
        "\uc885\ub8cc",
        "\ucca0\uc218",
        "\ud3ec\uae30",
        # French
        "fermeture",
        "migration",
        "abandon",
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

# ─── Risk Patterns ────────────────────────────────────────────────

R0_PATTERNS = [
    r"--dry-run",
    r"--what-if",
    r"-WhatIf",
    r"git\s+clean\s+-n",
    r"terraform\s+plan\b",
    r"rm\s+[^-]\S*\.(tmp|temp|log|bak)$",
]

R1_PATTERNS = [
    r"rm\s+-r[f]?\s+\.?/?(node_modules|dist|build|\.next|__pycache__|"
    r"\.cache|\.venv|venv|\.tox|\.pytest_cache|\.turbo|\.parcel-cache)\b",
    r"git\s+stash\s+drop",
    r"docker\s+container\s+prune\b(?!.*-a)",
    r"pip\s+cache\s+purge",
    r"npm\s+cache\s+clean",
    r"pnpm\s+store\s+prune",
]

R2_PATTERNS = [
    r"rm\s+-[rR]f?\s+\S+",
    r"del\s+/[sS]",
    r"rd\s+/[sS]\s+/[qQ]",
    r"Remove-Item.*-Recurse",
    r"DROP\s+TABLE",
    r"TRUNCATE\s+TABLE",
    r"DELETE\s+FROM\s+\w+\s*;?\s*$",
    r"git\s+branch\s+-[dD]\s+",  # both -d/-D caught here
    r"docker\s+(rm|rmi)\s+",
    r"npm\s+unpublish",
    r"pip\s+uninstall",
    r"chmod\s+(-R\s+)?[0-7]*7[0-7]*\s+/(etc|var|usr|home|opt|sys)",
    r"useradd\s+.*-u\s+0\b",
    r"usermod\s+.*-u\s+0\b",
    r"ALTER\s+TABLE\s+\w+\s+DROP\b",
    # NOTE 2026-04-26 (3.1.3): ``npm\s+publish\b`` removed from R2 alongside
    # ``twine upload`` (already removed 2026-04-21 / 3.1.1) — both publish
    # operations now live in ``concinno.release_authorization`` so the
    # ``release_auth.disabled`` toggle can opt them out without weakening the
    # data-deletion patterns in this module. Doc reshuffle was 2026-04-21;
    # the npm regex was overlooked until the wiring audit caught it.
    r"docker\s+run\s+.*--privileged",
]

R3_PATTERNS = [
    r"terraform\s+destroy",
    r"pulumi\s+destroy",
    r"kubectl\s+delete\s+(namespace|deployment|statefulset|pvc|pv)\b",
    r"docker\s+system\s+prune\s+-a",
    r"docker\s+volume\s+prune",
    r"git\s+push\s+--force",
    r"git\s+push\s+-f\b",
    r"git\s+reset\s+--hard",
    r"DROP\s+(DATABASE|SCHEMA)",
    r"heroku\s+(apps|addons):destroy",
    r"aws\s+s3\s+rb\s+.*--force",
    r"aws\s+cloudformation\s+delete-stack",
    r"gcloud\s+(projects|compute)\s+delete",
    r"az\s+(group|vm|webapp)\s+delete",
    r"rm\s+-[rR]f\s+/(var|etc|usr|home|opt|srv|data)\b",
    r"rm\s+-[rR]f\s+\.\s*$",
    r"base64\s+.*\|\s*(ba)?sh",
    r"curl\s+.*\|\s*(ba)?sh",
    r"wget\s+.*\|\s*(ba)?sh",
    r"eval\s+.*\$",
    r"export\s+LD_PRELOAD\b",
    r"export\s+PATH\s*=",
    r"mongosh?\s+.*drop(Database|Collection)",
    r"kubectl\s+exec\s+.*-it?\s+.*prod",
    r"cat\s+.*\.\./\.\./",
    r"FORMAT\s+[A-Z]:",
    # Reverse shells
    r"/dev/tcp/",
    r"nc\s+.*-[elp]",
    r"ncat\s+.*-[elp]",
    r"socket\.connect\(",
    r"fsockopen\(",
    r"python\s+-c\s+.*socket.*connect",
    r"perl\s+-e\s+.*Socket.*connect",
    r"php\s+-r\s+.*fsockopen",
    # Log tampering
    r"history\s+-c",
    r">\s*/var/log/",
    r"shred\s+.*(/var/log|\.log)",
    r"rm\s+.*\.bash_history",
    # Cron / persistence
    r"crontab\s+-",
    r">>\s*/etc/crontab",
    r"/etc/systemd/system/",
    r"systemctl\s+enable\s+",
    r"\|\s*at\s+(now|midnight|noon)",
    # SSH manipulation
    r">>\s*.*authorized_keys",
    r">>\s*/etc/ssh/sshd_config",
    r"ssh\s+-R\s+",
    # Network recon
    r"nmap\s+",
    r"masscan\s+",
    r"nc\s+.*-l",
    r"tcpdump\s+",
    # Crypto mining
    r"xmrig",
    # Firewall / security disable
    r"iptables\s+-F",
    r"ufw\s+disable",
    r"setenforce\s+0",
    # Process kill (dangerous)
    r"kill\s+-9\s+-1",
    r"killall\s+(sshd|nginx|postgres|mysql|mongod)",
    r"pkill\s+-9\s+(postgres|mysql|mongod|redis|nginx)",
    # Symlink attacks on system files
    r"ln\s+-s[f]?\s+.*\s+/etc/(passwd|shadow|cron|systemd|ssh)",
    r"ln\s+-s[f]?\s+/etc/(passwd|shadow|cron)",
    # Disk wipe
    r"shred\s+.*(/dev/sd|/dev/nvm)",
    r"wipefs\s+",
    # Other
    r"helm\s+uninstall\s+.*prod",
    r"redis-cli\s+(FLUSHALL|FLUSHDB)",
    # NOTE 2026-04-21..26: ``twine upload`` (3.1.1), ``docker push <public-registry>``
    # (3.1.1), and ``npm publish`` (3.1.3) moved out of destruction_guard into
    # ``concinno.release_authorization`` so the publish authorisation toggle
    # (``~/.concinno/release_auth.json::disabled``) can opt out of the publish
    # gate without disabling the data-deletion patterns in this module. The
    # rule docstring in ``rules/official/L1/release_coord.md`` was updated
    # 2026-04-21; the npm regex was overlooked until the 2026-04-26 wiring
    # audit. Keeping ``docker push`` private-registry-only matches is
    # unnecessary because the destination registry is a runtime arg, not a
    # token.
    r"cp\s+.*\s+/etc/(ssh|passwd|shadow|cron)",
    r"xargs\s+rm\s+-rf",
    r"export\s+(HOME|PYTHONPATH)\s*=",
    # Backtick/subshell obfuscation
    r"`.*`\s+-rf",
    # Cloud destructive ops
    r"aws\s+ec2\s+terminate-instances",
    r"aws\s+ec2\s+delete-snapshot",
    r"aws\s+ecr\s+batch-delete-image",
    r"gcloud\s+container\s+images\s+delete",
    r"skopeo\s+delete\b",
    # DNS manipulation
    r">>\s*/etc/hosts\b",
    r">\s*/etc/resolv\.conf",
    r"sed\s+.*-i.*\s+/etc/(hosts|resolv\.conf|nsswitch\.conf)",
    # Backup destruction
    r"restic\s+forget\b",
    # Git reachable-object prune (今天踩過：`gc --prune=now` + `--aggressive`
    # 不可逆移除 reachable 以外的物件 + reflog expire 後舊 commit 永久消失。
    # Matches bare `git gc`-family invocations that skip the cache/TTL
    # heuristics and force immediate destruction of pack/reflog state.)
    r"git\s+gc\s+.*--prune=(now|all|\d+\.\w+\.ago)\b",
    r"git\s+gc\s+.*--aggressive\b",
    r"git\s+reflog\s+expire\s+.*--expire=now\b",
    r"git\s+prune\s+--expire=now\b",
    r"git\s+filter-(branch|repo)\s+",
    # BFG repo cleaner — rewrites history irreversibly
    r"bfg\s+.*--strip-blobs-bigger-than\b",
    r"bfg\s+.*--delete-files\b",
    # Credential stuffing / offensive tools
    r"\bhydra\s+",
    r"\bmedusa\s+.*-[hHuUPM]",
    r"\bhashcat\s+",
    # Web exploitation tools
    r"\bsqlmap\s+",
    r"\bnikto\s+",
    r"\bgobuster\s+",
    # Wireless attack tools
    r"\baircrack-ng\s+",
    r"\baireplay-ng\s+",
    r"\bairodump-ng\s+",
    # Memory forensics evasion
    r">\s*/proc/sys/vm/drop_caches",
    r"\bsdmem\s+",
    # Supply chain attacks
    r"pip\s+install\s+.*--index-url\s+https?://(?!pypi\.org)",
    r"pip\s+install\s+.*--extra-index-url\s+https?://(?!pypi\.org)",
    r"npm\s+config\s+set\s+registry\s+https?://(?!registry\.npmjs\.org)",
    r"gem\s+install\s+.*--source\s+https?://(?!rubygems\.org)",
    # Git history manipulation
    r"git\s+filter-branch\b",
    r"\bbfg\b.*\.(jar|exe)",
    r"git\s+reflog\s+expire\b",
    r"git\s+push\s+--force\s+--all",
    # Kernel module attacks
    r"\binsmod\s+",
    r"\bmodprobe\s+(?!-r\b)",
    r"\brmmod\s+",
    # Disk encryption attacks
    r"cryptsetup\s+(luksErase|luksRemoveKey|luksKillSlot)",
    # Certificate manipulation
    r"cp\s+.*\s+/usr/local/share/ca-certificates/",
    r"update-ca-certificates",
    r"cp\s+.*\s+/etc/(ssl|pki)/",
    r">\s*/etc/(ssl|pki)/",
    # Service disruption / DoS
    r"\bstress\s+--",
    r"\bhping3?\s+",
    r"while\s+true\s*;\s*do\b.*&\s*done",
    r"dd\s+if=/dev/(zero|urandom)\s+of=/tmp/",
    # Data corruption
    r"dd\s+if=/dev/(zero|urandom)\s+of=/var/",
    r"\btruncate\s+-s\s+0\s+",
    r"sed\s+-i\s+.*\s+/etc/(fstab|passwd|shadow|group)",
    r"\bfallocate\s+-l\s+\d+[GT]\s+",
    # Encoding bypasses
    r"xxd\s+.*\|\s*(ba)?sh",
    r"python3?\s+-c\s+.*exec\s*\(",
    # Time manipulation
    r"\bdate\s+-s\s+",
    r"timedatectl\s+set-time\b",
    r"\bntpdate\s+",
    # User manipulation
    r"\bchpasswd\b",
    r">>\s*/etc/sudoers\b",
    r"usermod\s+.*-[aG]+.*root",
    # Audit evasion
    r"auditctl\s+-e\s*0",
    r"journalctl\s+--vacuum-(size|time)=0",
    r">\s*/var/log/(wtmp|utmp|btmp|lastlog)",
    r"unset\s+HISTFILE",
    # Package repository attacks
    r">>\s*/etc/apt/sources\.list",
    r">\s*/etc/yum\.repos\.d/",
    r"apt-key\s+add\s+-",
    # Crypto mining (additional)
    r"\bcpuminer\b",
    r"\bminerd\b",
    # Privilege escalation
    r"chmod\s+.*\+s\s+/usr/",
    r"\bsetcap\s+",
    r">\s*/etc/ld\.so\.preload",
    r">\s*/etc/pam\.d/",
    # Ransomware patterns
    r"find\s+.*-exec\s+openssl\s+enc\b",
    r"find\s+.*-exec\s+mv\s+.*encrypted",
    r"gpg\s+.*--passphrase\s+.*\s+-c\s+",
    # Network tunneling
    r"\bsocat\s+TCP-LISTEN",
    r"\bchisel\s+client\b",
    r"ssh\s+-D\s+",
    # Bootloader attacks
    r"\bgrub-install\s+",
    r"\befibootmgr\s+.*-B",
    # Process injection / tracing
    r"\bptrace\s*\(",
    r"\bstrace\s+.*-p\s+",
    r"\bgdb\s+.*-p\s+.*call\s+system",
]

R4_PATTERNS = [
    r"rm\s+-[rR]f\s+/\s*$",
    r"rm\s+-[rR]f\s+/\*",
    r"rm\s+-[rR]f\s+~/?$",
    r"rm\s+-[rR]f\s+\$HOME/?$",
    r"mkfs\.",
    r"dd\s+if=.*of=/dev/",
    r":\(\)\s*\{\s*:\|:&\s*\}\s*;:",
    r"terraform\s+destroy.*--auto-approve.*(prod|production)",
    r"kubectl\s+delete\s+namespace\s+(prod|production|default)\b",
    r"rm\s+-[rR]f\s+/?(Windows|System32|Program\s*Files)",
]


# ─── Config ──────────────────────────────────────────────────────


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


# ─── Classification ──────────────────────────────────────────────


def _strip_echo_content(cmd: str) -> str:
    """Strip echo/printf quoted content and heredocs to prevent false positives."""
    result = re.sub(r"""(?:echo|printf)\s+(?:-[neE]+\s+)?(['"])(.*?)\1""", "echo", cmd)
    result = re.sub(r"<<-?\s*['\"]?(\w+)['\"]?.*?\1", "", result, flags=re.DOTALL)
    return result


def split_commands(cmd: str) -> list[str]:
    """Split compound commands on ||, &&, ;, |."""
    parts = re.split(r"\s*(?:\|\||&&|;)\s*", cmd)
    result = []
    for p in parts:
        result.extend(re.split(r"\s*\|\s*", p))
    return [x.strip() for x in result if x.strip()]


def classify_bash(command: str) -> tuple[int, str]:
    """Classify Bash command risk. Returns (risk_level, reason)."""
    # Strip #DESTROY_CONFIRMED[:reason] tag before pattern matching so that
    # $ anchored patterns (e.g. R4's `rm -rf / $`) still match correctly
    # when the user appends confirmation. The evaluate() function reads
    # confirmation from the original command separately.
    stripped = re.sub(r"\s*#DESTROY_CONFIRMED(?::.+)?$", "", command)
    cleaned = _strip_echo_content(stripped)

    # Case-sensitive safe exits: git branch -d (lowercase) is safe
    # because it only deletes merged branches. -D (uppercase) is
    # force delete and should be caught by R2.
    if re.search(r"git\s+branch\s+-d\s+", cleaned) and not re.search(
        r"git\s+branch\s+-D\s+", cleaned
    ):
        return R0, ""

    # Pre-split check: patterns that span pipe/chain boundaries
    # (e.g. fork bomb, curl|bash, base64|bash)
    for p in R4_PATTERNS:
        if re.search(p, cleaned, re.IGNORECASE):
            reason = "Catastrophic operation — potentially irreversible total loss"
            return R4, f"{RISK_ICONS[R4]} {reason}"
    for p in R3_PATTERNS:
        if re.search(p, cleaned, re.IGNORECASE):
            reason = "High risk — potential large-scale data loss"
            return R3, f"{RISK_ICONS[R3]} {reason}"

    parts = split_commands(cleaned)
    max_risk, max_reason = R0, ""

    for part in parts:
        if any(re.search(p, part, re.IGNORECASE) for p in R0_PATTERNS):
            continue
        if any(re.search(p, part, re.IGNORECASE) for p in R1_PATTERNS):
            if R1 > max_risk:
                max_risk, max_reason = R1, f"{RISK_ICONS[R1]} Low risk — deleting regenerable files"
            continue
        for risk, patterns, label in [
            (R4, R4_PATTERNS, "Catastrophic operation — potentially irreversible total loss"),
            (R3, R3_PATTERNS, "High risk — potential large-scale data loss"),
            (R2, R2_PATTERNS, "Medium risk — may delete important files or data"),
        ]:
            if any(re.search(p, part, re.IGNORECASE) for p in patterns):
                if risk > max_risk:
                    max_risk, max_reason = risk, f"{RISK_ICONS[risk]} {label}"
                break

    return max_risk, max_reason


def classify_write(file_path: str, new_content: str) -> tuple[int, str]:
    """Detect dangerous Write overwrites (empty/massive reduction)."""
    if not file_path or not os.path.exists(file_path):
        return R0, ""
    try:
        existing_size = os.path.getsize(file_path)
    except OSError:
        return R0, ""

    new_size = len(new_content.encode("utf-8")) if new_content else 0

    if existing_size > 500 and new_size == 0:
        return R3, (
            f"{RISK_ICONS[R3]} Empty overwrite detected — will erase {existing_size:,} bytes"
        )
    if existing_size > 1000 and new_size < existing_size * 0.1:
        pct = 100 - (new_size * 100 // existing_size)
        return R2, (
            f"{RISK_ICONS[R2]} Content reduction {existing_size:,} -> {new_size:,} bytes "
            f"({pct}% loss)"
        )
    return R0, ""


# ─── Confirmation ────────────────────────────────────────────────


def check_confirmed(text: str) -> tuple[bool, str]:
    """Check for #DESTROY_CONFIRMED[:reason] marker."""
    m = re.search(r"#DESTROY_CONFIRMED(?::(.+?))?$", text)
    if m:
        return True, (m.group(1) or "").strip()
    return False, ""


def is_reason_valid_r4(reason: str) -> bool:
    """R4 requires specific keyword in reason."""
    rl = reason.lower()
    return any(kw in rl for kw in VALID_REASON_KEYWORDS)


# ─── Backup ──────────────────────────────────────────────────────


def _backup_dir(config: dict) -> Path:
    raw = config.get("backup", {}).get("dir", ".destruction_backups")
    p = Path(raw)
    if not p.is_absolute():
        p = _project_dir() / p
    return p


def backup_targets(command: str, config: dict) -> Optional[str]:
    """Auto-backup targets before destructive Bash operation. Returns backup ID or None."""
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
                    f.stat().st_size for f in tp.rglob("*") if f.is_file() and f.name not in exclude
                )
                if total <= max_bytes:
                    shutil.copytree(tp, bpath / tp.name, ignore=shutil.ignore_patterns(*exclude))
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


# ─── Audit ───────────────────────────────────────────────────────


_AUDIT_ROTATE_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
_AUDIT_ROTATE_MAX_AGE_DAYS = 90
_AUDIT_ROTATE_KEEP = 3  # .log.1.gz .. .log.3.gz


def _rotate_audit_log(log_path: Path) -> None:
    """Rotate audit log when it exceeds size or age threshold.

    Keeps ``_AUDIT_ROTATE_KEEP`` compressed archives (``.log.1.gz`` newest,
    ``.log.<KEEP>.gz`` oldest). Called from :func:`audit` before each
    append — best-effort, never raises. Cheap `stat()` on the common
    "no rotation needed" path.
    """
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

    # Shift existing archives: .log.(N-1).gz -> .log.N.gz
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

    # Compress current log into .log.1.gz
    try:
        with open(log_path, "rb") as src_f:
            archive_path = log_path.with_name(f"{log_path.name}.1.gz")
            with gzip.open(archive_path, "wb") as dst_f:
                shutil.copyfileobj(src_f, dst_f)
    except OSError:
        return

    # Truncate the active log
    try:
        log_path.unlink()
    except OSError:
        pass


def audit(cmd: str, risk: int, decision: str, detail: str = "") -> None:
    """Append to audit log with size/age-based rotation.

    Rotation is handled inline (no background thread / cron). Cheap
    ``stat()`` on every append keeps the common path fast. When the log
    hits 10 MB or 90 days old, it's gzipped to ``destruction_audit.log.1.gz``
    and the chain shifts; we keep the last 3 archives.
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


# ─── Block Messages ──────────────────────────────────────────────


def block_message(risk: int, command: str, reason: str) -> str:
    """Generate human-readable block message for a given risk level."""
    cmd_preview = command[:100] + ("..." if len(command) > 100 else "")
    sep = "\u2501" * 20

    if risk == R2:
        return (
            f"\U0001f7e0 MEDIUM RISK — Confirmation Required\n{sep}\n"
            f"\U0001f4cb Command: `{cmd_preview}`\n"
            f"\u26a0\ufe0f  Impact: {reason}\n\n"
            f"\U0001f527 Use AskUserQuestion to confirm with user:\n"
            f"   1. Explain what will be deleted and the impact scope\n"
            f"   2. If user agrees, retry with #DESTROY_CONFIRMED appended"
        )
    elif risk == R3:
        return (
            f"\U0001f534 HIGH RISK — Confirmation + Reason Required\n{sep}\n"
            f"\U0001f4cb Command: `{cmd_preview}`\n"
            f"\U0001f480 Impact: {reason}\n\n"
            f"\U0001f527 Use AskUserQuestion to confirm with user:\n"
            f"   1. Explain target, impact scope, and irreversibility\n"
            f"   2. Ask user for their reason\n"
            f"   3. If user agrees, retry with #DESTROY_CONFIRMED:<reason>"
        )
    else:  # R4
        return (
            f"\u26d4 CATASTROPHIC — Forbidden by Default\n{sep}\n"
            f"\U0001f4cb Command: `{cmd_preview}`\n"
            f"\u2620\ufe0f  Highest risk level. Potentially irreversible total loss.\n\n"
            f"\U0001f527 Only allowed with a valid justification keyword:\n"
            f"   abandon/bankrupt/migrate/transfer/decommission/sunset/EOL/\n"
            f"   \u5ee2\u68c4/\u5012\u9589/\u8f49\u79fb/\u7d42\u6b62/\u653e\u68c4/"
            f"\u5ec3\u6b62/\u5012\u7523/\ud3d0\uae30/\ud3d0\uc5c5/fermeture/stilllegung/cierre\n"
            f"   Use AskUserQuestion, then retry with #DESTROY_CONFIRMED:<valid reason>"
        )


# ─── Hook Entry Point ────────────────────────────────────────────


def _notify_deny(reason: str, config: dict) -> None:
    """Notify user when a destructive operation is denied."""
    if not config.get("notify_on_deny", True):
        return
    import sys

    # stderr ANSI red — visible in terminal
    print(f"\033[91m\U0001f6e1\ufe0f BLOCKED: {reason}\033[0m", file=sys.stderr)
    # Toast notification (best-effort, non-blocking)
    try:
        from concinno.core.notify import show_toast

        show_toast(
            "Claude Code",
            f"\U0001f6e1\ufe0f Blocked | {reason[:80]}",
            enabled=True,
            tag="destruction-guard",
            group="concinno",
        )
    except Exception:
        pass


def evaluate(tool_name: str, tool_input: dict) -> dict:
    """Evaluate a tool call and return hook decision.

    Returns: {"permissionDecision": "allow"} or {"permissionDecision": "deny", "reason": "..."}
    This is the main entry point called by hook templates.
    """
    config = load_config()

    if not config.get("enabled", True):
        return {"permissionDecision": "allow"}

    # Write tool
    if tool_name == "Write":
        fp = tool_input.get("file_path", "")
        content = tool_input.get("content", "")
        risk, reason = classify_write(fp, content)
        if risk == R0:
            return {"permissionDecision": "allow"}
        bid = backup_file(fp, config)
        audit(f"Write:{fp}", risk, "block", reason)
        msg = (
            f"{RISK_ICONS[risk]} File Overwrite Protection\n"
            f"\u2501" * 20 + "\n"
            f"\U0001f4c4 File: `{fp}`\n"
            f"\u26a0\ufe0f  {reason}\n"
        )
        if bid:
            msg += f"\U0001f4be Backed up: {bid}\n"
        msg += "\n\U0001f527 Use AskUserQuestion to confirm overwrite with user"
        _notify_deny(f"Write overwrite: {fp}", config)
        return {"permissionDecision": "deny", "reason": msg}

    # Bash tool
    if tool_name != "Bash":
        return {"permissionDecision": "allow"}

    command = tool_input.get("command", "")
    if not command:
        return {"permissionDecision": "allow"}

    risk, reason = classify_bash(command)

    if risk == R0:
        return {"permissionDecision": "allow"}

    if risk == R1:
        backup_targets(command, config)
        audit(command, risk, "allow", "low risk")
        return {"permissionDecision": "allow"}

    # R2+: check confirmation
    confirmed, conf_reason = check_confirmed(command)

    if confirmed:
        if risk == R2:
            bid = backup_targets(command, config)
            audit(command, risk, "allow", f"confirmed|backup:{bid}")
            return {"permissionDecision": "allow"}

        if risk == R3:
            if conf_reason and len(conf_reason) > 3:
                bid = backup_targets(command, config)
                audit(command, risk, "allow", f"reason:{conf_reason}|backup:{bid}")
                return {"permissionDecision": "allow"}
            msg = (
                "\U0001f534 High risk requires a reason after #DESTROY_CONFIRMED:\n"
                "Format: #DESTROY_CONFIRMED:<user's reason>"
            )
            _notify_deny(msg, config)
            return {"permissionDecision": "deny", "reason": msg}

        if risk == R4:
            if conf_reason and is_reason_valid_r4(conf_reason):
                bid = backup_targets(command, config)
                audit(command, risk, "allow", f"R4:{conf_reason}|backup:{bid}")
                return {"permissionDecision": "allow"}
            msg = (
                "\u26d4 R4 requires a valid justification keyword.\n"
                "Accepted: abandon/migrate/decommission/sunset/EOL/"
                "\u5ee2\u68c4/\u5012\u9589/\u8f49\u79fb/\u7d42\u6b62 etc.\n"
                "Format: #DESTROY_CONFIRMED:<valid keyword reason>"
            )
            _notify_deny(msg, config)
            return {"permissionDecision": "deny", "reason": msg}

    # Not confirmed — block
    audit(command, risk, "block", reason)
    msg = block_message(risk, command, reason)
    _notify_deny(msg, config)

    # Preload AskUserQuestion template + safer alternatives so the LLM
    # can surface concrete choices to the user on the next turn
    # (CC L6 can't raise AskUser directly from a hook — see
    # confirm_with_options docstring).
    extras: dict[str, object] = {}
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
                },
            )
        # Always include the "proceed with reason" escape.
        options.append(
            {
                "label": "proceed with #DESTROY_CONFIRMED:<reason>",
                "description": (
                    "Retry the original command with a valid reason "
                    "keyword appended (migrate/decommission/archive/...)."
                ),
                "preview": f"{command.strip()} #DESTROY_CONFIRMED:<reason>",
            },
        )
        # confirm_with_options enforces 2-4 options; trim to 4 max.
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
    result: dict = {"permissionDecision": "deny", "reason": msg}
    if extras:
        result["additionalContext"] = extras
    return result


# ─── CLI Functions (called by concinno backup ...) ──────────────


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
    """Copy one backup entry to its original location.

    Directory targets: remove stale dst (if any) then copytree. File
    targets: copy2 (preserves metadata). Extracted out of restore_backup
    to flatten 6-deep nesting (butterfly #7, 2026-04-18).
    """
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

    # Directory-style backup: match by basename against recorded targets.
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

    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["pinned"] = pinned
    manifest.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    action = "Pinned" if pinned else "Unpinned"
    return f"{action}: {backup_id}"


# ── AskUserQuestion template + safer-alternatives (P1 #5, #6) ───


def confirm_with_options(
    prompt: str,
    options: list[dict],
    default: str = "abort",
) -> dict:
    """Build an AskUserQuestion template.

    CC's L6 hook API cannot directly call AskUserQuestion from a hook
    subprocess — it can only return a deny + reason. We therefore
    package a question-template dict that the LLM is instructed to
    paste into a real AskUserQuestion call on the next turn.

    Args:
        prompt: Main question shown to the user.
        options: List of ``{"label", "description", "preview"}`` entries.
            Must be 2-4 items to satisfy CC AskUserQuestion schema.
        default: Label to treat as default when user dismisses.
            Defaults to ``"abort"`` (always safest).

    Returns:
        ``{"question", "options", "default"}`` suitable for
        `additionalContext.ask_user_question_template`.

    Raises:
        ValueError: If options count is not 2-4.
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


# Static lookup: dangerous commands → safer alternatives.
# Each entry returns a list of {label, description, command} proposals
# the LLM can show the user in an AskUserQuestion. Ordering is worst->best.
_SAFER_ALTERNATIVES: list[tuple[re.Pattern[str], list[dict]]] = [
    (
        re.compile(r"\brm\s+-f\s+.*\.lock\b", re.IGNORECASE),
        [
            {
                "label": "python unlink(missing_ok=True)",
                "description": (
                    "Idempotent removal with stdlib. Doesn't invoke shell rm "
                    "so destruction_guard doesn't need to gate it."
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
                    "mv to _trash/<timestamp>/ — reversible for TTL window, "
                    "cleanup handled by periodic /tidy."
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
                    "Respects gc.auto + pack.deltaCacheLimit; only repacks "
                    "when thresholds hit. Non-destructive."
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
                    "PyPI upload is irreversible — `--skip-existing` lets "
                    "retries be safe when a previous attempt partially "
                    "published."
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
                    "Preserves data for audit + easy undo. Drop for real "
                    "only after verifying no consumers."
                ),
                "command": (
                    "ALTER TABLE <name> RENAME TO "
                    "<name>_deprecated_<YYYYMMDD>;"
                ),
            },
        ],
    ),
    (
        re.compile(r"\bkubectl\s+delete\s+(ns|namespace)\s+\S+", re.IGNORECASE),
        [
            {
                "label": "snapshot resources before delete",
                "description": (
                    "Dump all resources to YAML so the ns can be rebuilt "
                    "if the delete turns out to be wrong."
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
                    "Removes stopped containers + dangling images only. "
                    "`-a` additionally nukes all unused images incl. base "
                    "layers you'd pull again."
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
                    "Turn on bucket versioning so deletes leave tombstones "
                    "and versioned objects stay recoverable for 30d."
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
                    "History rewrites break every existing clone. Do a "
                    "fresh mirror clone, rewrite there, verify, then "
                    "force-with-lease push."
                ),
                "command": (
                    "git clone --mirror <repo> && cd <repo>.git && "
                    "bfg --strip-blobs-bigger-than 10M"
                ),
            },
        ],
    ),
]


def suggest_safer_alternative(command: str) -> list[dict] | None:
    """Look up safer alternatives for a destructive command.

    Scans ``command`` against a curated table of known patterns and
    returns the first match. Callers (``evaluate()`` + ``/tidy``) use
    this to preload AskUserQuestion options when denying R2+ commands.

    Args:
        command: Bash command string as received by the hook.

    Returns:
        List of ``{"label", "description", "command"}`` proposals, or
        ``None`` when no pattern matches (no free lunch — stay silent).
    """
    if not command:
        return None
    cleaned = _strip_echo_content(command)
    for pattern, alternatives in _SAFER_ALTERNATIVES:
        if pattern.search(cleaned):
            # Return shallow copy so callers can't mutate the table.
            return [dict(alt) for alt in alternatives]
    return None


# ── @destruction_gate decorator (P1 #7) ─────────────────────────


class DestructionBlockedError(RuntimeError):
    """Raised when a decorated destructive function is called outside a
    hook context without a valid reason keyword.

    Hook-context calls (``CLAUDE_PROJECT_DIR`` set AND the per-op escape
    env flag raised) pass through silently — the hook pipeline itself is
    the auth layer there.
    """


def _hook_context_permits(op_name: str) -> bool:
    """Decide whether we're running inside a trusted hook call.

    We treat the combination of ``CLAUDE_PROJECT_DIR`` + a per-op escape
    env flag as "hook context" — the stop/tool hook pipeline itself gates
    the operation upstream. Outside that combo, the gate fires.

    Explicit escape flags keep the surface audit-visible — a rogue env
    var name can be grepped for post-mortem.
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
      * **Hook context** (``CLAUDE_PROJECT_DIR`` set + op-specific escape
        env flag = ``"1"``): pass-through — the stop/tool hook is the
        auth layer.
      * **Direct call** from user code: require ``reason=`` kwarg with
        a keyword from :data:`VALID_REASON_KEYWORDS` (>3 chars). Missing
        or invalid → raise :class:`DestructionBlockedError`. Audited
        either way.

    The gate recognises ``reason`` as an explicit kwarg; positional
    callers get a clear error.
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

            # Direct call path — enforce reason keyword.
            reason_str = (reason or "").strip().lower()
            has_keyword = any(
                kw in reason_str for kw in VALID_REASON_KEYWORDS
            )
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
        _wrapper.__wrapped__ = fn  # introspection
        return _wrapper

    return _decorator


# ── BaseGuard adapter ───────────────────────────────────────────


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
            # evaluate() now returns a dict under additionalContext
            # (ask_user_question_template + safer_alternatives). Serialize
            # to JSON string for the GuardResult.context channel so
            # downstream consumers (hook template renderer, tests) can
            # parse it back when needed.
            if isinstance(additional, dict):
                additional_context = json.dumps(
                    additional, ensure_ascii=False,
                )
            else:
                additional_context = str(additional) if additional else ""
            return GuardResult.deny(
                result.get("reason", self.name),
                context=additional_context,
            )
        return None
