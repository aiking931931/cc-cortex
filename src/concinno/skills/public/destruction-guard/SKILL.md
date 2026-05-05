---
name: concinno-destruction-guard
description: Pre-execution intercept of irreversible Bash ops — rm -rf, git reset --hard, force push main, DROP TABLE. R0-R4 risk gating with per-op escape env flags.
triggers: [rm -rf, reset --hard, force push, drop table, irreversible, destruction]
user-invocable: false
license: Apache-2.0
upstream: https://github.com/aiking931931/concinno
---

# DestructionGuard

PreToolUse pipeline gate. Each Bash command is risk-tiered (R0 trivial -> R4 catastrophic) and high-tier ops require either explicit per-op escape env flag (audit-visible) or AskUser confirmation token.

## Install

```bash
pip install concinno
concinno init
```

## See also

Source: [src/concinno/guards/](https://github.com/aiking931931/concinno/tree/main/src/concinno/guards)
