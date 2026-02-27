# Real-Data Canary

This project runs a daily canary on real public sources to validate end-to-end operability.

## Flow

The canary executes:

1. `sync` for each source in [`canary/sources.json`](../canary/sources.json)
2. `export`
3. `review-queue`
4. rollback functional check (default enabled)

All command outputs are JSON-line logs with `run_id`.

## Daily Schedule

Workflow: [`canary.yml`](../.github/workflows/canary.yml)

- trigger: `workflow_dispatch` + daily cron (`02:20 UTC`)
- artifacts uploaded:
  - command stdout/stderr logs
  - parsed event logs
  - canary export/review outputs
  - canary report JSON

## Running Locally

```bash
python scripts/run_canary.py \
  --sources-file canary/sources.json \
  --workspace artifacts/canary
```

## Report Fields

Canary report includes:

- per-source sync summaries (`fetched/new/updated/errors`)
- aggregate error rate
- `cli_error` count
- `pipeline_*_error` count
- `BLOCKED_BY_ROBOTS` count
- missing `run_id` event count
- rollback check result

See alert policy in [`docs/alerts.md`](alerts.md).
