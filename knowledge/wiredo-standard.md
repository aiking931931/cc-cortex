# WIREDO Delivery Standard

Six-dimension verification for any deliverable. If it's not WIREDO, it's not done.
Build toward WIREDO from the start — it's a direction, not a checklist applied after the fact.

## The Six Dimensions

### W — Wired (Connected)

The deliverable is connected to the system. Someone calls it, imports it,
or depends on it. Removing it would cause a failure somewhere.

**Verify**: `grep` confirms something references it. If nothing does, it's
dead code — delete it or wire it in before claiming completion.

### I — Inherited & Aligned (Template + Architecture)

The deliverable follows existing patterns. It uses the base class, the shared
template, the established module structure. It lives in the architecturally
correct location.

**Verify**: Does it look like its siblings? Is it in the right directory?
Does it extend the right base type?

### R — Responsive & Performant (Works everywhere, works fast)

For UI: works on desktop and mobile. For code: no O(n²), no N+1 queries,
no unnecessary blocking. For APIs: response time is acceptable.

**Verify**: Performance — benchmark or profile. UI — test on multiple viewports.
API — measure latency under load.

### E — Extensible (Configurable)

Magic numbers are constants. Behavior is configurable. Future requirements
won't require rewriting the core.

**Verify**: Are hardcoded values at the top as named constants? Is there a
config interface for tunable behavior? Can you add a new case without
modifying existing code?

### D — Documented, Defended & Verified

There are tests. The tests pass. The linter is clean. Evidence exists that
the deliverable works as claimed.

**Visual verification is the default for UI/frontend changes.**
If a change touches anything the user can see (layout, styling, components,
pages, navigation), take a screenshot or run Playwright to prove it looks
correct. Only skip visual verification when it is genuinely impossible
(e.g. pure backend logic, CI-only environment without a browser).

**Verify**: Automated tests cover the happy path and key edge cases.
`lint` / `tsc` / `ruff` reports zero errors. Screenshots or test output
prove it works. For UI: deploy → screenshot → confirm visually.

### O — Observable (Can you see it working?)

The deliverable emits signals about its health. Structured return values,
stats endpoints, log entries, health checks.

**Verify**: Is there a `stats()` method? Structured logging? Error tracking?
For SaaS: health check endpoint, monitoring dashboard.

## Workflow

### Phase 1: Start — List Both Checklists

When starting a task, create the task checklist AND the WIREDO checklist
side by side. WIREDO is not an afterthought — it guides how you build.

### Phase 2: Build — Aim Toward WIREDO

Before writing each piece of code, ask the six questions:

1. W — Where does this live in the system? Who calls it?
2. I — What does it look like? Does it match existing patterns?
3. R — What's the performance cost?
4. E — Is this configurable?
5. D — How will I prove it works?
6. O — How will I observe it in production?

### Phase 3: Complete — Verify Each Dimension

Check off each dimension with evidence, not assumptions.

## Applicability

- **Code tasks** (features, refactors, bug fixes): All 6 dimensions
- **Non-SaaS code** (SDK, CLI, local tools): O is optional ("N/A — not SaaS")
- **Non-code tasks** (writing, research, planning): WIREDO does not apply

## Asset Types

WIREDO extends beyond code to five asset types:
**code** · **image** · **video** · **audio** · **document**

Each type has dimension-specific checks. For example:

- Image: W=used in UI, I=matches style guide, R=optimized size, D=visual QA
- Document: W=linked from index, I=follows template, D=spell-checked
