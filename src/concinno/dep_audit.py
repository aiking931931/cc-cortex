"""concinno.dep_audit — Dependency typosquatting + scope spoofing + blocklist guard.

@module dep_audit
@responsibility Catch typosquatting attacks, npm scope spoofing, and known-malicious
    packages in pip/npm install commands. PreToolUse gate.
@dependencies concinno.constants, concinno.guards.base
@exports check, DepAuditGuard
"""

from __future__ import annotations

import os
import re
from typing import Optional

from concinno.constants import make_deny
from concinno.guards.base import BaseGuard, GuardCategory, GuardContext, GuardResult

# ── Known typosquatting patterns ─────────────────────────────────

# Map: typosquat → real package (common attacks)
_TYPOSQUATS: dict[str, str] = {
    # Python
    "reqeusts": "requests",
    "requets": "requests",
    "requsts": "requests",
    "beautifulsoup": "beautifulsoup4",
    "python-nmap": "nmap",
    "python-dateutil ": "python-dateutil",
    "djago": "django",
    "djnago": "django",
    "fask": "flask",
    "flak": "flask",
    "numppy": "numpy",
    "pandsa": "pandas",
    "scapy": "scipy",  # scapy is real but often confused
    "pyyml": "pyyaml",
    "httplib2": "httplib2",
    "urlib3": "urllib3",
    "psychopg2": "psycopg2",
    "psycopg": "psycopg2",
    # Node
    "lodahs": "lodash",
    "lodasch": "lodash",
    "axois": "axios",
    "axos": "axios",
    "momnet": "moment",
    "exrpess": "express",
    "epxress": "express",
    "recat": "react",
    "raect": "react",
}

# ── Known malicious packages (blocklist) ─────────────────────────

# Packages confirmed malicious / removed from registries.
# Source: PyPI removals, npm advisories, Snyk DB.
_BLOCKLIST: set[str] = {
    # Python — confirmed malicious (PyPI removed)
    "colourama",          # typosquat of colorama, steals credentials
    "python3-dateutil",   # steals SSH keys
    "jeIlyfish",          # jellyfish impersonator (capital I)
    "python-mongo",       # steals env vars
    "ctx",                # steals env vars (2022)
    "phpass",             # steals AWS creds (2022)
    "setup-tools",        # dash variant — steals tokens
    "distutils-precedence",  # fake distutils
    "mloab",              # typosquat of moab
    "python-binance-sdk", # fake Binance — steals API keys
    "tensarflow",         # TensorFlow typosquat
    "torchvisiion",       # torchvision typosquat
    "noblesse",           # steals Discord tokens + credit cards
    # Node — confirmed malicious (npm removed)
    "event-stream",       # cryptojacking via flatmap-stream
    "ua-parser-js",       # cryptominer injection (compromised)
    "coa",                # compromised — installs backdoor
    "rc",                 # compromised — env var stealer
    "colors.js",          # sabotaged by maintainer (infinite loop)
    "faker.js",           # sabotaged by maintainer
    "getcookies",         # backdoor via express middleware
    "eslint-scope",       # credential stealer (compromised)
    "crossenv",           # cross-env typosquat — env stealer
    "mongose",            # mongoose typosquat
    "babelcli",           # babel-cli typosquat
    "d3.js",              # d3 typosquat
    "gruntcli",           # grunt-cli typosquat
    "ffmepg",             # ffmpeg typosquat
    "discordi.js",        # discord.js typosquat — token stealer
}

# ── Scope spoofing patterns (npm) ────────────────────────────────

# Legitimate scopes → their real packages. Attackers register
# similar scope names to trick installs.
_KNOWN_SCOPES: dict[str, set[str]] = {
    "@angular": {"core", "cli", "common", "forms", "router", "compiler", "platform-browser"},
    "@types": set(),    # any sub-package is valid
    "@babel": {"core", "cli", "preset-env", "parser", "traverse", "generator"},
    "@vue": {"cli", "compiler-sfc", "reactivity", "runtime-core", "runtime-dom"},
    "@react-native": set(),
    "@tanstack": {"query", "table", "router", "virtual", "form"},
    "@trpc": {"server", "client", "react-query", "next"},
    "@nestjs": {"core", "common", "cli", "platform-express", "testing"},
    "@anthropic-ai": {"sdk", "bedrock-sdk", "vertex-sdk"},
    "@openai": set(),
}

# Scope look-alikes: typo → real scope
_SCOPE_TYPOSQUATS: dict[str, str] = {
    "@angualr": "@angular",
    "@angulr": "@angular",
    "@bable": "@babel",
    "@babsl": "@babel",
    "@tpyes": "@types",
    "@tyeps": "@types",
    "@vuejs": "@vue",
    "@nestjss": "@nestjs",
    "@anthropic": "@anthropic-ai",
    "@openaii": "@openai",
    "@tanstack-": "@tanstack",
}

# ── Install command patterns ─────────────────────────────────────

_PIP_INSTALL = re.compile(
    r"(?:pip|pip3|python\s+-m\s+pip)\s+install\s+(.+)", re.IGNORECASE
)
_NPM_INSTALL = re.compile(
    r"(?:npm|yarn|pnpm)\s+(?:install|add|i)\s+(.+)", re.IGNORECASE
)
_UV_INSTALL = re.compile(
    r"uv\s+(?:pip\s+install|add)\s+(.+)", re.IGNORECASE
)

# ── Supply-chain poisoning patterns (LiteLLM/Apifox 2026-03 attack vectors) ─

# .pth files auto-execute on Python startup — no import needed
_PTH_PATTERN = re.compile(r"\.pth\b", re.IGNORECASE)
# post-install scripts in setup.py / pyproject.toml
_POST_INSTALL_PATTERNS = re.compile(
    r"(?:cmdclass|scripts|entry_points.*console_scripts"
    r"|post_install|install_requires.*subprocess"
    r"|setup\(\s*.*cmdclass)",
    re.IGNORECASE,
)
# Suspicious file writes to site-packages
_SITE_PACKAGES_WRITE = re.compile(
    r"(?:site-packages|dist-packages).*\.pth\b", re.IGNORECASE,
)

# ── Editable install from untrusted sources ─────────────────
_EDITABLE_INSTALL = re.compile(
    r"pip\s+install\s+(?:.*\s)?-e\s+(\S+)", re.IGNORECASE,
)
# Trusted editable dirs: current dir variants
_TRUSTED_EDITABLE = {".", "./", ".[", ".[dev]", ".[test]", ".[all]"}

# pyproject.toml / setup.cfg suspicious build hooks
_BUILD_HOOK_PATTERNS = re.compile(
    r"(?:\[tool\.setuptools\.cmdclass\]"
    r"|cmdclass\s*=\s*\{"
    r"|build-backend\s*=.*(?:flit|hatch|meson|custom)"
    r"|script\s*=.*(?:subprocess|os\.system|shutil\.rmtree))",
    re.IGNORECASE,
)

# Flags to strip from package args
_FLAG_RE = re.compile(r"^-")


def _extract_packages(args_str: str) -> list[str]:
    """Extract package names from install command args.

    Preserves npm scoped packages (e.g. @scope/pkg).
    """
    packages = []
    parts = args_str.split()
    i = 0
    while i < len(parts):
        part = parts[i]
        if _FLAG_RE.match(part) and not part.startswith("@"):
            i += 1
            continue
        # Strip version specifiers
        name = re.split(r"[>=<!~\[]", part)[0].strip().lower()
        if name and len(name) > 1:
            packages.append(name)
        i += 1
    return packages


def _check_scope_spoofing(pkg: str) -> Optional[dict]:
    """Check npm scoped packages for scope typosquatting."""
    if not pkg.startswith("@") or "/" not in pkg:
        return None

    scope, _sep, _name = pkg.partition("/")

    # Check scope typosquats
    real_scope = _SCOPE_TYPOSQUATS.get(scope)
    if real_scope:
        return make_deny(
            (
                f"🚨 Scope spoofing: `{scope}` looks like a typo of `{real_scope}`. "
                f"Did you mean `{real_scope}/{_name}`? "
                f"Fake scopes are used to distribute malware."
            ),
            typosquat=pkg,
            real_scope=real_scope,
            check_type="scope_spoof",
        )
    return None


def check(
    tool_name: str,
    tool_input: dict,
    *,
    extra_typosquats: dict[str, str] | None = None,
    extra_blocklist: set[str] | None = None,
) -> Optional[dict]:
    """Check Bash install commands for supply-chain attacks.

    Checks (in priority order):
      1. Known malicious packages (blocklist)
      2. npm scope spoofing (@typo-scope/pkg)
      3. Typosquatting (misspelled package names)

    Args:
        tool_name: Must be "Bash" to trigger.
        tool_input: Tool input dict with "command" key.
        extra_typosquats: Additional {typo: real_name} mappings.
        extra_blocklist: Additional blocked package names.

    Returns:
        Dict with deny decision, or None if clean.
    """
    if tool_name != "Bash":
        return None
    command = tool_input.get("command", "") if isinstance(tool_input, dict) else ""
    if not command:
        return None

    # Try all install patterns
    packages: list[str] = []
    for pattern in (_PIP_INSTALL, _NPM_INSTALL, _UV_INSTALL):
        m = pattern.search(command)
        if m:
            packages.extend(_extract_packages(m.group(1)))

    if not packages:
        return None

    # Build merged sets
    full_blocklist = _BLOCKLIST | extra_blocklist if extra_blocklist else _BLOCKLIST
    typosquats = dict(_TYPOSQUATS)
    if extra_typosquats:
        typosquats.update(extra_typosquats)

    for pkg in packages:
        # 1. Blocklist (highest priority — built-in + custom)
        if pkg in full_blocklist:
            is_builtin = pkg in _BLOCKLIST
            return make_deny(
                (
                    f"🚫 Blocked: `{pkg}` is a known malicious package "
                    f"(removed from registry or confirmed harmful). "
                    f"Do NOT install this package."
                ) if is_builtin else (
                    f"🚫 Blocked: `{pkg}` is on your custom blocklist."
                ),
                blocked_package=pkg,
                check_type="blocklist",
            )

        # 2. Scope spoofing (npm only)
        result = _check_scope_spoofing(pkg)
        if result:
            return result

        # 3. Typosquatting
        real = typosquats.get(pkg)
        if real:
            return make_deny(
                (
                    f"🚨 Typosquatting alert: `{pkg}` looks like a typo of `{real}`. "
                    f"Did you mean `{real}`? Typosquatting packages can contain malware."
                ),
                typosquat=pkg,
                real_package=real,
                check_type="typosquat",
            )

    return None


# ── Supply-chain poisoning checks ──────────────────────────────


def check_poisoning(
    tool_name: str,
    tool_input: dict,
) -> Optional[dict]:
    """Check for supply-chain poisoning vectors (beyond typosquatting).

    Detects:
      1. .pth file creation/modification (auto-execute on Python start)
      2. Suspicious post-install script patterns (setup.py + pyproject.toml)
      3. Writes to site-packages directories
      4. Editable installs from untrusted sources
      5. pyproject.toml/setup.cfg build hook manipulation

    Triggered by Bash commands and Write/Edit tool operations.
    """
    if tool_name == "Bash":
        command = tool_input.get("command", "") if isinstance(tool_input, dict) else ""
        if not command:
            return None

        # .pth file manipulation — broad detection (any .pth write)
        if _PTH_PATTERN.search(command):
            # High confidence: writing to site-packages
            if _SITE_PACKAGES_WRITE.search(command):
                return make_deny(
                    "🚨 Supply-chain attack vector: .pth files in site-packages "
                    "auto-execute on Python startup without import. "
                    "This is the exact vector used in the LiteLLM attack (2026-03).",
                    check_type="pth_injection",
                )
            # Medium confidence: write/redirect to .pth file (not read)
            if re.search(r"(?:echo|printf|tee|>>?).*\.pth", command, re.IGNORECASE):
                return make_deny(
                    "🚨 Writing .pth file detected. .pth files auto-execute on "
                    "Python startup — verify this is intentional and not a "
                    "supply-chain attack vector.",
                    check_type="pth_write_bash",
                )

        # Suspicious post-install patterns in setup commands
        if re.search(r"setup\.py\s+install", command, re.IGNORECASE):
            if _POST_INSTALL_PATTERNS.search(command):
                return make_deny(
                    "⚠️ Suspicious post-install script detected in setup.py. "
                    "Post-install hooks can execute arbitrary code during pip install.",
                    check_type="post_install_script",
                )

        # Editable install from untrusted source
        m = _EDITABLE_INSTALL.search(command)
        if m:
            target = m.group(1).rstrip("/").lower()
            # Strip version extras like .[dev]
            base = re.split(r"\[", target)[0]
            if base not in _TRUSTED_EDITABLE and not base.startswith("."):
                return make_deny(
                    f"⚠️ Editable install from untrusted source: `{m.group(1)}`. "
                    "Editable installs execute setup.py/pyproject.toml build hooks "
                    "from the target directory. Only use -e with trusted local paths.",
                    check_type="untrusted_editable",
                    target=m.group(1),
                )

    elif tool_name in ("Write", "Edit"):
        file_path = tool_input.get("file_path", "") if isinstance(tool_input, dict) else ""
        if not file_path:
            return None

        # .pth file write
        if file_path.endswith(".pth"):
            return make_deny(
                "🚨 Blocked: writing .pth file. These auto-execute on Python "
                "startup and are a known supply-chain attack vector.",
                check_type="pth_write",
            )

        # pyproject.toml / setup.cfg with suspicious build hooks
        basename = os.path.basename(file_path).lower()
        if basename in ("pyproject.toml", "setup.cfg", "setup.py"):
            content = tool_input.get("content", "") or tool_input.get("new_string", "")
            if content and _BUILD_HOOK_PATTERNS.search(content):
                return make_deny(
                    f"⚠️ Suspicious build hook in `{basename}`. "
                    "Custom cmdclass/build scripts can execute arbitrary code "
                    "during `pip install`. Verify this is intentional.",
                    check_type="build_hook",
                    file=basename,
                )

    return None


# ── BaseGuard adapter ───────────────────────────────────────────


class DepAuditGuard(BaseGuard):
    """Dependency typosquatting + scope spoofing + blocklist guard."""

    name = "dep_audit"
    category = GuardCategory.SECURITY

    def check(self, ctx: GuardContext) -> GuardResult | None:
        """Block install commands targeting malicious, typosquatted, or scope-spoofed packages.

        Also checks for supply-chain poisoning vectors (.pth injection, post-install scripts).

        Args:
            ctx: Guard context with tool_name and tool_input.

        Returns:
            GuardResult.deny with package details and correct name, or None if clean.
        """
        # 1. Typosquatting + blocklist + scope spoofing
        result = check(ctx.tool_name, ctx.tool_input)
        if result is not None:
            return GuardResult.deny(
                result.get("reason", self.name),
                context=result.get("additionalContext", ""),
                check_type=result.get("check_type", ""),
            )

        # 2. Supply-chain poisoning (.pth, post-install)
        poison = check_poisoning(ctx.tool_name, ctx.tool_input)
        if poison is not None:
            return GuardResult.deny(
                poison.get("reason", self.name),
                context=poison.get("additionalContext", ""),
                check_type=poison.get("check_type", ""),
            )

        return None
