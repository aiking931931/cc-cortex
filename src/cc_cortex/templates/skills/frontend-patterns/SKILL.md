---
name: frontend-patterns
description: Frontend/React/Next.js patterns and anti-patterns. Triggers on "frontend", "前端", "React pattern", "component design", "state management", "rendering".
user-invocable: true
disable-model-invocation: true
---

# /frontend-patterns — Frontend Architecture Patterns

I build UIs that are fast by default, accessible by design, and maintainable by convention.

> **You MUST** verify with visual output (screenshot or browser), not just TypeScript compilation.
> **You MUST** test mobile (≤768px) AND desktop (≥1024px).
> **You MUST** keep components under 200 lines — extract or compose.

## Decision Tree

```
State scope?
  ├─ Component-local → useState/useReducer
  ├─ Subtree shared → Context (if ≤3 consumers)
  ├─ App-wide → Zustand/Jotai (client) or Server Components (RSC)
  └─ Server → React Query / SWR with cache

Rendering?
  ├─ Static content → SSG (generateStaticParams)
  ├─ Per-request data → SSR (Server Component)
  ├─ Interactive → Client Component (use client)
  └─ Heavy list → Virtualization (react-window/tanstack-virtual)
```

## Anti-Patterns

| Anti-pattern | Fix |
|-------------|-----|
| Prop drilling >3 levels | Context or composition |
| useEffect for derived state | useMemo or compute inline |
| State for URL params | useSearchParams |
| Giant monolith component | Extract by responsibility |
| CSS-in-JS runtime in RSC | CSS Modules or Tailwind |
| Fetching in useEffect | Server Component or React Query |
