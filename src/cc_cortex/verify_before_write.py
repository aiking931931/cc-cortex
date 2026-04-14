"""cc_cortex.verify_before_write — Verify external references before writing.

@module verify_before_write
@responsibility CBUA Law #3 + #6: scan Write/Edit content for external
    references (imports, API calls, version numbers) and warn if the
    referenced package/endpoint was never Read/Grepped in this session.
@dependencies cc_cortex.guards.base, cc_cortex.core.state_store
@exports VerifyBeforeWriteGuard

PostToolUse guard (inject warning, never deny). Tracks which packages
and files have been read/grepped, then flags written content containing
unverified external references.
"""

from __future__ import annotations

import re
from typing import Optional

from cc_cortex.constants import READ_TOOLS, WRITE_TOOLS_EXT
from cc_cortex.core.state_store import StateStore
from cc_cortex.guards.base import BaseGuard, GuardCategory, GuardContext, GuardResult

_NS = "verify_write"

# ── Detection patterns for external references ───────────────────

# Python imports: "from X import Y" or "import X"
_PYTHON_IMPORT = re.compile(
    r"(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))",
)

# JS/TS imports: "import X from 'Y'" or "require('Y')"
_JS_IMPORT = re.compile(
    r"""(?:import\s+.*?\s+from\s+['"]([^'"]+)['"]|require\s*\(\s*['"]([^'"]+)['"]\s*\))""",
)

# Version numbers in dependency context: "package": "^1.2.3"
_VERSION_PIN = re.compile(
    r'"([\w@/.-]+)"\s*:\s*"[\^~>=<]*\d+\.\d+',
)

# API endpoints: /api/v2/... or https://api.example.com/...
_API_ENDPOINT = re.compile(
    r"(?:/api/v\d+/[\w/.-]+|https?://(?!localhost|127\.0\.0\.1)\S+/v\d+/\S*)",
)

# Stdlib / builtins that don't need verification
_STDLIB_ALLOWLIST = frozenset({
    "os", "sys", "re", "json", "typing", "dataclasses", "pathlib",
    "collections", "functools", "itertools", "math", "hashlib",
    "logging", "datetime", "time", "tempfile", "unittest", "abc",
    "enum", "io", "copy", "textwrap", "shutil", "contextlib",
    "warnings", "threading", "subprocess", "argparse", "glob",
    "fnmatch", "uuid", "base64", "struct", "socket", "http",
    "__future__", "pytest", "builtins",
    # JS builtins
    "react", "next", "path", "fs", "url", "util", "events",
})


def _extract_packages(content: str) -> set[str]:
    """Extract external package references from content."""
    pkgs: set[str] = set()

    for m in _PYTHON_IMPORT.finditer(content):
        pkg = (m.group(1) or m.group(2) or "").split(".")[0]
        if pkg and pkg not in _STDLIB_ALLOWLIST:
            pkgs.add(pkg)

    for m in _JS_IMPORT.finditer(content):
        raw = m.group(1) or m.group(2) or ""
        # Scoped packages: @scope/name -> @scope/name
        # Regular: name/sub -> name
        if raw.startswith("@"):
            pkg = "/".join(raw.split("/")[:2])
        elif raw.startswith("."):
            continue  # relative import — skip
        else:
            pkg = raw.split("/")[0]
        if pkg and pkg not in _STDLIB_ALLOWLIST:
            pkgs.add(pkg)

    for m in _VERSION_PIN.finditer(content):
        pkgs.add(m.group(1))

    return pkgs


def _extract_api_endpoints(content: str) -> list[str]:
    """Extract API endpoint references from content."""
    return [m.group() for m in _API_ENDPOINT.finditer(content)]


def _extract_written_text(tool_input: dict) -> str:
    """Extract the text content being written."""
    return (
        tool_input.get("content", "")
        or tool_input.get("new_string", "")
        or tool_input.get("new_source", "")
    )


class VerifyBeforeWriteGuard(BaseGuard):
    """CBUA Law #3 + #6: verify external references before writing.

    PostToolUse on Write/Edit:
    1. Scan content for external references (import statements, API calls,
       version numbers)
    2. Check if corresponding Read/Grep/WebSearch was done in session
    3. If not -> inject warning "verify before writing"

    Category: QUALITY (inject warning, don't deny — to avoid blocking
    too aggressively)
    """

    name = "verify_before_write"
    category = GuardCategory.QUALITY

    def check(self, ctx: GuardContext) -> Optional[GuardResult]:
        """Not used for PreToolUse. See on_post_tool."""
        return None

    def on_post_tool(self, ctx: GuardContext) -> Optional[GuardResult]:
        """Scan written content for unverified external references."""
        if not ctx.cache_dir:
            return None

        # Track reads as verified sources
        if ctx.tool_name in READ_TOOLS:
            self._track_read(ctx)
            return None

        # Only scan write tools
        if ctx.tool_name not in WRITE_TOOLS_EXT:
            return None

        content = _extract_written_text(ctx.tool_input)
        if not content or len(content) < 20:
            return None

        # Extract references
        packages = _extract_packages(content)
        endpoints = _extract_api_endpoints(content)

        if not packages and not endpoints:
            return None

        # Check session's verified set
        store = StateStore(ctx.cache_dir)
        state = store.read(_NS, ctx.session_id, default={})
        verified: set[str] = set(state.get("verified_packages", []))
        reads: list[str] = state.get("tool_reads", [])

        # Filter to unverified packages only
        unverified_pkgs = packages - verified
        # API endpoints: check if any read touched a related URL
        unverified_eps = [
            ep for ep in endpoints
            if not any(ep[:20] in r for r in reads)
        ]

        if not unverified_pkgs and not unverified_eps:
            return None

        # Build warning
        parts: list[str] = []
        if unverified_pkgs:
            pkg_list = ", ".join(sorted(unverified_pkgs)[:5])
            parts.append(f"未驗證的套件/模組：{pkg_list}")
        if unverified_eps:
            ep_list = ", ".join(unverified_eps[:3])
            parts.append(f"未驗證的 API 端點：{ep_list}")

        detail = "\n".join(f"  - {p}" for p in parts)

        # Record flag count
        flags = state.get("flag_count", 0)
        state["flag_count"] = flags + 1
        state["last_flagged"] = list(unverified_pkgs)[:5]
        store.write(_NS, ctx.session_id, state)

        return GuardResult.allow(
            context=(
                "⚠ CBUA Law #3 驗證至上：寫入內容含未經 Read/Grep 驗證的"
                "外部參照：\n"
                f"{detail}\n\n"
                "建議：先 Read/Grep 確認 API 簽名、版本相容性，"
                "再寫入。猜前先查。"
            ),
        )

    def _track_read(self, ctx: GuardContext) -> None:
        """Track read/search actions as verification sources."""
        store = StateStore(ctx.cache_dir)
        state = store.read(_NS, ctx.session_id, default={})

        verified: list[str] = state.get("verified_packages", [])
        reads: list[str] = state.get("tool_reads", [])

        # Extract the file/path being read
        path = (
            ctx.tool_input.get("file_path", "")
            or ctx.tool_input.get("path", "")
            or ""
        )
        if path:
            reads.append(path)
            # If reading a package's source, mark it verified
            # e.g., reading node_modules/axios/... -> verify "axios"
            parts = path.replace("\\", "/").split("/")
            for part in parts:
                if part and part not in ("node_modules", "site-packages", "src", "lib"):
                    verified.append(part)

        # Grep pattern may reference a package
        pattern = ctx.tool_input.get("pattern", "")
        if pattern:
            # Simple heuristic: the grep pattern itself is a package name
            clean = re.sub(r"[^a-zA-Z0-9_@/.-]", "", pattern)
            if clean and len(clean) > 1:
                verified.append(clean)

        # Deduplicate, keep bounded
        state["verified_packages"] = list(set(verified))[-100:]
        state["tool_reads"] = reads[-50:]
        store.write(_NS, ctx.session_id, state)
