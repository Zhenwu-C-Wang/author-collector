"""
Core Pydantic models for author-collector.

Design principles:
- Every model is explicitly typed and validated
- Evidence is first-class (not an afterthought)
- No sensitive/large data in core fields (no body text, PII handling separate)
- Deterministic serialization (for hashing, deduping, versioning)
"""

from datetime import datetime
from enum import Enum
from typing import Optional, List, Dict, Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, field_validator


# ============================================================================
# Enums
# ============================================================================

class EvidenceType(str, Enum):
    """Where did this evidence come from?"""
    META_TAG = "meta_tag"  # <meta name="..." content="...">
    JSON_LD = "json_ld"  # JSON-LD structured data
    EXTRACTED = "extracted"  # Extracted from readable text
    FETCHED_CONTENT = "fetched_content"  # Raw HTML/content


class RunStatus(str, Enum):
    """Status of a run."""
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class FetchErrorCode(str, Enum):
    """Why did a fetch fail?"""
    TIMEOUT = "TIMEOUT"
    SECURITY_BLOCKED = "SECURITY_BLOCKED"  # SSRF, IP blocklist, etc.
    FETCH_ERROR = "FETCH_ERROR"  # Network error
    BLOCKED_BY_ROBOTS = "BLOCKED_BY_ROBOTS"
    BODY_TOO_LARGE = "BODY_TOO_LARGE"
    REDIRECT_LIMIT = "REDIRECT_LIMIT"


# ============================================================================
# Evidence
# ============================================================================

class Evidence(BaseModel):
    """
    A piece of evidence backing a claim.

    Design:
    - claim_path: JSON Pointer (RFC 6901) to the field in Article (e.g., "/title", "/author_hint")
    - extracted_text: Short snippet (≤800 chars) backing the claim
    - Includes replay/audit fields (retrieved_at, extractor_version, input_ref) for reproducibility

    Example:
      claim_path = "/title"  # JSON Pointer
      evidence_type = "meta_tag"
      source_url = "https://example.com/article"
      extracted_text = "Breaking: AI Achieves AGI"  # Snippet (≤800 chars)
      confidence = 0.95
      extractor_version = "jsonld@1.0"
    """
    id: str = Field(default_factory=lambda: str(uuid4()))
    article_id: str  # FK to Article

    # JSON Pointer (RFC 6901) to the claim (e.g., "/title", "/author_hint", "/published_at")
    claim_path: str

    evidence_type: EvidenceType

    source_url: str  # Where this evidence came from
    extraction_method: Optional[str] = None  # e.g., "trafilatura", "meta_og:title", "json_ld_headline"

    extracted_text: str  # Short snippet backing the claim (max 800 chars recommended)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)

    metadata: Dict[str, Any] = Field(default_factory=dict)  # Extra context

    # Replay/audit fields: allow future reproducibility of extraction
    retrieved_at: datetime  # When was this evidence collected?
    extractor_version: Optional[str] = None  # e.g., "trafilatura@1.9.0", "jsonld@1.0"
    input_ref: Optional[str] = None  # e.g., CSS selector / JSON-LD path / meta name (for replay)
    snippet_max_chars_applied: Optional[int] = None  # Truncation policy at extraction time

    created_at: datetime = Field(default_factory=datetime.utcnow)
    run_id: str  # Which run added this evidence?

    class Config:
        json_schema_extra = {
            "examples": [
                {
                    "id": "uuid",
                    "article_id": "uuid",
                    "claim_path": "/title",
                    "evidence_type": "meta_tag",
                    "source_url": "https://example.com",
                    "extraction_method": "meta_og:title",
                    "extracted_text": "Example Article",
                    "confidence": 0.95,
                    "retrieved_at": "2025-02-27T10:00:00",
                    "extractor_version": "jsonld@1.0",
                    "input_ref": "og:title",
                    "created_at": "2025-02-27T10:00:00",
                    "run_id": "run_123"
                }
            ]
        }


# ============================================================================
# Article (Final Export)
# ============================================================================

class Article(BaseModel):
    """
    A published article (or content piece) discovered from a source.

    This is what gets exported as JSONL. It includes:
    - Core metadata (title, author_hint, date, canonical URL)
    - Snippet (short excerpt, max 5000 chars)
    - Evidence chain (proof for each claim)
    - NO full body text (compliance boundary)
    """
    id: str = Field(default_factory=lambda: str(uuid4()))

    # Dedup key
    canonical_url: str
    source_id: str  # e.g., "rss:techblog", "html:author_page", "arxiv:cs"

    # Core fields
    title: Optional[str] = None
    author_hint: Optional[str] = None  # Unresolved author name
    published_at: Optional[datetime] = None

    # Snippet (no full body)
    snippet: Optional[str] = None

    # Evidence chain
    evidence: List[Evidence] = Field(default_factory=list)

    # Versioning
    version: int = 1

    # Timestamps
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    @field_validator("snippet")
    @classmethod
    def validate_snippet_length(cls, v: Optional[str]) -> Optional[str]:
        """Ensure snippet doesn't exceed 1500 chars (conservative for v0)."""
        if v and len(v) > 1500:
            return v[:1500] + "…"
        return v

    class Config:
        json_schema_extra = {
            "examples": [
                {
                    "id": "uuid",
                    "canonical_url": "https://example.com/article",
                    "source_id": "rss:techblog",
                    "title": "Breaking: AI Achieves AGI",
                    "author_hint": "Jane Doe",
                    "published_at": "2025-02-27T10:00:00",
                    "snippet": "In a stunning turn of events... (5000 chars max)",
                    "evidence": [
                        {
                            "id": "uuid",
                            "article_id": "uuid",
                            "claim_path": "title",
                            "evidence_type": "meta_tag",
                            "source_url": "https://example.com/article",
                            "extracted_text": "Breaking: AI Achieves AGI",
                            "confidence": 0.95,
                            "created_at": "2025-02-27T10:00:00",
                            "run_id": "run_123"
                        }
                    ],
                    "version": 1,
                    "created_at": "2025-02-27T10:00:00",
                    "updated_at": "2025-02-27T10:00:00"
                }
            ]
        }


# ============================================================================
# Article Working Model (Pre-Storage)
# ============================================================================

class ArticleDraft(BaseModel):
    """
    Article in draft state (before storage).

    Same as Article but:
    - No id (assigned during storage)
    - Evidence is separate (added during extract stage)
    - Used as pipeline intermediate representation
    """
    canonical_url: str
    source_id: str

    title: Optional[str] = None
    author_hint: Optional[str] = None
    published_at: Optional[datetime] = None

    snippet: Optional[str] = None

    @field_validator("snippet")
    @classmethod
    def validate_snippet_length(cls, v: Optional[str]) -> Optional[str]:
        if v and len(v) > 1500:
            return v[:1500] + "…"
        return v
# ============================================================================

class Parsed(BaseModel):
    """
    Result of parsing HTML/content into structured data.

    Used as intermediate representation between fetch and extract stages.
    No evidence yet; that's added in extract stage.
    """
    url: str  # Original fetch URL

    # Raw/readable content
    text: Optional[str] = None  # Readable main text (not full — truncated for memory safety)

    # Metadata extracted from HTML/JSON-LD
    title: Optional[str] = None
    date_published: Optional[datetime] = None
    author_names: List[str] = Field(default_factory=list)

    # HTML-level metadata
    html_title: Optional[str] = None  # <title> tag
    meta_tags: Dict[str, str] = Field(default_factory=dict)  # og:image, og:author, etc.
    json_ld_blocks: List[Dict[str, Any]] = Field(default_factory=list)  # All JSON-LD blocks found

    # Canonical reference
    canonical_url: Optional[str] = None

    # Raw HTML (kept for fallback, but not stored in final article)
    original_html: Optional[str] = None


# ============================================================================
# Fetch / Network Logging
# ============================================================================

class FetchLog(BaseModel):
    """
    Log entry for a single fetch operation.
    """
    id: str = Field(default_factory=lambda: str(uuid4()))
    url: str

    status_code: Optional[int] = None  # HTTP status
    latency_ms: Optional[int] = None  # Time to response
    bytes_received: Optional[int] = None

    error_code: Optional[FetchErrorCode] = None

    created_at: datetime = Field(default_factory=datetime.utcnow)
    run_id: str


class RunLog(BaseModel):
    """
    Log entry for an entire run (e.g., one RSS feed sync).
    """
    id: str = Field(default_factory=lambda: str(uuid4()))
    source_id: str  # e.g., "rss:techblog"

    started_at: datetime = Field(default_factory=datetime.utcnow)
    ended_at: Optional[datetime] = None

    status: RunStatus = RunStatus.RUNNING
    error_message: Optional[str] = None

    # Summary stats (updated as run progresses)
    fetched_count: int = 0
    new_articles_count: int = 0
    updated_articles_count: int = 0
    error_count: int = 0


# ============================================================================
# Identity Resolution (Author Dedup)
# ============================================================================

class Account(BaseModel):
    """
    A discovered author account in a source.

    Example:
      source_id = "rss:techblog-author-field"
      source_identifier = "john@example.com"  # email, handle, ID in that source
      author_id = None (initially unresolved) or "author_123" (resolved)
    """
    id: str = Field(default_factory=lambda: str(uuid4()))

    source_id: str
    source_identifier: str

    author_id: Optional[str] = None  # FK to Author (resolved)

    created_at: datetime = Field(default_factory=datetime.utcnow)


class Author(BaseModel):
    """
    Canonical author identity (after merging/resolving accounts).
    """
    id: str = Field(default_factory=lambda: str(uuid4()))

    canonical_name: str
    metadata: Dict[str, Any] = Field(default_factory=dict)

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class MergeDecision(BaseModel):
    """
    Audit trail for merging authors.

    Example:
      from_author_id = "author_a"
      to_author_id = "author_b"
      This means: all accounts/articles of author_a are merged into author_b
    """
    id: str = Field(default_factory=lambda: str(uuid4()))

    from_author_id: str
    to_author_id: str

    evidence_ids: List[str] = Field(default_factory=list)
    decision_criteria: Optional[str] = None

    created_at: datetime = Field(default_factory=datetime.utcnow)
    created_by: Optional[str] = None
    run_id: str

    # Rollback support
    reverted_at: Optional[datetime] = None
    reverted_by: Optional[str] = None
    reverted_reason: Optional[str] = None
