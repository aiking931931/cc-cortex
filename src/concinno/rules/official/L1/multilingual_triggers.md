<!-- concinno-official-rule: do-not-edit -->

# Multilingual semantic triggers (L1)

I match on meaning, not surface string.
Keywords in rules, skills, and tool names are English — the canonical
form — but the same concept in another natural language must trigger
the same behaviour.

## Why this rule exists

- Writing every keyword in every language (en / zh-TW / zh-CN / ja /
  ko / …) bloats the rule surface and still leaks when someone uses a
  synonym the list missed.
- Hook-level regex matching on a specific string is brittle: a user
  who switches language, paraphrases, or rearranges the phrasing
  slips past the guard and the intended behaviour doesn't fire.
- The LLM already has cross-lingual semantic understanding — the
  cheapest robust matcher is *the model itself*, using this rule as
  its instruction.

## Canonical behaviour

1. All UI strings, code identifiers, feature names, skill names,
   slash-command slugs, and example descriptions are written in
   English.
2. User messages may be in any language. When a user phrase maps
   semantically to a documented English trigger, act as if the
   English trigger was given.
3. Non-English triggers do **not** need to be enumerated in rule
   files. Match by meaning.
4. Reply in the user's language (see `language_enforce` feature).
   The internal trigger matching is language-agnostic; the surface
   answer respects locale.

## Worked examples (illustrative — do not treat as exhaustive)

| English trigger | Equivalent user phrasing (any → same action) |
| --- | --- |
| `handoff`, `/handoff` | 交接 / 交棒 / 引き継ぎ / 인수인계 / hand over / pass the baton |
| `checkpoint`, `/checkpoint` | 檢查點 / 檢點 / セーブ / 체크포인트 / save progress |
| `full mode` | 全自動模式 / 完全模式 / フルモード / 풀 모드 |
| `red team`, `/redteam-cycle` | 紅隊 / 红队 / レッドチーム / 레드팀 / adversarial review |
| `benchmark` | 跑分 / 基準測試 / ベンチマーク / 벤치마크 |
| `ship`, `publish` | 上線 / 發布 / 发布 / リリース / 배포 / cut a release |
| `rollback`, `revert` | 回滾 / 回退 / ロールバック / 롤백 / undo the change |
| `precise fix` | 精準修復 / 單題修復 / 一問ずつ修正 / 한 문제씩 수정 |

The LLM applies the same principle to triggers not listed — e.g. a
feature called `image_upscale_4x` with description "4× LANCZOS upscale
for small images" is triggered when the user says "放大圖片" / "小圖
放大" / "画像拡大" / "이미지 확대" etc.

## What this rule does NOT do

- **Does not replace hard-gate string matching** where the operator
  intentionally picked a fixed string as the authorisation token
  (e.g. `release_authorization` demands the *exact* literal
  `go publish <pkg> <version>` — a translation is **not** equivalent
  because it is the very act of typing that literal string that
  constitutes authorisation; semantic paraphrase is by design
  inadequate).
- Does not change how `language_enforce` works — that controls
  **output** language, not trigger recognition.
- Does not authorise the LLM to invent triggers that aren't
  documented somewhere in rules / skills / feature descriptions.

## Guidance to hook authors

- Prefer **syntactic** / **structural** detectors (regex on shell
  tokens like `rm -rf`, file paths, argument shapes) over natural-
  language keyword grep. Syntactic detectors do not depend on the
  user's language.
- When semantic detection is unavoidable, route through a tiny
  Haiku-as-judge call with a clear English prompt instead of a
  regex keyword list — the judge handles paraphrase and language
  switching natively and is cheap enough to live on a hot path.
- Only fall back to English keyword regex when (a) the cost model
  forbids a judge call and (b) the keyword is a stable protocol
  token (CLI flag, API name, file extension) rather than a natural-
  language concept.

## Self-application

This rule is itself mostly English with a short multilingual
illustration. Do not add more language columns to the table above —
that defeats the purpose. If a new canonical concept lands in the
codebase, add one English row and trust the semantic match.
