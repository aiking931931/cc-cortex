W (Wired) — connected to the system. Required:
  - imports/exports resolve (no dangling symbols)
  - at least one caller exists (not orphan code)
  - registered in the right registry / router / pipeline
  ✓ evidence: grep result showing caller, route table entry, init import
  ✗ evidence: "added FooHandler but no router.register(FooHandler)"
