<!-- generic_v2 — concinno bundled handoff template (every project) -->
<!-- markers: §0/§1/§2/§3/§4/§5 are required; do not rename -->

## §0 Auto-revival (FieldRead-populated, do not hand-edit)

<!-- concinno:field_read_begin -->
| Item | Value |
|---|---|
| pod_id | {{pod_id}} |
| ssh_cmd | {{ssh_cmd}} |
| vault_aliases | {{vault_aliases}} |
| env_vars | {{env_vars}} |
| last_commit | {{last_commit}} |
| last_session_id | {{last_session_id}} |
| last_token_usage | {{last_token_usage}} |
| populated_at | {{populated_at}} |
<!-- concinno:field_read_end -->

> Stale §0 (>24hr old) is re-populated by the `handoff_resume` hook.
> Manual rebuild: `concinno handoff refresh-section0 <project>` (planned).

## §1 Status header

<!-- concinno:status_begin -->
- **counts**: {{open_count}} ⬜ / {{paused_count}} ⏸ / {{done_count}} ✅
- **focus**: {{current_focus}}
- **last session**: {{last_session_id}}
<!-- concinno:status_end -->

## §2 Decision rationale (manual, ≤30 lines)

<!-- Why this approach, not the alternatives? Short bullet list. -->

## §3 Pitfalls + warnings (manual, ≤30 lines)

<!-- Specific traps already encountered. Cite feedback file or MEMORY # if relevant. -->

## §4 Pointer table (manual, relative paths only)

| Topic | Read this file |
|---|---|
| Architecture | <relative-path-from-handoff> |

## §5 next_step (manual one-liner)

<!-- Single concrete actionable command or task name. -->
