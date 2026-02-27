# Verification Checklist: 10-Point v0 Refinements

This document provides the **authoritative checklist** for verifying that all 10 critical refinements for v0 have been correctly implemented. Use the commands below to reproduce the verification.

## Quick Verify (All Checks in 1 Command)

```bash
# Run all verification tests
pytest tests/contract/test_schema_compliance.py tests/contract/test_alignment.py -v

# Confirm all tests pass (should show ✓ for all items)
```

---

## Fix 1-2: JSON Pointer + Evidence Audit Fields

**Checklist**:
- [ ] `claim_path` is documented as RFC 6901 JSON Pointer
- [ ] Evidence has `retrieved_at`, `extractor_version`, `input_ref`, `snippet_max_chars_applied`
- [ ] All evidence examples use `/title`, `/author_hint`, `/published_at` format
- [ ] Schema enforces JSON Pointer format

**Verification commands**:

```bash
# Check that models use JSON Pointer format
grep -n "claim_path.*JSON Pointer" core/models.py
# Expected: Line mentioning RFC 6901

# Check that audit fields exist
grep -n "retrieved_at\|extractor_version\|input_ref\|snippet_max_chars_applied" core/models.py
# Expected: All 4 fields present

# Check evidence fixtures use correct Pointer format
grep -n 'claim_path.*"/' tests/conftest.py
# Expected: See /title, /author_hint, etc. (not "title" or $.title)

# Check schema enforces pointer
grep -n "RFC 6901" schemas/evidence.schema.json
# Expected: Found (line ~17)
```

---

## Fix 3: Article Primary Key Alignment

**Checklist**:
- [ ] Article.id is PRIMARY KEY (global unique)
- [ ] (canonical_url, source_id) is UNIQUE constraint (dedup key)
- [ ] No PRIMARY KEY on canonical_url alone
- [ ] Design rationale documented in migration

**Verification commands**:

```bash
# Check DB schema has both keys
grep -A 20 "CREATE TABLE articles" storage/migrations/0001_init.sql | \
  grep "PRIMARY KEY\|UNIQUE"
# Expected:
#   id TEXT PRIMARY KEY
#   UNIQUE(canonical_url, source_id)

# Confirm no misalignment
grep -n "PRIMARY KEY.*canonical_url" storage/migrations/0001_init.sql
# Expected: No output (canonical_url is NOT primary key)

# Check design documentation
grep -n "KEY DESIGN DECISION" storage/migrations/0001_init.sql
# Expected: Found (explains the dual-key strategy)
```

**Test**:
```bash
pytest tests/contract/test_alignment.py::TestDBSchemaAlignment::test_article_has_dedup_key_not_pk -v
```

---

## Fix 4: Snippet Size Limit = 1500 Chars (Consistency)

**Checklist**:
- [ ] core/config.py: SNIPPET_MAX_CHARS = 1500
- [ ] core/models.py: Article.validate_snippet_length truncates at 1500
- [ ] schemas/article.schema.json: snippet maxLength = 1500
- [ ] Test enforces 1500 limit

**Verification commands**:

```bash
# Check config default
grep -n "SNIPPET_MAX_CHARS\s*=" core/config.py
# Expected: SNIPPET_MAX_CHARS: int = 1500

# Check model validator
grep -n "1500" core/models.py
# Expected: Multiple lines with 1500

# Check schema
grep -n "maxLength.*1500" schemas/article.schema.json
# Expected: Found (line ~35)

# Check test
grep -n "1501\|1500" tests/contract/test_schema_compliance.py
# Expected: test_article_snippet_max_length uses both limits
```

**Test**:
```bash
pytest tests/contract/test_schema_compliance.py::TestArticleSchema::test_article_snippet_max_length -v
```

---

## Fix 5: Content-Type Aware Body Limits (+ PDF = 0)

**Checklist**:
- [ ] MAX_BODY_BYTES_BY_TYPE dict exists in config
- [ ] "application/pdf" = 0 (PDFs explicitly blocked)
- [ ] "text/html" = 5_000_000 (5 MB)
- [ ] "application/json" = 2_000_000 (2 MB)
- [ ] Fallback MAX_BODY_BYTES_DEFAULT exists

**Verification commands**:

```bash
# Check content-type limits
grep -A 15 "MAX_BODY_BYTES_BY_TYPE" core/config.py
# Expected: See type-specific limits with PDF: 0

# Specifically check PDF is disabled
grep -n 'application/pdf.*0' core/config.py
# Expected: Found

# Check fallback
grep -n "MAX_BODY_BYTES_DEFAULT" core/config.py
# Expected: Found with reasonable default (e.g., 500_000)

# Verify config validation includes body byte checks
grep -n "MAX_BODY_BYTES_BY_TYPE" core/config.py | grep -i validate
# Expected: Found in validation logic
```

**Test**:
```bash
pytest tests/contract/test_alignment.py::TestComplianceConfigDefaults::test_pdf_explicitly_disabled -v
```

---

## Fix 6: FetchedDoc Interface Replaces`fetch() -> bytes`

**Checklist**:
- [ ] FetchedDoc class exists with: status_code, final_url, headers, body_bytes, body_sha256
- [ ] FetchStage.fetch() returns `tuple[Optional[FetchedDoc], FetchLog]`
- [ ] ParseStage.parse() accepts FetchedDoc (not raw bytes)
- [ ] Pipeline correctly unpacks FetchedDoc
- [ ] 304 responses have body_bytes = None

**Verification commands**:

```bash
# Check FetchedDoc definition
grep -A 10 "class FetchedDoc" core/models.py
# Expected: All required fields present

# Check fetch signature
grep -n "def fetch.*FetchedDoc" core/pipeline.py
# Expected: Returns tuple[Optional[FetchedDoc], FetchLog]

# Check parse accepts FetchedDoc
grep -n "def parse.*FetchedDoc" core/pipeline.py
# Expected: Accepts FetchedDoc parameter

# Check pipeline integration
grep -n "fetched_doc.*self.fetch" core/pipeline.py
# Expected: Pipeline unpacks and passes FetchedDoc to parse

# Check 304 handling
grep -n "status_code.*304\|body_bytes.*None" core/models.py
# Expected: 304 documented as having no body
```

**Test**:
```bash
pytest tests/contract/test_alignment.py::TestFetchedDocContract -v
```

---

## Fix 7: Robots Failure Strategy (Graceful Degradation)

**Checklist**:
- [ ] ROADMAP.md documents 4 failure modes: 200/404/5xx/timeout
- [ ] Failure handling flowchart visible in M1.1
- [ ] TTL strategy documented (1hr successful, 4hr 404, 15min 5xx, 1hr timeout)
- [ ] Rate decrease strategy documented (2x multiplier on 5xx)
- [ ] config.py enforces ROBOTS_CHECK_REQUIRED = True

**Verification commands**:

```bash
# Check ROADMAP has detailed flowchart
grep -n "Detailed Failure Handling Flowchart\|ON SUCCESS\|ON 404\|ON 5xx\|ON TIMEOUT" ROADMAP.md
# Expected: Found (lines ~120-158)

# Check config enforces robots
grep -n "ROBOTS_CHECK_REQUIRED" core/config.py
# Expected: ROBOTS_CHECK_REQUIRED: bool = True

# Check TTL strategy documented
grep -n "TTL:" ROADMAP.md
# Expected: Found with specific times (1hr, 4hr, 15min, 1hr)

# Check rate algorithm documented
grep -n "Rate Decrease\|2x" ROADMAP.md
# Expected: Found (line ~159)
```

**Test**:
```bash
pytest tests/contract/test_alignment.py::TestComplianceConfigDefaults::test_robots_check_required -v
```

---

## Fix 8: IPv6 SSRF Coverage

**Checklist**:
- [ ] BLOCKED_IP_RANGES includes IPv6 loopback: `::1/128`
- [ ] BLOCKED_IP_RANGES includes IPv6 link-local: `fe80::/10`
- [ ] BLOCKED_IP_RANGES includes IPv6 ULA: `fc00::/7`
- [ ] BLOCKED_IP_RANGES includes IPv6 multicast: `ff00::/8`
- [ ] IPv4 ranges still present (169.254, 10.0, 172.16, 192.168, etc.)

**Verification commands**:

```bash
# Check IPv6 coverage
grep -n "::1/128\|fe80::/10\|fc00::/7\|ff00::/8" core/config.py
# Expected: All 4 ranges found

# Check full blocklist
grep -A 20 "BLOCKED_IP_RANGES" core/config.py
# Expected: Mix of IPv4 and IPv6 ranges

# Count ranges
grep "^[ ]*\".*/.*/\"" core/config.py | wc -l
# Expected: >= 12 ranges (IPv4 + IPv6)
```

**Test**:
```bash
pytest tests/contract/test_alignment.py::TestComplianceConfigDefaults::test_ipv6_ssrf_coverage -v
```

---

## Fix 9: Identity Scoring Precision (5 Rules + Normalized Distance)

**Checklist**:
- [ ] ROADMAP.md M5.1 lists 5 explicit scoring rules
- [ ] Normalized Levenshtein formula documented: `distance = lev(a,b) / max(len(a), len(b))`
- [ ] Review queue thresholds documented: ≥0.75 HIGH, 0.5-0.74 MEDIUM, <0.5 hidden
- [ ] "No auto-merge" constraint explicitly stated for v0

**Verification commands**:

```bash
# Check 5 rules documented
grep -n "Rule 1:\|Rule 2:\|Rule 3:\|Rule 4:\|Rule 5:" ROADMAP.md
# Expected: All 5 rules found (lines ~406-428)

# Check Levenshtein formula
grep -n "Normalized Levenshtein\|distance.*lev" ROADMAP.md
# Expected: Found with exact formula (line ~420)

# Check thresholds
grep -n "≥0.75\|0.5-0.74\|<0.5" ROADMAP.md
# Expected: Found (line ~430)

# Check no-auto-merge emphasized
grep -n "does NOT auto-merge\|never.*auto" ROADMAP.md
# Expected: Found (line ~435)
```

---

## Fix 10: Rollback Saga Pattern (DB Layer + Documentation)

**Checklist**:
- [ ] All state tables have `run_id` column
- [ ] Indexes on `run_id` exist for fast rollback
- [ ] ROLLBACK.md documents 4-stage saga pattern
- [ ] Compensation sequence explained (reverse order)
- [ ] 3 detailed examples provided
- [ ] Run ID assignment lifecycle documented

**Verification commands**:

```bash
# Check all tables have run_id
for table in fetch_log evidence versions merge_decisions; do
  grep -c "run_id" storage/migrations/0001_init.sql | \
    xargs -I {} echo "$table has run_id: {}"
done
# Expected: All tables have run_id fields

# Check indexes
grep -c "idx.*run_id" storage/migrations/0001_init.sql
# Expected: >= 4 indexes on run_id

# Check saga pattern doc
grep -n "Saga Pattern\|Compensating Transactions\|Compensation Sequence" ROLLBACK.md
# Expected: Found (lines ~211-275)

# Check examples
grep -n "Example 1:\|Example 2:\|Example 3:" ROLLBACK.md
# Expected: All 3 examples present (lines ~280-364)

# Check lifecycle
grep -n "Timeline of a run_id" ROLLBACK.md
# Expected: Found (line ~367)
```

**Test**:
```bash
pytest tests/contract/test_alignment.py::TestDBSchemaAlignment::test_all_tables_have_run_id_for_rollback -v
```

---

## Full Verification Suite

Run all verification tests in one command:

```bash
# All schema/contract/alignment tests
pytest tests/contract/ -v --tb=short

# Confirm NO test failures
# Expected output: All tests PASSED
```

---

## Manual Spot Checks (If Needed)

If any test fails, use these commands to debug:

```bash
# 1. Inspect DB schema directly
sqlite3 :memory: < storage/migrations/0001_init.sql
# Then: .schema articles (etc.)

# 2. Inspect model definitions
python -m pytest tests/contract/test_schema_compliance.py -v -s -k "schema"

# 3. Check config is loaded
python -c "from core.config import ComplianceConfig; print(ComplianceConfig.SNIPPET_MAX_CHARS)"
# Expected: 1500

# 4. Verify JSON schemas are valid
python -c "import json; json.loads(open('schemas/article.schema.json').read())" && echo "✓ Valid"

# 5. Check git history for changes
git log --oneline -10
# Should see the refinement commits
```

---

## Success Criteria

✅ **All 10 fixes verified** if:
- [ ] All pytest contract/alignment tests pass
- [ ] All grep commands return expected output
- [ ] No errors in config validation (`ComplianceConfig.validate()`)
- [ ] schema JSON files are valid
- [ ] Database migration can be applied without errors

**Status**: Pass all tests → v0 implementation is complete and aligned.

---

## Notes for Reviewers

1. **These tests are cumulative**: Each fix depends on prior ones. If Fix 3 (PK) fails, Fix 10 (rollback) tests will also fail.

2. **Grep commands are not tests**: Use them for quick verification. Formal tests are in `tests/contract/test_*.py`.

3. **JSON schema validation**: Separate from Pydantic validation. Both must pass for complete compliance.

4. **Rollback verification**: Run `pytest tests/contract/test_schema_compliance.py::TestEvidenceValidation` specifically to confirm evidence constraint logic is enforced.

---

**Last Updated**: 2025-02-27

**Maintained By**: author-collector maintainers

**Purpose**: Ensure v0 design decisions remain durable and defensible as the codebase evolves.
