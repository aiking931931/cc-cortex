# `concinno session-switches` — Install Guide

> 2.16.0+. L2 summary layer for switch visibility. See
> [`~/.claude/rules/switches.md`](../../.claude/rules/switches.md) for the
> L1 index of every Concinno switch.

## What it does

At the start of every Claude Code session, emit a one-line summary of the
user's **non-default** switch values to the agent's system context via
`stderr`. The agent can't plausibly forget the user's opt-outs when the
very first line of its context says:

```txt
concinno: active switches — release_auth.disabled=True, handoff_mode=full
```

## Install

### 1. Check it's wired

```bash
concinno session-switches --format=text
```

You should see either your non-default switches listed, or
`concinno session switches: all defaults (nothing to show)`.

### 2. Hook it to SessionStart

Edit `~/.claude/settings.json::hooks.SessionStart[]` and add a hook entry
that runs `concinno session-switches --format=hook`:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "",
        "hooks": [
          { "type": "command",
            "command": "python -m concinno.cli.session_switches_cmd --format=hook" }
        ]
      }
    ]
  }
}
```

Or if you use Concinno's built-in hook installer:

```bash
concinno init --force
```

and the SessionStart hook template will already have the session-switches
line appended (from 2.16.0+ templates).

### 3. Customize (optional)

Override the top-10 list or add project-specific switches:

```bash
# ~/.concinno/session_switches.json
{
  "enabled": true,
  "top_n": 15,
  "hook_format_compact": true,
  "extra_switches": ["my_custom_feature", "another_feature"]
}
```

Since this file is in `~/.concinno/`, it survives `pip install --upgrade
concinno` (see [Upgrade Safety](../README.md#upgrade-safety-2160) in the
main README).

## Opt out

If you don't want any of this:

```bash
# Env toggle (session-scoped)
CONCINNO_SESSION_SWITCHES_ENABLED=0

# Or persistent via feature config
python -c "from concinno.feature_config import set_feature; \
  set_feature('session_switches', 'enabled', False)"
```

## Formats

- `--format=text` — human-readable multi-line; lists each switch with
  `key = value (default: X, source: Y)`.
- `--format=json` — structured payload with stable schema
  `concinno.session_switches.v1`. Useful for ops pipelines / dashboards.
- `--format=hook` — single line, written to **stderr** (so stdout
  pipelines stay clean), prefixed with `concinno: active switches — `.

## Top-10 Default Switch Set

These are the switches whose non-default values are surfaced. Priority
order matches display order:

1. `release_auth.disabled` — default `False`. `True` = publish auto-proceeds.
2. `destruction_guard.enabled` — default `True`. `False` removes destructive-op interception.
3. `handoff_mode` — default `phase`. `full` / `save` change gate behavior.
4. `toast_notify.enabled` — default `False`. `True` surfaces Windows toasts.
5. `locale` — default `auto`. Explicit override changes i18n chain.
6. `auto_commit.enabled` — default `True`. `False` skips git autocommit.
7. `sweep_guard.enabled` — default `True`. `False` skips end-of-task sweeps.
8. `butterfly_guard.enabled` — default `True`. Gate on pre-existing bug detection.
9. `wiredo.enabled` — default `True`. WIREDO delivery gate.
10. `premise_gate.enabled` + `premise_gate.mode` — default on / `mode_1+mode_2`.

When in doubt, check `~/.claude/rules/switches.md` for the full 22-row index.
