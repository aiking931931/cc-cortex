---
name: changelog
description: Changelog generation and semantic versioning. Triggers on "changelog", "變更日誌", "release notes", "版本號", "semver", "CHANGELOG".
user-invocable: true
---

# /changelog — Changelog & Semantic Versioning

I write changelogs for humans, not machines. Every entry answers "why should I care?"

> **You MUST** follow Keep a Changelog format.
> **You MUST** use semantic versioning (MAJOR.MINOR.PATCH).
> **You MUST** group by: Added, Changed, Deprecated, Removed, Fixed, Security.

## Semver Decision

```
What changed?
  ├─ Breaking API/behavior → MAJOR (1.x → 2.0)
  ├─ New feature (backwards compatible) → MINOR (1.1 → 1.2)
  ├─ Bug fix → PATCH (1.1.1 → 1.1.2)
  └─ Internal refactor (no behavior change) → PATCH or skip
```

## Usage

```
/changelog              — Generate from git log since last tag
/changelog v1.2.0       — Generate for specific version
/changelog --bump       — Suggest next version number
```

## Format

```markdown
## [1.2.0] - 2026-04-02

### Added
- Feature X for doing Y (#123)

### Fixed
- Bug where Z happened when W (#456)

### Security
- Updated dependency A to fix CVE-XXXX (#789)
```
