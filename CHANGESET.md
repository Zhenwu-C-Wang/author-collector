# CHANGESET: Per-Milestone File Changes

This document lists exactly which files are created/modified in each milestone, for change management and risk assessment.

---

## Milestone 0: Core Data Model + Pipeline Contract

### New Files

```
core/
  __init__.py
  models.py                  # Pydantic: Author, Account, Article, Evidence, RunLog, FetchLog, ArticleDraft
  pipeline.py               # Pipeline interface: discover → fetch → parse → extract → store → export
  config.py                 # DEFAULT compliance config (immutable in v0)
  evidence.py               # Evidence ID generation, hashing, validation

schemas/
  article.schema.json       # JSON Schema for Article export (no body field)
  evidence.schema.json      # JSON Schema for Evidence

tests/
  __init__.py
  conftest.py               # Fixtures (temp DB, sample data)
  contract/
    test_schema_compliance.py  # Contract tests: export must match schema
  fixtures/
    sample_article.json     # Example valid article
    sample_evidence.json    # Example valid evidence
```

### Modified Files

```
README.md                   # Add: "Why no body field" + "Why robots mandatory" + pipeline diagram
pyproject.toml             # Add: pydantic, jsonschema, pytest dependencies
.gitignore                 # (unchanged, should already have Python patterns)
```

### DB Schema Files (Prepared, Not Yet Migrated)

```
storage/
  __init__.py
  migrations/
    0001_init.sql          # Full schema: articles, evidence, versions, fetch_log, run_log, accounts, authors, merge_decisions
```

**File Count**: 14 new, 2 modified
**Risk Level**: LOW (models only, no I/O, no behavior)
**Rollback**: Delete all new files, restore README/pyproject.toml to HEAD

---

## Milestone 1: Fetcher Compliance Foundation

### New Files

```
fetcher/
  __init__.py
  http.py                   # HTTP fetch with security constraints + IP blocklist + redirect limits
  robots.py                 # robots.txt parser + cache (in-memory or SQLite)
  politeness.py             # Rate limiting per domain + global concurrency control
  logging.py                # Structured JSON logging

storage/
  sqlite.py                 # SQLite operations + migrations + fetch_log writes
  models.py                 # SQLAlchemy models for articles, evidence, fetch_log, etc.

tests/
  integration/
    test_fetcher_security.py   # SSRF tests, redirect limits, protocol whitelist
    test_robots.py              # Robots parsing + cache behavior
    test_politeness.py           # Rate limiting + concurrency tests
  fixtures/
    robots.txt.samples/      # Various robots.txt examples
    test_urls.json          # URLs for security testing (localhost, private IPs, etc.)
```

### Modified Files

```
core/config.py              # (already exists from M0) — no changes needed
core/models.py              # Add: FetchLog, RunLog models
pyproject.toml             # Add: requests, cachetools dependencies
```

### Database Schema Evolution

```
storage/migrations/
  0001_init.sql            # (bumped from M0 preparation) — create fetch_log, run_log tables
```

**File Count**: 13 new, 2 modified
**Risk Level**: MEDIUM (network I/O, IP checks, security constraints critical)
**Acceptance**: Integration tests pass; SSRF blocklist verified
**Rollback**: Set `robots_check=False`, `max_global_concurrency=100`, revert network calls; OR revert to pre-M1 tag

---

## Milestone 2: Parser + Extractor

### New Files

```
parser/
  __init__.py
  html.py                   # HTML → readable text (trafilatura/readability), snippet truncation
  jsonld.py                 # JSON-LD + meta tag extraction

extractor/
  __init__.py
  article.py                # ArticleDraft + Evidence[] generation (deterministic)
  pii.py                    # Optional PII scrubbing (default off in v0)

tests/
  integration/
    test_parser_extractor.py   # Three fixture sets (normal/edge/malformed)
  fixtures/
    html/
      simple.html            # Basic article page
      edge_cases.html       # Missing title, malformed JSON-LD, etc.
      malformed.html        # Broken HTML structures
    expected_outputs/
      simple.json           # Expected ArticleDraft + Evidence for simple.html
      edge_cases.json       # Expected output for edge_cases.html
```

### Modified Files

```
core/models.py              # Add: Parsed, ArticleDraft, Evidence models (if not in M0)
core/pipeline.py            # Integrate parse + extract stages (flesh out the interface)
pyproject.toml             # Add: trafilatura, (or readability), lxml dependencies
```

**File Count**: 11 new, 2 modified
**Risk Level**: LOW (parsing only, no new I/O or external dependencies beyond HTML libs)
**Acceptance**: Three fixture sets pass; evidence counts correct; deterministic
**Rollback**: Disable M2 pipeline (set `extract_enabled=False`); revert to M1 state

---

## Milestone 3: Storage + Dedup + Versioning + Export

### New Files

```
quality/
  __init__.py
  urlnorm.py                # URL canonicalization rules (lowercase, fragment, utm_)

cli/
  __init__.py
  main.py                   # Entry point: add, sync, export, review commands
  export.py                 # Export to JSONL + schema validation
  review_queue.py           # (prepared for M5, can be stubbed in M3)

storage/
  migrations/
    0001_init.sql           # (updated) full schema: articles, evidence, versions, fetch_log, run_log, merge_decisions, etc.
    0002_versioning.sql     # (if split) versions table, indexes

tests/
  integration/
    test_storage_upsert.py     # Dedup, upsert, re-sync same data
    test_versioning.py         # Content change → new version
    test_export.py             # Export to JSONL, schema validation
  fixtures/
    sample_articles.json   # Known articles for re-sync testing
```

### Modified Files

```
core/models.py              # Model version fields, ensure run_id everywhere
core/pipeline.py            # Integrate storage stage
storage/sqlite.py           # Flesh out upsert, versioning logic
storage/models.py           # Add Version, MergeDecision models
pyproject.toml             # (minimal, likely no new deps)
```

### Database Migrations

```
storage/migrations/
  0001_init.sql            # (final version) all tables
  (no backward-incompatible changes allowed)
```

**File Count**: 15 new, 3 modified
**Risk Level**: MEDIUM-HIGH (data mutations, irreversible writes)
**Acceptance**: Multiple sync runs don't duplicate; versioning works; export valid JSON
**Rollback**: Use `rollback --run <id>` command (deletes version records + evidence for that run); OR restore DB from pre-M3 backup

---

## Milestone 4: Three Friendly Connectors

### New Files

```
connectors/
  __init__.py
  base.py                   # Abstract connector interface (discover → Iterator[URL])
  rss.py                    # RSS feed parser
  html_author_page.py       # HTML scraping for author page discovery
  arxiv.py                  # arXiv Atom feed parser

tests/
  integration/
    test_rss_connector.py        # E2E: fixture feed → articles in DB
    test_html_author_page.py     # E2E: fixture HTML → discovered URLs → articles
    test_arxiv_connector.py      # E2E: fixture Atom → articles in DB
  fixtures/
    rss/
      example_feed.xml      # Valid RSS with 3-5 articles
    html/
      author_page.html      # HTML page with article links
    arxiv/
      response.atom         # Mock arXiv Atom response
```

### Modified Files

```
core/pipeline.py            # Add connector integration point
cli/main.py                 # Add `sync --source-id rss:...` command
pyproject.toml             # Add: feedparser (or xml parser), no new critical deps
```

**File Count**: 11 new, 2 modified
**Risk Level**: LOW-MEDIUM (external data sources, but read-only)
**Acceptance**: Each connector runs end-to-end; all create valid articles; integration tests pass
**Rollback**: Disable connectors (set `enabled_connectors=[]`); revert to M3 code

---

## Milestone 5: Identity Resolution v0

### New Files

```
resolution/
  __init__.py
  scoring.py                # Rule-based candidate scoring (no auto-merge)
  merge.py                  # Merge operations (create merge_decisions, update author references)

cli/
  review_queue.py           # (flesh out from M3 stub) Generate review.json
  commands.py               # review-queue, review apply subcommands

tests/
  integration/
    test_resolution.py      # Candidate scoring, review queue generation
    test_merge_apply.py     # Apply review decisions, create merge_decisions
    test_merge_rollback.py  # Undo merges via rollback
  fixtures/
    candidates.json         # Pre-computed candidates for testing
```

### Modified Files

```
core/models.py              # Add: MergeDecision, Candidate models (if not in M3)
storage/models.py           # (already exists from M3) MergeDecision table
cli/main.py                 # Integrate review commands
pyproject.toml             # (minimal, no new deps)
```

**File Count**: 10 new, 2 modified
**Risk Level**: HIGH (author mutations, can't be undone without rollback)
**Acceptance**: Candidates identified correctly; review queue editable; merges create audit trail; rollback works
**Rollback**: `rollback --run <id>` or `rollback --merge <merge_id>`

---

## Summary: Total Changeset

| Milestone | New Files | Modified Files | Risk Level |
|-----------|-----------|-----------------|-----------|
| M0        | 14        | 2               | LOW       |
| M1        | 13        | 2               | MEDIUM    |
| M2        | 11        | 2               | LOW       |
| M3        | 15        | 3               | MEDIUM-HIGH |
| M4        | 11        | 2               | LOW-MEDIUM |
| M5        | 10        | 2               | HIGH      |
| **Total** | **74**    | **13**          | —         |

## Key Protected/Reviewed Files

- `core/config.py` — Default compliance constraints (review before any change)
- `fetcher/robots.py`, `fetcher/http.py` — Security constraints (code review + security audit before ship)
- `storage/migrations/` — Database schema (only append, never backward-incompatible)
- CLI interface (`cli/main.py`) — Public API (document thoroughly)

## Dependency Summary (Added via pyproject.toml)

- **M0**: pydantic, jsonschema, pytest
- **M1**: requests, cachetools
- **M2**: trafilatura (or readability), lxml
- **M4**: feedparser (or xml.etree, built-in)
- **M5**: (none)

**Do not add**: playwright, selenium, browser automation, arbitrary code execution

---

## Pre-Deployment Checklist

- [ ] All migrations tested on fresh + seeded databases
- [ ] All integrations tested with fixtures
- [ ] Security review: SSRF, rate limiting, robots enforcement
- [ ] Performance: No memory leaks in parser/extractor
- [ ] Logging: All major operations captured with run_id
- [ ] README: Updated with "Why no body", "Why robots mandatory", architecture
