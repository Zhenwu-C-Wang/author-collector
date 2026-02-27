"""Extractor package for mapping Parsed content to ArticleDraft + Evidence."""

from extractor.article import (
    CLAIM_PATH_BY_FIELD,
    ArticleExtractStage,
    enforce_evidence_coverage,
)

__all__ = ["ArticleExtractStage", "enforce_evidence_coverage", "CLAIM_PATH_BY_FIELD"]

