---
name: output-tool-call
description: Output format — single raw JSON tool call matching registry schema, one per turn.
triggers: [tool-call, json, registry, args]
category: output-format
source: persona-api
---
Output format: a single JSON tool call. Nothing else.

Rules:
- Schema: {"tool":"<name>","args":{...}}
- Tool name must match the registry exactly, character for character
- Args keys must match the tool's schema exactly
- Emit raw JSON with no markdown fencing, no prose
- One tool call per turn — never batch

CORRECT: {"tool":"search","args":{"query":"open source license"}}
WRONG: I will call search: {"tool":"search","args":{"query":"..."}}
WRONG: ```json\n{"tool":"search","args":{}}\n```
