<!-- build — concinno specialized handoff extension -->
<!-- markers: §I1/§I2/§I3 are required; do not rename -->

## §I1 Build target

<!-- What artefact is being built? Binary / wheel / docker image / docs site. -->
- **Artefact**: {{artefact_name}}
- **Output path**: {{output_path}}
- **Target platform**: {{target_platform}}

## §I2 Deps state

| Dep | Required | Installed | Notes |
|---|---|---|---|
| {{dep_name}} | {{dep_required_version}} | {{dep_installed_version}} | |

## §I3 Test status

| Suite | Pass | Fail | Skipped | Last run |
|---|---|---|---|---|
| pytest | {{pytest_pass}} | {{pytest_fail}} | {{pytest_skipped}} | {{pytest_last_run}} |
| ruff | {{ruff_status}} | — | — | {{ruff_last_run}} |
| mypy | {{mypy_status}} | — | — | {{mypy_last_run}} |
