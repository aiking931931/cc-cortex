# Three-Layer Cognitive Architecture for System Prompts

> Research-backed prompt engineering pattern for maximizing LLM task performance.
> Based on: NAACL 2024 role-prompting studies, Chroma 2025 context-rot research,
> Lost-in-the-Middle attention analysis, and empirical testing.

## Core Insight

**Identity claims don't improve accuracy. Behavioral directives do.**

- "I am the best engineer" → 0% accuracy improvement (PromptHub 2024)
- "Think step by step" → +6-19% accuracy improvement (multiple studies)
- Specific domain expert framing → significant improvement on open-ended tasks (NAACL 2024)

## The Three Layers

### Layer 1: Identity Anchor (1 line, always-on)

**Purpose**: Define WHAT the AI does, not WHO it is. Verbs > adjectives.

```
# Bad (identity claim — no measurable effect)
I am the world's best AI, omniscient and omnipotent.

# Good (action-oriented — activates problem-solving pathways)
I solve every problem. Cross-domain integration is my core capability.
```

**Rules**:
- ≤20 words. First line of system prompt (highest attention via primacy effect)
- Use action verbs: "solve", "build", "analyze" — not "am", "know", "can"
- If cross-domain needed: explicitly state "cross-domain integration"
- If single-domain: name the domain ("I build production-grade TypeScript systems")

### Layer 2: Behavioral Constraints (3-5 lines, always-on)

**Purpose**: The actual performance drivers. These shape every token generated.

Two complementary mechanisms:

| Mechanism | When | What it does |
|-----------|------|-------------|
| **Think step by step** | DURING reasoning | Improves logical quality (+6-19% accuracy) |
| **Consequence analysis** | BEFORE action | Prevents damage (impact chain reasoning) |

```
Think step by step: decompose → reason step-by-step → verify each step → zero-error delivery.
Consequence analysis: before acting, trace the impact chain — will changing A break B? Side effects? Reversible?
```

**Rules**:
- "Think step by step" is the single most cost-effective prompt directive (proven across multiple benchmarks)
- "Zero-error delivery" creates productive tension — every output is measured against this standard
- Consequence analysis prevents the #1 cause of AI-introduced bugs: untracked side effects

### Layer 3: Dynamic Domain Expertise (context-dependent)

**Purpose**: Activate domain-specific knowledge when needed, return to integration mode after.

```
# In system prompt (meta-instruction):
Dynamic expertise: detect task domain → switch to that domain's top expert mindset → return to integrator after completion.

# Programmatic (SDK/API):
Detect domain from user message keywords → inject:
"[Dynamic Expertise] You are now a top {domain} expert — specialized in {specifics}. Return to integration perspective after completion."
```

**Why this works**: Research shows specific expert framing ("10-year Python developer") significantly outperforms vague framing ("best programmer") on open-ended tasks. Dynamic injection gives you specificity without sacrificing breadth.

**Domain detection**: Simple keyword matching with scoring. Map domains to keyword arrays, count matches, highest score wins.

```python
DOMAIN_MAP = {
    "code": {"keywords": ["bug", "api", "deploy", "test", "refactor"], "expert": "top software engineer"},
    "biology": {"keywords": ["cell", "gene", "protein", "neural"], "expert": "top biologist"},
    "business": {"keywords": ["acquisition", "valuation", "market"], "expert": "top business strategist"},
    # ... extend as needed
}
```

## Optimal Prompt Budget

| Component | Token budget | Attention zone |
|-----------|-------------|----------------|
| Layer 1 (Identity) | ≤50 tokens | START (highest attention) |
| Layer 2 (Behavioral) | ≤100 tokens | START (highest attention) |
| Layer 3 (Dynamic) | ≤80 tokens | Injected AFTER task mode instructions |
| Task-specific rules | ≤500 tokens | Middle (load on-demand only) |
| Critical reminders | ≤50 tokens | END (high attention via recency) |
| **Total system prompt** | **≤2000 tokens** | Beyond 3000 → performance degrades |

## U-Shaped Attention Optimization

LLMs attend most to the START and END of prompts. Middle gets lost.

```
[START — highest attention]
  Layer 1: Identity anchor
  Layer 2: Behavioral constraints

[MIDDLE — lowest attention]
  Task mode instructions
  Domain-specific rules (load on-demand)
  Layer 3: Dynamic domain injection

[END — high attention]
  Current time/state
  Critical constraint reminders
```

## Implementation Checklist

- [ ] Layer 1: ≤1 line, action-oriented identity at prompt start
- [ ] Layer 2: "Think step by step" + consequence analysis (always present)
- [ ] Layer 3: Domain detection → expert injection (keyword-based)
- [ ] Total system prompt ≤2000 tokens
- [ ] Most important rules at START and END, not middle
- [ ] On-demand loading for domain-specific rules (not all at once)

## References

- PromptHub 2024: Role-Prompting persona study (personas don't help factual tasks)
- NAACL 2024 (Kong et al.): Role-Play Prompting across 12 reasoning benchmarks
- Chroma 2025: Context Rot study across 18 LLMs
- Lost in the Middle (TACL 2024): U-shaped attention in long contexts
- LessWrong 2024: Prompt framing effects on performance and safety
