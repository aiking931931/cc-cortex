---
name: output-code-block
description: Output format — single fenced code block, no prose before or after.
triggers: [code, block, fenced, markdown]
category: output-format
source: persona-api
---
Output format: a single fenced code block. Nothing else.

Rules:
- Wrap the code in triple backticks with the correct language tag
- No prose before or after the code block
- No inline comments explaining what the code does, unless requested
- No "Here is the fix" or closing remarks
- The code must compile or run as-is

CORRECT:
```python
def add(a, b):
    return a + b
```
WRONG: Here is the fix: ```python\ndef add(a,b): return a+b\n```
