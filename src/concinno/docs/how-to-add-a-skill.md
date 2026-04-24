# How to add a skill

A **skill** is a markdown SOP an LLM agent loads at session start —
knowledge base, slash-command handler, or domain playbook. Concinno
discovers skills automatically; you only need to write the file.

## 30-second version

1. Pick a name (snake_case, e.g. `kb_gpu_quirks` or `commit_guard`).
2. Create the directory: `~/.claude/skills/user/<name>/`
3. Drop a `SKILL.md` inside with the template below.
4. The GUI (`concinno gui`) refreshes within 3 seconds. The new
   skill appears on the Skills tab.

That's it. No restart, no registration.

## The template

```markdown
---
name: my_skill
description: One line the user will see in the GUI and /help
triggers: [keyword1, keyword2, alias]
user-invocable: true
---

# my_skill

I <verb> when <condition>.

## Why this skill exists

Explain the problem this skill solves. Ground the reader in a
concrete scenario; don't write generalities.

## When to use

- Trigger A (most common)
- Trigger B
- Trigger C

## When NOT to use

- Out-of-scope case 1
- Out-of-scope case 2

## Core content / SOP / steps

Free-form markdown. Put the actual knowledge here — whatever shape
the skill needs (checklist, table, decision tree, example code).
```

## Required frontmatter fields

| Field | Type | Required | Meaning |
|---|---|---|---|
| `name` | string | yes | Must equal the directory name |
| `description` | string | yes | One line — shows in GUI + `/help` |
| `triggers` | list of strings | no | Keywords that bring the skill to the LLM's attention |
| `user-invocable` | bool | no (default `true`) | `true` = user types `/<name>` to invoke; `false` = skill loads only when LLM judges it relevant |

Any additional YAML keys are preserved but ignored today — the schema
may expand in future versions, never shrink.

## Scope (where to put the file)

| Scope | Path | Shared with? | When to use |
|---|---|---|---|
| `user` | `~/.claude/skills/user/<name>/` | Just you | Personal tools |
| `project` | `./.claude/skills/<name>/` | Your team via git | Repo-specific SOPs |
| `private` | `~/.claude/skills/private/<name>/` | Just you, never shipped | Secrets / personal shortcuts |
| `official` | bundled with `pip install concinno` | Everyone | Only Concinno maintainers write these |

The GUI shows all scopes in one list with a scope badge per card.

## Enable / disable after creation

Skills are enabled by default. To toggle:

```bash
concinno skills disable my_skill
concinno skills enable my_skill
```

State persists in `~/.concinno/skills.json`. The GUI's Skills tab
has a toggle per card that writes the same file.

## Auto-refresh (2.30.0+)

When the GUI is running and you add, edit or remove a skill, the
Skills tab refreshes automatically within 3 seconds. Behind the
scenes `/api/features/digest` hashes every discoverable `SKILL.md`
mtime; when the hash changes, the client re-fetches the tab.

Paths inside `.git/`, `node_modules/`, `__pycache__/`, `.venv/`
and `venv/` are excluded from the hash so submodule churn does not
cause spurious refreshes.

## Delete cleanly

```bash
rm -r ~/.claude/skills/user/<name>
```

Remove the card from the GUI within 3 seconds. The entry in
`~/.concinno/skills.json` becomes orphan but harmless — it is
ignored when no matching directory exists.

## Minimum viable example

```markdown
---
name: hydrate_reminder
description: Nudges me to drink water every 45 minutes
triggers: [hydrate, water, break]
user-invocable: true
---

# hydrate_reminder

I remind you to drink water when 45 minutes have elapsed without
a break.

## Core

Every checkpoint / long task boundary, emit one line: "💧 Drink
some water." Match the user's session language.
```

Five lines of frontmatter + two short paragraphs + one heading.
Copy, save, done.

## Writing good skills — the short version

- Lead with a first-person belief statement: "I do X when Y."
  Agents pattern-match on this anchor.
- Put the most useful content in the first ~20 lines. Later
  sections are reference; early sections are operational.
- Use English for identifiers (`name`, trigger keywords, section
  headings) so multilingual trigger matching works (see
  `rules/official/L1/multilingual_triggers.md`).
- Test by loading the GUI and checking the skill appears with a
  readable description. If the description is vague in the card,
  it's vague in the agent's context too.

## Coming in 2.30.1

- `concinno skills new <name>` interactive scaffolder — prompts
  for all required fields, writes the file, opens the GUI.
- `--no-interactive` mode with flags for agent automation.
- Template variants (`--body-template {minimal,standard,kb}`).

Until then, the template above is the authoritative shape.
