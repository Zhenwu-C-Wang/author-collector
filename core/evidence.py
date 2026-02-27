"""
Evidence generation and hashing utilities.

Ensures deterministic evidence creation and content verification.
"""

import hashlib
import json
from uuid import uuid4
from datetime import UTC, datetime

from core.models import Evidence, EvidenceType, Article


def utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(UTC)


def hash_evidence(evidence: Evidence) -> str:
    """
    Create a deterministic hash of evidence for deduplication.

    Hash includes: claim_path, evidence_type, extracted_text, confidence, source_url
    (Excludes timestamps and run_id to catch duplicate evidence across runs)

    Args:
        evidence: Evidence object

    Returns:
        SHA256 hex digest
    """
    data = {
        "claim_path": evidence.claim_path,
        "evidence_type": evidence.evidence_type.value,
        "extracted_text": evidence.extracted_text,
        "confidence": evidence.confidence,
        "source_url": evidence.source_url,
    }
    payload = json.dumps(data, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def hash_article_content(article: Article) -> str:
    """
    Create a deterministic hash of article content for versioning.

    Hash includes: title, author_hint, snippet, published_at
    (Excludes timestamps, version, evidence to track "content" changes only)

    Args:
        article: Article object

    Returns:
        SHA256 hex digest
    """
    data = {
        "title": article.title,
        "author_hint": article.author_hint,
        "snippet": article.snippet,
        "published_at": article.published_at.isoformat() if article.published_at else None,
    }
    payload = json.dumps(data, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def create_evidence(
    article_id: str,
    claim_path: str,
    evidence_type: EvidenceType,
    source_url: str,
    extracted_text: str,
    run_id: str,
    extraction_method: str | None = None,
    confidence: float = 1.0,
    metadata: dict | None = None,
    retrieved_at: datetime | None = None,
) -> Evidence:
    """
    Factory for creating Evidence objects with defaults.

    Args:
        article_id: ID of the article this backs
        claim_path: JSON Pointer to claim (RFC 6901, e.g., "/title", "/author_hint")
        evidence_type: Where this came from
        source_url: URL where found
        extracted_text: Short snippet (max 800 chars)
        run_id: Which run added this
        extraction_method: Optional (e.g., "trafilatura", "meta_og:title")
        confidence: Optional (default 1.0)
        metadata: Optional extra data
        retrieved_at: Optional (defaults to now)

    Returns:
        Evidence object (with generated id, created_at, etc.)
    """
    return Evidence(
        id=str(uuid4()),
        article_id=article_id,
        claim_path=claim_path,
        evidence_type=evidence_type,
        source_url=source_url,
        extracted_text=extracted_text,
        extraction_method=extraction_method,
        confidence=confidence,
        metadata=metadata or {},
        retrieved_at=retrieved_at or utc_now(),
        created_at=utc_now(),
        run_id=run_id,
    )


def validate_evidence(article: Article) -> tuple[bool, list[str]]:
    """
    Validate that article has sufficient evidence.

    Rules:
    - Every non-null field should have ≥1 evidence entry
    - Evidence claim_path (JSON Pointer) must correspond to article field
    - Evidence source_url must be valid

    Args:
        article: Article to validate

    Returns:
        (is_valid: bool, errors: list[str])
    """
    errors = []

    # Mapping: field names → expected JSON Pointer claim_path
    # (e.g., "title" field → "/title" claim_path)
    field_to_pointer = {
        "title": "/title",
        "author_hint": "/author_hint",
        "published_at": "/published_at",
    }

    field_claims = {
        "title": article.title,
        "author_hint": article.author_hint,
        "published_at": article.published_at,
    }

    for field_name, field_value in field_claims.items():
        if field_value is not None:
            expected_pointer = field_to_pointer[field_name]
            # Check if this field has evidence
            has_evidence = any(
                e.claim_path == expected_pointer for e in article.evidence
            )
            if not has_evidence:
                errors.append(f"Field '{field_name}' has no evidence")

    # Check that evidence claim_paths use valid JSON Pointers
    valid_claim_paths = {"/title", "/author_hint", "/published_at"}
    for evidence in article.evidence:
        if evidence.claim_path not in valid_claim_paths:
            errors.append(
                f"Evidence has invalid claim_path: '{evidence.claim_path}' "
                f"(valid: {valid_claim_paths})"
            )

    return len(errors) == 0, errors
