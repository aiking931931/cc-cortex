# CC Cortex Script SOP

> All contributors (human and AI) MUST follow this SOP.

## Folder Rules

| Type | Location | Forbidden |
| ---- | -------- | --------- |
| CCC core modules | `src/concinno/` root | No new subdirs unless subpackage |
| Guard framework | `src/concinno/guards/` | No BaseGuard subclasses here — put in root |
| Guard implementations | `src/concinno/<name>.py` | No new guard subdirs |
| Hook entry points | `src/concinno/hooks/` | No business logic — lifecycle only |
| Infrastructure | `src/concinno/core/` | No business logic — config/log/state/path only |
| CC Hook wrappers | `.claude/hooks/` | Never copy CCC code (boundary rule) |
| CC Skill definitions | `.claude/skills/<name>/SKILL.md` | No .py logic |
| Personal scripts | `_AI_BRAIN/06_Monitor/` | Never mix into CCC |
| Retired code | `src/concinno/_recycled/` | Don't delete — keep README |
| PowerShell scripts | `src/concinno/scripts/` | Don't scatter in root |

## Naming Rules

| Type | Pattern | Example |
| ---- | ------- | ------- |
| Guard module | `<function>.py` or `<function>_guard.py` | `secret_scan.py`, `identity_guard.py` |
| CCC Hook entry | `on_<event>.py` (underscore) | `on_pre_tool.py` |
| CC Hook wrapper | `on-<event>.py` (hyphen) | `on-pre-tool.py` |
| Utility module | `<function>.py` | `rag.py`, `linting.py` |
| Test file | `test_<module>.py` | `test_cognitive_anchor.py` |
| PowerShell | `<function>-<action>.ps1` | `schedule-dashboard.ps1` |

## Code Rules (New Modules MUST Follow)

1. **`@module` docstring required** — `@module` + `@responsibility` + `@dependencies` + `@exports`
2. **Guards inherit `BaseGuard`** — set `name`, `category`, `step_back_reason`
3. **Guards MUST be registered in `registry.py`** — unregistered = orphan code
4. **Guards MUST be registered in `feature_config.py`** — unregistered = no user control
5. **Guards MUST have pytest** — minimum 5 test cases
6. **Never import CC-specific paths** — no `_AI_BRAIN`, `E:\Cursor`, hardcoded Chinese in library code
7. **Use `StateStore` for persistence** — don't read/write JSON directly
8. **Deny messages use solid-state language** — `MUST`, `Evidence:`, `user explicitly requested`; never use soft words like "consider" or "maybe"

## Canonical Implementations (Use These, Don't Reinvent)

| Function | Canonical Module | Why |
| -------- | ---------------- | --- |
| stdin parsing | `hooks/io_utils.py` → `read_hook_data()` | Unified JSON parse + error handling |
| Guard deny | `constants.py` → `make_deny()` | Standard format factory |
| Guard inject | `hypothesis_tracker.py` → `GuardResult.allow(context=...)` | Simplest cognitive injection |
| ripgrep search | `delivery.py` → `_is_symbol_imported_rg()` | Fastest search with walk fallback |
| State persistence | `core/state_store.py` → `StateStore.read/write` | Unified JSON I/O |
| Path extraction | `core/path_utils.py` → `extract_file_path()` | 3 callsites already migrated |
| Process detection | `process_guard.py` → `_find_git_bash()` | Dynamic detection, no hardcoded paths |
| Failure detection | `hypothesis_tracker.py` → `_is_failure()` | Unified error signal heuristic |
