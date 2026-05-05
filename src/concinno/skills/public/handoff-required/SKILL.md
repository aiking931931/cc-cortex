---
name: concinno-handoff-required
description: Stop hook gate — if session edited >=3 files or ran >=20 minutes without touching a handoff file, warn and inject minimal handoff template.
triggers: [handoff, session end, hygiene]
user-invocable: false
license: Apache-2.0
upstream: https://github.com/aiking931931/concinno
---

# HandoffGuard

Stop event gate. Tracks: file edits this session, elapsed time, last touch time of any handoff_*.md (path configurable). Trip conditions: >=3 files edited AND no handoff touched -> warn; >20 min elapsed AND >=1 commit AND no handoff touched -> warn; Stop attempted with warning unaddressed -> soft block. Escape CONCINNO_HANDOFF_OPTIONAL=1.

## Install

```bash
pip install concinno
concinno init
```

## See also

Source: [src/concinno/guards/](https://github.com/aiking931931/concinno/tree/main/src/concinno/guards)
