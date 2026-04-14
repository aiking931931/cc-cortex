---
name: kb_testing
description: Testing knowledge base — pytest, Jest, Playwright, test architecture, coverage strategies. Triggers on "測試知識", "testing KB", "pytest tips", "Jest patterns", "Playwright guide", "test architecture".
user-invocable: false
---

# 測試知識庫

> I write tests that catch real bugs, not tests that pass by coincidence. A green suite that misses regressions is worse than no suite at all.

> **You MUST** test behavior through public interfaces, not internal implementation.
> **You MUST** keep tests independent — no shared mutable state, no ordering dependency.
> **You MUST** name tests descriptively: `test_<scenario>_<expected>`.

## 鐵律

1. **Real DB for integration** — Mocking DB hides real bugs (migration, constraint, encoding)
2. **One assertion focus** — Multiple assertions OK if testing one behavior
3. **Fast feedback** — Unit <100ms, Integration <5s, E2E <30s per test
4. **Flaky = P1 bug** — Fix or delete, never skip indefinitely

## 決策樹

```
Framework?
  ├─ Python → pytest + pytest-asyncio + factory_boy
  ├─ TypeScript → Jest (unit) + Playwright (E2E)
  ├─ Go → testing + testify + httptest
  └─ Rust → cargo test + proptest (property-based)

Coverage target?
  ├─ Critical paths (auth, payment, data) → 90%+
  ├─ Business logic → 80%+
  ├─ UI components → 60%+ (E2E covers rest)
  └─ Generated/config code → Skip
```

## 按需讀取

| 要做什麼 | 讀哪個檔案 |
| --- | --- |
| 測試策略選擇 | `/testing-patterns` skill |
| TDD 紅綠循環 | `/tdd` skill |
| E2E 視覺驗證 | `/qa` skill |
