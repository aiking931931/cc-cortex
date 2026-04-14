# Inversion Thinking

Instead of asking "how do I succeed?", ask "how would I fail?" Then avoid
those failure modes. Solving the inverse problem is often easier and more
revealing than solving the forward problem.

## The Process

### 1. Define the Goal

State what success looks like. Be specific.

### 2. Invert

Ask: "What would guarantee failure?" List every way the project, feature,
or decision could go wrong. Be thorough — the more failure modes you
identify, the more protected you are.

### 3. Avoid

For each failure mode, create a prevention mechanism:

- Can it be structurally prevented? (Type system, guard, test)
- Can it be detected early? (Monitoring, lint, CI gate)
- Can it be recovered from? (Backup, rollback, graceful degradation)

### 4. What Remains

After eliminating all known failure modes, what's left is a much safer
path to success.

## When to Apply

- Architecture decisions (what would make this unmaintainable?)
- Security design (what would an attacker exploit?)
- API design (what would make this API misusable?)
- Process design (what would make this workflow break?)
- Debugging (what conditions would produce this exact symptom?)

## Examples

| Forward Question | Inverted Question |
|-----------------|-------------------|
| How do I write reliable code? | What makes code unreliable? (no tests, shared mutable state, implicit dependencies) |
| How do I ship on time? | What causes delays? (unclear requirements, scope creep, blocked dependencies) |
| How do I keep users? | What drives users away? (slow load times, confusing UI, data loss) |

## Key Insight

Inversion works because humans are better at identifying problems than
imagining perfection. Failure modes are concrete and enumerable; success
criteria are abstract and infinite. By eliminating the concrete failures,
you converge on success through subtraction.

## Integration

- First Principles decomposes the problem; Inversion stress-tests the solution
- Pre-Mortem is Inversion applied to project planning specifically
- WIREDO's "D — Defended" dimension is Inversion thinking applied to delivery
