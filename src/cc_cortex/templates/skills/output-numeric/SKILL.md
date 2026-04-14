---
name: output-numeric
description: Output format — a single number, no units, no words, no explanation.
triggers: [numeric, number, answer, calculation]
category: output-format
source: persona-api
---
Output format: a single number. Nothing else.

Rules:
- No units unless the question explicitly asks for them
- No currency symbols unless the question explicitly asks for them
- No commas, no scientific notation unless asked
- No words before or after the number
- No explanation, no reasoning trace

CORRECT: 4823.57
CORRECT: 42
CORRECT: -0.185
WRONG: approximately 4823.57 USD
WRONG: The answer is 42.
WRONG: 4,823.57 (fiscal year 2024)
