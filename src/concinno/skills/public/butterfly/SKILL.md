---
name: concinno-butterfly
description: Detect pre-existing bugs surfaced during current task; refuse Stop until handled or written into handoff section.
triggers: [butterfly, pre-existing bug, technical debt]
user-invocable: false
license: Apache-2.0
upstream: https://github.com/aiking931931/concinno
---

# ButterflyGuard

PostToolUse + Stop pipeline gate. When current edit/test surfaces a pre-existing issue (lint/type/test fail not introduced by current change), guard records it. Stop hook checks for unhandled records and blocks Stop unless handoff section was updated.

## Install

```bash
pip install concinno
concinno init
```

## See also

Source: [src/concinno/guards/](https://github.com/aiking931931/concinno/tree/main/src/concinno/guards)
