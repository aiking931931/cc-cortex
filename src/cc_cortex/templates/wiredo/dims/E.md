E (Extensible) — config-driven where appropriate. Required:
  - thresholds / endpoints / api keys come from env or config
  - no magic numbers blocking future variation
  - feature toggles where two callers might want different behavior
  ✓ evidence: "uses cfg.threshold('foo_max', 100)"
  ✗ evidence: "hardcoded RETRY_COUNT = 3 inside loop"
