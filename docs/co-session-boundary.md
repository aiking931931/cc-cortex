# Co-Session Boundary — GAIA × Sancio parallel work on Concinno

> Two concurrent Claude Code sessions are both editing `projects/concinno/` +
> `projects/persona-api/` in the 2026-04-23 cycle. This file is the
> **mechanical** boundary to prevent merge conflicts. Read before touching
> any file listed below.

## Who owns what

### GAIA session (benchmark / agent strength)

Owns — edits freely, others must not touch:

- `src/concinno/agent/*.py` — `format_guard`, `commander`, `ziq_anchor_router`,
  `confidence_fusion`, MAS (solver/critic/judge) loop, asymmetry probe,
  `AGENT_GUIDANCE_*` strings, `extract_sentinel_answer`.
- `src/concinno/runner/gaia_*.py` — benchmark scoring, ZIQ per-task anchor.
- `tests/test_agent_*.py`, `tests/test_runner_*.py` — agent / runner test
  files.
- Benchmark result dirs, GAIA question-level experiments, retry-layer
  ablations.
- RunPod Pod GPU deployments for GAIA rounds.

Typical GAIA commits — `feat(agent): …`, `feat(runner): …`,
`experiments(gaia_*): …`.

### Sancio session (runtime / Concinno upgrade / pip trajectory)

Owns — edits freely, GAIA must not touch:

- `src/concinno/cli/*.py` — all `*_cmd.py` subcommands, `main.py`.
- `src/concinno/core/*.py` — `config`, `notify`, `credentials`,
  `feature_config`.
- `src/concinno/hooks/*.py` — `ask_user_toast`, `on_stop`, `on_pre_tool`,
  `on_post_tool`, `on_pre_compact`, `on_post_compact`.
- `src/concinno/tools/builtin/*.py` except `fetch_image.py` — those reference
  tools land in the shared zone because both sides touched them this cycle.
- `projects/concinno-skills-*/` — all 20 sub-packages.
- `projects/persona-api/` — **whole repo is Sancio territory**. Sancio =
  persona-api per MEMORY #18. pyproject `concinno>=` dep bump, tools
  adapters, providers, routes, agent_api, MCP wiring.
- `~/.claude/skills/credentials/`, `~/.claude/skills/new-feature/`,
  `~/.claude/skills/kb_*`, `~/.claude/skills/sancio-*`.
- `~/.claude/rules/00-L0.md`, `~/.claude/rules/L1/*.md` (project + public
  copies) — governance.
- `~/.claude/rules/switches.md` — switch index.
- RELEASE_COORDINATION.md + CHANGELOG stewardship at the release cadence.

Typical Sancio commits — `release(X.Y.Z): …`, `deps: …`, `feat(cli): …`,
`feat(core): …`, `docs(release): …`.

## Shared zone (both touch — coordinate before edit)

These files are **legitimate shared state** — both sessions updated them
this cycle. Rule: if you changed one, leave a one-line note in the commit
message referencing the other session's last touch so the merge reviewer
can reconcile.

- `pyproject.toml` — `[project].version` plus any new `[project.optional-dependencies]`
  extras added by either side.
- `src/concinno/__init__.py` — `__version__`.
- `CHANGELOG.md` — both sides append to `## [X.Y.Z]` heading. Append-only
  rule: never rewrite another session's bullet.
- `RELEASE_COORDINATION.md` — snapshot + Pending Queue. Both sides may
  add a Queue record; do NOT delete or move another session's record.
- `src/concinno/tools/builtin/fetch_image.py` — added by GAIA session for
  Concinno 2.18.0 multimodal roadmap; Sancio picks up as consumer via
  `persona-api` adapter. Modifications to this file must be declared in
  the commit so both sides know.
- `src/concinno/tools/builtin/read_attachment.py` — same shared treatment.

## Conflict SOP

1. **Before starting a chunk of work**: run `git status` + `git log --oneline
   HEAD~5..HEAD` in both `projects/concinno/` and `projects/persona-api/` to
   see what the other session did recently.
2. **Before editing a shared-zone file**: re-read the file top to bottom so
   your edit is aware of the other session's changes.
3. **On commit**: if your diff touches any shared-zone file, mention the
   other session's most recent shared-file commit hash in the message so
   merge reviewers can trace back.
4. **On release**: only **Sancio session** performs `python -m build` +
   `twine upload` + `git tag vX.Y.Z`. GAIA session commits release-candidate
   code but does not run the release path — that's Sancio's stewardship
   role per the 2026-04-23 user directive.

## This-cycle reconciliation record (2026-04-23)

Both sessions shipped interleaved without conflict:

| Order | Session | Commit | Scope | PyPI |
|---|---|---|---|---|
| 1 | GAIA | `f84ef76 feat(agent): format_guard (2.17.0)` | `agent/format_guard.py` + retry-layer tests | — (pre-ship) |
| 2 | Sancio | `5ad3bdb feat(cli): __main__.py + docs` | `__main__.py` + ecosystem / pod-merge docs | — |
| 3 | Sancio | `87b1995 release(2.17.0): new-feature CLI + skill` | `cli/new_feature_cmd.py` + test + skill 3 files | 2.17.0 |
| 4 | Sancio | `6451078 release(2.17.1): hook subprocess cc_config.json fallback` | `core/config.py` fallback fix | 2.17.1 |
| 5 | GAIA | `684150a feat: 2.18.0 — paraphrase_risk + fetch_image` | `agent/format_guard.py` extension, `tools/builtin/fetch_image.py`, roadmap | 2.18.0 |
| 6 | Sancio | `persona-api 1d2dea4 deps: bump concinno>=2.18.0 + fetch_image integration` | `persona-api/pyproject.toml` + adapters + openai multimodal split | — (Sancio-side) |

Ship trajectory: 2.16.0 → 2.16.1 (skipped, superseded) → 2.17.0 → 2.17.1 →
2.18.0 — no version collision, no file double-edit, no force-push.

## Next-cycle scope (2.19+)

Per GAIA session's own roadmap note + 2026-04-23 user boundary directive:

- **Concinno 2.19+** (either session may ship, Sancio-preferred): `fetch_audio` /
  `fetch_video` builtin tools, `ModelCapabilities` helper table,
  `parse_media_markers` extension (supersedes `parse_image_marker` as the
  media-agnostic dispatcher).
- **Sancio runtime** (Sancio session only — requires CC-ceiling bypass):
  `ollama_native` provider (direct local Ollama, skip OpenAI-compat
  translation layer), vision-aware auto-fallback router, `/api/show`
  capability probe cache.
- **GAIA session** continues per-task debugging of solo FAIL pattern,
  starting with #15 (empty retries — agent not calling `fetch_image`
  despite infra ship — needs prompt / tool-use discovery debug).

## Cross-reference

- MEMORY #18 — Sancio = persona-api, not `projects/munio/`
- MEMORY #67 — outer-inner repo race (`inner concinno` is source of truth)
- `projects/concinno/CLAUDE.md` §Boundary — Concinno vs Sancio — capability
  routing rule ("Can CC do it today?")
- `projects/concinno/RELEASE_COORDINATION.md` — release SOP (only Sancio
  session performs release path per this boundary)
- `projects/concinno/docs/pod-merge-2.16.0.md` — historical myth, kept as
  lesson (pod-merge was a mythical remote-push scenario; actual GAIA work
  was always on the local feat branch, reconfirmed this cycle)
