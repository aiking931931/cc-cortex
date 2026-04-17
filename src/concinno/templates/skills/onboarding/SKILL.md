---
name: onboarding
description: Developer onboarding guide generation — project setup, architecture overview, key concepts. Triggers on "onboarding", "入門", "getting started", "new developer", "project setup", "架構概覽".
user-invocable: true
---

# /onboarding — Developer Onboarding Guide

I write onboarding docs that get a new dev productive in hours, not weeks.

> **You MUST** verify every setup command actually works before documenting.
> **You MUST** include "first task" suggestion at the end.
> **You MUST** keep under 500 lines — link to details, don't inline everything.

## Usage

```
/onboarding             — Generate onboarding guide for current project
/onboarding --quick     — Quick-start only (setup + run)
/onboarding --arch      — Architecture deep-dive
```

## Generated Sections

1. **Quick Start** — Clone, install deps, run dev server (≤10 commands)
2. **Architecture Overview** — System diagram, key modules, data flow
3. **Key Concepts** — Domain terms, patterns used, conventions
4. **Directory Guide** — What lives where and why
5. **Common Tasks** — How to add a feature, fix a bug, write a test
6. **Gotchas** — Known quirks, common mistakes, tribal knowledge
7. **First Task** — A small, scoped task to start contributing

## Execution

1. Read `package.json`/`pyproject.toml` for stack detection
2. Scan directory structure for architecture patterns
3. Read README.md, CONTRIBUTING.md if they exist
4. Generate guide with verified commands
