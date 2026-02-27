"""
Contract tests for schema compliance.

Ensures that all exports match the defined schema (article.schema.json, evidence.schema.json).
These tests are run on CI and must pass before any feature work.
"""

import json
from pathlib import Path
from datetime import UTC, datetime

import pytest
import jsonschema

from core.models import (
    Article,
    Evidence,
    EvidenceType,
)
from core.evidence import validate_evidence


# Load schemas
SCHEMAS_DIR = Path(__file__).parent.parent.parent / "schemas"
ARTICLE_SCHEMA = json.loads((SCHEMAS_DIR / "article.schema.json").read_text())
EVIDENCE_SCHEMA = json.loads((SCHEMAS_DIR / "evidence.schema.json").read_text())


# Note: sample_evidence and other fixtures are imported from conftest.py


@pytest.fixture
def sample_article(sample_evidence: Evidence) -> Article:
    """Sample valid article."""
    return Article(
        id="art-001",
        canonical_url="https://example.com/article",
        source_id="rss:example",
        title="Example Article Title",
        author_hint="John Doe",
        published_at=datetime(2025, 2, 27, 10, 0, 0),
        snippet="This is an example article snippet...",
        evidence=[sample_evidence],
        version=1,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


# ============================================================================
# Evidence Schema Tests
# ============================================================================

class TestEvidenceSchema:
    """Evidence must conform to evidence.schema.json."""

    def test_evidence_against_schema(self, sample_evidence: Evidence):
        """Evidence serialization matches schema."""
        data = json.loads(sample_evidence.model_dump_json())
        try:
            jsonschema.validate(data, EVIDENCE_SCHEMA)
        except jsonschema.ValidationError as e:
            pytest.fail(f"Evidence schema validation failed: {e.message}")

    def test_evidence_requires_mandatory_fields(self):
        """Missing mandatory fields fail validation."""
        invalid_evidence = {
            "id": "ev-invalid",
            # Missing: article_id, claim_path, evidence_type, source_url, extracted_text
        }
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(invalid_evidence, EVIDENCE_SCHEMA)

    def test_evidence_rejects_extra_fields(self):
        """Extra fields fail validation (additionalProperties: false)."""
        invalid_evidence = {
            "id": "ev-001",
            "article_id": "art-001",
            "claim_path": "/title",
            "evidence_type": "meta_tag",
            "source_url": "https://example.com",
            "extracted_text": "Title",
            "retrieved_at": "2025-02-27T10:00:00",
            "created_at": "2025-02-27T10:00:00",
            "run_id": "run-001",
            "extra_field": "not allowed",  # INVALID
        }
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(invalid_evidence, EVIDENCE_SCHEMA)

    def test_claim_path_must_be_json_pointer(self):
        """claim_path must be RFC 6901 JSON Pointer (starts with '/')."""
        valid_data = {
            "id": "ev-001",
            "article_id": "art-001",
            "claim_path": "/title",
            "evidence_type": "meta_tag",
            "source_url": "https://example.com",
            "extracted_text": "Text",
            "retrieved_at": "2025-02-27T10:00:00",
            "created_at": "2025-02-27T10:00:00",
            "run_id": "run-001",
        }
        jsonschema.validate(valid_data, EVIDENCE_SCHEMA)

        invalid_data = valid_data.copy()
        invalid_data["claim_path"] = "title"
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(invalid_data, EVIDENCE_SCHEMA)

    def test_evidence_type_enum_validation(self):
        """Evidence type must be one of the allowed enum values."""
        valid_types = ["meta_tag", "json_ld", "extracted", "fetched_content"]
        for ev_type in valid_types:
            data = {
                "id": "ev-001",
                "article_id": "art-001",
                "claim_path": "/title",  # JSON Pointer
                "evidence_type": ev_type,
                "source_url": "https://example.com",
                "extracted_text": "Text",
                "retrieved_at": "2025-02-27T10:00:00",
                "created_at": "2025-02-27T10:00:00",
                "run_id": "run-001",
            }
            try:
                jsonschema.validate(data, EVIDENCE_SCHEMA)
            except jsonschema.ValidationError as e:
                pytest.fail(f"Valid evidence_type '{ev_type}' failed: {e.message}")

        # Invalid type should fail
        invalid_data = {
            "id": "ev-001",
            "article_id": "art-001",
            "claim_path": "/title",
            "evidence_type": "invalid_type",  # NOT in enum
            "source_url": "https://example.com",
            "extracted_text": "Text",
            "retrieved_at": "2025-02-27T10:00:00",
            "created_at": "2025-02-27T10:00:00",
            "run_id": "run-001",
        }
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(invalid_data, EVIDENCE_SCHEMA)

    def test_confidence_bounds(self):
        """Confidence must be 0.0-1.0."""
        # Valid
        valid_data = {
            "id": "ev-001",
            "article_id": "art-001",
            "claim_path": "/title",
            "evidence_type": "meta_tag",
            "source_url": "https://example.com",
            "extracted_text": "Text",
            "confidence": 0.5,
            "retrieved_at": "2025-02-27T10:00:00",
            "created_at": "2025-02-27T10:00:00",
            "run_id": "run-001",
        }
        jsonschema.validate(valid_data, EVIDENCE_SCHEMA)

        # Invalid: > 1.0
        invalid_data = valid_data.copy()
        invalid_data["confidence"] = 1.5
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(invalid_data, EVIDENCE_SCHEMA)

        # Invalid: < 0.0
        invalid_data["confidence"] = -0.1
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(invalid_data, EVIDENCE_SCHEMA)


# ============================================================================
# Article Schema Tests
# ============================================================================

class TestArticleSchema:
    """Article must conform to article.schema.json."""

    def test_article_against_schema(self, sample_article: Article):
        """Article serialization matches schema."""
        data = json.loads(sample_article.model_dump_json())
        try:
            jsonschema.validate(data, ARTICLE_SCHEMA)
        except jsonschema.ValidationError as e:
            pytest.fail(f"Article schema validation failed: {e.message}")

    def test_article_requires_mandatory_fields(self):
        """Missing mandatory fields fail validation."""
        invalid_article = {
            "id": "art-001",
            # Missing: canonical_url, source_id, evidence, version, created_at, updated_at
        }
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(invalid_article, ARTICLE_SCHEMA)

    def test_article_no_body_field(self, sample_article: Article):
        """Article export must not include 'body' field (compliance boundary)."""
        data = json.loads(sample_article.model_dump_json())

        # Ensure no 'body' or 'full_text' field
        assert "body" not in data, "Article must not have 'body' field"
        assert "full_text" not in data, "Article must not have 'full_text' field"

        # Verify schema also forbids it (additionalProperties: false)
        data_with_body = data.copy()
        data_with_body["body"] = "This should not be allowed"
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(data_with_body, ARTICLE_SCHEMA)

    def test_article_snippet_max_length(self):
        """Snippet must not exceed 1500 chars (v0 conservative)."""
        invalid_article = {
            "id": "art-001",
            "canonical_url": "https://example.com",
            "source_id": "rss:example",
            "evidence": [],
            "version": 1,
            "created_at": "2025-02-27T10:00:00",
            "updated_at": "2025-02-27T10:00:00",
            "snippet": "x" * 1501,  # EXCEEDS LIMIT
        }
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(invalid_article, ARTICLE_SCHEMA)

        # Exactly 1500 is OK
        valid_article = invalid_article.copy()
        valid_article["snippet"] = "x" * 1500
        jsonschema.validate(valid_article, ARTICLE_SCHEMA)

    def test_article_version_bounds(self):
        """Version must be ≥1."""
        # Valid
        valid_article = {
            "id": "art-001",
            "canonical_url": "https://example.com",
            "source_id": "rss:example",
            "evidence": [],
            "version": 1,
            "created_at": "2025-02-27T10:00:00",
            "updated_at": "2025-02-27T10:00:00",
        }
        jsonschema.validate(valid_article, ARTICLE_SCHEMA)

        # Invalid: version 0
        invalid_article = valid_article.copy()
        invalid_article["version"] = 0
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(invalid_article, ARTICLE_SCHEMA)

    def test_article_evidence_chain(self, sample_article: Article):
        """Article evidence must be valid Evidence objects."""
        data = json.loads(sample_article.model_dump_json())
        for ev in data.get("evidence", []):
            try:
                jsonschema.validate(ev, EVIDENCE_SCHEMA)
            except jsonschema.ValidationError as e:
                pytest.fail(f"Article evidence failed schema: {e.message}")


# ============================================================================
# Evidence Validation Tests
# ============================================================================

class TestEvidenceValidation:
    """Test the evidence validation logic (non-schema, semantic checks)."""

    def test_article_with_title_requires_evidence(self):
        """Article with title must have ≥1 evidence."""
        article = Article(
            id="art-001",
            canonical_url="https://example.com",
            source_id="rss:example",
            title="My Title",  # Non-null
            evidence=[],  # MISSING EVIDENCE
            version=1,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        is_valid, errors = validate_evidence(article)
        assert not is_valid
        assert any("title" in err for err in errors)

    def test_article_without_title_needs_no_evidence(self):
        """Article with null title doesn't need evidence for it."""
        article = Article(
            id="art-001",
            canonical_url="https://example.com",
            source_id="rss:example",
            title=None,  # Null
            evidence=[],
            version=1,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        is_valid, errors = validate_evidence(article)
        assert is_valid, f"Should be valid, got errors: {errors}"

    def test_evidence_claim_path_must_match(self):
        """Evidence claim_path must match article fields."""
        article = Article(
            id="art-001",
            canonical_url="https://example.com",
            source_id="rss:example",
            title="My Title",
            evidence=[
                Evidence(
                    id="ev-001",
                    article_id="art-001",
                    claim_path="/invalid_field",  # NOT A VALID FIELD (invalid JSON Pointer)
                    evidence_type=EvidenceType.META_TAG,
                    source_url="https://example.com",
                    extracted_text="Text",
                    retrieved_at=datetime.now(UTC),
                    created_at=datetime.now(UTC),
                    run_id="run-001",
                )
            ],
            version=1,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        is_valid, errors = validate_evidence(article)
        assert not is_valid
        assert any("claim_path" in err for err in errors)


# ============================================================================
# End-to-End Export Tests
# ============================================================================

class TestExportCompliance:
    """Test that export meets compliance requirements."""

    @pytest.mark.contract
    def test_empty_export_is_valid(self, tmp_path):
        """Empty export (no articles) is valid JSON."""
        # Empty JSONL is valid
        export_file = tmp_path / "export.jsonl"
        export_file.write_text("")

        # Should be valid (empty iterator)
        with open(export_file) as f:
            for line in f:
                data = json.loads(line)
                jsonschema.validate(data, ARTICLE_SCHEMA)

    @pytest.mark.contract
    def test_article_export_schema(self, sample_article: Article, tmp_path):
        """Article export as JSONL validates against schema."""
        export_file = tmp_path / "export.jsonl"

        # Write single article as JSONL line
        data = json.loads(sample_article.model_dump_json())
        export_file.write_text(json.dumps(data) + "\n")

        # Read back and validate
        with open(export_file) as f:
            for line in f:
                data = json.loads(line)
                try:
                    jsonschema.validate(data, ARTICLE_SCHEMA)
                except jsonschema.ValidationError as e:
                    pytest.fail(f"Export validation failed: {e.message}")

    @pytest.mark.contract
    def test_no_duplicate_article_ids(self, tmp_path):
        """Export must not have duplicate (canonical_url, source_id) dedup keys."""
        export_file = tmp_path / "export.jsonl"

        # Write 3 articles with unique dedup keys
        articles = [
            {"id": "art-001", "canonical_url": "https://a.com", "source_id": "rss:test", "evidence": [], "version": 1, "created_at": "2025-02-27T10:00:00", "updated_at": "2025-02-27T10:00:00"},
            {"id": "art-002", "canonical_url": "https://b.com", "source_id": "rss:test", "evidence": [], "version": 1, "created_at": "2025-02-27T10:00:00", "updated_at": "2025-02-27T10:00:00"},
            {"id": "art-003", "canonical_url": "https://a.com", "source_id": "rss:other", "evidence": [], "version": 1, "created_at": "2025-02-27T10:00:00", "updated_at": "2025-02-27T10:01:00"},
        ]

        lines = "\n".join(json.dumps(art) for art in articles) + "\n"
        export_file.write_text(lines)

        # Check for duplicate dedup keys
        seen_keys = set()
        duplicates = set()
        with open(export_file) as f:
            for line in f:
                if not line.strip():
                    continue
                data = json.loads(line)
                key = (data["canonical_url"], data["source_id"])
                if key in seen_keys:
                    duplicates.add(key)
                seen_keys.add(key)

        assert not duplicates, f"Export must not contain duplicate dedup keys, found: {duplicates}"
