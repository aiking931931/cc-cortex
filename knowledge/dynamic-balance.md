# Dynamic Balance (Equilibrium Principle)

Every system has opposing forces. Health comes from balance, not from
maximizing one side. When you add, also remove. When you tighten, also loosen.

## Core Principles

### Yin-Yang Complementarity

Every strength creates a corresponding vulnerability:

- **More guards** = more safety, but also more friction and more token cost
- **More automation** = less manual work, but also less human oversight
- **More abstraction** = cleaner code, but also harder debugging
- **More features** = more capability, but also more attack surface

The goal is not maximum of either side, but the **optimal tension** between them.

### Dynamic Equilibrium

Balance is not a static state — it's continuous adjustment:

- **Add a rule** → ask "what can I remove?" (Attention Budget)
- **Harden a behavior** → delete the corresponding soft rule (rule graduation)
- **Expand scope** → prune unused features (anti-bloat)
- **Increase complexity** → simplify something else (complexity budget)

### Self-Evolution

Systems that don't evolve die. Systems that evolve too fast destabilize.

- **Gradual change**: Small, verified steps. Each step must prove itself
  before the next begins
- **Reversibility preference**: Prefer reversible changes over irreversible ones
- **Feedback loops**: Every change should generate observable signal — if you
  can't tell whether it helped, you can't evolve

## The Conservation Laws

### Attention Budget Conservation

Total attention capacity is fixed. When a behavior is hardened (automated by
hook or compiler), its corresponding rule must be deleted, returning attention
budget to the pool.

Four stages: Learning (rule) → Sticky note (hook) → Muscle memory (TypeScript)
→ Release (delete rule)

### Complexity Budget Conservation

Adding complexity in one place requires simplifying another. If total system
complexity only grows, the system eventually becomes unmanageable.

Measure: If you can't explain the system to a new contributor in 15 minutes,
it's too complex.

## When to Apply

- System design (balancing competing requirements)
- Feature prioritization (what to add vs. what to cut)
- Rule management (preventing rule bloat)
- Architecture evolution (growing without destabilizing)

## Anti-Patterns

- **Maximizing one axis**: "More security is always better" — at some point,
  the friction cost exceeds the risk reduction
- **Addition-only thinking**: Never removing, only adding. Entropy wins
- **False balance**: Not everything deserves equal weight. Some things matter
  more — but even dominant priorities have costs
