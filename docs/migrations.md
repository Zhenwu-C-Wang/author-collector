# Migration Path (v0)

This project uses a conservative migration policy for SQLite to keep long-running deployments safe and reversible.

## Policy: Additive Changes Only

- v0 migrations are **additive**:
  - add tables
  - add nullable columns
  - add indexes
- No destructive migration in place:
  - no drop table
  - no drop column
  - no type rewrite that breaks old rows
- Existing `run_id` lineage must be preserved for rollback and audit.

## Startup Upgrade Flow

Schema upgrades are applied at startup by [`SQLiteRunStore.initialize_schema`](../storage/sqlite.py):

1. Ensure DB directory exists.
2. If `run_log` does not exist, apply base migration [`0001_init.sql`](../storage/migrations/0001_init.sql).
3. Run additive guards in `_ensure_additive_columns(...)` to patch older DBs forward.

This means operators can deploy new code without running a separate migration command for v0 additive changes.

## Rollback Strategy

Rollback is run-scoped (`rollback --run <id>`), not schema-scoped:

- remove artifacts created by the target run (`fetch_log`, `evidence`, `versions`, `merge_decisions`)
- restore touched `articles` to latest pre-run snapshot when available
- delete articles created only by that run
- mark `run_log.status = CANCELLED`

Operational rollback details are documented in [`ROLLBACK.md`](../ROLLBACK.md).

## Version Compatibility Checks

Compatibility checks are enforced in two layers:

- **Code-level schema guard**: `_ensure_additive_columns(...)` upgrades older DBs with required additive fields.
- **Export contract validation**: every exported row is validated against [`article.schema.json`](../schemas/article.schema.json), fail-fast on first invalid row.

Recommended deploy check sequence:

1. Start app against a copy of production DB.
2. Run `author-collector export --output /tmp/verify.jsonl`.
3. Run `pytest -q` (or at minimum contract + integration suites).
4. Deploy only if all checks pass.
