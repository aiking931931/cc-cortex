# Reference rules — graduated to `official/` in 2.29.0

**Status (2.29.0, 2026-04-24)**: the eight files that lived here in
2.28.1 have completed the red / blue / commander CBUA pass described
in `RELEASE_COORDINATION.md` and are now published as
`rules/official/L1/<name>.md`. The `reference/` directory is retained
only as a pointer — a forwarding address for anyone following links
from older versions.

Each officially-shipped file is the **methodology-only** distillation.
The author-specific material that was tangled into each file in 2.28.1
— `MEMORY` index references, `_AI_BRAIN/` pointers, private skill
names (`/handoff`, `/evolve`, `/tidy`, `/kb_*`), `~/.concinno/`
snapshot values, benchmark numbers tied to one operator's sessions —
has been removed, not moved to a separate template. The rationale is
that template variables for that content would be a puzzle the user
has to re-derive from scratch, not a reusable template.

If you previously copied a file from `reference/` and want the new
clean version, install it via:

```bash
concinno rules install
```

which drops `~/.claude/rules/official/L1/*.md` onto your machine
without touching your `private/` tree or the canonical rule files at
`~/.claude/rules/00-L0.md` / `~/.claude/rules/L1/*.md`.

## Graduated files

| Old path (2.28.1) | New path (2.29.0) |
| --- | --- |
| `reference/autonomous.md` | `official/L1/autonomous.md` |
| `reference/cbua.md` | `official/L1/cbua.md` |
| `reference/handoff.md` | `official/L1/handoff.md` |
| `reference/rag_sop.md` | `official/L1/rag_sop.md` |
| `reference/redteam.md` | `official/L1/redteam.md` |
| `reference/release_coord.md` | `official/L1/release_coord.md` |
| `reference/switches.md` | `official/L1/switches.md` |
| `reference/task_execution.md` | `official/L1/task_execution.md` |

## Why the clean versions are shorter

The graduated files are shorter than their 2.28.1 counterparts
because the author-specific material was removed, not because
methodology was lost. A representative before/after:

- `autonomous.md`: 122 → ~150 lines (methodology slightly expanded,
  author diary entries and dated directives removed)
- `cbua.md`: 104 → ~190 lines (six laws + 22-stage pipeline kept,
  `ZIQ` / `Sancio` / `Cigito` project names stripped)
- `redteam.md`: 161 → ~230 lines (red + blue prompt templates
  kept verbatim as they are reusable, `claude-opus-4-7[1m]` model
  pin removed, `session 648cae48` evidence and `CC L1/L6`
  ceiling references removed)

The full red / blue / commander verdict per file lives in
`CHANGELOG.md` under the 2.29.0 entry.
