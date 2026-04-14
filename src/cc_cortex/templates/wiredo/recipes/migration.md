migration (DB schema / data backfill)
  ✓ evidence: `alembic upgrade head` (or equiv) on staging DB +
    SELECT showing migrated rows + dry-run rollback test.
  ✗ evidence: SQL written but never applied even on staging.
