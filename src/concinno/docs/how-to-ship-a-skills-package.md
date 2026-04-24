# How to ship a `concinno-skills-*` package (2.31.0+)

This guide is for third-party developers who want to extend Concinno's
feature switches, skill catalogue, or agent tool registry through an
installable PyPI package. After `pip install concinno-skills-your-thing`,
users' `concinno gui` will show your features and skills automatically,
with no post-install CLI step.

## Minimum `pyproject.toml`

```toml
[project]
name = "concinno-skills-your-thing"
version = "0.1.0"
description = "Your-thing integration for Concinno agents"
requires-python = ">=3.10"
dependencies = [
    "concinno >= 2.31.0",   # entry-points groups for features + skills
]

[project.entry-points."concinno.tools"]
your_thing_search = "concinno_skills_your_thing.tools:YourThingSearch"

[project.entry-points."concinno.features"]
your_thing = "concinno_skills_your_thing.features:FEATURE_META"

[project.entry-points."concinno.skills"]
your_thing = "concinno_skills_your_thing.skills:SKILLS_DIR"
```

All three entry-points groups are optional — ship only the surfaces you
need. A package that contributes only tools declares only
`concinno.tools`; a pure policy layer may declare only
`concinno.features`; a documentation/skill bundle may declare only
`concinno.skills`.

## Feature meta schema

Each value under `concinno.features` resolves to a `dict[str, dict]`
keyed by feature name. The inner dict mirrors the shape of Concinno's
shipped `FEATURE_META` entries:

```python
# concinno_skills_your_thing/features.py
FEATURE_META: dict[str, dict] = {
    "your_thing_rate_limit": {
        "category": "plugin_gate",          # required
        "description": "One-line English description (OSS audience).",  # required
        "description_zh": "可選：繁體中文說明",                # optional
        "enabled": True,                   # required (default)
        "schema_version": 1,               # recommended (forward-compat)
        "ziq_autotunable": False,          # optional (default False)
        "cosmetic": False,                 # optional (default False)
        "params": {                        # optional
            "max_per_minute": {
                "type": "int",
                "default": 60,
                "min": 1,
                "max": 3600,
            },
        },
    },
}
```

Concinno's validator (`concinno.plugins.features._validate_feature_meta`)
enforces:

- `category`, `description`, and `enabled` are **required** — missing
  any means the feature is dropped with a GUI-visible error.
- `schema_version` is **accept-unknown**:
  - Missing → treated as v1 with a stderr warning.
  - `> 1` (future Concinno schema) → accepted with a warning; unknown
    fields are preserved but may not render in older GUIs.
  - `< 0` or non-int → hard reject with a visible error.
- Everything else is preserved verbatim so GUI features land untouched.

## Packaging SKILL.md

Bundle a directory of `SKILL.md` files. One skill per immediate
subdirectory:

```
src/concinno_skills_your_thing/skills/
    your_thing_bulk_rename/
        SKILL.md
    your_thing_triage/
        SKILL.md
```

Point the entry-point at that directory:

```python
# concinno_skills_your_thing/skills/__init__.py
from importlib.resources import files

SKILLS_DIR = str(files("concinno_skills_your_thing") / "skills")
```

SKILL.md frontmatter shape (parsed by `concinno.skill_parser`):

```markdown
---
name: your_thing_triage
description: One-line English description
triggers: [triage, inbox, your-thing]
user-invocable: true
---

# your_thing_triage

Body paragraph for the skill...
```

Concinno's hardened parser tolerates:

- UTF-8 BOM, CRLF line endings
- Inline list (`[a, b]`) or block list (`- a\n- b`) for `triggers`
- Truthy tokens (`true` / `yes` / `on`) for `user-invocable`
- Missing closing `---` (recovers whatever parsed up to EOF)
- Special characters in values (quotes, colons, emoji)

## Testing your plugin locally

Before publishing:

```bash
pip install -e .
pip install concinno>=2.31.0
python -c "import concinno; print(concinno.__version__)"

# Verify entry-points registered
python -c "
from concinno.plugins.features import discover_feature_entrypoints
for m in discover_feature_entrypoints():
    print(m.package, m.entry_point_name, 'valid:', m.valid, 'errors:', m.errors)
"

python -c "
from concinno.plugins.skills import iter_plugin_skill_roots
print(list(iter_plugin_skill_roots()))
"

# Inspect in GUI
concinno gui &
# browser -> http://localhost:7321
# Features tab should show your cards with `source: plugin:...` badge.
# Skills tab should show your SKILL.md files with scope `plugin:...`.
```

## Publishing

Standard PyPI flow:

```bash
python -m build
python -m twine check dist/*
python -m twine upload dist/*
```

Adopt the `concinno-skills-*` naming convention so users discover your
package via PyPI search for that prefix.

## Versioning and schema compatibility (Semver commitment)

Concinno guarantees:

- Breaking changes to the feature meta schema (removing required
  fields, renaming existing fields, changing their types) will bump
  Concinno's **major** version. Your plugin pinned to `concinno>=2.x.y`
  keeps working.
- Forward-compatible additions (new optional fields, new
  `schema_version` values) can appear within **minor** versions. Use
  `schema_version` to declare what you rely on; Concinno will accept
  higher values with warnings and strip unknown fields from rendering.

To prepare your plugin for a Concinno major-bump:

1. Read the Concinno `CHANGELOG.md` for migration notes.
2. Bump your plugin's required Concinno version range.
3. Ship a new plugin version that declares the new `schema_version`.

## Security model (Important)

`pip install concinno-skills-foo` **runs the plugin's entry-points
module** at discovery time — same trust boundary as pytest plugins
(`pytest11`), flask extensions, mkdocs plugins, llama-index tools,
etc. Concinno does not add an additional signing or sandboxing layer.

Users concerned about supply-chain risk have two explicit escape
hatches built into 2.31.0+:

- `CONCINNO_PLUGINS_ENABLED=0` — disables all plugin discovery
  system-wide. Good for CI, forensics, or full lockdown.
- `CONCINNO_PLUGINS_ALLOWLIST=pkg-a,pkg-b` — restricts which installed
  packages actually get loaded. Good for gradual adoption or
  enterprise deploys where only vetted plugins are permitted.

As a plugin author, you can help users by:

- Keeping your entry-points module's top-level imports minimal (no I/O
  at import time).
- Documenting exactly what your plugin runs, and when.
- Shipping an `__init__.py` free of side effects (any network, disk,
  or subprocess work happens inside callable entry points, not at
  module load).

## Prior-art comparison

Concinno's entry-points layer borrows from well-trodden Python
ecosystem patterns:

| Project | Group name | Discovery mechanism | Notes |
|---|---|---|---|
| pytest | `pytest11` | `entry_points` | Loads plugin modules at pytest startup. |
| mkdocs | `mkdocs.plugins` | `entry_points` | Each EP resolves to a plugin class; mkdocs instantiates it per build. |
| flask | `flask.commands` | `entry_points` | CLI extension points. |
| llama-index | `llama_index.tools` | `entry_points` | Same `dict[str, Tool]` shape as Concinno's `concinno.tools`. |
| setuptools | `console_scripts` | `entry_points` | The canonical example — every CLI you've installed uses this. |
| Concinno 2.31.0 | `concinno.features`, `concinno.skills`, `concinno.tools`, `concinno.guards`, `concinno.preset_consumers` | `entry_points` | Five groups, each mapped to a specific Concinno consumer. |

The key design choice Concinno makes differently from pytest / mkdocs
is that **features are declared as data, not code**: `concinno.features`
entry-points resolve to a plain `dict[str, dict]` rather than a class.
This keeps plugin authoring low-friction (no base class to inherit, no
API surface to learn), at the cost of forcing schema validation on the
consumer side. For Concinno that trade is worth it — feature metadata
is small, declarative, and naturally JSON-shaped.

## See also

- [Concinno CHANGELOG](../../../CHANGELOG.md) — release notes and
  compatibility surface.
- `concinno-skills-*` packages already on PyPI — practical examples of
  the entry-points pattern in use today.
- `src/concinno/plugin_loader.py` — the older `concinno.guards` group
  follows the same discovery pattern for BaseGuard subclasses.
