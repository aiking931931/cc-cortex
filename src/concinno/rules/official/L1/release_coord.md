<!-- concinno-official-rule: do-not-edit -->

# Release coordination (L1)

I register before I publish. I check before I bump. I lock before
I upload.

**switch**: publish authorization — see the Switch Index. When the
authorization layer is disabled, irreversible publish operations
auto-pass without a typed string; when enabled, the authorization
flow runs.

**Load triggers**: release / publish / version bump / PyPI upload /
npm publish / cargo publish / docker push registry / any
**irreversible** package distribution.

## Core

A distributable-package project (pip / npm / cargo / gem / docker
publish / maven publish / …) **must have a
`RELEASE_COORDINATION.md`** at its root. No file = the first task
is to create it from the template below, not to skip.

## Naming SOP (language-agnostic)

| Location | Filename | Role |
|---|---|---|
| **Project root** | `RELEASE_COORDINATION.md` | Authoritative (single source of truth) |
| Handoff area (if any) | `RELEASE_COORDINATION.md` | Pointer file — one line to the project root |

Any legacy `RELEASE.md` / `PUBLISHING.md` → merge into
`RELEASE_COORDINATION.md`, don't keep parallel copies.

## Eight mandatory sections

1. **Current snapshot** — registry latest / local target / WIP /
   irreversibility points
2. **Iron rules** — version-sync / publish-ban states (e.g.
   version already taken, tests red)
3. **Pre-upgrade checklist** — tests / CHANGELOG / version aligned
   at 3 locations / build / registry version not taken
4. **Upgrade steps** — build → check → upload → tag → verify
5. **Pending Publish Queue** — ship-ready but unpublished records,
   a file-level handoff so an irreversible publish does not block
   inside a prepare session waiting for authorization
6. **Lock mechanism** — active session + hostname + timestamp +
   target version
7. **Session registry** — Active / History sections
8. **Irreversibility points** — which operations cannot be undone
   post-publish

## Pending Publish Queue (file-level handoff)

**Principle**: an irreversible publish **must not block inside a
prepare session** waiting for the user's authorization. The
prepare session finishes everything reversible (build / test /
review / bump), writes ship-ready state into the queue, and exits.
The next session with authorization — or CI, or a human operator —
reads the queue and picks up. Coordination happens via **file**,
not via a live session.

### Record schema (v1)

```yaml
- version: "X.Y.Z"
  state: ready-to-publish | claimed | published | failed | expired
  queued_by:
    session: <id>
    host: <hostname>
    queued_at: <ISO-8601>
  artifacts:
    wheel: <path>
    sdist: <path>
    dist_check: PASSED | FAILED
    built_from: <commit hash>
  verification:
    tests_full: "<N passed>"
    lint: clean | N issues
    triple_source_aligned: true | false
    redteam_review: <summary or "SKIPPED">
  blocking_on:
    - user_authorization
    - lock_acquisition
    - <other gates>
  suggested_command: |
    # DO NOT auto-run. Next session should...
  expires_at: <ISO-8601>   # typically +7d; rebuild past this
  notes: |
    (optional context)
```

### Lifecycle

| State | Trigger | Action |
|---|---|---|
| `ready-to-publish` | Prepare session finishes build + verify | Append YAML block to queue |
| `claimed` | Publisher session takes it | Add `claimed_by` to the same block + acquire lock |
| `published` | Upload succeeds | **Move the whole block to `Session Registry::History`** + `result: ok` + registry URL |
| `failed` | Upload error / verification fail | **Move the whole block to History** + `result: failed: <reason>` + rollback action |
| `expired` | `expires_at` < now still ready | Rebuild artifacts or delete record; do not publish as-is (wheel may not match current HEAD) |

### Concurrency

- One version = one record. When the queue already has
  `2.2.0 ready-to-publish`, a new session **does not append a
  second record** — it reviews the existing block.
- Multiple versions in the queue at once is fine (`2.2.0` and
  `2.3.0` queued in parallel); publisher sessions claim each
  and acquire their own lock.

## Lock rules

- One package = one session holds release-lock at a time
  (`Active` section is the queue's execution-layer pair).
- Before acquiring lock: read `Active`. Active present = **don't
  steal** — work on a different task or check the queue for an
  unclaimed record.
- After acquiring lock: write `hostname + session id + ISO
  timestamp + target version` + mark the queue record
  `state: claimed`.
- Release lock: on success **or** failure, move to `History`
  (queue and lock settle together).
- Timeout: lock older than ~4 hours auto-considered stale (a
  forgotten release); claimed queue records past timeout revert
  to `ready-to-publish`.

## Irreversibility points (stop and ask user)

| Operation | Why irreversible |
|---|---|
| `twine upload` (PyPI) | Cannot revoke — only yank. Yanked versions are still installable with `==version`. |
| `npm publish` | Unpublishable within 72 hours only; after that permanent. |
| `cargo publish` | Permanent — can only be yanked. |
| `docker push` to a public registry | Tag overwrite works but history is preserved; sensitive content leaks are already out. |
| `git tag push` to remote | Deletable, but anyone who cloned already has it. |

Full autonomy mode does **not** exempt these five. Before running
any of them, one of the following must hold:

1. The library's publish-authorization switch is `disabled`
   (auto-pass).
2. The user has typed the authorization string for this package
   and version.
3. The user has picked the equivalent authorization option in an
   AskUser prompt.

### Check ordering (mandatory)

1. **First** query the library's auth layer's current config.
2. `disabled=True` → the library layer stops reporting blocked
   and stops asking for a string.
3. `disabled=False` → authorization flow applies.

### Library layer green is *not* free permission to run bash

Any agent harness (Claude Code, Cursor, Aider, …) has its own
permission sandbox (allow / deny / ask lists + default heuristics)
that lives **separately** from your library's authorization layer.
An opt-out at the library layer does not propagate to the harness.

Before an irreversible op, list **both** layers' current state.
When the library is green but the harness has no matching rule:
(a) ask the user to approve once in the UI, (b) add an allow rule
to the harness config, or (c) type the library's auth string.
**Do not blind-run bash** — the harness will deny, the turn is
wasted, and the report will wrongly blame the library layer.

## Authorization modes (for the library's layer)

**Separate from destruction-guard protections.** Publish
authorization (irreversible but not destructive) is its own module.
Destruction-guard covers data destruction (recursive delete, DB
drop, force-push main, git gc prune, etc.). Disabling publish auth
does **not** disable destruction protection — the two configs are
independent.

### Two modes (user-selectable)

| Mode | Auth signal | Typical setting |
|---|---|---|
| `STRING_MATCH` (**default**) | User types an exact authorization string in chat | Standard CLI / IDE flow |
| `ASKUSER_ANSWER` | User picks an answer from an AskUser prompt containing the authorization string | UI / mobile / voice flows where typing is awkward |

- The string must match **character-for-character** (case-
  insensitive, whitespace tolerant). Wrong package / wrong
  version / missing word all fail to authorize.
- `ASKUSER_ANSWER` mode also accepts the typed string
  (hybrid compatibility).
- Version prefixes do not swallow each other:
  `<auth-string> 2.12.0rc1` does not authorize `2.12.0`, and
  vice versa.

### Disable toggle

`disabled=True` → publish ops **auto-pass**. No string, no
AskUser. Suitable for:

- CI / scheduled publisher
- Power users who explicitly opt out

**Default `disabled=False`** — new users are protected by the
gate.

### Config source chain (later overrides earlier)

1. Default: `mode=STRING_MATCH`, `disabled=False`
2. User config file (`~/.concinno/release_auth.json` or
   equivalent)
3. Environment variables (e.g. `<FRAMEWORK>_RELEASE_AUTH_MODE`,
   `<FRAMEWORK>_RELEASE_AUTH_DISABLED`)

Malformed or unknown values → **fail-closed** (fall back to the
strictest `STRING_MATCH`) and record a warning in the config.

### Inspecting current state

Expose a one-line API that prints the current mode, disabled
flag, source (default / file / env), and config-file location.
Agents must run this inspection **before** any irreversible
publish op.

## Auto-create when missing

When you open a distributable-package project (look for
`pyproject.toml` / `package.json` / `Cargo.toml` / `Dockerfile`+registry),
check the root for `RELEASE_COORDINATION.md`. If absent, **create
it immediately** from the template below, filled with the current
snapshot. Don't skip, don't defer, don't ask whether to create —
the rule is that the file exists.

## Coordination file template (copy-paste ready)

```markdown
# <project> RELEASE_COORDINATION

> Every session/agent upgrading this project reads this file
> first.

## Current snapshot (<YYYY-MM-DD>)

| Field | Value |
|---|---|
| Registry latest | - |
| Local version (e.g. pyproject) | - |
| CHANGELOG latest | - |
| Main-branch HEAD | - |
| Next publish target | - |

## WIP changes

- (untracked / uncommitted / unreleased-commit highlights)

## Iron rules

- (project-specific hard rules)

## Pre-upgrade checklist

- [ ] tests all green
- [ ] lint / type check pass
- [ ] CHANGELOG has the target-version entry
- [ ] version aligned across 3 locations
- [ ] build succeeds
- [ ] registry version not yet taken
- [ ] release-lock acquired

## Upgrade steps

1. Decide target version (semver)
2. Update version + CHANGELOG
3. Build
4. Check dist
5. Upload
6. Tag + push
7. Verify install

## Pending Publish Queue

(empty)

## Session Registry

### Active
None

### History
- (per release: date / session / target version / result)

## Irreversibility points
- (list which operations this project cannot undo)
```
