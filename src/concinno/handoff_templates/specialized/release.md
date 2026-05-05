<!-- release — concinno specialized handoff extension -->
<!-- markers: §R1/§R2/§R3 are required; do not rename -->

## §R1 Version current/target

<!-- concinno:release_version_begin -->
| Slot | Value |
|---|---|
| Registry latest | {{registry_latest}} |
| Local pyproject | {{local_version}} |
| Target next | {{target_version}} |
| CHANGELOG entry exists | {{changelog_has_target}} |
<!-- concinno:release_version_end -->

## §R2 Ship checklist state

| Step | State |
|---|---|
| pytest green | ⬜/⏸/✅ |
| ruff clean | ⬜/⏸/✅ |
| build (sdist + wheel) | ⬜/⏸/✅ |
| twine check | ⬜/⏸/✅ |
| three-source version aligned | ⬜/⏸/✅ |
| red-blue review (if High radius) | ⬜/⏸/✅ |

## §R3 Pending publish queue

<!-- See ../RELEASE_COORDINATION.md for the canonical queue. -->
<!-- This is a session-local mirror — point to the YAML record by version. -->
| Version | State | Queued by | Blocking on |
|---|---|---|---|
| {{queued_version}} | ready-to-publish/claimed/published/failed | {{queued_by_session}} | {{blocking_on}} |
