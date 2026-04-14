---
name: coding-standards
description: Cross-language coding standards and conventions. Triggers on "coding standards", "命名規範", "code style", "convention", "lint rules", "formatting".
user-invocable: true
disable-model-invocation: true
---

# /coding-standards — Cross-Language Coding Standards

I write code that reads like prose. Naming is design. Consistency is kindness.

> **You MUST** cite file:line for every finding.
> **You MUST** separate auto-fixable (formatter) from manual-fix (naming/logic).
> **You MUST** respect existing project conventions over personal preference.

## Execution

```
/coding-standards              — Audit git diff for style violations
/coding-standards <file>       — Audit specific file
/coding-standards --init       — Generate .editorconfig + lint config for project
```

### 1. Detect Project Stack
Read `package.json`, `pyproject.toml`, `go.mod`, `Cargo.toml` → identify languages.

### 2. Audit
For each language, check:
- **Naming**: variables (camelCase/snake_case), constants (UPPER), types (PascalCase)
- **Structure**: max function length (≤40 lines), max file length (≤400 lines), nesting depth (≤3)
- **Imports**: ordering, unused, circular
- **Comments**: no commented-out code, no TODO without issue ref

### 3. Report
```
## Standards Audit — <N> findings

### Auto-fixable (formatter/linter)
1. `file:line` — <issue>

### Manual Fix
1. `file:line` — <issue> → <suggestion>
```
