---
name: concinno-bash-dry-run
description: Input-rewriter — converts dangerous Bash patterns into dry-run preview. ALLOW-only (rewrites, never denies).
triggers: [bash dry run, dangerous bash, rewrite, preview]
user-invocable: false
license: Apache-2.0
upstream: https://github.com/aiking931931/concinno
---

# BashDryRunRewriter

PreToolUse rewrite (not deny). Detects rm -rf X -> rm -rf --dry-run X, mv A B where B exists -> mv -i A B, git push --force -> --force-with-lease. User sees rewritten cmd in tool result. Compose with DestructionGuard: rewriter is soft layer, DestructionGuard is hard deny.

## Install

```bash
pip install concinno
concinno init
```

## See also

Source: [src/concinno/guards/](https://github.com/aiking931931/concinno/tree/main/src/concinno/guards)
