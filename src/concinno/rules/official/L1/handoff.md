<!-- concinno-official-rule: do-not-edit -->

# Handoff hygiene (L1)

I write handoffs so the next session — a future me, a teammate, or a
fresh agent — can pick up cold. A handoff file is a *router*, not a
database. Everything still-open belongs here; everything done is a
pointer to history.

**switch**: `handoff_required_guard` — see the Switch Index. When
the guard is off, handoffs are advisory; hardening features in the
last section only bite when the guard is on.

## Three-tier architecture

| Tier | Location | Content | Budget | When read |
|---|---|---|---|---|
| **Index** | `<project>/handoff.md` (or equivalent) | Status + open items + `next_step` + pointers | ≤200 lines | Every session start |
| **Summary** | Adjacent to Index | Recent-session details + decision rationale | ≤300 lines | When context is needed |
| **Archive** | Adjacent or versioned | Complete history | Unbounded | Archaeology only |

**Demotion rule**: completed items older than ~14 days drop out of Index
(Summary keeps a one-line trace). Open / paused items stay in Index
indefinitely.

**Sedimentation priority**: architectural decisions belong in a
planning doc or knowledge base. The Index only keeps a one-line
pointer — it is not the decision record.

## Write-then-clean (anti-entropy, ordering matters)

Before modifying any long-running file, four steps in this order:

1. Read the whole file.
2. Mark removable items (expired, duplicated, demoted).
3. Delete or compress.
4. *Then* write the new content.

Net line count after write must not exceed the budget. The rule is
proportional: if you add 10 lines, cut ~5 equivalent. Long-term the
file stays flat or shrinks.

When unsure whether to delete, compress to a one-line pointer — not a
preserved paragraph.

## Read-handoff (anti-desync four-step)

The top-of-file `next_step` is the *most recent focus*, not the *whole
picture*. On session start:

1. **Scan the whole file.** At minimum the "unresolved", "history",
   and "pointers" sections. A planning-doc path mentioned twice or
   more is a signal to open that doc.
2. **Inventory legacy assets.** For each paused / deferred / "dropped"
   item, ask "does the downgrade reason still hold?" If you cannot
   answer, re-evaluate — do not treat the deferral as permanent.
3. **Align key numbers at the source.** Before quoting a
   headline number (SOTA, baseline, %win, pass-rate), confirm it
   appears identically in at least two files of the same project.
   Mismatches are never "quoted with a caveat" — they are "not
   citable until reconciled".
4. **Cross-check.** After reading the Index, scan related feedback
   notes or memory for contradictions.

**User signal triggers a full re-read**: phrases that imply the user
remembers something you missed ("didn't we already …", "wasn't there
…", "can't we use …") — re-read the whole Index plus adjacent
memory. Do not push back before the re-read.

## Seven-section Index format

1. **Status overview** — counts of done / paused / open + current focus
2. **Iron rules / constraints** — project-specific hard rules
3. **Unresolved** — what is stuck, why, how far it got; write
   "none" when truly none
4. **`next_step`** — concrete enough to execute
5. **Recent sessions** — last ~3 full entries
6. **History** — 4th-and-older compressed to one line each
7. **Pointers** — planning docs, knowledge-base files, external
   references

## What never deletes

- Open / paused items
- The "unresolved" section
- Any paragraph referenced by another live section
- Permanent-milestone markers (whatever symbol your project picks)

## Hardening (when the guard is on)

- **Line-budget gate** — a write that exceeds the Index budget is
  rejected; an explicit overflow marker lets the current write
  through for one turn.
- **Emergency handoff** — when the process is about to die with
  several files changed and no handoff written, a ≤20-line stub is
  auto-emitted so the next session is not blind.
- **Idle warning** — long stretches of file edits without a handoff
  write surface as a stderr notice.

Implementation-specific names of these guards live in the host
project; the contracts above are what matters.

## Other

- Paths are always full (repo root or absolute).
- Cross-project work: move the handoff entry to the correct
  project's file and leave a one-line pointer behind.
- Lint tolerance: zero. The handoff is a working document but
  renders as docs — treat it that way.
