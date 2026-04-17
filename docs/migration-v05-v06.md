# Migration Guide: v0.5 → v0.6+ Guard Pipeline

## Changes at a Glance

| Old API (v0.5) | New API (v0.6+) | Status |
|----------------|-----------------|--------|
| `Pipeline` | `GuardPipeline` | **Deprecated** (removed in v1.0) |
| `HookResult` | `GuardResult` | **Deprecated** (removed in v1.0) |
| `GuardFn` (callable) | `BaseGuard` (class) | **Deprecated** (removed in v1.0) |
| `pipe.add_deny_guard()` | `pipe.register()` | **Deprecated** |
| `pipe.run()` | `pipe.run_pre_tool()` | **Deprecated** |

## Timeline

- **v0.6** (2026-03): Guard Pipeline introduced. v0.5 API still works.
- **v0.8** (2026-03): `DeprecationWarning` emitted on v0.5 imports.
- **v1.0** (planned): v0.5 API removed.

## Step-by-step

### Before (v0.5)

```python
from concinno import HookResult, Pipeline
from concinno.destruction_guard import evaluate

def my_guard(tool_name, tool_input, **ctx):
    if tool_name == "Bash" and "rm -rf" in tool_input.get("command", ""):
        return HookResult.deny("Blocked")
    return None

pipe = Pipeline()
pipe.add_deny_guard("destruction", evaluate)
pipe.add_deny_guard("my_guard", my_guard)
result = pipe.run("Bash", {"command": "rm -rf /"})
```

### After (v0.6+)

```python
from concinno import BaseGuard, GuardCategory, GuardContext, GuardResult
from concinno import create_default_pipeline

class MyGuard(BaseGuard):
    name = "my_guard"
    category = GuardCategory.SECURITY

    def check(self, ctx: GuardContext) -> GuardResult | None:
        if ctx.tool_name == "Bash" and "rm -rf" in ctx.tool_input.get("command", ""):
            return GuardResult.deny("Blocked")
        return None

pipe = create_default_pipeline()  # includes all 27 built-in guards
pipe.register(MyGuard())
result = pipe.run_pre_tool(GuardContext.from_hook_data({
    "tool_name": "Bash",
    "tool_input": {"command": "rm -rf /"},
}))
```

## Key Differences

| Aspect | v0.5 | v0.6+ |
|--------|------|-------|
| Guard definition | Plain function | `BaseGuard` subclass |
| Result type | `HookResult` or `dict` | `GuardResult` (frozen dataclass) |
| Execution order | Manual (add order) | Automatic (by `GuardCategory`) |
| Three layers | No | SECURITY → QUALITY → COGNITIVE |
| Health tracking | No | Auto-disable on 3 consecutive failures |
| Step-back middleware | No | Auto-applied for QUALITY guards |
| PostToolUse/Stop | Separate code | `on_post_tool()` / `on_stop()` overrides |

## FAQ

**Do I have to migrate now?**
No. v0.5 API works until v1.0. But new projects should use v0.6+ API.

**Will my existing hooks break?**
No. Starting from v0.8, you'll see `DeprecationWarning` on import, but functionality is unchanged.

**Can I mix v0.5 and v0.6 guards?**
Yes. `create_default_pipeline()` uses v0.6 guards internally. You can still use `Pipeline` separately.
