# ROLLBACK.md: Operational Recovery Playbook

This document describes how to **identify, prevent, and recover from incidents** in author-collector. All strategies depend on `run_id` tracking and immutable audit trails.

---

## Core Rollback Principles

1. **Every mutation is tagged with `run_id`** — All writes to `articles`, `evidence`, `versions`, `merge_decisions` MUST include `run_id`.
2. **Nothing is auto-merged** — v0 policy: no automatic author identity resolution.
3. **All destructive ops are audit-logged** — Before deletion/merge, record decision in database.
4. **Recovery is reproducible** — Rollback commands are idempotent; can re-run safely.

---

## Incident Types & Recovery

### Incident 1: Over-Fetching (Crawling Too Aggressively)

**Symptom**: Suddenly fetched 10,000 URLs from a single domain; site blocked us; legal complaint risk.

**Prevention** (built into design):
- `max_global_concurrency = 1` (no parallelism)
- `per_domain_delay_seconds = 5` (minimum gap)
- `robots_check = REQUIRED` (can't be disabled)
- Each connector has per-run URL discovery limits (configurable)

**Recovery Procedure**:

```bash
# 1. Stop current run immediately
# (Ctrl+C or kill process)

# 2. Identify the problematic run
$ author-collector list-runs
# Output:
# run_id | source_id | status   | fetched_count | started_at
# abc123 | rss:example | FAILED | 10000 | 2025-02-27T10:05:00Z

# 3. Inspect what was fetched
$ author-collector inspect-run --run-id abc123 --show-urls | head -20
# Shows sample of URLs fetched in that run

# 4. Check live status (optional, if site has monitoring)
# Visit site, check robots.txt, check for IP ban symptoms

# 5. Rollback the run
$ author-collector rollback --run abc123 --verbose
# - Deletes evidence records for run abc123
# - Deletes version records for run abc123
# - Reverts articles to their pre-abc123 state (or deletes if newly created in abc123)
# - Output: "Rolled back 10000 fetch_log entries, 10000 evidence entries, 5000 versions"

# 6. Optionally blocklist the source/domain
$ author-collector config update blocklist --add-domain example.com
# OR disable the connector:
$ author-collector config update --disable-source rss:example

# 7. Re-run with corrected config
$ author-collector sync --source-id rss:example  # now with robots enforced, slower
```

**Rationale**: `run_id` tracking makes it trivial to unpick one bad run without affecting others.

---

### Incident 2: SSRF / Internal Network Probe

**Symptom**: Logs show fetch attempts to `http://169.254.169.254` (AWS metadata) or `http://localhost:5432` (internal DB).

**Prevention** (built into design):
- IP blocklist in `fetcher/http.py`: `127.0.0.1/8`, `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `169.254.0.0/16`
- Redirect chain validation: must stay http(s), no protocol downgrade
- Max redirect hops: 5

**Recovery Procedure** (if somehow bypassed):

```bash
# 1. Identify malicious request
$ grep -i "169.254\|localhost\|10\." fetch_log.json | head -5

# 2. Disable the connector that produced the malicious seed
$ author-collector config update --disable-source <source_id>

# 3. Verify no network trace occured (check server/IDS logs)
# (This is a deployment concern, not app concern)

# 4. Rollback the run
$ author-collector rollback --run <run_id>

# 5. Fix the connector or seed URL list
# (E.g., if HTML author page connector is scraping user-supplied links,
#  add URL validation before feeding to fetch stage)
```

**Note**: Prevention is better than recovery here. IP blocklist + DNS resolution validation before fetch happens automatically.

---

### Incident 3: Data Corruption / Schema Violation

**Symptom**: Export produces invalid JSON; missing required evidence fields; schema validation fails on export.

**Prevention**:
- Contract tests verify export schema on every build
- Before storage, `ArticleDraft` + `Evidence` are validated against Pydantic models
- Export validates each row before writing (fail-fast)

**Recovery Procedure**:

```bash
# 1. Identify the corrupted article
$ author-collector export --check-schema 2>&1 | grep -A2 "INVALID"
# Output: article_id=xyz: missing evidence[0].claim_path

# 2. Inspect the article in DB
$ sqlite3 collector.db "SELECT * FROM articles WHERE id='xyz'"
$ sqlite3 collector.db "SELECT * FROM evidence WHERE article_id='xyz'"

# 3. Determine which run introduced corruption
$ sqlite3 collector.db "SELECT run_id FROM evidence WHERE article_id='xyz' LIMIT 1"

# 4. Rollback that run (or broader investigation)
$ author-collector rollback --run <run_id> --verbose
# Verify article restored to valid state

# 5. If corruption is widespread, restore from snapshot
$ cp collector.db.backup collector.db
$ author-collector export  # verify valid again
```

**Alternative: Data Patching** (if rollback is too costly):

```bash
# For known minor issues (e.g., missing evidence field):
$ author-collector admin patch-evidence --article-id xyz --regenerate-from-content
# (Requires saved HTML/content in evidence or archive)
```

---

### Incident 4: Accidental Author Merge (Identity Conflicts)

**Symptom**: Two different people with same name got merged; need to unmerge.

**Prevention**:
- v0 design: NO auto-merge; all merges are manual review queue
- Merges are never automatic; require explicit `review apply`

**Recovery Procedure** (if human made mistake in review):

```bash
# 1. Identify the merge decision
$ sqlite3 collector.db "SELECT * FROM merge_decisions WHERE from_author_id='A' AND to_author_id='B'"
# Output:
# merge_id | from_author_id | to_author_id | created_at | run_id
# merge_456 | author_A | author_B | 2025-02-27T... | review_run_123

# 2. Inspect what was merged
$ author-collector inspect-merge --merge-id merge_456
# Shows: N articles re-attributed, evidence preserved

# 3. Unmerge (rollback)
$ author-collector rollback --merge merge_456 --verbose
# - Restores account relationships to pre-merge state
# - Articles revert to original author_id
# - merge_decisions row marked as "reverted" (not deleted, for audit trail)
# Output: "Reverted merge 456: 50 articles restored to author_A"

# 4. Re-review (optional, with better criteria)
$ author-collector review-queue --min-score 0.95  # higher threshold
# Edit review.json with correct decisions
$ author-collector review apply review_v2.json
```

**Key**: All merge history is preserved; unmerging is idempotent.

---

### Incident 5: Duplicate Data Accumulation

**Symptom**: Export shows 5 versions of the same article; dedup broke.

**Prevention**:
- URL canonicalization in `quality/urlnorm.py` is deterministic
- `(canonical_url, source_id)` is UNIQUE constraint in DB → enforces dedup at insert time
- Each re-sync of same URL triggers upsert, not insert

**Recovery Procedure**:

```bash
# 1. Verify dedup is broken
$ sqlite3 collector.db "SELECT canonical_url, COUNT(*) FROM articles GROUP BY canonical_url HAVING COUNT(*) > 1 LIMIT 5"

# 2. Check canonicalization logic
$ author-collector admin test-urlnorm --url "https://example.com/page?utm_source=twitter"
# Output: https://example.com/page (utm params stripped)

# 3. If canonicalization changed, re-run
$ author-collector admin recompute-canonical-urls --dry-run
# Preview what would be updated
$ author-collector admin recompute-canonical-urls --confirm
# Merges duplicates under true canonical URL

# 4. Or rollback entire session and re-run with fixed logic
$ author-collector rollback --run <affected_run> --verbose
```

---

## Rollback Data Model

All rollback operations are based on `run_id` propagation. Here's how data flows:

```
FetchLog row:
  id, url, status_code, run_id ← identifies which run produced this fetch

Evidence row:
  id, article_id, claim_path, run_id ← identifies which run added this evidence

Version row:
  id, article_id, version, content_hash, run_id ← identifies which run triggered version bump

MergeDecision row:
  id, from_author_id, to_author_id, evidence_ids, run_id ← identifies review session

Article row:
  id, canonical_url, version ← no run_id (immutable key), but version track who updated
```

**Rollback strategy**:

```
rollback --run <run_id>:
  1. Find all FetchLog rows with run_id → DELETE (cleanup logs)
  2. Find all Evidence rows with run_id → DELETE (remove evidential claims)
  3. Find all Version rows with run_id → DELETE (remove version bumps)
  4. For each Article touched by roll-back run:
     a. If created in this run → DELETE article
     b. If updated in this run → REVERT to previous version
  5. Run sanity checks (schema validation on remaining data)
  6. Log rollback event: "Rolled back run {run_id}: {count} deletions, {count} reverts"
```

---

## Rollback Command Reference

### Command: `author-collector rollback`

```bash
# Rollback entire run
$ author-collector rollback --run abc123 --verbose

# Rollback specific merge decision
$ author-collector rollback --merge merge_456 --verbose

# Dry-run (show what would be deleted)
$ author-collector rollback --run abc123 --dry-run

# Selective rollback (only evidence, keep articles)
$ author-collector rollback --run abc123 --type evidence-only
```

### Command: `author-collector inspect-run`

```bash
# Show run summary
$ author-collector inspect-run --run-id abc123
# Output:
# run_id: abc123
# source_id: rss:example
# status: COMPLETED
# started_at: 2025-02-27T10:00:00Z
# ended_at: 2025-02-27T10:15:00Z
# fetch_count: 152
# new_articles: 42
# updated_articles: 5
# evidence_count: 189
# errors: 2 (list below)

# Show URLs fetched
$ author-collector inspect-run --run-id abc123 --show-urls | head -20

# Show articles created/modified
$ author-collector inspect-run --run-id abc123 --show-articles | jq '.[] | {title, canonical_url}'
```

### Command: `author-collector list-runs`

```bash
# List all runs with summary
$ author-collector list-runs --order created_at --limit 10
# Output:
# run_id    | source_id      | status    | fetched | articles | created_at
# run_789   | rss:techblog   | COMPLETED |    250  |      45  | 2025-02-27T10:30:00Z
# run_456   | arxiv:cs       | COMPLETED |    120  |      30  | 2025-02-27T09:00:00Z
# run_123   | rss:example    | FAILED    | 10000   |    5000  | 2025-02-27T08:30:00Z (ROLLED BACK)
```

---

## Snapshot-Based Recovery (Fallback)

If per-run rollback is insufficient (e.g., corrupted DB schema), use file snapshots:

```bash
# Before any major operation, create snapshot
$ cp collector.db collector.db.pre_large_sync.2025-02-27

# After incident, restore snapshot
$ cp collector.db.pre_large_sync.2025-02-27 collector.db

# Verify restored state
$ author-collector export --check-schema
```

**Recommendation**: Automated daily snapshots via cron:

```bash
# crontab -e
0 2 * * * cp /path/to/collector.db /archive/collector.db.$(date +\%Y\%m\%d)
```

---

## Prevention Checklist

- [ ] `run_id` is always generated before any run (base UUID)
- [ ] Every INSERT/UPDATE to `articles`, `evidence`, `versions`, `fetch_log` includes `run_id`
- [ ] Before schema changes, migration test on copy of production DB
- [ ] Daily snapshots of `collector.db` (or continuous backup)
- [ ] Robots.txt enforcement is non-optional (no config knob to disable)
- [ ] IP blocklist is hardcoded, not configurable (security boundary)
- [ ] All merge decisions go through manual review (no auto-merge in v0)
- [ ] Export validates schema before writing (fail-fast if corrupt)

---

## Incident Timeline: Example Recovery

**Timeline**: `2025-02-27 10:00 UTC`

| Time | Event | Action |
|------|-------|--------|
| 10:05 | RSS connector discovers 10k URLs (bug: missing discovery limit) | Operator notices high fetch_log count |
| 10:07 | Operator kills fetch process (Ctrl+C) | Run status = FAILED, run_id = abc123 |
| 10:08 | Verify scope: `author-collector inspect-run --run-id abc123` | Shows 9k fetch_log entries, 5k potential articles |
| 10:10 | Review connector bug in git; confirm fix ready | Deploy fixed connector code |
| 10:12 | Rollback run: `author-collector rollback --run abc123 --verbose` | Deletes all entries from run abc123; articles reverted |
| 10:14 | Verify export valid: `author-collector export --check-schema` | All rows valid, count matches pre-abc123 |
| 10:16 | Re-run with fixed connector: `author-collector sync --source-id rss:example` | Discovers 42 URLs (expected), completes cleanly |

**Recovery time**: ~15 minutes. **Data loss**: None (all retained in history, run_id enables selective undo).

---

## Monitoring & Alerting (Recommended Post-v0)

```
Alerts to set up:
1. Run duration > 1 hour → anomaly (should be ~5-30 min)
2. Fetch error rate > 10% → check robots/SSRF
3. Articles created > 1000 per run → anomaly (should be 10-100)
4. Evidence count < article count → validation warning
5. Schema validation failure on export → CRITICAL
6. IP blocklist hit → informational (expected for SSRF attempts)
```

---

## Testing Rollback (Regression Tests)

In CI, add rollback tests:

```python
def test_rollback_by_run_id():
    # Create articles in run_1
    run_1 = create_run("test_1")
    article_1 = store_article("example.com/article1", run_id=run_1)

    # Create articles in run_2
    run_2 = create_run("test_2")
    article_2 = store_article("example.com/article2", run_id=run_2)

    # Rollback run_2
    assert count_articles() == 2
    rollback(run_id=run_2)

    # Verify only run_1 articles remain
    assert count_articles() == 1
    assert get_article("example.com/article1").version == 1
    assert get_article("example.com/article2") is None

def test_rollback_merge_decision():
    # Merge author A → B
    merge = merge_authors(A, B)
    assert author_A.is_merged is True

    # Rollback merge
    rollback_merge(merge.id)

    # Verify unmerged
    assert author_A.is_merged is False
    assert author_A.articles() == [article_1, article_2]  # restored
```

This ensures rollback logic is always working.

---

## Summary

- **Prevention first**: Robots mandatory, concurrency=1, snippet limits, no auto-merge
- **Every mutation is tagged**: `run_id` enables selective undo
- **Recovery is documented**: Clear procedures for each incident type
- **Rollback is tested**: CI verifies rollback logic on every commit
- **Monitoring ready**: Prepare alerts post-v0

With this design, **you can safely ship a content aggregator that respects legal/technical boundaries**.
