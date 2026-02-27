# Changelog

All notable changes to this project are documented in this file.

## v0.1.0 - 2026-02-27

### Added
- End-to-end v0 pipeline milestones M0-M5 (contract, fetcher compliance, parser/extractor, storage/export/rollback, connectors, manual identity review loop).
- Structured JSON logging with `run_id` across fetch + CLI + key pipeline/storage exception paths.
- CI workflow with lint (`ruff`) and test gate (`pytest`) plus coverage threshold (`>= 80%`).
- Daily canary workflow (`sync -> export -> review-queue`) against real public sources.
- Canary reporting/alert rules for:
  - `cli_error`
  - `pipeline_*_error`
  - `BLOCKED_BY_ROBOTS`
- Migration path documentation (`docs/migrations.md`).

### Changed
- Project status from milestone implementation to release-ready `v0.1.0`.
- Repository guardrails aligned around required CI checks and coverage gate.

### Notes
- Branch protection API enforcement requires repository admin token; automation helper is documented in `docs/repo-guardrails.md`.
