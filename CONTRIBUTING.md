# Contributing

## Development Setup

```bash
python -m pip install -e .
python -m pip install -e .[dev]
```

## Quality Gates

Run these before opening a PR:

```bash
pytest -q
ruff check .
```

## Contribution Rules

- Keep changes aligned with compliance-first defaults.
- Do not introduce full-body export fields.
- Keep `claim_path` as JSON Pointer (`/field`).
- Add or update tests with behavior changes.
- Keep docs in sync with actual behavior.

## Commit/PR Expectations

- Explain what changed and why.
- Include migration implications (if any schema changes are made).
- Call out compliance-impacting changes explicitly.
