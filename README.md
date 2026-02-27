# author-collector

A **compliance-first**, **evidence-first** pipeline for aggregating **public author content** across platforms with **manual identity resolution**, **structured extraction**, and **operational guardrails**.

> Not a bulk downloader. Not a full-text archive. Not an auto-merge identity engine.

---

## Quick mental model (3 primitives)

- **`run_id` = traceability + rollback handle**
	- Every pipeline action is tagged with a `run_id`.
	- Logs, exports, and DB writes can be traced back to a run.
	- When something goes wrong, you can roll back by `run_id` instead of manually cleaning state.

- **`evidence[]` = field-level provenance**
	- Any key field (title/author/date) is not just a value.
	- It ships with where it came from (URL), how it was extracted (jsonld/meta/dom), and optional selectors/snippets.
	- This makes extraction **auditable**, **replayable**, and safe to evolve.

- **Review loop (HITL) = identity resolution governance**
	- The system may suggest candidate merges.
	- v0 requires a human decision (`accept/reject/hold`).
	- Decisions are written to an audit trail and can be replayed/revoked.

---

## ⚠️ Compliance-First Design

This project prioritizes **legal compliance, ethical sourcing, and operational safety** over speed or scale.

### Non-Negotiables in v0

1. **Evidence-First** — Every claim (title, author, date) is backed by traceable evidence with source URLs.
2. **No Auto-Merge** — Identity resolution is **manual review only** (no automatic merging).
3. **No Full Body Text Storage** — We store **snippets only** (≤ 1500 chars in v0). Full article text is never stored.
4. **Robots.txt Enforcement** — Mandatory and cannot be disabled.
5. **Serial Execution** — Single concurrent fetch only (`MAX_GLOBAL_CONCURRENCY=1`). No parallel crawling.
6. **Rate Limiting** — Minimum 5-second gap between requests to the same domain.
7. **SSRF Prevention** — Private IPs (localhost, 10.x, 192.168.x, etc.) are blocked. No internal network access.

See: [`docs/compliance.md`](docs/compliance.md)

---

## What this repo gives you

### ✅ Outputs (default)
- URL indexing
- Metadata extraction
- Constrained summaries (optional/limited)
- Field-level evidence (auditable extraction)
- Versioned, deduplicated storage (SQLite)
- JSONL export (schema-validated)
- Manual review loop for identity resolution (audit trail)
- Per-run traceability (`run_id`) + rollback support

### 🚫 Explicitly out of scope (v0)
- Login bypass / paywall bypass
- Bulk full-text crawling or redistribution
- Any automatic author identity merging

---

## Pipeline Architecture

```

Seed Input

↓

[Discover] → Iterator of URLs

↓

[Fetch] → FetchedDoc (status/headers/body + robots + rate limiting + SSRF checks)

↓

[Parse] → Structured hints (title, author hints, JSON-LD)

↓

[Extract] → ArticleDraft + Evidence[] (with claim paths)

↓

[Store] → Versioned, deduplicated articles (SQLite)

↓

[Export] → JSONL (schema-validated)

↓

[Review] → Manual identity resolution (merge decisions audit trail)

```

---

## Evidence-first data model (example)

Minimal `Article` output (inline evidence + `run_id`):

```

{

"id": "art_01",

"canonical_url": "https://example.com/post/123",

"source_id": "rss:example_feed",

"title": "Building Reliable Pipelines",

"author_hint": "Jane Doe",

"published_at": "2026-02-26T08:00:00Z",

"snippet": "This post explains how to build robust data pipelines...",

"evidence": [

{

"id": "ev_1001",

"claim_path": "/title",

"evidence_type": "json_ld",

"source_url": "https://example.com/post/123",

"extraction_method": "jsonld",

"extracted_text": "Building Reliable Pipelines",

"confidence": 0.98,

"metadata": {

"selector": "script[type='application/ld+json']",

"field": "headline"

}

}

],

"version": 2,

"run_id": "run_20260227_093258",

"pipeline_stage": "exported",

"created_at": "2026-02-27T09:00:00Z",

"updated_at": "2026-02-27T09:10:00Z"

}

```

Schemas live in: [`schemas/`](schemas/)

---

## Getting Started

### Installation

```

# Clone and install

git clone https://github.com/Zhenwu-C-Wang/author-collector.git

cd author-collector

pip install -e .

```

### Run core commands (M0–M4)

```

# Validate contract schemas

author-collector validate-schemas

# Export baseline JSONL (empty file in v0 contract baseline)

author-collector export --output articles.jsonl --run-id run-export-demo

# Run connector sync (rss/html/arxiv source IDs are supported)

author-collector sync \

--source-id rss:example_feed \

--seed tests/fixtures/rss/example.xml \

--run-id run-demo

```

All CLI commands emit **structured JSON lines** and include `run_id` for traceability.

---

## Canary workflow (real-data verification)

The repo includes a canary workflow for real sources and reproducible checks.

See:
- [`canary/`](canary/)
- [`docs/canary.md`](docs/canary.md)

---

## Testing

```

# Contract tests (schema compliance)

pytest tests/contract -m contract -v

# All tests

pytest tests -v

```

All tests must pass before commit (enforced by CI).

---

## Documentation map (start here)

Core:
- [`ROADMAP.md`](ROADMAP.md) — milestone breakdown, acceptance criteria, risk assessment
- [`ROLLBACK.md`](ROLLBACK.md) — incident recovery procedures, per-run undo
- [`docs/releases/v0.1.0.md`](docs/releases/v0.1.0.md) — release notes

Governance & safety:
- [`docs/non-negotiables.md`](docs/non-negotiables.md) — hard boundaries in v0
- [`docs/compliance.md`](docs/compliance.md) — rationale for compliance-first choices
- [`docs/verification.md`](docs/verification.md) — reproducible verification checklist

Operability:
- [`docs/alerts.md`](docs/alerts.md) — minimal operational alert rules
- [`docs/repo-guardrails.md`](docs/repo-guardrails.md) — CI/coverage/branch protection

Data layer:
- [`storage/migrations/0001_init.sql`](storage/migrations/0001_init.sql) — DB schema (run_id tracking, versioning, merge audit trail)
- [`docs/migrations.md`](docs/migrations.md) — additive migration policy & compatibility checks
- [`CHANGESET.md`](CHANGESET.md) — file-level changes per milestone
- [`CHANGELOG.md`](CHANGELOG.md) — release-oriented changelog

---

## Project Status

- **v0.1.0 (Release)**: Milestones 0–5 complete and release-gated  
  (contract, fetcher, parser/extractor, storage/export/rollback, connectors, manual review-loop identity resolution, CI + coverage gate, structured logs, migration docs).
- **Current focus (M6 mainline)**: scheduler + operability hardening  
  (daily canary, alerting, run governance).

See [`ROADMAP.md`](ROADMAP.md) for the authoritative status.

---

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md).

Please do **not** submit PRs that add:
- login/paywall bypass,
- full-text storage/redistribution,
- automatic identity merges.

---

## License

MIT
