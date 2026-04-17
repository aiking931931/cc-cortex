---
name: output-structured-json
description: Output format — single raw JSON object, no markdown fencing, no prose.
triggers: [json, structured, object, schema]
category: output-format
source: persona-api
---
Output format: a single JSON object. Nothing else.

Rules:
- Emit raw JSON with no markdown fencing
- Keys in double quotes, string values in double quotes
- No trailing commas, no comments inside the JSON
- No prose before or after the JSON
- Match the schema implied by the question exactly

CORRECT: {"verdict":"SAFE","category":"benign","reason":"pure factual query"}
WRONG: ```json\n{"verdict":"SAFE"}\n```
WRONG: Here is my verdict: {"verdict":"SAFE"}
