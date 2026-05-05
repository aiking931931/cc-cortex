# How to add a feature

A **feature** is a switch-able behavior flag with metadata:
`enabled`, `ziq_autotunable`, `cosmetic`, plus optional tunable
parameters with ranges. Shipped features live in
`concinno.feature_config.FEATURE_META`; user features will live in
`~/.concinno/user_features.json` (Phase C, scheduled for 2.30.1).

## 30-second version (today — 2.30.0)

You can already override any **shipped** feature's parameters without
touching Concinno's source:

```bash
# Inspect the full list in the GUI:
concinno gui

# Or via CLI:
concinno features list
concinno features get <name>
concinno features set <name> <key> <value>
```

Overrides persist in `~/.concinno/<feature>.json` and take precedence
over the shipped default (6-source chain — see
`rules/official/L1/switches.md`).

Currently, **adding a brand-new feature requires editing
`concinno/feature_config.py`** (a PyPI-shipped file). Concinno 2.30.1
will add a user-level registry so you can register a new feature
without patching the library source.

## Shipped-feature template (current, 2.30.0)

If you are a Concinno contributor adding a new shipped feature, the
entry in `FEATURE_META` follows this shape:

```python
"my_guard": {
    "category": "hard_gate",       # hard_gate | hard_quality | info | ux | ...
    "description": "One-line English summary",
    "description_zh": "繁體中文一行摘要",
    "enabled": True,                # default value
    "ziq_autotunable": False,       # may online-learner auto-tune parameters?
    "cosmetic": False,              # UX-only? (skip from ZIQ budget)
    "params": {                     # optional
        "threshold": {
            "type": "int",
            "default": 10,
            "min": 1,
            "max": 100,
            "recommended": 10,
            "risk_low": "may under-trigger",
            "risk_high": "may over-trigger",
            "risk_low_zh": "可能觸發不足",
            "risk_high_zh": "可能過度觸發",
        },
    },
},
```

### Required meta fields

| Field | Type | Required | Meaning |
|---|---|---|---|
| `category` | string | yes | UI grouping; common values in the existing `FEATURE_META` |
| `description` | string | yes | One line, English |
| `description_zh` | string | no | Traditional-Chinese translation (users in `zh-TW` locale see this) |
| `enabled` | bool | yes | Default on/off |
| `ziq_autotunable` | bool | yes | Is the online-learner allowed to adjust this feature's params? |
| `cosmetic` | bool | yes | `True` = UX/display only, `False` = affects task completion |
| `params` | dict | no | Per-parameter schema (type, default, range, risk) |

### Param schema

- `type`: `"bool" | "int" | "float" | "str"`
- `default`: matches `type`
- `min` / `max`: numeric ranges (int / float only)
- `recommended`: non-binding suggestion shown in GUI
- `risk_low` / `risk_high` + `_zh`: localised human descriptions of
  what happens at the edges

## Reading feature state at runtime

```python
from concinno.feature_config import (
    list_features,
    get_feature,
    set_feature,
)

# Full registry
features = list_features()

# One feature
print(get_feature("my_guard"))

# Write an override (goes to ~/.concinno/my_guard.json)
set_feature("my_guard", "enabled", False)
```

The 6-source precedence chain (rule: shipped default → FEATURE_META
default → project config → `~/.concinno/<feature>.json` → env var →
user session override) is applied transparently by
`get_feature`.

## GUI integration

- The GUI Features tab lists every entry with a "source: official"
  badge (user-feature support ships in 2.30.1 with a `user` badge).
- Parameters render as typed widgets (toggle / number / dropdown).
- Overrides are saved to `~/.concinno/<feature>.json` on edit.
- Auto-refresh within 3 s of editing (2.30.0).

## Writing a rule file to declare the switch

When a feature controls an L1 rule's hard behavior, the rule file
declares the switch at its header:

```markdown
**switch**: `my_guard` — see the Switch Index. When disabled the
hard-behavior section is skipped.
```

See `rules/official/L1/switches.md` for the full switch-first rule
application SOP.

## Definition of Done (L0 rule #6)

Every new feature — shipped or user — must satisfy:

1. **Toggle** — `enabled: bool` turns the whole feature off.
2. **Parameters** — every tunable threshold is exposed, no magic
   numbers.
3. **Sources** — honors the 6-source precedence chain.
4. **Index entry** — one row in the Switch Index (`switches.md`).
5. **Rule header** — if the feature has a hard rule in an L1 file,
   that file declares the switch.
6. **Sedimentation** — when the feature was born from a correction,
   a note in feedback or changelog captures the origin.

## Coming in 2.30.1

- **`~/.concinno/user_features.json`** — add new features without
  patching shipped source. Same schema as `FEATURE_META` plus
  a `schema_version` field for migration safety.
- **`concinno features register <name>`** — interactive scaffolder
  that prompts for the meta fields, validates, and writes the file.
- **`--params-json` flag** for scripted param declaration.
- **Collision handling**: shipped-wins by default, with a visible
  badge in the GUI when a user-feature name collides with a shipped
  one (so the ghosted entry is not invisible).

Until 2.30.1 ships, shipped-feature edits go through a Concinno pull
request; user-level feature additions wait.
