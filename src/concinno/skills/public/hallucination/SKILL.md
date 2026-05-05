---
name: concinno-hallucination
description: Detect specific assertions (URLs, version numbers, API names) written without backing Read/Grep/Bash evidence in the same session. stderr warns before the claim ships.
triggers: [hallucination, unsourced claim, citation needed]
user-invocable: false
license: Apache-2.0
upstream: https://github.com/aiking931931/concinno
---

# HallucinationGuard

PostToolUse Write/Edit scanner. Pattern-matches URLs, semver strings, function names against the session evidence set. Mismatched assertions get tagged unverified. Useful for doc generation and CHANGELOG entries.

## Install

```bash
pip install concinno
concinno init
```

## See also

Source: [src/concinno/guards/](https://github.com/aiking931931/concinno/tree/main/src/concinno/guards)
