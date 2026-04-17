"""concinno.publish_scan — Pre-publish artifact scanner.

@module publish_scan
@responsibility Scan sdist/wheel build artifacts for accidentally bundled secrets,
    private keys, and personal paths before PyPI upload.
@dependencies none (stdlib only)
@exports scan_dist, scan_dist_summary, scan_file

Usage:
    from concinno.publish_scan import scan_dist

    issues = scan_dist("dist/")
    if issues:
        print("BLOCKED — fix these before publishing:")
        for i in issues:
            print(f"  {i['severity']} {i['file']}: {i['reason']}")

CLI:
    concinno publish-scan [dist_dir]
"""

from __future__ import annotations

import ast
import os
import re
import tarfile
import zipfile
from typing import Optional

from concinno.guards.base import BaseGuard, GuardCategory, GuardContext, GuardResult

# ── Dangerous file patterns ──────────────────────────────────

_DANGEROUS_NAMES: set[str] = {
    ".env",
    ".env.local",
    ".env.production",
    ".env.staging",
    "credentials.json",
    "service-account.json",
    "serviceAccountKey.json",
    "id_rsa",
    "id_ed25519",
    "id_ecdsa",
    "id_dsa",
    ".pem",
    ".p12",
    ".pfx",
    ".key",
    "token.json",
    "secrets.json",
    "secrets.yaml",
    "secrets.yml",
    ".npmrc",          # may contain auth tokens
    ".pypirc",         # PyPI credentials
    ".netrc",
    ".htpasswd",
}

_DANGEROUS_EXTENSIONS: set[str] = {
    ".pem", ".p12", ".pfx", ".key", ".jks", ".keystore",
}

# Basenames that are always suspicious in a package
_SUSPICIOUS_BASENAMES_RE = re.compile(
    r"^(?:\.env(?:\..+)?|.*(?:secret|credential|private.?key).*\.(?:json|yaml|yml|toml))$",
    re.IGNORECASE,
)

# ── Content patterns (secrets in source files) ───────────────

_SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("AWS Access Key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("Private Key (PEM)", re.compile(r"-----BEGIN\s+(?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----")),
    ("GitHub Token", re.compile(r"gh[pousr]_[A-Za-z0-9_]{36,}")),
    ("Anthropic Key", re.compile(r"sk-ant-api\d{2}-[A-Za-z0-9_\-]{80,}")),
    ("OpenAI Key", re.compile(r"sk-[A-Za-z0-9]{48,}")),
    ("Stripe Key", re.compile(r"[sr]k_(?:live|test)_[A-Za-z0-9]{20,}")),
    ("Slack Token", re.compile(r"xox[bporas]-[A-Za-z0-9\-]{10,}")),
    ("SendGrid Key", re.compile(r"SG\.[A-Za-z0-9_\-]{22}\.[A-Za-z0-9_\-]{43}")),
    ("GitLab Token", re.compile(r"glpat-[A-Za-z0-9_\-]{20,}")),
]

# ── Personal path patterns ───────────────────────────────────

_PERSONAL_PATH_RE = re.compile(
    r"(?:"
    r"[A-Z]:\\Users\\[^\\]+\\"           # Windows: C:\Users\xxx\
    r"|/home/[^/]+/"                      # Linux: /home/xxx/
    r"|/Users/[^/]+/"                     # macOS: /Users/xxx/
    r")",
)

# Files worth scanning for content (source code / config)
_SCANNABLE_EXTENSIONS: set[str] = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".json", ".yaml", ".yml",
    ".toml", ".cfg", ".ini", ".conf", ".sh", ".bash", ".ps1",
    ".env", ".txt", ".md", ".rst",
}

# Max file size to scan content (skip large binaries)
_MAX_SCAN_SIZE = 1_000_000  # 1MB


# ── Issue type ───────────────────────────────────────────────

def _issue(
    severity: str, file: str, reason: str, pattern: str = "",
) -> dict[str, str]:
    """Create a structured issue dict."""
    d: dict[str, str] = {
        "severity": severity,
        "file": file,
        "reason": reason,
    }
    if pattern:
        d["pattern"] = pattern
    return d


# ── Scanners ─────────────────────────────────────────────────

def _check_filename(member_path: str) -> Optional[dict[str, str]]:
    """Check if a filename is dangerous."""
    basename = os.path.basename(member_path).lower()
    _, ext = os.path.splitext(basename)

    if basename in _DANGEROUS_NAMES:
        return _issue(
            "CRITICAL", member_path,
            f"Dangerous file bundled: {basename}",
            "dangerous_name",
        )
    if ext in _DANGEROUS_EXTENSIONS:
        return _issue(
            "CRITICAL", member_path,
            f"Key/certificate file bundled: {basename}",
            "dangerous_ext",
        )
    if _SUSPICIOUS_BASENAMES_RE.match(basename):
        return _issue(
            "HIGH", member_path,
            f"Suspicious file: {basename}",
            "suspicious_name",
        )
    return None


def _check_content(member_path: str, content: str) -> list[dict[str, str]]:
    """Scan file content for secrets and personal paths."""
    issues: list[dict[str, str]] = []

    for name, pattern in _SECRET_PATTERNS:
        if pattern.search(content):
            issues.append(_issue(
                "CRITICAL", member_path,
                f"Hardcoded secret detected: {name}",
                "secret",
            ))

    if _PERSONAL_PATH_RE.search(content):
        issues.append(_issue(
            "HIGH", member_path,
            "Personal/absolute path found in source",
            "personal_path",
        ))

    return issues


def _is_scannable(member_path: str) -> bool:
    """Check if file content should be scanned."""
    _, ext = os.path.splitext(member_path.lower())
    return ext in _SCANNABLE_EXTENSIONS


# ── Archive scanners ─────────────────────────────────────────


def _scan_member_content(read_fn, name: str) -> list[dict[str, str]]:
    """Read and scan a single archive member's content."""
    try:
        raw = read_fn()
        if raw is None:
            return []
        content = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
        return _check_content(name, content)
    except Exception:
        return []


def _scan_wheel(path: str) -> list[dict[str, str]]:
    """Scan a .whl (zip) file."""
    issues: list[dict[str, str]] = []
    try:
        with zipfile.ZipFile(path, "r") as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                fname_issue = _check_filename(info.filename)
                if fname_issue:
                    issues.append(fname_issue)
                if _is_scannable(info.filename) and info.file_size <= _MAX_SCAN_SIZE:
                    issues.extend(_scan_member_content(
                        lambda: zf.read(info.filename), info.filename,
                    ))
    except (zipfile.BadZipFile, OSError):
        issues.append(_issue("ERROR", path, "Cannot read wheel file", "io_error"))
    return issues


def _scan_sdist(path: str) -> list[dict[str, str]]:
    """Scan a .tar.gz sdist file."""
    issues: list[dict[str, str]] = []
    try:
        with tarfile.open(path, "r:*") as tf:
            for member in tf.getmembers():
                if not member.isfile():
                    continue
                fname_issue = _check_filename(member.name)
                if fname_issue:
                    issues.append(fname_issue)
                if _is_scannable(member.name) and member.size <= _MAX_SCAN_SIZE:
                    def _read(m=member):
                        f = tf.extractfile(m)
                        return f.read() if f else None
                    issues.extend(_scan_member_content(_read, member.name))
    except (tarfile.TarError, OSError):
        issues.append(_issue("ERROR", path, "Cannot read sdist file", "io_error"))
    return issues


# ── Public API ───────────────────────────────────────────────

def scan_file(path: str) -> list[dict[str, str]]:
    """Scan a single distribution file (.whl or .tar.gz).

    Returns list of issue dicts (empty = clean).
    """
    lower = path.lower()
    if lower.endswith(".whl") or lower.endswith(".zip"):
        return _scan_wheel(path)
    elif lower.endswith(".tar.gz") or lower.endswith(".tar.bz2"):
        return _scan_sdist(path)
    else:
        return [_issue("WARN", path, f"Unknown archive format: {os.path.basename(path)}")]


def scan_dist(dist_dir: str = "dist") -> list[dict[str, str]]:
    """Scan all distribution files in a directory.

    Args:
        dist_dir: Path to dist/ directory (default: "dist").

    Returns:
        List of issue dicts. Empty = safe to publish.
    """
    if not os.path.isdir(dist_dir):
        return [_issue("ERROR", dist_dir, "dist directory not found", "no_dist")]

    issues: list[dict[str, str]] = []
    found_any = False

    for fname in sorted(os.listdir(dist_dir)):
        fpath = os.path.join(dist_dir, fname)
        if not os.path.isfile(fpath):
            continue
        lower = fname.lower()
        if lower.endswith((".whl", ".tar.gz", ".tar.bz2", ".zip")):
            found_any = True
            issues.extend(scan_file(fpath))

    if not found_any:
        issues.append(_issue("WARN", dist_dir, "No distribution files found"))

    return issues


def scan_dist_summary(dist_dir: str = "dist") -> str:
    """Scan and return a human-readable summary.

    Returns:
        Formatted string with results.
    """
    issues = scan_dist(dist_dir)
    if not issues:
        return f"✅ publish-scan: {dist_dir}/ is clean — safe to upload."

    critical = sum(1 for i in issues if i["severity"] == "CRITICAL")
    high = sum(1 for i in issues if i["severity"] == "HIGH")

    lines = [f"🚨 publish-scan: {len(issues)} issue(s) found in {dist_dir}/"]
    if critical:
        lines.append(f"  ❌ {critical} CRITICAL (secrets/keys)")
    if high:
        lines.append(f"  ⚠ {high} HIGH (suspicious files/paths)")

    for i in issues:
        lines.append(f"  [{i['severity']}] {i['file']}: {i['reason']}")

    lines.append("\nFix these issues before publishing to PyPI.")
    return "\n".join(lines)


# ── Semver Breaking Change Detection ────────────────────────


def _extract_func_sig(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Extract function signature: `def name(param1, param2, ...)`."""
    params: list[str] = []
    for arg in node.args.args:
        if arg.arg == "self" or arg.arg == "cls":
            continue
        params.append(arg.arg)
    for arg in node.args.kwonlyargs:
        params.append(f"*{arg.arg}")
    if node.args.vararg:
        params.append(f"*{node.args.vararg.arg}")
    if node.args.kwarg:
        params.append(f"**{node.args.kwarg.arg}")
    return f"def {node.name}({', '.join(params)})"


def _extract_module_names(tree: ast.Module) -> set[str]:
    """Extract public names from a parsed module AST."""
    names: set[str] = set()
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not node.name.startswith("_"):
                names.add(_extract_func_sig(node))
        elif isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            names.add(f"class {node.name}")
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and not target.id.startswith("_"):
                    names.add(f"var {target.id}")
    return names


def extract_public_api(src_dir: str) -> dict[str, set[str]]:
    """Extract public API signatures from Python source files.

    Scans __init__.py and top-level modules for public names
    (functions, classes, constants without _ prefix).

    Returns:
        Dict mapping module path → set of public names.
    """
    api: dict[str, set[str]] = {}

    if not os.path.isdir(src_dir):
        return api

    for root, _dirs, files in os.walk(src_dir):
        for fname in files:
            if not fname.endswith(".py"):
                continue
            fpath = os.path.join(root, fname)
            rel = os.path.relpath(fpath, src_dir).replace("\\", "/")
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    source = f.read()
                tree = ast.parse(source)
            except (SyntaxError, OSError):
                continue

            names = _extract_module_names(tree)
            if names:
                api[rel] = names

    return api


def check_breaking_changes(
    old_api: dict[str, set[str]],
    new_api: dict[str, set[str]],
) -> list[dict[str, str]]:
    """Compare old vs new public API and detect breaking changes.

    Breaking = removed or renamed public function/class/constant.
    Non-breaking = additions (new functions/classes).

    Returns:
        List of breaking change issue dicts.
    """
    issues: list[dict[str, str]] = []

    for module, old_names in old_api.items():
        if module not in new_api:
            issues.append(_issue(
                "CRITICAL", module,
                f"Module removed: {module} ({len(old_names)} public names lost)",
                "module_removed",
            ))
            continue

        new_names = new_api[module]
        removed = old_names - new_names
        for name in sorted(removed):
            issues.append(_issue(
                "CRITICAL", module,
                f"Breaking: `{name}` removed from public API",
                "api_removed",
            ))

    return issues


def semver_gate(
    src_dir: str,
    old_api: dict[str, set[str]] | None = None,
    old_api_file: str = "",
) -> list[dict[str, str]]:
    """Semver breaking change gate.

    Compares current source against saved API snapshot.
    If breaking changes found, recommends major version bump.

    Args:
        src_dir: Source directory to scan.
        old_api: Previous API snapshot (dict).
        old_api_file: Path to JSON file with previous API.

    Returns:
        List of breaking change issues (empty = safe for minor/patch).
    """
    import json as _json

    new_api = extract_public_api(src_dir)

    if old_api is None and old_api_file:
        try:
            with open(old_api_file, "r", encoding="utf-8") as f:
                raw = _json.load(f)
            old_api = {k: set(v) for k, v in raw.items()}
        except (OSError, _json.JSONDecodeError):
            return [_issue(
                "WARN", old_api_file,
                "Cannot read previous API snapshot",
                "no_baseline",
            )]

    if old_api is None:
        return []

    return check_breaking_changes(old_api, new_api)


def save_api_snapshot(src_dir: str, output_file: str) -> None:
    """Save current public API snapshot to JSON for future comparison."""
    import json as _json

    api = extract_public_api(src_dir)
    serializable = {k: sorted(v) for k, v in api.items()}
    os.makedirs(os.path.dirname(output_file) or ".", exist_ok=True)
    with open(output_file, "w", encoding="utf-8") as f:
        _json.dump(serializable, f, indent=2, ensure_ascii=False)


# ── Guard Pipeline Adapters ────────────────────────────────


class PublishScanGuard(BaseGuard):
    """Block PyPI publish if dist artifacts contain secrets or dangerous files."""

    name = "publish_scan"
    category = GuardCategory.SECURITY

    def check(self, ctx: GuardContext) -> GuardResult | None:
        if ctx.tool_name != "Bash":
            return None
        cmd = ctx.tool_input.get("command", "") if isinstance(ctx.tool_input, dict) else ""
        if not re.search(r"(?:twine\s+upload|flit\s+publish|poetry\s+publish)", cmd):
            return None
        issues = scan_dist()
        critical = [i for i in issues if i["severity"] == "CRITICAL"]
        if critical:
            details = "; ".join(f"{i['file']}: {i['reason']}" for i in critical[:3])
            return GuardResult.deny(
                f"publish-scan: {len(critical)} CRITICAL issue(s) in dist/",
                context=details,
                check_type="publish_scan",
            )
        return None


class SemverGuard(BaseGuard):
    """Warn on breaking API changes before publish (semver gate)."""

    name = "semver_gate"
    category = GuardCategory.QUALITY

    _snapshot_path = ".concinno_cache/api_snapshot.json"

    def check(self, ctx: GuardContext) -> GuardResult | None:
        if ctx.tool_name != "Bash":
            return None
        cmd = ctx.tool_input.get("command", "") if isinstance(ctx.tool_input, dict) else ""
        if not re.search(r"(?:twine\s+upload|flit\s+publish|poetry\s+publish)", cmd):
            return None
        if not os.path.isfile(self._snapshot_path):
            return None
        issues = semver_gate("src", old_api_file=self._snapshot_path)
        breaking = [i for i in issues if i["severity"] == "CRITICAL"]
        if breaking:
            details = "; ".join(f"{i['file']}: {i['reason']}" for i in breaking[:5])
            return GuardResult.deny(
                f"semver-gate: {len(breaking)} breaking change(s) — bump major version",
                context=details,
                check_type="semver_breaking",
            )
        return None
