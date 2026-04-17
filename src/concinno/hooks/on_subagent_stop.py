"""concinno SubagentStop hook — verify subagent artifacts on completion.

Problem: Subagents claim to have written files that don't exist on disk.
PostToolUse/Agent catches some, but SubagentStop fires with full subagent
context (subagentId, result) — more precise signal.

Solution: Extract file paths from subagent result, verify on disk,
inject manifest into additionalContext.
"""

from __future__ import annotations

import json
import os
import sys


def _extract_paths_from_result(text: str) -> list[str]:
    """Extract file paths from subagent result text.

    Reuses logic from agent_artifact_guard but kept minimal here
    to avoid heavy imports in a hook script.
    """
    import re

    paths: list[str] = []
    seen: set[str] = set()

    patterns = [
        # Windows absolute: C:\project\... or C:/project/...
        re.compile(r'[A-Z]:[/\\][\w./\\-]+\.\w{1,10}'),
        # Unix absolute: /home/... /tmp/...
        re.compile(r'/(?:home|tmp|var|usr|opt|etc)/[\w./\\-]+\.\w{1,10}'),
        # Backtick-quoted relative: `src/foo.py`
        re.compile(r'`([\w./\\-]+\.\w{1,10})`'),
    ]

    for pat in patterns:
        for m in pat.finditer(text):
            p = m.group(1) if m.lastindex else m.group(0)
            p = p.rstrip(".,;:)")
            if p not in seen:
                seen.add(p)
                paths.append(p)

    return paths


def _resolve_path(raw: str, workspace: str) -> str:
    """Resolve a path to absolute, handling relative paths."""
    if os.path.isabs(raw):
        return os.path.normpath(raw)
    return os.path.normpath(os.path.join(workspace, raw))


_WIRING_WHITELIST = ("src", "projects", "lib")
_WIRING_SKIP_DIRS = frozenset((
    "node_modules", "__pycache__", ".git", "dist", "build",
    ".venv", "venv", ".tox", ".mypy_cache", ".ruff_cache",
    ".concinno_cache", "_AI_BRAIN",
))
_WIRING_MAX_FILES = 500
_WIRING_SRC_EXT = (".py", ".ts")


def _search_import(stem: str, basename: str, workspace: str) -> bool:
    """Search whitelisted dirs for a reference to *stem*. Cap at 500 files."""
    import re as _re

    files_checked = 0
    for subdir in _WIRING_WHITELIST:
        search_root = os.path.join(workspace, subdir)
        if not os.path.isdir(search_root):
            continue
        for root, dirs, files in os.walk(search_root):
            dirs[:] = [d for d in dirs if d not in _WIRING_SKIP_DIRS]
            for fname in files:
                if fname == basename or not any(fname.endswith(e) for e in _WIRING_SRC_EXT):
                    continue
                files_checked += 1
                if files_checked > _WIRING_MAX_FILES:
                    return True  # budget exhausted — assume OK
                try:
                    with open(os.path.join(root, fname), encoding="utf-8", errors="ignore") as f:
                        content = f.read(8192)
                    if _re.search(rf'\b{_re.escape(stem)}\b', content):
                        return True
                except OSError:
                    continue
    return False


def _verify_wiring(confirmed: list[str], workspace: str) -> list[str]:
    """WIREDO W: Check new source files are imported somewhere.

    Only checks .py/.ts source files (not tests/configs).
    Searches whitelisted dirs only, with file-count cap for performance.
    """
    warnings: list[str] = []
    skip_patterns = (".test.", "_test.", "test_", "conftest", "__init__")

    for path in confirmed:
        basename = os.path.basename(path)
        if not any(path.endswith(ext) for ext in _WIRING_SRC_EXT):
            continue
        if any(skip in basename for skip in skip_patterns):
            continue
        stem = os.path.splitext(basename)[0]
        if not _search_import(stem, basename, workspace):
            warnings.append(f"W ❌ {basename} not imported anywhere")

    return warnings


def _verify_tests(confirmed: list[str]) -> list[str]:
    """WIREDO D: Check new source files have corresponding tests."""
    warnings: list[str] = []
    for path in confirmed:
        basename = os.path.basename(path)
        # Only check source .py files (not tests themselves)
        if not path.endswith(".py"):
            continue
        if "test" in basename or basename == "__init__.py":
            continue

        # Check for test file in standard locations
        stem = os.path.splitext(basename)[0]
        parent = os.path.dirname(path)

        # Look for test_<name>.py in sibling tests/ dir or same dir
        candidates = [
            os.path.join(parent, f"test_{stem}.py"),
            os.path.join(parent, "tests", f"test_{stem}.py"),
            os.path.join(os.path.dirname(parent), "tests", f"test_{stem}.py"),
        ]
        if not any(os.path.isfile(c) for c in candidates):
            warnings.append(f"D ❌ {basename} has no test file")

    return warnings


def _build_manifest(
    confirmed: list[str],
    missing: list[str],
    wiring_warnings: list[str] | None = None,
    test_warnings: list[str] | None = None,
) -> str:
    """Build artifact verification manifest with WIREDO checks."""
    lines: list[str] = []
    if confirmed:
        lines.append(f"✅ Verified artifacts ({len(confirmed)}):")
        for p in confirmed[:10]:
            lines.append(f"  {p}")
    if missing:
        lines.append(f"❌ Missing artifacts ({len(missing)}):")
        for p in missing[:10]:
            lines.append(f"  {p}")
        lines.append(
            "⚠ Subagent claimed these files but they don't exist. "
            "Re-check or re-create."
        )
    if wiring_warnings:
        lines.append("⚠ WIREDO Wiring issues:")
        lines.extend(f"  {w}" for w in wiring_warnings)
    if test_warnings:
        lines.append("⚠ WIREDO Test issues:")
        lines.extend(f"  {w}" for w in test_warnings)
    return "\n".join(lines)


def _extract_result_text(hook_data: dict) -> str:
    """Extract subagent result text from hook_data."""
    # Try direct result keys first
    for key in ("subagentResult", "result", "output"):
        val = hook_data.get(key, "")
        if isinstance(val, str) and val:
            return val
        if isinstance(val, dict):
            return json.dumps(val, ensure_ascii=False)

    # Fallback: last assistant message
    messages = hook_data.get("messages", [])
    if not isinstance(messages, list):
        return ""
    for msg in reversed(messages):
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            texts = [
                p.get("text", "") for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            ]
            return "\n".join(texts)
    return ""


def _verify_paths(
    raw_paths: list[str], workspace: str,
) -> tuple[list[str], list[str]]:
    """Verify extracted paths exist on disk."""
    confirmed: list[str] = []
    missing: list[str] = []
    for raw in raw_paths:
        resolved = _resolve_path(raw, workspace)
        if os.path.isfile(resolved):
            confirmed.append(resolved)
        else:
            missing.append(f"{raw} → {resolved}")
    return confirmed, missing


def _write_output(event_name: str, context: str) -> None:
    """Write hook JSON output to stdout."""
    output = json.dumps({
        "hookSpecificOutput": {
            "hookEventName": event_name,
            "additionalContext": context,
        }
    }, ensure_ascii=False)
    if hasattr(sys.stdout, "buffer"):
        sys.stdout.buffer.write(output.encode("utf-8"))
        sys.stdout.buffer.flush()
    else:
        sys.stdout.write(output)
        sys.stdout.flush()


def main(hook_data: dict | None = None) -> None:
    """SubagentStop entry point."""
    try:
        if hook_data is None:
            hook_data = json.loads(sys.stdin.read())
    except Exception:
        return

    workspace = os.environ.get("CLAUDE_PROJECT_DIR", "")
    if not workspace:
        return

    result_text = _extract_result_text(hook_data)
    if not result_text:
        return

    raw_paths = _extract_paths_from_result(result_text)
    if not raw_paths:
        return

    confirmed, missing = _verify_paths(raw_paths, workspace)
    if not confirmed and not missing:
        return

    # WIREDO W+D checks on confirmed artifacts
    wiring_warnings = _verify_wiring(confirmed, workspace)
    test_warnings = _verify_tests(confirmed)

    # AgentSupervisor contract verification
    supervisor_ctx = ""
    try:
        from concinno.hooks.io_utils import cache_path
        cache_dir = cache_path()
        if cache_dir:
            from concinno.agent_supervisor import verify_task
            agent_id = hook_data.get("subagentId", "")
            if agent_id:
                vr = verify_task(cache_dir, agent_id, workspace, result_text)
                supervisor_ctx = vr.summary()
    except Exception:
        pass

    manifest = _build_manifest(
        confirmed, missing, wiring_warnings, test_warnings,
    )
    if supervisor_ctx:
        manifest = manifest + "\n" + supervisor_ctx

    _write_output("SubagentStop", manifest)


if __name__ == "__main__":
    main()
