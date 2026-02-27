# author-collector v0 Roadmap

**Goal**: Build a compliant, evidence-first aggregation pipeline that can run long-term without legal/technical risk, with identity resolution as manual review queue (not automatic merge).

**Phases**: 5 milestones, each with clear acceptance criteria and contractual boundaries.

---

## Milestone 0: Core Data Model + Pipeline Contract (Required First)

**Duration**: Define once, all downstream = fill-in-the-blanks.
**Status**: `[x] Completed (Contract Baseline Landed)`

### 0.1 - Core Pydantic Models & JSON Schemas
- **Deliverables**:
  - `core/models.py`: `Author`, `Account`, `Article`, `Evidence`, `RunLog`, `FetchLog`, `ArticleDraft`
  - `schemas/article.schema.json`: What goes into final export (strict, no `body` field)
  - `schemas/evidence.schema.json`: What evidence structure must satisfy
- **Evidence Structure** (critical):
  ```json
  {
    "id": "uuid",
    "claim_path": "/title",  // JSON Pointer (RFC 6901) to the claim
    "evidence_type": "meta_tag"|"json_ld"|"extracted"|"fetched_content",
    "source_url": "string",
    "extraction_method": "readability|trafilatura|meta|...",
    "extracted_text": "...",
    "confidence": 0.0-1.0,
    "metadata": {...}
  }
  ```
- **Article Output Schema** (sample):
  ```json
  {
    "id": "uuid",
    "canonical_url": "string (PRIMARY KEY)",
    "source_id": "string (e.g., 'rss:feedname')",
    "title": "string",
    "author_hint": "string (unresolved)",
    "published_at": "ISO8601",
    "snippet": "string (max 1500 chars, no full body)",
    "evidence": [
      { "claim_path": "/title", "evidence_type": "meta_tag", ... },
      { "claim_path": "/author_hint", "evidence_type": "extracted", ... }
    ],
    "version": int,
    "created_at": "ISO8601",
    "updated_at": "ISO8601"
  }
  ```
- **Run & Fetch Logs** (for traceability):
  - `RunLog`: `id, started_at, ended_at, source_id, status, error_message, run_id`
  - `FetchLog`: `id, url, status_code, latency_ms, bytes_received, error_code, created_at, run_id`

### 0.2 - Minimum Pipeline Interface
- **Contract**: `discover(seed) -> Iterator[URL]` → `fetch(URL) -> bytes` → `parse(bytes) -> Parsed` → `extract(Parsed) -> ArticleDraft + Evidence[]` → `store(ArticleDraft) -> Article` → `export() -> JSONL`
- **Immutable order**: No skipping stages.
- File: `core/pipeline.py`

### 0.3 - Fixtures + Contract Tests
- **Test file**: `tests/contract/test_schema_compliance.py`
- **Tests**:
  1. Export JSON must match `article.schema.json` (mandatory fields + no extra `body`)
  2. Every evidence element must satisfy `evidence.schema.json`
  3. claim_path must exist in article
  4. Dedupe test: same (canonical_url, source_id) → same article (no duplicates)
- **CI green**: All contract tests pass before any feature work.

### 0.4 - Default Compliance Config
- **File**: `core/config.py`
- **Defaults** (immutable in v0):
  ```python
  max_global_concurrency = 1  # v0: no parallelism
  per_domain_delay_seconds = 5  # min gap between requests
  robots_check = REQUIRED  # not optional
  snippet_max_chars = 1500  # v0 conservative, never full text
  evidence_snippet_max_chars = 800  # shorter for evidence
  max_body_bytes_by_type = {  # content-type aware
    "text/html": 5_000_000,  # 5MB
    "application/json": 2_000_000,  # 2MB
    "application/pdf": 0,  # PDFs not fetched
    ...
  }
  fetch_timeout_seconds = 30
  max_redirects = 5
  blocked_protocols = ["file", "gopher", ...]  # only http(s)
  blocked_ip_ranges = ["127.0.0.1/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "::1/128", "fe80::/10", "fc00::/7", ...]  # SSRF + IPv6
  ```
- **Rationale**: All defensible in docs → no accidents from "someone changed concurrency to 100".

### Acceptance Criteria
- [x] All Pydantic models defined & validated
- [x] Both `.schema.json` files exist, are valid, and loadable in code
- [x] Contract tests run in CI, all pass
- [x] Empty connector (no URLs) produces valid JSONL export with zero rows
- [x] README documents the pipeline flow + "Why no body field" + "Why robots is mandatory"

---

## Milestone 1: Fetcher Compliance Foundation

**Goal**: Network layer is "safe, rate-limited, observable" — connector just produces URLs.
**Status**: `[x] Completed (Fetcher Foundation Landed)`

**Delta from M0**: Fetch interface upgraded from `fetch(URL) -> bytes` to `fetch(URL) -> FetchedDoc` (includes status, headers, final_url, body for caching and 304 support).

### 1.1 - Robots.txt Parser + Cache
- **File**: `fetcher/robots.py`
- **Behavior**:
  - Parse robots.txt at domain discovery time
  - Cache result (in-memory dict or SQLite, configurable)
  - **Disallow decision**: If robots.txt explicitly disallows URL → block fetch, log as `error_code = BLOCKED_BY_ROBOTS`
  - **Cannot be disabled** in v0 config (no `--ignore-robots` flag)
- **Failure handling strategy** (important distinction):
  - No robots.txt → **allow** (site opted out of robots protocol)
  - robots.txt fetch **404** or **timeout** → **allow** + **log warning** (conservative but practical; avoids false negatives)
  - robots.txt fetch **5xx** → **allow** + **decrease rate** (temporary service error; be extra polite)
  - Redirect chain to robots.txt → follow up to `max_redirects`, then treat as timeout (allow + log)
  - **Rationale**: v0 assumes good faith. Site temporarily down shouldn't block discovery; slower rate is safer.
- **Detailed Failure Handling Flowchart**:
  ```
  For each domain D discovered:
    1. Check cache[D] → if hit, return cached result (instant)
    2. Resolve D → get IPv4/IPv6 addresses
       - If private IP (127.0.0.1/8, 10.0.0.0/8, etc.) → SSRF block (never try robots)
    3. Fetch robots.txt@D:
       HEAD /robots.txt (30s timeout, follow up to 5 redirects, same-protocol preferred)

    ON SUCCESS (200):
      - Parse robots.txt with `urllib.robotparser`
      - For user-agent "author-collector" or "*", extract Disallow rules
      - Cache result (TTL: 1 hour, or configurable per domain)
      - For each URL in discovery, check: robotparser.can_fetch(user_agent, url) → true = allow, false = block

    ON 404 (robots.txt missing):
      - Assume site doesn't use robots protocol → ALLOW
      - Log warning: "robots.txt not found for {domain}; assuming robots protocol not enforced"
      - Cache as "allow_all" (TTL: 4 hours, less aggressive than 200 case)
      - Continue fetching domain URLs

    ON 5xx (server error):
      - Site temporarily unavailable → ALLOW (but stay conservative)
      - Log warning: "robots.txt returned {status_code} for {domain}; allowing with reduced rate"
      - Cache as "allow_with_caution" (TTL: 15 min, short TTL for recovery)
      - Increase per_domain_delay_seconds by 2x for this domain (temporary politeness boost)
      - Continue fetching domain URLs

    ON TIMEOUT (>30s with no response):
      - Network issue or overloaded server → ALLOW
      - Log warning: "robots.txt request timeout for {domain}; assuming protocol not enforced"
      - Cache as "allow_all" (TTL: 1 hour)
      - Continue fetching domain URLs

    ON REDIRECT LOOP (>5 hops):
      - Treat as timeout (robots.txt unreachable) → ALLOW
      - Log warning: "robots.txt redirect loop for {domain}"
      - Cache as "allow_all" (TTL: 1 hour)
  ```
- **Rate Decrease Algorithm**:
  - Normal rate: `per_domain_delay_seconds = 5`
  - On robots.txt 5xx: multiply by 2x → `10 seconds`
  - On robots.txt disallow: `5 seconds` (standard, just respect rule)
  - Reset to normal when: domain recovers, next hourly refresh cache check
- **Cache Eviction**:
  - Successful parse: 1 hour TTL
  - 404 (no robots): 4 hours TTL (less frequent re-check)
  - 5xx or timeout: 15 min TTL (quick recovery window)
  - Manual flush: `config.robot_cache_flush_hours` (default: none, infinite until app restart)

### 1.2 - Per-Domain Rate Limiting + Global Concurrency
- **File**: `fetcher/politeness.py`
- **Behavior**:
  - Per-domain: track `last_fetch_time[domain]`, enforce `per_domain_delay_seconds`
  - Global: semaphore with `max_global_concurrency = 1` (no parallelism in v0)
  - Enqueue requests per domain, serialize globally
  - Log: `fetch_log.created_at`, `fetch_log.latency_ms`
- **Integration**: HTTP fetcher uses this before each request.

### 1.3 - Fetch Security Constraints
- **File**: `fetcher/http.py`
- **Constraints**:
  1. **Protocol whitelist**: Only `http://` and `https://`
  2. **IP blocklist**: Reject private IPs (127.0.0.1, 10.x, 172.16-31.x, 192.168.x.x) via DNS check before fetch
  3. **Redirect limits**: max 5 hops, each must be http(s), same-protocol preferred
  4. **Body size limit**: `max_body_bytes = 10MB` (configurable per content-type)
  5. **Timeout**: 30 seconds per fetch
  6. **User-Agent**: Set a descriptive one (e.g., `author-collector/0.1 (+https://github.com/.../README)`)
- **Error handling**:
  - SSRF attempt → `error_code = SECURITY_BLOCKED` in `fetch_log`
  - Timeout → `error_code = TIMEOUT`
  - 404 → `status_code = 404` (normal, not error)
  - Unknown error → `error_code = FETCH_ERROR`

### 1.4 - Observability: Structured Logs + fetch_log Table
- **File**: `fetcher/logging.py`, `storage/sqlite.py` (add table)
- **fetch_log schema**:
  ```sql
  CREATE TABLE fetch_log (
    id TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    status_code INT,
    latency_ms INT,
    bytes_received INT,
    error_code TEXT,  -- NULL | TIMEOUT | SECURITY_BLOCKED | FETCH_ERROR | BLOCKED_BY_ROBOTS
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    run_id TEXT NOT NULL,
    FOREIGN KEY(run_id) REFERENCES run_log(id)
  );
  ```
- **Logs**: Structured JSON to stdout (timestamp, url, status, latency, run_id, error_code).

### Acceptance Criteria
- [x] Integration tests: 200, 304, robots-disallow, timeout all log correctly
- [x] SSRF blocklist tests pass (localhost, 10.0.0.1, 127.0.0.1, 192.168.1.1)
- [x] Redirect limit enforced (5 → pass, 6 → blocked)
- [x] fetch_log table populated correctly during runs
- [x] Robots cache works (second call to same domain is instant)

---

## Milestone 2: Parser + Extractor

**Goal**: Parse HTML → readable text (no full body storage); extract title/date/author hints + exact evidence links.
**Status**: `[x] Completed (Parser + Extractor Landed)`

### 2.1 - HTML → Readable Text
- **File**: `parser/html.py`
- **Library**: `trafilatura` (preferred, lighter than readability; can fallback to `readability`)
- **Behavior**:
  - Extract main content + metadata (title, date, author from tags)
  - Return `Parsed` object with: `text, title, date, author_hints, html_title, meta_tags`
  - **Do NOT save full text** — only extract fields needed for article + snippet
- **Snippet generation**:
  - First 1500 chars of readable text (v0 conservative cap)
  - Truncate mid-word → add "…"

### 2.2 - JSON-LD + Meta Tag Extraction
- **File**: `parser/jsonld.py`
- **What to extract**:
  - JSON-LD: author, datePublished, headline, description, url, image, publisher
  - Meta: og:title, og:image, og:url, description, author, article:published_time
  - Canonical: link[rel=canonical] href
- **Output**: Each extracted field paired with its source (JSON-LD vs meta vs text).

### 2.3 - Extract → ArticleDraft + Evidence[]
- **File**: `extractor/article.py`
- **Behavior**:
  - Input: `Parsed` (from parser)
  - Output: `ArticleDraft { title, author_hint, published_at, snippet, source_url }` + `Evidence[]`
  - **Evidence generation rule**:
    - `title` from JSON-LD → evidence with `claim_path=/title, evidence_type=json_ld`
    - `title` fallback to meta og:title → evidence with `claim_path=/title, evidence_type=meta_tag`
    - etc. for author_hint, published_at
  - **Key constraint**: Every required field must have ≥1 evidence entry (or field is null + warning logged)
- **Deterministic**: Same input HTML → same output (for regression tests).

### 2.4 - Snippet Truncation + Optional PII Scrubbing
- **File**: `extractor/pii.py` (optional)
- **Snippet truncation**: 1500 chars max (see 2.1)
- **PII scrubbing** (opt-in, default off for v0):
  - Regex patterns for email, phone, SSN placeholders (not removal, just masking in snippet)
  - Config: `enable_pii_scrub = False` (don't do it in v0, it's nice-to-have)

### Acceptance Criteria
- [x] Three fixture sets (normal/edge/malformed HTML) produce stable output
- [x] Evidence counts match claims (no orphaned evidence)
- [x] Snippet always ≤1500 chars
- [x] Same input HTML → same ArticleDraft + Evidence (deterministic)
- [x] Regression tests lock down key edge cases (missing title, malformed JSON-LD, etc.)

---

## Milestone 3: Storage + Dedup + Versioning + Export

**Goal**: Idempotent upsert, exact dedup, minimal versioning, JSONL export.
**Status**: `[x] Completed (Storage + Export + Rollback Landed)`

### 3.1 - SQLite Schema + Migrations
- **File**: `storage/migrations/0001_init.sql`
- **Tables** (see detailed schema in `sqlite_schema.sql`):
  - `articles`: Main content (canonical_url + source_id = unique key)
  - `evidence`: Evidence links (article_id → evidence records)
  - `versions`: Version history (article_id, version, content_hash, created_at, run_id)
  - `accounts`: Author accounts (for identity resolution)
  - `authors`: Resolved author identities (canonical)
  - `fetch_log`: Request logs
  - `run_log`: Execution logs
  - `merge_decisions`: (for v0 identity resolution review)
- **Constraint**: No backward-incompatible migrations in v0 (only append new columns if needed).

### 3.2 - URL Canonicalization + Dedup Key
- **File**: `quality/urlnorm.py`
- **Canonicalization rules**:
  - Lowercase domain, path
  - Remove fragment (#...)
  - Sort query params
  - Remove utm_* params, session IDs (configurable list)
  - Prefer https over http
- **Dedup key**: `(canonical_url, source_id)` → UNIQUE constraint in DB
- **Effect**: Same URL from same source → upsert (update), not insert (no duplicates).

### 3.3 - Minimal Versioning
- **Strategy**:
  - Track content hash of `{title, author_hint, snippet, published_at}`
  - On upsert: if hash differs from last version → insert new `versions` row
  - Update `articles.version` counter
  - **Keep old versions** (immutable history for debugging/rollback)
- **Table: `versions`**:
  ```sql
  CREATE TABLE versions (
    id TEXT PRIMARY KEY,
    article_id TEXT NOT NULL,
    version INT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    run_id TEXT NOT NULL,
    FOREIGN KEY(article_id) REFERENCES articles(id),
    FOREIGN KEY(run_id) REFERENCES run_log(id),
    UNIQUE(article_id, version)
  );
  ```

### 3.4 - Export: JSONL (Schema Validated)
- **File**: `author_collector/cli.py` (subcommand: `author-collector export`)
- **Behavior**:
  - Query `articles` + `evidence` joins
  - Serialize as JSONL (one article per line)
  - **Validate each row** against `article.schema.json` before output
  - Fail export if any row invalid (prevents corrupt data flowing downstream)
- **Output sample**:
  ```jsonl
  {"id":"...", "canonical_url":"...", "title":"...", ..., "evidence":[...], "version":1}
  {"id":"...", "canonical_url":"...", "title":"...", ..., "evidence":[...], "version":2}
  ```

### Acceptance Criteria
- [x] Multiple sync runs don't create duplicates (dedup test)
- [x] Content change produces new version record (versioning test)
- [x] Export validates all rows against schema (schema compliance test)
- [x] Migrations run cleanly from fresh DB → populated state
- [x] rollback --run <id> works (see Milestone 5 + ROLLBACK.md)

---

## Milestone 4: Three Friendly Connectors

**Goal**: RSS / HTML author page / arXiv all run end-to-end independently.
**Status**: `[x] Completed (RSS + HTML Author Page + arXiv Landed)`

### 4.1 - RSS Connector
- **File**: `connectors/rss.py`
- **Behavior**:
  - Input: RSS feed URL (seed)
  - Discover: Parse RSS → iterator of (article_url, title, published_date, author_name)
  - Each item becomes URL → `fetch` → `parse` → `extract` → `store`
- **Fixtures**: `tests/fixtures/rss/example.xml` (minimal valid RSS)
- **Integration test**: `test_rss_connector_e2e` → sync from fixture feed → verify 3-5 articles in DB.

### 4.2 - HTML Author Page Connector
- **File**: `connectors/html_author_page.py`
- **Behavior**:
  - Seed: HTML page with author's articles (e.g., `/articles` or `/publications` on author site)
  - Discover: Parse page → scrape article links + titles + dates
  - Each link → normal pipeline
  - **Only list discovery**, no deep crawl (1 page only)
- **Fixtures**: `tests/fixtures/html/author_page.html`
- **Integration test**: Parse fixture → discover 5+ article URLs.

### 4.3 - arXiv Connector
- **File**: `connectors/arxiv.py`
- **Behavior**:
  - **Final v0 position**: arXiv works as a **URL discoverer** under the immutable pipeline (discover → fetch → parse → extract → store → export)
  - Seed: Author ID or search query (`search_query`) OR direct Atom URL/file
  - Discover: Parse Atom and emit non-PDF `abs` URLs (deduped)
  - Fetch PDF? **No** — PDF links are filtered at discovery; only `abs` pages enter pipeline
  - Note: “Atom metadata direct-store mode” is deferred to a future milestone to avoid stage-skipping in v0
- **Fixtures**: `tests/fixtures/arxiv/response.atom` (mock arXiv Atom response)
- **Integration test**: Query fixture → discover papers → store in DB.

### 4.4 - Connector Integration Tests
- **Each connector test**:
  - Setup: clear DB or temp DB
  - Run: `sync --source-id rss:example_feed` (or similar)
  - Assert: article count, schema compliance, fetch_log entries
  - Cleanup: isolated test DB
- **Failure isolation**: If RSS fails mid-sync, other connectors unaffected (no cascade).

### Acceptance Criteria
- [x] Each connector has working fixtures
- [x] Each connector runs sync end-to-end (discover → fetch → parse → extract → store → export)
- [x] Each produces valid JSONL export
- [x] Integration tests: all three connectors pass independently
- [x] One connector failure doesn't halt entire sync

---

## Milestone 5: Identity Resolution v0 (Manual Review, No Auto-Merge)

**Goal**: "Human-in-the-loop" identity merging — candidates identified, human decides.
**Status**: `[x] Completed (Manual Review Loop Landed)`

### 5.1 - Candidate Scoring (Rule-Based, No Auto-Merge)
- **File**: `resolution/scoring.py`
- **Scoring Rules** (cumulative, max 1.0):

  **Rule 1: Exact Account Match**
  - Same email/social URL from different sources → +1.0 (highest confidence, one-time auto-allow in review, but NO auto-merge)
  - Evidence: explicit account matching (e.g., Twitter @user appears in multiple articles)

  **Rule 2: Same Domain + Profile Link**
  - Same canonical domain + explicit profile/biography page link → +0.9
  - Evidence: both authors found on same domain (e.g., blog.example.com/author/jane and blog.example.com/people/jane)

  **Rule 3: Exact Name Match + Same Domain**
  - Name(A) == Name(B) AND domain(A) == domain(B) → +0.8
  - Evidence: canonical_url domain must be identical (e.g., both published on `techblog.com`)

  **Rule 4: Similar Name (Normalized Levenshtein) + Same Domain**
  - normalized_distance(Name(A), Name(B)) ≤ 0.15 AND same domain → +0.6
  - **Normalized Levenshtein formula**: `distance = levenshtein_edit_distance(a, b) / max(len(a), len(b))`
  - Example: "Jane Doe" vs "Jane Do" → distance = 1/8 = 0.125 < 0.15 ✓
  - Example: "Jane Doe" vs "John Smith" → distance = 7/9 = 0.78 > 0.15 ✗
  - Evidence: name similarity + same publishing domain

  **Rule 5: Same Domain Only (Weak Signal)**
  - Same published domain, different names (no similarity) → +0.3
  - Low confidence, informational only
  - Evidence: both articles from same site, names differ

  **Review Queue Thresholds** (v0 conservative):
  - Score ≥ 0.75: HIGH confidence → appears in review queue, flagged for quick approval
  - Score 0.5-0.74: MEDIUM confidence → appears in review queue, requires careful review
  - Score < 0.5: NOT SHOWN in review queue (too speculative)

  **Key Constraint**: Score 1.0 does NOT auto-merge in v0. All merges require explicit human `accept` in review queue.
  - Rationale: Even "1.0" matches could be accidental duplicates (different people with same email, etc.)
  - v0 design: human is the ultimate arbiter; system suggests, human decides

- **Output**: `Candidate { from_author, to_author, score, scoring_breakdown, evidence }`
  - `scoring_breakdown`: dict of rule name → contribution (for transparency in review UI)
  - `evidence`: list of supporting facts (e.g., ["email match", "same domain"])

### 5.2 - Review Queue Output
- **File**: `resolution/scoring.py`, `author_collector/cli.py` (`review-queue`)
- **Behavior**:
  - Run: `author-collector review-queue`
  - Output: `review.json` with all candidates (score ≥ 0.6)
  - **Human reviews** & edits (`accept`, `reject`, `hold`)
  - Save edited file
- **Output structure**:
  ```json
  {
    "candidates": [
      {
        "id": "merge_123",
        "from_author": {...},
        "to_author": {...},
        "score": 0.8,
        "evidence": [...],
        "decision": null  // human sets to "accept" | "reject" | "hold"
      }
    ]
  }
  ```

### 5.3 - CLI Apply Review Decisions
- **Command**: `author-collector review apply review.json`
- **Behavior**:
  - Read review.json
  - For each "accept": `merge_author(from_id, to_id)` → creates `merge_decisions` record
  - For each "reject": log, skip
  - Replay-safe: same candidate ID is idempotent (no duplicate merge_decisions rows)
  - **Reversible**: All merges logged with evidence + can be undone (see ROLLBACK.md)

### Acceptance Criteria
- [x] Same-name candidates appear in review queue
- [x] No auto-merges (score < 1.0 never auto-applies)
- [x] Review queue editable, replayable
- [x] `review apply` creates merge_decisions records
- [x] Merges are undoable (rollback-friendly)

---

## Cross-Milestone Criteria (All Phases)

- [x] All code has docstrings (no excessive comments)
- [x] CI: contract tests, unit tests, integration tests all green
- [x] No panics/unhandled exceptions (graceful degradation)
- [x] Logs (stdout JSON) include `run_id` for traceability
- [x] README updated w/ compliance philosophy + "Why no body field" + "Why robots mandatory"
- [x] Migration path documented (if upgrading DB schema later)

---

## Test Strategy (Across All Milestones)

- **Contract tests**: Schema compliance (Milestone 0, then preserved)
- **Unit tests**: Each module (parser, extractor, robots, etc.)
- **Integration tests**: Full pipeline per connector (Milestone 4)
- **Regression fixtures**: Lock down key inputs (HTML samples, RSS feeds, etc.)
- **CI green**: All tests pass before merge to main

---

## Known Risks & Mitigations (See ROLLBACK.md for details)

| Risk | Mitigation |
|------|-----------|
| Excessive crawling (legal/reputation) | Snippet limit + robots mandatory + global concurrency=1 |
| SSRF/internal network probe | IP blocklist + protocol whitelist + DNS validation |
| Malicious plugins/code execution | Deny list (no playwright/selenium), static parsing only |
| Accidental auto-merge of wrong authors | v0 = manual review only, no auto-merge |
| Data drift / duplicate accumulation | Versioning + canonicalization + per-run rollback |
| Unrecoverable data corruption | Per-run tracking (run_id) + snapshots |

---

## Timeline Guidance (Removed as Per Instructions)

No time estimates provided. Focus on per-milestone acceptance criteria. Prioritize Milestone 0 (contract) → Milestone 1 (fetcher safety) before any connector work.

---

## Next Steps

1. Review this roadmap with stakeholders
2. Confirm milestone priorities (OK to skip Milestone 4 or 5 if needed)
3. Generate detailed SQL schema (see `sqlite_schema.sql`)
4. Begin Milestone 0 implementation (models + contract tests)
