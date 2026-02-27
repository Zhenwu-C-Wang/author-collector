# Minimal Alert Rules

The canary runner enforces minimal operational alerts on structured logs.

Source: [`scripts/run_canary.py`](../scripts/run_canary.py)

## Critical Alerts (Fail Canary)

- `cli_error` count > 0
- `pipeline_*_error` count > 0
- missing/empty `run_id` in structured events > 0
- command exit failures in canary flow
- rollback functional check fails
- aggregate `error_rate` exceeds threshold (default: `0.2`)
- `BLOCKED_BY_ROBOTS` count exceeds fail threshold (default: `20`)

## Warning Alerts (Non-fatal)

- `BLOCKED_BY_ROBOTS` count > 0 and <= fail threshold

## Why These Signals

- `cli_error` indicates top-level command failures.
- `pipeline_*_error` indicates stage-level failure paths.
- `BLOCKED_BY_ROBOTS` tracks external policy rejections and source drift.
- `run_id` traceability is required for rollback and audits.

## Tuning Thresholds

Example:

```bash
python scripts/run_canary.py \
  --error-rate-fail-threshold 0.15 \
  --blocked-by-robots-fail-threshold 10
```
