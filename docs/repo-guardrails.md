# Repository Guardrails

This document captures merge gates and branch protection expectations for `main`.

## Required Checks

- `lint` (ruff)
- `test` (pytest + coverage gate)

These checks are produced by [`CI workflow`](../.github/workflows/ci.yml).

## Coverage Gate

`test` job enforces:

- `pytest --cov-... --cov-fail-under=80`

Current threshold: **80%**.

## Branch Protection Target

For branch `main`, enforce:

- require PR before merge
- require status checks to pass (`lint`, `test`)
- require branch up-to-date before merge (`strict=true`)
- require at least 1 approving review
- dismiss stale approvals
- require conversation resolution
- block force-push and deletion

## Apply Protection via API

Use:

```bash
export GITHUB_TOKEN=<repo-admin-token>
python scripts/apply_branch_protection.py \
  --owner Zhenwu-C-Wang \
  --repo author-collector \
  --branch main \
  --checks lint test \
  --approvals 1
```

Dry run:

```bash
python scripts/apply_branch_protection.py \
  --owner Zhenwu-C-Wang \
  --repo author-collector \
  --dry-run
```
