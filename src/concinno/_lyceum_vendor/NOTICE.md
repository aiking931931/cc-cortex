# `concinno._lyceum_vendor` — vendored upstream attribution

This subpackage contains a frozen copy of the Lyceum substrate modules
that Concinno's governance shims (`concinno.destruction_guard`,
`concinno.approval_mode`, `concinno.security.ssrf_guard`) delegate to.
It was vendored in Concinno 5.2.0 because the upstream import name
`lyceum` is squatted on PyPI by an unrelated educational package.

## Upstream

- **Project**: NousResearch/hermes-agent (the "Lyceum agent v0.1.0"
  internal codename for AI King's fork at the time of vendoring)
- **License**: MIT
- **Pinned commit**: `75e1339d4cdb32652e560eccc3930cc9264ac67b`
- **Authoritative attribution**: see `aiking/THIRD_PARTY_NOTICES.md`
  in the AI King monorepo for the verbatim upstream MIT license text
  and the canonical attribution chain.

## Files

All `.py` files in this subpackage carry an SPDX header:

```
# SPDX-FileCopyrightText: 2025 Nous Research
# SPDX-License-Identifier: MIT
```

This NOTICE exists so downstream auditors scanning this subdirectory
in isolation (e.g. via SBOM tooling) find the attribution without
having to climb back to the monorepo root.

## Cross-references

- `aiking/THIRD_PARTY_NOTICES.md` — verbatim upstream MIT text,
  authoritative.
- `projects/concinno-king/LICENSE-MIT-Hermes` — same upstream license
  text in the sibling fork project.
- `projects/concinno-king/NOTICE.md` — AI King contributions on top
  of the same upstream.
