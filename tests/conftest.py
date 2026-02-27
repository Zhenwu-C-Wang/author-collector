"""
Shared pytest fixtures and configuration for author-collector tests.
"""

import pytest
from pathlib import Path
from datetime import datetime
from uuid import uuid4

from core.models import (
    Article,
    ArticleDraft,
    Evidence,
    EvidenceType,
    Parsed,
    Author,
    Account,
)


# ============================================================================
# Fixtures: Evidence
# ============================================================================

@pytest.fixture
def sample_evidence() -> Evidence:
    """Sample valid evidence entry."""
    return Evidence(
        id=str(uuid4()),
        article_id="article-001",
        claim_path="title",
        evidence_type=EvidenceType.META_TAG,
        source_url="https://example.com/article",
        extraction_method="meta_og:title",
        extracted_text="Example Article: Breaking News",
        confidence=0.95,
        metadata={"tag": "og:title"},
        created_at=datetime.utcnow(),
        run_id="run-001",
    )


@pytest.fixture
def sample_evidence_author() -> Evidence:
    """Sample evidence for author field."""
    return Evidence(
        id=str(uuid4()),
        article_id="article-001",
        claim_path="author_hint",
        evidence_type=EvidenceType.EXTRACTED,
        source_url="https://example.com/article",
        extraction_method="trafilatura",
        extracted_text="Jane Doe",
        confidence=0.8,
        created_at=datetime.utcnow(),
        run_id="run-001",
    )


# ============================================================================
# Fixtures: Article
# ============================================================================

@pytest.fixture
def sample_article(sample_evidence: Evidence) -> Article:
    """Sample valid article with evidence."""
    return Article(
        id="article-001",
        canonical_url="https://example.com/article",
        source_id="rss:example",
        title="Example Article: Breaking News",
        author_hint="Jane Doe",
        published_at=datetime(2025, 2, 27, 10, 0, 0),
        snippet="In a stunning turn of events, artificial intelligence has achieved...",
        evidence=[sample_evidence],
        version=1,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )


@pytest.fixture
def sample_article_minimal() -> Article:
    """Minimal article (only canonical_url + source_id, no content)."""
    return Article(
        id="article-minimal",
        canonical_url="https://example.com/minimal",
        source_id="rss:example",
        title=None,
        author_hint=None,
        published_at=None,
        snippet=None,
        evidence=[],
        version=1,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )


# ============================================================================
# Fixtures: ArticleDraft
# ============================================================================

@pytest.fixture
def sample_article_draft() -> ArticleDraft:
    """Sample article draft (before storage)."""
    return ArticleDraft(
        canonical_url="https://example.com/article",
        source_id="rss:example",
        title="Draft Article",
        author_hint="John Doe",
        published_at=datetime(2025, 2, 27, 10, 0, 0),
        snippet="This is a draft snippet...",
    )


# ============================================================================
# Fixtures: Parsed Content
# ============================================================================

@pytest.fixture
def sample_parsed() -> Parsed:
    """Sample parsed HTML content."""
    return Parsed(
        url="https://example.com/article",
        text="This is the readable main text extracted from the HTML...",
        title="Article Title",
        date_published=datetime(2025, 2, 27, 10, 0, 0),
        author_names=["Jane Doe", "John Smith"],
        html_title="Article Title | Example Site",
        meta_tags={
            "og:title": "Article Title",
            "og:image": "https://example.com/image.jpg",
            "og:description": "A brief description",
            "author": "Jane Doe",
        },
        json_ld_blocks=[
            {
                "@context": "https://schema.org",
                "@type": "Article",
                "headline": "Article Title",
                "author": {"@type": "Person", "name": "Jane Doe"},
                "datePublished": "2025-02-27T10:00:00",
            }
        ],
        canonical_url="https://example.com/article",
        original_html="<html>...</html>",
    )


# ============================================================================
# Fixtures: Identity Resolution
# ============================================================================

@pytest.fixture
def sample_author() -> Author:
    """Sample canonical author."""
    return Author(
        id=str(uuid4()),
        canonical_name="Jane Doe",
        metadata={"affiliation": "Example University"},
    )


@pytest.fixture
def sample_account(sample_author: Author) -> Account:
    """Sample account (discovered in source)."""
    return Account(
        id=str(uuid4()),
        source_id="rss:techblog-author-field",
        source_identifier="jane.doe@example.com",
        author_id=sample_author.id,
    )


@pytest.fixture
def sample_account_unresolved() -> Account:
    """Sample account with no resolved author yet."""
    return Account(
        id=str(uuid4()),
        source_id="rss:techblog-author-field",
        source_identifier="unknown.author@example.com",
        author_id=None,
    )


# ============================================================================
# Fixtures: File Paths
# ============================================================================

@pytest.fixture
def schemas_dir() -> Path:
    """Path to schemas directory."""
    return Path(__file__).parent.parent / "schemas"


@pytest.fixture
def fixtures_dir() -> Path:
    """Path to test fixtures directory."""
    return Path(__file__).parent / "fixtures"


# ============================================================================
# Fixtures: Cleanup
# ============================================================================

@pytest.fixture
def temp_db(tmp_path):
    """Temporary SQLite database for testing."""
    db_path = tmp_path / "test.db"
    # In future, initialize schema here
    return db_path


# ============================================================================
# Pytest Configuration
# ============================================================================

def pytest_configure(config):
    """Register custom markers."""
    config.addinivalue_line("markers", "contract: contract schema compliance tests")
    config.addinivalue_line("markers", "integration: end-to-end integration tests")
    config.addinivalue_line("markers", "unit: unit tests")
