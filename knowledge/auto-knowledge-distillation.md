# Auto Knowledge Distillation

Every correction is a lesson. Three corrections on the same pattern become
a rule. Seven become a hard enforcement. Knowledge flows upward automatically.

## The Distillation Pipeline

### Stage 1: Capture

When something goes wrong (or surprisingly right), record it:

- **Corrections**: What was wrong? What was the fix? What domain?
- **Success patterns**: What approach worked? Was it non-obvious?
- **Tool**: Append to structured log (JSONL, not free-text)

### Stage 2: Distill (Pattern Recognition)

Periodically review captured data for recurring patterns:

- **Count ≥ 3**: Same domain + same pattern → candidate for knowledge entry
- **Count ≥ 5**: Promote to explicit rule or guideline
- **Count ≥ 7**: Consider automation (hook, guard, compiler enforcement)

### Stage 3: Verify (Does the knowledge stick?)

After promoting a pattern to a rule:

- **Track compliance**: Does the same error still occur?
- **60 days without recurrence**: Mark as "mastered"
- **Recurrence after promotion**: Mark as "relapse" → escalate enforcement
- **False positive rate**: If the rule blocks correct behavior, relax it

### Stage 4: Graduate (Delete the sticky note)

When a behavior is fully automated (hook enforcement, type system, compiler):

- **Delete the corresponding rule** — it's now redundant
- **Return the attention budget** — cognitive load is finite
- **Document the graduation** — so future maintainers know why the rule
  is gone (it's enforced mechanically, not forgotten)

## Skill Progression (Fitts & Posner)

```
Cognitive (know how)    → Knowledge entry
Associative (do with reminders) → Rule / guideline
Autonomous (do without thinking) → Hook / type system / compiler
```

Each stage has a different enforcement mechanism. Using the wrong mechanism
for the stage wastes resources (heavy enforcement for already-mastered
behaviors) or fails (light reminders for unlearned behaviors).

## Cross-Project Transfer

Some lessons are domain-specific, others are universal:

- **Domain-specific**: Only relevant to one project. Stay local
- **Universal**: Relevant across projects. Tag with `#cross-project`
  and propagate to shared knowledge base
- **Test transfer**: If a lesson from Project A prevents errors in
  Project B for 30 days → transfer confirmed

## Anti-Patterns

- **Capture without distillation**: Collecting data nobody reads
- **Rules without verification**: Adding rules without checking if they work
- **Never graduating**: Keeping rules for mastered behaviors (attention waste)
- **Over-generalizing**: Turning a project-specific fix into a universal rule
