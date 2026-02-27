"""
Alignment contract tests: verify DB schema aligns with model contracts and pipeline.

These tests ensure that the database constraints and model definitions are
consistent across layers (models, schema, config, pipeline).
"""

import sqlite3
from pathlib import Path
import pytest

from core.models import FetchedDoc, Article, Evidence, EvidenceType, RunLog, FetchLog
from core.config import ComplianceConfig


class TestDBSchemaAlignment:
    """Verify that SQLite schema enforces required constraints."""

    @pytest.fixture
    def migration_sql(self):
        """Load the migration SQL."""
        path = Path(__file__).parent.parent.parent / "storage" / "migrations" / "0001_init.sql"
        return path.read_text()

    def test_article_has_dedup_key_not_pk(self, migration_sql):
        """
        Article table must have:
        - id as PRIMARY KEY (global unique)
        - (canonical_url, source_id) as UNIQUE (dedup key)
        - NOT canonical_url as sole PRIMARY KEY
        """
        # Check for PRIMARY KEY on id
        assert "id TEXT PRIMARY KEY" in migration_sql, \
            "Article.id must be PRIMARY KEY"

        # Check for UNIQUE(canonical_url, source_id)
        assert "UNIQUE(canonical_url, source_id)" in migration_sql, \
            "Article must have UNIQUE(canonical_url, source_id) dedup key"

        # Ensure no PRIMARY KEY on canonical_url
        assert "canonical_url TEXT PRIMARY KEY" not in migration_sql, \
            "canonical_url must NOT be PRIMARY KEY (only id)"

    def test_evidence_has_claim_path_rfc6901(self, migration_sql):
        """
        Evidence.claim_path must be documented as RFC 6901 JSON Pointer.
        """
        assert "JSON Pointer" in migration_sql, \
            "Evidence claim_path must document RFC 6901 JSON Pointer format"
        assert "RFC 6901" in migration_sql, \
            "Evidence schema must reference RFC 6901 standard"

    def test_all_tables_have_run_id_for_rollback(self, migration_sql):
        """
        Tables that modify state must have run_id for per-run rollback.
        - fetch_log: has run_id
        - evidence: has run_id
        - versions: has run_id
        - merge_decisions: has run_id
        """
        state_tables = ["fetch_log", "evidence", "versions", "merge_decisions"]
        for table in state_tables:
            # Each table should have run_id column and FK to run_log
            pattern = f"CREATE TABLE {table}"
            assert pattern in migration_sql, f"{table} must exist"

            # Rough check: run_id appears in the table definition
            table_start = migration_sql.find(pattern)
            table_end = migration_sql.find("CREATE TABLE", table_start + 1)
            if table_end == -1:
                table_end = len(migration_sql)
            table_def = migration_sql[table_start:table_end]

            assert "run_id" in table_def, \
                f"{table} must have run_id column for rollback tracking"
            assert f"FOREIGN KEY(run_id) REFERENCES run_log" in table_def, \
                f"{table} must have FK to run_log via run_id"

    def test_indexes_on_run_id(self, migration_sql):
        """
        Tables with run_id should have indexes for fast rollback queries.
        """
        indexes = [
            "idx_evidence_run_id",
            "idx_fetch_log_run_id",
            "idx_versions_run_id",
            "idx_merge_decisions_run_id",
        ]
        for idx in indexes:
            assert idx in migration_sql, \
                f"Index {idx} must exist for efficient run-based queries"


class TestFetchedDocContract:
    """Verify FetchedDoc is properly integrated into pipeline."""

    def test_fetched_doc_has_required_fields(self):
        """FetchedDoc must have fields for proper HTTP semantics."""
        required_fields = {
            "status_code": int,
            "final_url": str,
            "headers": dict,
            "body_bytes": None,  # Optional[bytes]
            "body_sha256": None,  # Optional[str]
            "latency_ms": int,
            "retrieved_at": None,  # datetime
        }
        doc = FetchedDoc(
            status_code=200,
            final_url="https://example.com/article",
            headers={"content-type": "text/html"},
            body_bytes=b"test",
            body_sha256="abc123",
            latency_ms=100,
        )

        # Validate all fields exist and can be serialized
        data = doc.model_dump()
        for field in required_fields:
            assert field in data, f"FetchedDoc must have field: {field}"

    def test_fetched_doc_supports_304_not_modified(self):
        """FetchedDoc must properly represent 304 responses (no body)."""
        doc_304 = FetchedDoc(
            status_code=304,
            final_url="https://example.com/article",
            headers={"etag": '"abc123"'},
            body_bytes=None,  # 304 has no body
            body_sha256=None,
            latency_ms=50,
        )

        assert doc_304.status_code == 304
        assert doc_304.body_bytes is None, \
            "304 responses must have body_bytes=None (client uses cache)"
        assert doc_304.body_sha256 is None, \
            "304 responses must not have body_sha256"

    def test_fetched_doc_serializes_for_logging(self):
        """FetchedDoc must serialize to JSON for logging."""
        doc = FetchedDoc(
            status_code=200,
            final_url="https://example.com",
            headers={"content-type": "text/html"},
            body_bytes=b"content",
            body_sha256="hash",
            latency_ms=100,
        )

        json_str = doc.model_dump_json()
        assert "200" in json_str
        assert "content-type" in json_str
        assert "latency_ms" in json_str


class TestComplianceConfigDefaults:
    """Verify compliance config enforces v0 safety defaults."""

    def test_robots_check_required(self):
        """Robots.txt enforcement must be mandatory in v0."""
        assert ComplianceConfig.ROBOTS_CHECK_REQUIRED is True, \
            "ROBOTS_CHECK_REQUIRED must be True (cannot disable)"

    def test_no_auto_merge_in_v0(self):
        """Author merging must be manual in v0."""
        assert ComplianceConfig.AUTO_MERGE_ENABLED is False, \
            "AUTO_MERGE_ENABLED must be False in v0"

    def test_no_full_body_storage(self):
        """Full article body must never be stored."""
        assert ComplianceConfig.STORE_FULL_BODY is False, \
            "STORE_FULL_BODY must be False (compliance boundary)"

    def test_snippet_size_consistent(self):
        """Snippet size limit must match schema maxLength."""
        assert ComplianceConfig.SNIPPET_MAX_CHARS == 1500, \
            "SNIPPET_MAX_CHARS must be 1500 in v0"
        assert ComplianceConfig.EVIDENCE_SNIPPET_MAX_CHARS == 800, \
            "EVIDENCE_SNIPPET_MAX_CHARS must be 800 (shorter for specificity)"

    def test_pdf_explicitly_disabled(self):
        """PDFs must be explicitly disabled (no fetching)."""
        assert "application/pdf" in ComplianceConfig.MAX_BODY_BYTES_BY_TYPE
        assert ComplianceConfig.MAX_BODY_BYTES_BY_TYPE["application/pdf"] == 0, \
            "PDFs must not be fetched (0 byte limit)"

    def test_ipv6_ssrf_coverage(self):
        """IPv6 ranges must be blocked for SSRF prevention."""
        ipv6_ranges = [
            "::1/128",      # Loopback
            "fe80::/10",    # Link-local
            "fc00::/7",     # Unique local
            "ff00::/8",     # Multicast
        ]
        for cidr in ipv6_ranges:
            assert cidr in ComplianceConfig.BLOCKED_IP_RANGES, \
                f"IPv6 range {cidr} must be blocked for SSRF prevention"
