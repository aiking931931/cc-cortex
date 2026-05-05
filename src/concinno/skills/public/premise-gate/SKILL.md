---
name: concinno-premise-gate
description: Block work when external constraints unread (Mode 1 — competition rules / spec docs) or platform ceilings unverified (Mode 2 — Claude Code L1-L8). Forces WebFetch official docs first.
triggers: [premise, ceiling, platform limit, spec verification]
user-invocable: false
license: Apache-2.0
upstream: https://github.com/aiking931931/concinno
---

# PremiseGate

PreToolUse signal. Mode 1 (external rules): if working on a competition/contract task and the source doc has not been Read this session, blocks with read the original spec first. Mode 2 (platform ceilings): if a Claude Code L1-L8 limit is being cited, require WebFetch of official docs.

## Install

```bash
pip install concinno
concinno init
```

## See also

Source: [src/concinno/guards/](https://github.com/aiking931931/concinno/tree/main/src/concinno/guards)
