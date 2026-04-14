R (Responsive) — UI / latency. Required when change touches UI or
hot path. N/A for pure backend logic. Required when applicable:
  - desktop + mobile screenshot pair (UI)
  - perf measurement vs baseline (hot path)
  ✓ evidence: "screenshots/verify/foo_desktop.png + foo_mobile.png"
  ✗ evidence: "UI changed but no screenshot tool call in session"
  N/A: "pure cli arg parser, no UI / no hot path"
