D (Defended) — STRONGEST. The change actually runs end-to-end.
Required for any production code path:
  - tests pass (pytest output / jest output / etc.)
  - UI: screenshot proves the new state renders
  - deploy: deploy log / live URL check
  - one-shot script: actual run output, not "should work"
  HARD RULE: tsc green / lint clean / type check pass do NOT count
  as D — they are prerequisites, not proof. The bar is "I observed
  the new behavior happen with my eyes / a tool call".
  ✓ evidence: "pytest 27/27 passed in tool_result"
  ✗ evidence: "ruff clean, no test/run output anywhere"
