"""WIREDO — Full 6-dimension delivery verification.

@module delivery.wiredo
@responsibility W+I+R+E+D+O mechanical checks, auto_delivery_gate
@dependencies delivery._base, delivery.gate, delivery.orphan
"""

from __future__ import annotations

import os
import shutil
import subprocess  # noqa: F401 - kept for legacy callers re-exporting from this module
from typing import Any, Optional

from concinno.core import subprocess_safe

from ._base import ExitCriteria, VerificationResult
from .gate import DeliveryGate
from .orphan import _SKIP_DIRS

# ── WIRED Check helpers ──────────────────────────────────────


def _get_session_code_files(cache_dir: str, session_id: str) -> list[str]:
    """Read sentinel state to get code files edited during this session."""
    try:
        from concinno.core.state_store import StateStore
        state_dir = cache_dir or os.path.join(
            os.environ.get("CLAUDE_PROJECT_DIR", "."),
            ".concinno_cache",
        )
        store = StateStore(state_dir)
        state = store.read("sentinel", session_id, default={})
        edited = state.get("edited_files", [])
    except Exception:
        return []

    code_exts = {".py", ".ts", ".tsx", ".js", ".jsx"}
    return [
        f for f in edited
        if os.path.splitext(f)[1] in code_exts and os.path.isfile(f)
    ]


def _is_wired_grep(combined: str, fpath: str, workspace: str) -> Optional[bool]:
    """Try rg/grep to check if stem is imported. Returns None if tools unavailable."""
    abs_self = os.path.normcase(os.path.abspath(fpath))
    for cmd in ["rg", "grep"]:
        exe = shutil.which(cmd)
        if not exe:
            continue
        # Use "." as search path with cwd=workspace to ensure correct scope
        args = (
            [exe, "-l", "-e", combined, "--type-add",
             "code:*.{py,ts,tsx,js,jsx}", "-t", "code", "."]
            if cmd == "rg"
            else [exe, "-rl", "-E", combined, "--include=*.py",
                  "--include=*.ts", "--include=*.tsx", "--include=*.js",
                  "."]
        )
        try:
            result = subprocess_safe.run(
                args, capture_output=True, text=True, timeout=5,
                cwd=workspace,
            )
            matches = _filter_self_matches(result.stdout, abs_self, workspace)
            return len(matches) > 0
        except Exception:
            continue
    return None


def _filter_self_matches(
    stdout: str, abs_self: str, workspace: str,
) -> list[str]:
    """Filter grep/rg output, excluding self-references."""
    matches = []
    for ln in stdout.strip().split("\n"):
        if not ln:
            continue
        abs_ln = os.path.normcase(
            os.path.abspath(os.path.join(workspace, ln)),
        )
        if abs_ln != abs_self:
            matches.append(ln)
    return matches


def _file_contains(path: str, needle: str) -> bool:
    """Check if file content contains needle (first 50K chars)."""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return needle in f.read(50000)
    except Exception:
        return False


def _is_wired_walk(stem: str, fpath: str, workspace: str) -> bool:
    """Fallback: walk workspace looking for stem in source files."""
    norm_self = os.path.normpath(fpath)
    count = 0
    for root, dirs, files in os.walk(workspace):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for fn in files:
            if not fn.endswith((".py", ".ts", ".tsx", ".js")):
                continue
            full = os.path.join(root, fn)
            if os.path.normpath(full) == norm_self:
                continue
            if _file_contains(full, stem):
                return True
            count += 1
            if count > 500:
                return True  # Assume wired if too many files
    return False


def _is_wired(stem: str, fpath: str, workspace: str) -> bool:
    """Check if a module is imported/referenced by any other file."""
    combined = "|".join([
        f"import.*{stem}", f"from.*{stem}", f'require.*{stem}', f'"{stem}"',
    ])
    grep_result = _is_wired_grep(combined, fpath, workspace)
    if grep_result is not None:
        return grep_result
    try:
        return _is_wired_walk(stem, fpath, workspace)
    except Exception:
        return False


def _find_unwired_files(code_files: list[str], workspace: str) -> list[str]:
    """Check each code file for W (Wired) — is it imported somewhere?"""
    orphans: list[str] = []
    for fpath in code_files:
        stem = os.path.splitext(os.path.basename(fpath))[0]
        if stem.startswith("_") or stem in ("__init__", "index"):
            continue
        if not _is_wired(stem, fpath, workspace):
            rel = os.path.relpath(fpath, workspace) if workspace else fpath
            orphans.append(rel)
    return orphans


# ── WIREDO-D: Defended (Functional Verification) ───────────────
#
# D = "does it actually work?" (functional), NOT "does it compile?" (tsc/lint).
# tsc/lint are prerequisites, not D.  If functional verification is not yet
# possible (project not runnable), D is deferred — no block, no warn.


_FRONTEND_EXTS = frozenset({
    ".tsx", ".jsx", ".css", ".scss", ".less", ".vue", ".svelte", ".html",
})
_BACKEND_EXTS = frozenset({".py", ".ts", ".js"})

_TEST_CMD_PATTERNS = (
    "pytest", "vitest", "jest", "mocha", "npm test", "npm run test",
    "pnpm test", "yarn test", "cargo test", "go test",
)


def _has_frontend_files(code_files: list[str]) -> bool:
    """Check if any session file is a frontend UI file."""
    return any(os.path.splitext(f)[1] in _FRONTEND_EXTS for f in code_files)


def _has_backend_files(code_files: list[str]) -> bool:
    """Check if any session file is a backend code file (not test file)."""
    for f in code_files:
        ext = os.path.splitext(f)[1]
        if ext not in _BACKEND_EXTS:
            continue
        base = os.path.basename(f).lower()
        if not base.startswith("test_") and not base.endswith(
            ("_test.py", ".test.ts", ".spec.ts")
        ):
            return True
    return False


def _has_screenshot_evidence(cache_dir: str, session_id: str) -> bool:
    """Check if screenshots were taken during this session (UIVerifyGuard state)."""
    try:
        from concinno.core.state_store import StateStore
        state_dir = cache_dir or os.path.join(
            os.environ.get("CLAUDE_PROJECT_DIR", "."), ".concinno_cache",
        )
        store = StateStore(state_dir)
        state = store.read("ui_verify", session_id, default={})
        if state.get("verified"):
            return True
        if state.get("verify_fails", 0) > 0:
            return True
    except Exception:
        pass
    return False


def _has_test_evidence(cache_dir: str, session_id: str) -> bool:
    """Check if test commands were run during this session (sentinel calls)."""
    try:
        from concinno.core.state_store import StateStore
        state_dir = cache_dir or os.path.join(
            os.environ.get("CLAUDE_PROJECT_DIR", "."), ".concinno_cache",
        )
        store = StateStore(state_dir)
        state = store.read("sentinel", session_id, default={})
        calls = state.get("calls", [])
        return _calls_contain_test(calls)
    except Exception:
        pass
    return False


def _calls_contain_test(calls: list[dict]) -> bool:
    """Check if any sentinel call is a test command."""
    for call in calls:
        if call.get("tool") != "Bash":
            continue
        pfx = (call.get("bash_pfx") or "").lower()
        if any(p in pfx for p in _TEST_CMD_PATTERNS):
            return True
    return False


def _defended_check(
    code_files: list[str], cache_dir: str, session_id: str,
) -> list[str]:
    """WIREDO-D: Check for functional verification evidence.

    D = functional verification (runs correctly, does what it should).
    tsc/lint passing is a prerequisite, NOT D.
    If the project is not yet runnable, D is deferred — returns empty.
    """
    lines: list[str] = []
    has_fe = _has_frontend_files(code_files)
    has_be = _has_backend_files(code_files)

    if has_fe and not _has_screenshot_evidence(cache_dir, session_id):
        lines.append("  ⏸ D(frontend) — UI changed, no visual verification yet")
        lines.append("    → Defer to deployment milestone or run playwright")

    if has_be and not _has_test_evidence(cache_dir, session_id):
        lines.append("  ⏸ D(backend) — code changed, no functional test yet")
        lines.append("    → Defer to runnable milestone or run test suite")

    return lines


# ── WIREDO-I: Inherited & Aligned Check ─────────────────────


# Files that MUST be in specific module directories (not project root)
_MODULE_DIRS = {
    ".py": {"src/", "lib/", "tests/", "scripts/"},
    ".ts": {"src/", "packages/", "lib/", "tests/"},
    ".tsx": {"src/", "packages/", "components/", "pages/", "app/"},
    ".js": {"src/", "lib/", "scripts/"},
    ".jsx": {"src/", "components/", "pages/", "app/"},
}


def _inherited_check(code_files: list[str], workspace: str) -> list[str]:
    """WIREDO-I: Check if files are in architecturally correct locations."""
    lines: list[str] = []
    for fpath in code_files:
        ext = os.path.splitext(fpath)[1]
        expected_dirs = _MODULE_DIRS.get(ext)
        if not expected_dirs:
            continue
        rel = os.path.relpath(fpath, workspace).replace("\\", "/")
        # Skip if in any expected directory
        if any(rel.startswith(d) or f"/{d}" in rel for d in expected_dirs):
            continue
        # Root-level code files are misplaced
        if "/" not in rel:
            lines.append(f"  ⚠ I — {rel} is in project root, not a module directory")
    return lines


# ── WIREDO-R: Responsive & Performant Check ──────────────────

# Code patterns that indicate performance issues
_PERF_ANTIPATTERNS = [
    (r"for\s+.*\s+in\s+.*:\s*\n\s+for\s+", "nested loop (potential O(n²))"),
    (r"\.query\(.*\)\s*.*\bfor\b", "query inside loop (potential N+1)"),
    (r"time\.sleep\(\s*[1-9]\d*\s*\)", "long sleep (blocking)"),
    (r"while\s+True.*:\s*\n(?:.*\n)*?\s+await\s+asyncio\.sleep", "polling loop"),
]


def _responsive_check(code_files: list[str]) -> list[str]:
    """WIREDO-R: Check for performance anti-patterns in edited files."""
    import re

    lines: list[str] = []
    for fpath in code_files:
        try:
            with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read(100_000)
        except Exception:
            continue
        rel = os.path.basename(fpath)
        for pattern, desc in _PERF_ANTIPATTERNS:
            if re.search(pattern, content):
                lines.append(f"  ⚠ R — {rel}: {desc}")
                break  # One warning per file
    return lines


# ── WIREDO-E: Extensible Check ───────────────────────────────

# Patterns indicating hardcoded values that should be configurable
_HARDCODE_PATTERNS = [
    (r'(?:host|url|endpoint)\s*=\s*["\']https?://', "hardcoded URL"),
    (r'(?:port|PORT)\s*=\s*\d{4,5}', "hardcoded port"),
    (r'(?:timeout|TIMEOUT)\s*=\s*\d+(?!\s*#)', "hardcoded timeout (no comment)"),
]


def _extensible_check(code_files: list[str]) -> list[str]:
    """WIREDO-E: Check for hardcoded values that should be configurable."""
    import re

    lines: list[str] = []
    for fpath in code_files:
        # Skip test files and config files
        base = os.path.basename(fpath).lower()
        if base.startswith("test_") or base.endswith((".test.ts", ".spec.ts", ".json")):
            continue
        try:
            with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read(50_000)
        except Exception:
            continue
        rel = os.path.basename(fpath)
        for pattern, desc in _HARDCODE_PATTERNS:
            if re.search(pattern, content):
                lines.append(f"  ⚠ E — {rel}: {desc}")
                break
    return lines


# ── WIREDO-O: Observable Check ───────────────────────────────

_OBSERVABLE_INDICATORS = (
    "logger", "logging", "console.log", "console.error",
    "metrics", "stats(", "health_check", "healthCheck",
    "structlog", "pino",
)


def _observable_check(code_files: list[str]) -> list[str]:
    """WIREDO-O: Check for observability in non-trivial code files."""
    lines: list[str] = []
    for fpath in code_files:
        base = os.path.basename(fpath).lower()
        # Skip small files, tests, types, configs
        if base.startswith(("test_", "__init__", "index.")):
            continue
        if base.endswith((".test.ts", ".spec.ts", ".d.ts")):
            continue
        try:
            with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read(50_000)
        except Exception:
            continue
        # Only check files with substantial logic (>50 lines)
        if content.count("\n") < 50:
            continue
        if not any(ind in content for ind in _OBSERVABLE_INDICATORS):
            rel = os.path.basename(fpath)
            lines.append(f"  ⚠ O — {rel}: no logging/metrics detected (>50 lines)")
    return lines


# ── Full 6-dimension WIREDO verification ──────────────────────


def wiredo_full_check(
    cache_dir: str = "", session_id: str = "",
) -> dict[str, tuple[bool, list[str]]]:
    """Run full 6-dimension WIREDO verification.

    Returns dict mapping dimension letter → (passed, detail_lines).
    """
    code_files = _get_session_code_files(cache_dir, session_id)
    if not code_files:
        return {}

    workspace = os.environ.get("CLAUDE_PROJECT_DIR", ".")

    # W — Wired
    orphans = _find_unwired_files(code_files, workspace)
    w_lines = [f"  ❌ {o} — no import/reference found" for o in orphans[:5]]

    # I — Inherited & Aligned
    i_lines = _inherited_check(code_files, workspace)

    # R — Responsive & Performant
    r_lines = _responsive_check(code_files)

    # E — Extensible
    e_lines = _extensible_check(code_files)

    # D — Defended & Verified
    d_lines = _defended_check(code_files, cache_dir, session_id)

    # O — Observable
    o_lines = _observable_check(code_files)

    return {
        "W": (len(w_lines) == 0, w_lines),
        "I": (len(i_lines) == 0, i_lines),
        "R": (len(r_lines) == 0, r_lines),
        "E": (len(e_lines) == 0, e_lines),
        "D": (len(d_lines) == 0, d_lines),
        "O": (len(o_lines) == 0, o_lines),
    }


# ── Combined checks ────────────────────────────────────────


def wiredo_check(cache_dir: str = "", session_id: str = "") -> str:
    """Run full WIREDO 6-dimension verification on files edited during this session.

    Returns stderr report string (empty if nothing to report).
    """
    results = wiredo_full_check(cache_dir, session_id)
    if not results:
        return ""

    code_files = _get_session_code_files(cache_dir, session_id)
    lines: list[str] = []

    _DIM_NAMES = {
        "W": "Wired",
        "I": "Inherited & Aligned",
        "R": "Responsive & Performant",
        "E": "Extensible",
        "D": "Defended (Functional)",
        "O": "Observable",
    }

    for dim, (passed, detail_lines) in results.items():
        if not passed and detail_lines:
            lines.append(f"\033[93m⚠ [WIREDO-{dim}] {_DIM_NAMES[dim]}:\033[0m")
            lines.extend(detail_lines)

    # Summary line
    status_parts = []
    for dim in "WIREDO":
        passed, _ = results.get(dim, (True, []))
        mark = "✅" if passed else "❌"
        status_parts.append(f"{dim}({_DIM_NAMES.get(dim, dim)}){mark}")

    lines.append(
        f"\033[36m[WIREDO] {len(code_files)} code files edited. "
        f"{' '.join(status_parts)}\033[0m"
    )
    return "\n".join(lines)


def auto_delivery_gate(
    cache_dir: str = "", session_id: str = "",
) -> str:
    """Run CCC's own Delivery Gate on session-edited files (D1→D6).

    Auto-generates ExitCriteria based on file types edited, gathers evidence
    from wired check + defended check, then verifies → reports → retry/rollback
    → audits → gate_check.

    Returns stderr report string (empty if nothing to report).
    """
    code_files = _get_session_code_files(cache_dir, session_id)
    if not code_files:
        return ""

    workspace = os.environ.get("CLAUDE_PROJECT_DIR", ".")
    has_fe = _has_frontend_files(code_files)
    has_be = _has_backend_files(code_files)

    # ── D1: Auto define_done ───────────────────────────────
    primary: list[str] = ["All new code files are wired (imported/referenced)"]
    safety: list[str] = []

    if has_fe:
        primary.append("Frontend UI changes verified with screenshots")
    if has_be:
        primary.append("Backend code changes verified with tests")

    gate = DeliveryGate(audit_dir=os.path.join(
        cache_dir or os.path.join(workspace, ".concinno_cache"),
        "delivery_audit",
    ))
    criteria = gate.define_done(
        task=f"Session auto-check ({len(code_files)} files)",
        primary=primary,
        safety=safety,
        task_id=f"auto_{session_id[:8]}" if session_id else "auto",
    )

    # ── D2: Gather evidence + verify ───────────────────────
    orphans = _find_unwired_files(code_files, workspace)
    evidence: dict[str, Any] = {
        "All new code files are wired (imported/referenced)": len(orphans) == 0,
    }
    if has_fe:
        evidence["Frontend UI changes verified with screenshots"] = (
            _has_screenshot_evidence(cache_dir, session_id)
        )
    if has_be:
        evidence["Backend code changes verified with tests"] = (
            _has_test_evidence(cache_dir, session_id)
        )

    result = gate.verify(criteria, evidence=evidence)

    # ── D3: Report ────────────────────────────────────────
    blockers: list[str] = []
    if orphans:
        blockers.extend(f"Unwired: {o}" for o in orphans[:5])

    report = gate.report(criteria, result, blockers=blockers)

    # ── D4: Should retry + rollback decision ─────────────
    retry = gate.should_retry(result, max_iterations=1, current_iteration=0)
    needs_rollback = gate.rollback_decision(result)

    # ── D5: Audit log ─────────────────────────────────────
    gate.audit_log(criteria, result, report, extra={
        "code_files": code_files[:10],
        "has_frontend": has_fe,
        "has_backend": has_be,
        "orphan_count": len(orphans),
        "should_retry": retry,
        "needs_rollback": needs_rollback,
    })

    # ── D6: Gate check ────────────────────────────────────
    gate_deny = gate.gate_check(criteria.task_id)

    # ── Format for stderr ─────────────────────────────────
    return _format_gate_report(
        result, criteria, orphans, needs_rollback, retry, gate_deny,
        len(code_files),
    )


def _format_gate_report(
    result: "VerificationResult",
    criteria: "ExitCriteria",
    orphans: list[str],
    needs_rollback: bool,
    retry: bool,
    gate_deny: Optional[str],
    file_count: int,
) -> str:
    """Format auto_delivery_gate result for stderr."""
    if result.all_passed:
        return (
            f"\033[32m✅ [DeliveryGate] {file_count} code files — "
            f"all {result.total_count} criteria passed\033[0m"
        )

    lines = [
        f"\033[93m⚠ [DeliveryGate] {file_count} code files — "
        f"{result.passed_count}/{result.total_count} criteria passed:\033[0m",
    ]
    for c in criteria.criteria:
        mark = "✅" if c.passed else "❌"
        lines.append(f"  {mark} {c.description}")
        if not c.passed and c.evidence and c.evidence != "not evaluated":
            lines.append(f"      → {c.evidence}")
    if orphans:
        lines.append("  Unwired files:")
        for o in orphans[:5]:
            lines.append(f"    - {o}")
    if needs_rollback:
        lines.append("  ⚠ Safety criteria failed — rollback recommended")
    if retry:
        lines.append("  → Fixable criteria detected — consider retry")
    if gate_deny:
        lines.append(f"  🚫 Gate: {gate_deny}")
    return "\n".join(lines)


# Backward compat alias (renamed from wired_check → wiredo_check)
wired_check = wiredo_check
