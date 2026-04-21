---
name: concinno-consecutive-fail
description: Three-strikes rule. Same op fails 2x triggers force-RAG; 3x hard-stops. Prevents agent grinding on a wrong approach for 10 retries.
triggers: [consecutive fail, three strikes, RAG, retry loop]
user-invocable: false
license: Apache-2.0
upstream: https://github.com/aiking931931/concinno
---

# ConsecutiveFailGuard

PreToolUse + signal injection. Tracks failure counts per (op_name, file_path). At 2 consecutive failures, injects MUST RAG reminder. At 3, hard-blocks the next attempt. Configurable via CONCINNO_FAIL_THRESHOLD env.

## Install

```bash
pip install concinno
concinno init
```

## See also

Source: [src/concinno/guards/](https://github.com/aiking931931/concinno/tree/main/src/concinno/guards)
