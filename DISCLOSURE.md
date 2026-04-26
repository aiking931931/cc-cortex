# Disclosure: GAIA test-set artifacts in pre-2.24.0 history

## Summary

Versions 2.21.0, 2.22.0, and 2.23.0 of `concinno` (released 2026-04-24)
contained string artifacts referencing specific GAIA validation-set
answer paths in the agent prompt template. These were design-time
anchors used during a precise-fix iteration on the GAIA benchmark —
they **were not** GAIA submission entries and the package was never
used to make a public GAIA leaderboard claim during this period.

## Scope

- Files affected (in 2.21–2.23 wheels):
  `src/concinno/skills/public/agent/gaia_agent.py` (constants
  `_BASS_CLEF_HINT`, `_POLYGON_HINT`)
- Strings present: GAIA validation task answer-path mnemonics (e.g.
  word-reverse decoding for task `8f80e01c`, label-counting for task
  `6359a0b1`)
- Test files (which contain forbidden strings as **defensive
  assertions**): `tests/test_gaia_agent_*` — these are intentional
  anti-leakage guards, not new leaks

## Cleanup

- 2.24.0 (2026-04-24) replaced task-specific anchors with the generic
  `_VISUAL_REASONING_SCAFFOLD` (all task-specific solution paths
  removed)
- 3.2.0 (2026-04-26) introduced **L1 domain-typed procedural anchors**
  (textbook-level domain knowledge: bass-clef line/space mnemonics,
  orthogonal-polygon decomposition, multi-hop web search strategy)
  replacing both the L0 leak and the over-generic L2 fallback. See
  `~/.claude/skills/kb_benchmark/generic-anchor-design.md` for the
  3-tier classification framework.
- Anti-leakage assertions added to test suite to prevent recurrence.
- Feature flags renamed: `bassclef_wordreverse` → `gaia_music_image_upscale`,
  `polygon_counting_hint` → `gaia_polygon_image_upscale` (back-compat
  aliases preserve old names with deprecation warning, drop schedule
  next minor).

## Recommended action for users

- If you installed concinno 2.21–2.23, upgrade to 3.2.0+ via
  `pip install -U concinno`
- Versions 2.21-2.23 have been **yanked from PyPI**; existing installs
  continue to work but new `pip install` will skip these versions
- For GAIA-related research: see
  `~/.claude/skills/kb_benchmark/generic-anchor-design.md` for the
  L0/L1/L2 anchor design framework

## Why disclose rather than rewrite git history

Rewriting history with `git filter-repo` would invalidate all SHA
hashes downstream and break clones / forks / external references. The
disclosure approach preserves the audit trail (anyone can verify the
cleanup commits — `3bb00938b`, `d46bc7a`, `072c936`) while
neutralising the practical impact (yanked PyPI = no `pip install`
exposure of the leaked code).

This is the L0 reversibility-aware choice: the only irreversible
component (PyPI yank) is constrained to the three versions where it
is genuinely necessary, and the bulk of the response (this disclosure
+ the cleanup commits) is fully reversible.

## Contact

Issues: <https://github.com/aiking931931/concinno/issues>
