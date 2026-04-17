"""
CC Cortex — Custom Hook Examples

Every example in this file uses REAL imports that work out of the box.
Run ``python examples/custom_hooks.py`` to verify.

Each example is a standalone guard function compatible with concinno.Pipeline.
"""

from __future__ import annotations

from concinno import HookResult, Pipeline

# ═══════════════════════════════════════════════════════════════════════════
# Example 1: Production Safety Guard
# ═══════════════════════════════════════════════════════════════════════════
# Combines the built-in destruction guard with a project-specific rule.


def production_guard(tool_name: str, tool_input: dict, **ctx) -> HookResult | None:
    """Block commands that touch production systems."""
    if tool_name != "Bash":
        return None
    cmd = tool_input.get("command", "")
    if "deploy" in cmd and "production" in cmd and "--confirm" not in cmd:
        return HookResult.deny(
            "Production deployment blocked. Add '--confirm' to proceed."
        )
    return None


# ═══════════════════════════════════════════════════════════════════════════
# Example 2: Protected File Guard
# ═══════════════════════════════════════════════════════════════════════════

PROTECTED = [".env.production", "docker-compose.prod.yml", "migrations/"]


def protected_file_guard(tool_name: str, tool_input: dict, **ctx) -> HookResult | None:
    """Prevent edits to critical files."""
    if tool_name not in ("Edit", "Write"):
        return None
    path = tool_input.get("file_path", "")
    for p in PROTECTED:
        if p in path:
            return HookResult.deny(f"Protected file: {path} (matches '{p}')")
    return None


# ═══════════════════════════════════════════════════════════════════════════
# Example 3: Composing a Full Pipeline
# ═══════════════════════════════════════════════════════════════════════════
# Chain built-in guards with your own in a single pipeline.


def build_pipeline() -> Pipeline:
    """Build a pipeline with built-in + custom guards."""
    from concinno.destruction_guard import evaluate
    from concinno.secret_scan import check as secret_check

    pipe = Pipeline()

    # Phase 1: deny guards (short-circuit on first deny)
    pipe.add_deny_guard("destruction", evaluate)
    pipe.add_deny_guard("secrets", secret_check)
    pipe.add_deny_guard("production", production_guard)
    pipe.add_deny_guard("protected_files", protected_file_guard)

    # Phase 2: warn guards (all run, warnings collected)
    # pipe.add_warn_guard("my_lint", my_lint_check)

    return pipe


# ═══════════════════════════════════════════════════════════════════════════
# Example 4: Using Pipeline in a Hook Script
# ═══════════════════════════════════════════════════════════════════════════
# This is what your actual hook script (on-pre-tool.py) would look like:
#
#   #!/usr/bin/env python3
#   import json, sys
#   from my_hooks import build_pipeline
#
#   data = json.loads(sys.stdin.read())
#   pipe = build_pipeline()
#   result = pipe.run(data["tool_name"], data.get("tool_input", {}))
#   json.dump(result, sys.stdout)


# ═══════════════════════════════════════════════════════════════════════════
# Verification
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    pipe = build_pipeline()
    print("Pipeline guards:", pipe.list_guards())

    # Test 1: safe command → allow
    r1 = pipe.run("Bash", {"command": "ls -la"})
    assert r1["permissionDecision"] == "allow", f"Expected allow, got {r1}"
    print("✓ Safe command: allow")

    # Test 2: destructive command → deny
    r2 = pipe.run("Bash", {"command": "rm -rf /"})
    assert r2["permissionDecision"] != "allow", f"Expected deny, got {r2}"
    print("✓ Destructive command: deny")

    # Test 3: production deploy without --confirm → deny
    r3 = pipe.run("Bash", {"command": "deploy production"})
    assert r3["permissionDecision"] == "deny", f"Expected deny, got {r3}"
    print("✓ Production guard: deny")

    # Test 4: protected file → deny
    r4 = pipe.run("Edit", {"file_path": "/app/.env.production"})
    assert r4["permissionDecision"] == "deny", f"Expected deny, got {r4}"
    print("✓ Protected file: deny")

    # Test 5: HookResult API
    hr = HookResult.deny("test reason", context="extra info")
    assert hr.denied
    assert hr.to_dict() == {
        "permissionDecision": "deny",
        "reason": "test reason",
        "additionalContext": "extra info",
    }
    print("✓ HookResult API: correct")

    print("\nAll examples verified. ✓")
