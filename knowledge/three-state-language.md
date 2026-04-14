# Three-State Language

Communication has three aggregate states, like matter: gas, liquid, solid.
Each serves a different purpose. Using the wrong state wastes energy or
creates brittleness.

## The Three States

### Gas — Inspirational / Belief (First Person)

Fluid, expansive, motivational. Sets direction without constraining method.

- "I verify before I hand over"
- "I don't check because someone told me to — I check because this is mine"
- "Code that isn't WIREDO isn't done"

**Use for**: Mission statements, cultural values, design philosophy.
Gas fills any container — it adapts to context without prescribing specifics.

### Liquid — Guideline / Best Practice

Structured but flexible. Clear recommendations that can adapt to context.

- "Prefer composition over inheritance when the relationship is 'has-a'"
- "Use structured logging instead of print statements for production code"
- "Split PRs that touch more than 3 subsystems"

**Use for**: Coding standards, architectural guidelines, team practices.
Liquid takes the shape of its container but maintains its volume (the
core guidance stays constant even as application varies).

### Solid — Hard Rule / Must (Second Person)

Rigid, non-negotiable. Enforced mechanically when possible.

- "You MUST run tests before merging"
- "API keys MUST NOT appear in source code"
- "All public functions MUST have type annotations"

**Use for**: Security requirements, compliance rules, safety constraints.
Solid holds its shape regardless of container — it doesn't bend.

## Choosing the Right State

| Situation | State | Why |
|-----------|-------|-----|
| New team/project culture | Gas | Inspire, don't constrain |
| Established practices | Liquid | Guide without rigidity |
| Security/safety critical | Solid | No room for interpretation |
| Cross-team alignment | Gas + Liquid | Vision + actionable guidance |
| Compliance requirements | Solid | Auditors need "MUST", not "consider" |

## Phase Transitions

Like physical matter, language can transition between states:

- **Gas → Liquid**: A belief becomes a documented practice when it proves
  useful repeatedly
- **Liquid → Solid**: A guideline becomes a hard rule when violations cause
  real damage (count ≥ 7 in distillation pipeline)
- **Solid → Gas**: A hard rule becomes a belief when it's so internalized
  that enforcement is unnecessary (graduation)

## Anti-Patterns

- **All solid**: Everything is a MUST → rule fatigue, nobody reads them
- **All gas**: Everything is inspirational → no actionable guidance
- **Wrong state for audience**: Solid rules for creative work (kills
  innovation). Gas for security (kills safety)
- **State mismatch with enforcement**: Calling something "MUST" but
  never checking → credibility erosion
