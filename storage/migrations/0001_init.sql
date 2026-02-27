-- author-collector v0 SQLite Schema
--
-- Design principles:
-- 1. run_id: Every mutation is tagged with run_id for per-run rollback
-- 2. Versioning: Content changes tracked in versions table
-- 3. Evidence: Every claim (title, author, etc.) has traceability to source
-- 4. Dedup: (canonical_url, source_id) is UNIQUE constraint (dedup key)
-- 5. Merge audit: All author merges logged with evidence + rollbackable
--
-- KEY DESIGN DECISION: Article Primary Key
-- - TABLE KEY: id (UUID, global unique identifier)
-- - DEDUP KEY: (canonical_url, source_id) UNIQUE constraint
-- - RATIONALE: Different sources may discover same article (same URL) independently.
--   Need both global id (for references) and source-scoped dedup (for upsert logic).
--
-- Migration history: 0001_init.sql (this file)
-- No backward-incompatible changes allowed. Only append columns/tables.

-- ============================================================================
-- Core Content Tables
-- ============================================================================

CREATE TABLE articles (
  id TEXT PRIMARY KEY,
  canonical_url TEXT NOT NULL,  -- URL after normalization (lowercase, no utm, no fragment)
  source_id TEXT NOT NULL,      -- e.g., "rss:techblog", "html:author_page", "arxiv:cs"

  title TEXT,
  author_hint TEXT,             -- unresolved author name (may have multiple articles per author_hint)
  published_at TEXT,            -- ISO8601 or NULL if unknown
  snippet TEXT,                 -- max 1500 chars, no full body (v0 conservative)

  version INT DEFAULT 1,        -- bumped when content changes
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP,

  UNIQUE(canonical_url, source_id)
) STRICT;

-- Note: no run_id here (articles are immutable keys; versions track mutations)
-- If an article is first created in run_abc and updated in run_def, we track that in versions table

CREATE INDEX idx_articles_source_id ON articles(source_id);
CREATE INDEX idx_articles_author_hint ON articles(author_hint);
CREATE INDEX idx_articles_published_at ON articles(published_at);


-- ============================================================================
-- Evidence Table
-- Every claim in an article is backed by evidence
-- ============================================================================

CREATE TABLE evidence (
  id TEXT PRIMARY KEY,
  article_id TEXT NOT NULL,

  -- claim_path: JSON Pointer (RFC 6901) to the claim in article
  -- Examples: "/title", "/author_hint", "/published_at"
  -- Must be one of the valid pointers; enforced by application layer
  claim_path TEXT NOT NULL,

  -- evidence_type: where this evidence came from
  -- - "meta_tag": <meta name="author" content="...">
  -- - "json_ld": JSON-LD structured data
  -- - "extracted": Extracted from readable text (trafilatura/readability)
  -- - "fetched_content": Raw HTML content
  evidence_type TEXT NOT NULL CHECK(evidence_type IN ('meta_tag', 'json_ld', 'extracted', 'fetched_content')),

  source_url TEXT NOT NULL,     -- where this evidence came from (article URL)
  extraction_method TEXT,       -- e.g., "trafilatura", "meta_og:title", "json_ld_headline"

  extracted_text TEXT NOT NULL,          -- the evidence content (snippet, max 800 chars)
  confidence REAL DEFAULT 1.0,  -- 0.0-1.0 confidence (not critical in v0, but structure it)

  metadata TEXT,                -- JSON: any extra context (e.g., {"selector": "og:author", "tag": "meta"})

  -- Replay/audit fields for reproducibility
  retrieved_at TEXT NOT NULL,  -- when was this evidence collected?
  extractor_version TEXT,      -- e.g., "trafilatura@1.9.0", "jsonld@1.0"
  input_ref TEXT,              -- e.g., CSS selector, JSON-LD path, meta name
  snippet_max_chars_applied INTEGER,  -- truncation limit at extraction time

  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  run_id TEXT NOT NULL,         -- key for rollback: which run added this evidence?

  FOREIGN KEY(article_id) REFERENCES articles(id),
  FOREIGN KEY(run_id) REFERENCES run_log(id)
) STRICT;

CREATE INDEX idx_evidence_article_id ON evidence(article_id);
CREATE INDEX idx_evidence_claim_path ON evidence(claim_path);
CREATE INDEX idx_evidence_run_id ON evidence(run_id);


-- ============================================================================
-- Versioning Table
-- Tracks content mutations (same article, different versions)
-- ============================================================================

CREATE TABLE versions (
  id TEXT PRIMARY KEY,
  article_id TEXT NOT NULL,
  version INT NOT NULL,

  -- content_hash: SHA256 of {title, author_hint, snippet, published_at}
  -- helps detect actual changes vs. metadata-only updates
  content_hash TEXT NOT NULL,

  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  run_id TEXT NOT NULL,  -- which run triggered this version bump?

  FOREIGN KEY(article_id) REFERENCES articles(id),
  FOREIGN KEY(run_id) REFERENCES run_log(id),
  UNIQUE(article_id, version)
) STRICT;

CREATE INDEX idx_versions_article_id ON versions(article_id);
CREATE INDEX idx_versions_run_id ON versions(run_id);


-- ============================================================================
-- Execution Logs (Observability)
-- ============================================================================

CREATE TABLE run_log (
  id TEXT PRIMARY KEY,  -- UUID for this entire run
  source_id TEXT NOT NULL,  -- which connector/source triggered this run?
  started_at TEXT DEFAULT CURRENT_TIMESTAMP,
  ended_at TEXT,  -- NULL if still running or failed

  status TEXT CHECK(status IN ('RUNNING', 'COMPLETED', 'FAILED', 'CANCELLED')) DEFAULT 'RUNNING',
  error_message TEXT,  -- if status=FAILED, why?

  -- summary stats
  fetched_count INT DEFAULT 0,       -- how many URLs fetched?
  new_articles_count INT DEFAULT 0,  -- how many new articles created?
  updated_articles_count INT DEFAULT 0,  -- how many articles updated?
  error_count INT DEFAULT 0          -- how many fetch/parse errors?
) STRICT;

CREATE INDEX idx_run_log_source_id ON run_log(source_id);
CREATE INDEX idx_run_log_started_at ON run_log(started_at);


CREATE TABLE fetch_log (
  id TEXT PRIMARY KEY,
  url TEXT NOT NULL,

  status_code INT,  -- HTTP status (200, 404, 500, etc.) or NULL if no response
  latency_ms INT,   -- time to response in milliseconds
  bytes_received INT,  -- how many bytes downloaded?

  -- error_code: why did this fail?
  -- - NULL: success (200/204/304)
  -- - "TIMEOUT": took > max_timeout_seconds
  -- - "SECURITY_BLOCKED": IP blocklist hit, SSRF prevented, etc.
  -- - "FETCH_ERROR": network error, connection refused, etc.
  -- - "BLOCKED_BY_ROBOTS": robots.txt disallowed
  -- - "BODY_TOO_LARGE": exceeded max_body_bytes
  -- - "REDIRECT_LIMIT": too many redirects (> max_redirects)
  error_code TEXT,

  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  run_id TEXT NOT NULL,

  FOREIGN KEY(run_id) REFERENCES run_log(id)
) STRICT;

CREATE INDEX idx_fetch_log_url ON fetch_log(url);
CREATE INDEX idx_fetch_log_run_id ON fetch_log(run_id);
CREATE INDEX idx_fetch_log_error_code ON fetch_log(error_code);


-- ============================================================================
-- Identity Resolution (Author Deduplication)
-- Minimal v0 structure: no automatic merging, only manual review
-- ============================================================================

-- accounts: discovered author accounts (one per {source, identifier})
-- e.g., (source="rss:techblog", identifier="john@example.com")
--    or (source="arxiv", identifier="smith.j.42")
CREATE TABLE accounts (
  id TEXT PRIMARY KEY,
  author_id TEXT,  -- references authors(id), initially NULL (unresolved)

  source_id TEXT NOT NULL,      -- e.g., "rss:techblog-author-field"
  source_identifier TEXT NOT NULL,  -- account name/email/handle in that source

  created_at TEXT DEFAULT CURRENT_TIMESTAMP,

  FOREIGN KEY(author_id) REFERENCES authors(id)
) STRICT;

CREATE INDEX idx_accounts_author_id ON accounts(author_id);
CREATE INDEX idx_accounts_source_id ON accounts(source_id);
CREATE UNIQUE INDEX idx_accounts_source_identifier ON accounts(source_id, source_identifier);


-- authors: canonical author identity (resolved)
-- created when human decides to merge accounts
CREATE TABLE authors (
  id TEXT PRIMARY KEY,
  canonical_name TEXT NOT NULL,
  metadata TEXT,  -- JSON: any extra info about this author (affiliation, bio, etc.)

  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP
) STRICT;


-- merge_decisions: audit trail of author merges
-- every merge is logged here + can be rolled back
CREATE TABLE merge_decisions (
  id TEXT PRIMARY KEY,
  from_author_id TEXT NOT NULL,  -- losing author (might be NULL, meaning "create new")
  to_author_id TEXT NOT NULL,    -- winning author

  -- which evidence + criteria justified this merge?
  evidence_ids TEXT NOT NULL,  -- JSON array of evidence IDs (optional, for audit)
  decision_criteria TEXT,       -- human explanation or rule name

  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  created_by TEXT,  -- who approved this merge? (user ID or "system", etc.)
  run_id TEXT NOT NULL,         -- which review/merge run created this?

  -- optional: revert column
  reverted_at TEXT,  -- NULL if active, timestamp if reverted
  reverted_by TEXT,
  reverted_reason TEXT,

  FOREIGN KEY(from_author_id) REFERENCES authors(id),
  FOREIGN KEY(to_author_id) REFERENCES authors(id),
  FOREIGN KEY(run_id) REFERENCES run_log(id)
) STRICT;

CREATE INDEX idx_merge_decisions_from_author_id ON merge_decisions(from_author_id);
CREATE INDEX idx_merge_decisions_to_author_id ON merge_decisions(to_author_id);
CREATE INDEX idx_merge_decisions_run_id ON merge_decisions(run_id);


-- ============================================================================
-- Configuration / Metadata
-- ============================================================================

CREATE TABLE config (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,  -- JSON or scalar, depending on key
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP
) STRICT;

-- example rows (populated by app on init):
-- key="version", value="0.1"
-- key="last_full_sync", value="2025-02-27T10:00:00Z"
-- etc.


-- ============================================================================
-- Indexes for Common Queries
-- ============================================================================

-- Find all articles for a given author_hint
CREATE INDEX idx_articles_author_hint_source ON articles(author_hint, source_id);

-- Articles by date (for incremental syncs)
CREATE INDEX idx_articles_published_at_source ON articles(published_at, source_id);

-- Find articles created/updated in a specific time window (for debugging)
CREATE INDEX idx_articles_updated_at ON articles(updated_at);

-- Evidence lookup by article + claim
CREATE INDEX idx_evidence_article_claim ON evidence(article_id, claim_path);

-- Fetch logs by status (for per-domain rate limiting / robots checking)
CREATE INDEX idx_fetch_log_status_created ON fetch_log(status_code, created_at);


-- ============================================================================
-- Pragma Settings (Performance + Safety)
-- ============================================================================

PRAGMA journal_mode = WAL;      -- Write-Ahead Logging (safer for concurrent reads)
PRAGMA synchronous = NORMAL;    -- Safer than FULL, faster than standard
PRAGMA foreign_keys = ON;       -- Enforce foreign key constraints
PRAGMA temp_store = MEMORY;     -- Temp tables in RAM


-- ============================================================================
-- Views (convenience queries)
-- ============================================================================

-- Articles with latest evidence
CREATE VIEW articles_with_evidence AS
SELECT
  a.id,
  a.canonical_url,
  a.source_id,
  a.title,
  a.author_hint,
  a.published_at,
  a.snippet,
  a.version,
  a.created_at,
  a.updated_at,
  json_group_array(
    json_object(
      'claim_path', e.claim_path,
      'evidence_type', e.evidence_type,
      'extracted_text', e.extracted_text
    )
  ) AS evidence
FROM articles a
LEFT JOIN evidence e ON a.id = e.article_id
GROUP BY a.id;


-- Run summary stats
CREATE VIEW run_summary AS
SELECT
  rl.id,
  rl.source_id,
  rl.status,
  rl.started_at,
  rl.ended_at,
  datetime(rl.ended_at, 'subday') - datetime(rl.started_at, 'subday') AS duration_seconds,
  COUNT(DISTINCT fl.url) AS fetch_count,
  COUNT(DISTINCT CASE WHEN fl.error_code IS NULL THEN fl.url END) AS fetch_success_count,
  COUNT(DISTINCT fl.error_code) AS fetch_error_types,
  COUNT(DISTINCT a.id) AS article_count
FROM run_log rl
LEFT JOIN fetch_log fl ON rl.id = fl.run_id
LEFT JOIN evidence e ON e.run_id = rl.id
LEFT JOIN articles a ON a.id = e.article_id
GROUP BY rl.id;


-- Active authors (not reverted merges)
CREATE VIEW active_merge_decisions AS
SELECT * FROM merge_decisions WHERE reverted_at IS NULL;


-- ============================================================================
-- End of Schema
-- ============================================================================
-- Generated by: author-collector v0 roadmap
-- Date: 2025-02-27
-- No backward-incompatible changes allowed in this migration file.
-- Future migrations (0002, 0003, etc.) must be additive only.
