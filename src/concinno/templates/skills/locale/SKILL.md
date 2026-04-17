---
description: Manage CC Cortex display language and add custom locale packs
user_invocable: true
disable-model-invocation: true
---

# /locale — Language Management for CC Cortex

## Usage

```
/locale              → Show current locale and available languages
/locale set <lang>   → Set display language (e.g. en, zh, ja, ko, es)
/locale add <lang>   → Generate a template locale file for a new language
/locale list         → List all loaded locales with pattern/message counts
```

## How It Works

CC Cortex has two language layers:

1. **Display messages** — UI text shown to the user (follows `CC_UX_LANG`)
2. **Detection patterns** — keywords/regex for detecting corrections, uncertainty, handoff files, etc. (merged from ALL loaded locales)

### Built-in locales
- `en` — English (default display + detection patterns)
- `zh_TW` — Traditional Chinese (detection patterns)
- `ja` — Japanese (correction detection only)
- `ko` — Korean (correction detection only)
- `es` — Spanish (correction detection only)

### Adding a new locale

1. Run `/locale add <lang>` to generate a template JSON
2. Edit the generated file at `concinno/locale/<lang>.json`
3. Fill in translated messages (optional) and detection patterns (recommended)
4. Set `CC_UX_LANG=<lang>` or run `/locale set <lang>`

### Locale JSON structure

```json
{
  "messages": {
    "confidence_gate.deny": "Translated deny message with {markers} placeholder",
    "handoff_engine.reminder": "Translated reminder with {token_k}K and {count} placeholders"
  },
  "patterns": {
    "correction_l1": ["pattern1", "pattern2"],
    "uncertainty": ["maybe_in_your_lang", "perhaps_in_your_lang"],
    "handoff_prefixes": ["your_lang_handoff_prefix_"]
  }
}
```

## Actions

### `/locale` or `/locale list`

Read the current state:

```python
from concinno.i18n import get_locale, get_active_locales, locale_dir
print(f"Display: {get_locale()}")
print(f"Active: {get_active_locales()}")
print(f"Locale dir: {locale_dir()}")
```

List files in `locale_dir()` and report message/pattern counts per locale.

### `/locale set <lang>`

```python
from concinno.i18n import set_locale
set_locale("<lang>")
```

Also update `cc_config.json` so it persists:
```json
{ "locale": "<lang>" }
```

### `/locale add <lang>`

1. Copy the English locale as template:
   ```python
   from concinno.i18n import locale_dir
   template = locale_dir() / "en.json"
   target = locale_dir() / "<lang>.json"
   ```
2. Write a skeleton with empty messages and pattern stubs
3. Tell the user to fill in their language's patterns
4. Run `reload()` to pick up the new file

## Key Pattern Categories

Users adding a new locale should prioritize these pattern keys:

| Key | Purpose | Example |
|-----|---------|---------|
| `correction_l1` | High-confidence correction detection | "wrong", "no", "fix this" |
| `correction_l2` | Implicit correction patterns | "change X to Y", "remove" |
| `uncertainty` | Uncertainty markers for confidence gate | "maybe", "not sure" |
| `handoff_prefixes` | Handoff file name prefixes | "handoff_", "引き継ぎ_" |
| `research_keywords` | Agent classification (research) | "investigate", "analyze" |
| `execution_keywords` | Agent classification (execution) | "edit", "deploy", "fix" |
| `stop_guard.*` | Session stop detection | completion/question/continuation keywords |
