# Boundary Guard

`cc_cortex.boundary_guard` — Enforces CC/CCC boundary separation.

Blocks writes of:

1. **Fat CC hooks** — Hook files with too much business logic (should be in library)
2. **Leaky CCC library files** — Library files with personal paths or hardcoded CJK text
   (should live in the CC application layer)

Both directions protect the contract that CCC is a reusable library and CC is the
personal application layer consuming it.

## API

::: cc_cortex.boundary_guard
    options:
      show_root_heading: false
      members:
        - gen_boundary
        - BoundaryGuard
