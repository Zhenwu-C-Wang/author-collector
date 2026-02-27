"""Core module for author-collector."""

from core.models import (
    Article,
    ArticleDraft,
    Evidence,
    EvidenceType,
    Parsed,
    Author,
    Account,
    MergeDecision,
    FetchedDoc,
    FetchLog,
    RunLog,
)
from core.config import ComplianceConfig
from core.pipeline import Pipeline

__all__ = [
    "Article",
    "ArticleDraft",
    "Evidence",
    "EvidenceType",
    "Parsed",
    "Author",
    "Account",
    "MergeDecision",
    "FetchedDoc",
    "FetchLog",
    "RunLog",
    "ComplianceConfig",
    "Pipeline",
]
