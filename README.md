# author-collector

A compliant, evidence-first pipeline for aggregating public author content across platforms with identity resolution and structured extraction.

## ⚠️ Compliance-First Design

This project prioritizes **legal compliance, ethical sourcing, and operational safety** over speed or scale. Key principles:

### Non-Negotiables in v0

1. **No Full Body Text Storage** — We extract snippets (≤1500 chars in v0) only. Full article text is never stored.
2. **Robots.txt Enforcement** — Mandatory and cannot be disabled. We respect `robots.txt` rules.
3. **Serial Execution** — Single concurrent fetch only (`MAX_GLOBAL_CONCURRENCY=1`). No parallel crawling.
4. **Rate Limiting** — Minimum 5-second gap between requests to same domain.
5. **SSRF Prevention** — Private IPs (localhost, 10.x, 192.168.x) are blocked. No internal network access.
6. **No Auto-Merge** — Author identity resolution is manual review only (no automatic merging).
7. **Evidence-First** — Every claim (title, author, date) is backed by traceable evidence with source URLs.

See [docs/compliance.md](docs/compliance.md) for detailed rationale.

## Pipeline Architecture

```
Seed Input
    ↓
[Discover] → Iterator of URLs
    ↓
[Fetch] → FetchedDoc (status/headers/body + robots + rate limiting + SSRF checks)
    ↓
[Parse] → Structured data (title, author hints, JSON-LD)
    ↓
[Extract] → ArticleDraft + Evidence[] (with claim paths)
    ↓
[Store] → Versioned, deduplicated articles (SQLite)
    ↓
[Export] → JSONL (schema-validated)
    ↓
[Review] → Manual identity resolution (merge decisions audit trail)
```

## Getting Started

### Installation

```bash
# Clone and install
git clone https://github.com/anthropics/author-collector.git
cd author-collector
pip install -e .
```

### Run Core Commands (M0-M4)

```bash
# Validate contract schemas
author-collector validate-schemas

# Export baseline JSONL (empty file in v0 contract baseline)
author-collector export --output articles.jsonl --run-id run-export-demo

# Run connector sync (rss/html/arxiv source IDs are supported)
author-collector sync --source-id rss:example_feed --seed tests/fixtures/rss/example.xml --run-id run-demo
```

All CLI commands emit structured JSON lines and include `run_id` for traceability.

## Documentation

- **[ROADMAP.md](ROADMAP.md)** — Milestone breakdown, acceptance criteria, risk assessment
- **[CHANGESET.md](CHANGESET.md)** — File-level changes per milestone
- **[ROLLBACK.md](ROLLBACK.md)** — Incident recovery procedures, per-run undo
- **[docs/compliance.md](docs/compliance.md)** — Why we make these design choices
- **[docs/non-negotiables.md](docs/non-negotiables.md)** — Hard boundaries in v0
- **[docs/verification.md](docs/verification.md)** — Verification checklist and reproducible checks
- **[docs/migrations.md](docs/migrations.md)** — Additive migration policy, startup upgrades, compatibility checks
- **[docs/releases/v0.1.0.md](docs/releases/v0.1.0.md)** — Release notes
- **[docs/canary.md](docs/canary.md)** — Real-data canary workflow
- **[docs/alerts.md](docs/alerts.md)** — Minimal operational alert rules
- **[docs/repo-guardrails.md](docs/repo-guardrails.md)** — CI/coverage/branch-protection guardrails
- **[storage/migrations/0001_init.sql](storage/migrations/0001_init.sql)** — Database schema (run_id tracking, versioning, merge audit trail)

## Testing

```bash
# Contract tests (schema compliance)
pytest tests/contract -m contract -v

# All tests
pytest tests -v
```

All tests must pass before commit (enforced by CI).

## Project Status

**v0.1.0 (Release)**: Milestones 0-5 complete and release-gated (contract, fetcher, parser/extractor, storage/export/rollback, connectors, manual review-loop identity resolution, CI + coverage gate, structured logs, migration-path docs).  
**Current focus (M6 mainline)**: scheduler + operability hardening (daily canary, alerting, run governance).

See [ROADMAP.md](ROADMAP.md) for detailed milestone status.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development guidelines.

## License

MIT
