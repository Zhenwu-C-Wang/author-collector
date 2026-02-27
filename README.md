# author-collector

A compliant, evidence-first pipeline for aggregating public author content across platforms with identity resolution and structured extraction.

## ⚠️ Compliance-First Design

This project prioritizes **legal compliance, ethical sourcing, and operational safety** over speed or scale. Key principles:

### Non-Negotiables in v0

1. **No Full Body Text Storage** — We extract snippets (≤5000 chars) only. Full article text is never stored.
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
[Fetch] → Bytes (robots.txt + rate limiting + SSRF checks)
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

### Run Default Connectors

```bash
# Sync from RSS feed
author-collector sync --source-id rss:example --seed https://example.com/rss

# Export to JSONL
author-collector export --output articles.jsonl

# Review identity candidates
author-collector review-queue --min-score 0.8 > candidates.json
# Edit candidates.json to accept/reject merges
author-collector review apply candidates.json
```

## Documentation

- **[ROADMAP.md](ROADMAP.md)** — Milestone breakdown, acceptance criteria, risk assessment
- **[CHANGESET.md](CHANGESET.md)** — File-level changes per milestone
- **[ROLLBACK.md](ROLLBACK.md)** — Incident recovery procedures, per-run undo
- **[docs/compliance.md](docs/compliance.md)** — Why we make these design choices
- **[docs/non-negotiables.md](docs/non-negotiables.md)** — Boundaries we don't cross
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

**v0 (Current)**: Basic pipeline + 3 connectors + manual identity resolution.

See [ROADMAP.md](ROADMAP.md) for detailed timeline and milestones.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development guidelines (not yet written).

## License

MIT
