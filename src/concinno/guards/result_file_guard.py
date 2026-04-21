"""concinno.guards.result_file_guard — warn when ablation / benchmark
results are written to ephemeral tmp paths.

@module result_file_guard
@responsibility Detect writes of result-shaped files (JSON / CSV /
    JSONL / log / parquet) to ``/tmp/``, ``C:\\Temp\\``, ``%TEMP%``,
    or ``~/tmp/`` in both Python code (``json.dump`` / ``open(...,
    'w')``) and Bash redirects (``> /tmp/x.json``). Results dropped
    here are lost on reboot — warn so authors commit to ``artifacts/``
    or ``outputs/`` instead.
@dependencies concinno.guards.base (stdlib re only)
@exports ResultFileGuard
"""

from __future__ import annotations

import re

from concinno.guards.base import BaseGuard, GuardCategory, GuardContext, GuardResult

# Paths treated as "ephemeral tmp" — case-insensitive on Windows paths.
_TMP_SEGMENTS = (
    "/tmp/",
    r"\tmp\\",
    "c:/temp/",
    "c:\\temp\\",
    "%temp%",
    "$temp/",
    "~/tmp/",
)

# Result-shaped file extensions.
_RESULT_EXTS = (
    ".json",
    ".jsonl",
    ".csv",
    ".tsv",
    ".parquet",
    ".log",
    ".npz",
    ".pkl",
    ".pickle",
    ".h5",
    ".hdf5",
    ".arrow",
)

# Result-hinting tokens in filename / keyword — raises specificity on
# ambiguous extensions like `.log` or `.json`.
_RESULT_HINT = re.compile(
    r"\b(?:result|results|output|outputs|metric|metrics|score|scores|"
    r"ablation|benchmark|eval|evaluation|report)\b",
    re.IGNORECASE,
)

# Python patterns — catches `open('/tmp/x.json', 'w')`, `json.dump(...,
# open('/tmp/x.json', 'w'))`, `pathlib.Path('/tmp/...')`.
_PY_OPEN = re.compile(
    r"open\s*\(\s*['\"]([^'\"]*)['\"]\s*,\s*['\"][wa]",
)
_PY_PATHLIB = re.compile(
    r"(?:Path|PurePath|PosixPath|WindowsPath)\s*\(\s*['\"]([^'\"]+)['\"]\s*\)",
)
_PY_DUMP_DIRECT = re.compile(
    r"(?:json|pickle|np|numpy)\.(?:dump|save|savez|savez_compressed|to_csv|to_json|to_parquet)"
    r"\s*\([^)]*['\"]([^'\"]+)['\"]",
)

# Bash redirects — `> /tmp/x`, `>> /tmp/x`, `tee /tmp/x`.
_BASH_REDIRECT = re.compile(
    r"(?:>>|>|\|\s*tee\s+(?:-a\s+)?)\s*([A-Za-z]:[\\/][^\s'\"<>|]+|/[^\s'\"<>|]+)",
)


def _is_tmp_path(path: str) -> bool:
    """True when *path* points to an ephemeral tmp location."""
    if not path:
        return False
    low = path.lower().replace("\\", "/")
    low = low.replace("\\", "/")  # double-normalize safety
    # Also check raw against backslash variants.
    raw = path.lower()
    if raw.startswith("/tmp/") or raw.startswith("c:/temp/") or raw.startswith("c:\\temp\\"):
        return True
    for seg in _TMP_SEGMENTS:
        if seg in low or seg in raw:
            return True
    return False


def _looks_like_result(path: str) -> bool:
    """True when *path* has a result extension or a result hint token."""
    if not path:
        return False
    low = path.lower()
    if low.endswith(_RESULT_EXTS):
        return True
    return bool(_RESULT_HINT.search(path))


def scan_python(content: str) -> list[str]:
    """Return tmp paths with result-shaped names written in *content*."""
    findings: list[str] = []
    seen: set[str] = set()
    for rx in (_PY_OPEN, _PY_PATHLIB, _PY_DUMP_DIRECT):
        for m in rx.finditer(content):
            path = m.group(1)
            if not _is_tmp_path(path):
                continue
            if not _looks_like_result(path):
                continue
            if path in seen:
                continue
            seen.add(path)
            findings.append(path)
    return findings


def scan_bash(command: str) -> list[str]:
    """Return tmp paths with result-shaped names redirected in *command*."""
    findings: list[str] = []
    seen: set[str] = set()
    for m in _BASH_REDIRECT.finditer(command):
        path = m.group(1)
        if not _is_tmp_path(path):
            continue
        if not _looks_like_result(path):
            continue
        if path in seen:
            continue
        seen.add(path)
        findings.append(path)
    return findings


class ResultFileGuard(BaseGuard):
    """Warn when results land in ephemeral tmp paths.

    Signal-only. Fires on PreToolUse Write / Edit (Python content) or
    Bash (redirects / tee). Suggests committing to ``artifacts/`` or
    ``outputs/`` instead.
    """

    name = "result_file"
    category = GuardCategory.QUALITY
    feature_name = "result_file"

    def check(self, ctx: GuardContext) -> GuardResult | None:
        if ctx.tool_name in ("Write", "Edit"):
            return self._check_py(ctx)
        if ctx.tool_name == "Bash":
            return self._check_bash(ctx)
        return None

    def _check_py(self, ctx: GuardContext) -> GuardResult | None:
        path = ctx.tool_input.get("file_path", "") or ""
        if not path.lower().endswith((".py", ".ipynb")):
            return None
        content = (
            ctx.tool_input.get("content", "")
            or ctx.tool_input.get("new_string", "")
            or ""
        )
        if not content:
            return None
        findings = scan_python(content)
        if not findings:
            return None
        return self._format_warning(findings)

    def _check_bash(self, ctx: GuardContext) -> GuardResult | None:
        command = ctx.tool_input.get("command", "") or ""
        if not command:
            return None
        findings = scan_bash(command)
        if not findings:
            return None
        return self._format_warning(findings)

    def _format_warning(self, findings: list[str]) -> GuardResult:
        bullets = "\n".join(f"  - {p}" for p in findings)
        msg = (
            "[result-file] result-shaped files written to ephemeral tmp — "
            "lost on reboot:\n"
            f"{bullets}\n"
            "  Fix: write to `artifacts/` or `outputs/` inside the repo, "
            "or use a dated run directory. Signal only — operation proceeds."
        )
        return GuardResult.allow_advisory(context=msg)
