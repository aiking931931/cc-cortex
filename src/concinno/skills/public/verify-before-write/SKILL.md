---
name: concinno-verify-before-write
description: Block Write/Edit if destination references files or APIs the agent has not Read this session. Prevents fictional imports.
triggers: [verify before write, fictional API, wrong signature]
user-invocable: false
license: Apache-2.0
upstream: https://github.com/aiking931931/concinno
---

# VerifyBeforeWriteGuard

PreToolUse Write/Edit gate. Parses content for from X import Y, require(Z), function calls to external libs. If the referenced module hasn not been Read or Grepped this session, blocks with read X first. Common save: agent writes from foo import bar based on training data, but foo.bar was renamed.

## Install

```bash
pip install concinno
concinno init
```

## See also

Source: [src/concinno/guards/](https://github.com/aiking931931/concinno/tree/main/src/concinno/guards)
