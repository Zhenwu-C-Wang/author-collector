"""Integration tests for extractor/article.py."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from core.evidence import create_evidence
from core.models import ArticleDraft, EvidenceType, FetchedDoc, Parsed
from extractor.article import (
    CLAIM_PATH_BY_FIELD,
    ArticleExtractStage,
    enforce_evidence_coverage,
)
from parser.html import HtmlParseStage


FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "html"


def _fixture_doc(name: str, url: str = "https://example.com/source") -> FetchedDoc:
    """Create fetched doc from HTML fixture."""
    body = (FIXTURES_DIR / name).read_text(encoding="utf-8").encode("utf-8")
    return FetchedDoc(
        status_code=200,
        final_url=url,
        headers={"content-type": "text/html; charset=utf-8"},
        body_bytes=body,
        body_sha256=None,
        latency_ms=7,
    )


def _assert_non_null_claims_have_evidence(draft: ArticleDraft, evidence_list: list) -> None:
    """Assertion helper for evidence coverage rule."""
    for field_name, claim_path in CLAIM_PATH_BY_FIELD.items():
        if getattr(draft, field_name) is None:
            continue
        assert any(item.claim_path == claim_path for item in evidence_list), (
            f"Expected evidence for non-null field '{field_name}' with claim_path '{claim_path}'"
        )


@pytest.mark.integration
def test_extract_stage_generates_evidence_for_non_null_claims():
    """Extractor output must satisfy non-null claim coverage."""
    parser_stage = HtmlParseStage()
    parsed = parser_stage.parse(_fixture_doc("normal.html"), run_id="run-1")

    extract_stage = ArticleExtractStage(source_id="rss:normal")
    draft, evidence_list = extract_stage.extract(parsed, run_id="run-1")

    assert draft.title == "JSON-LD Headline"
    assert draft.author_hint == "Jane Doe"
    assert draft.published_at is not None
    _assert_non_null_claims_have_evidence(draft, evidence_list)
    assert any(item.evidence_type == EvidenceType.JSON_LD for item in evidence_list)


@pytest.mark.integration
def test_extract_stage_uses_meta_fallback_from_edge_fixture():
    """Edge fixture should fallback to meta fields when JSON-LD is absent."""
    parser_stage = HtmlParseStage()
    parsed = parser_stage.parse(
        _fixture_doc("edge.html", url="https://example.org/edge-source"),
        run_id="run-1",
    )

    extract_stage = ArticleExtractStage(source_id="html:edge")
    draft, evidence_list = extract_stage.extract(parsed, run_id="run-1")

    assert draft.title == "Edge Fixture Title"
    assert draft.author_hint == "Edge Author A"
    assert draft.published_at is not None
    assert draft.canonical_url == "https://example.org/edge-case"
    _assert_non_null_claims_have_evidence(draft, evidence_list)


@pytest.mark.integration
def test_enforce_evidence_coverage_nulls_fields_without_evidence():
    """Fields lacking evidence should be nulled to keep output compliant."""
    draft = ArticleDraft(
        canonical_url="https://example.com/article",
        source_id="rss:test",
        title="Title",
        author_hint="Author",
        published_at=datetime(2026, 2, 27, 8, 0, tzinfo=UTC),
        snippet="Snippet",
    )
    title_evidence = create_evidence(
        article_id="__draft__",
        claim_path="/title",
        evidence_type=EvidenceType.META_TAG,
        source_url="https://example.com/article",
        extracted_text="Title",
        run_id="run-1",
        extraction_method="meta.og:title",
    )

    warnings = enforce_evidence_coverage(draft, [title_evidence])

    assert draft.title == "Title"
    assert draft.author_hint is None
    assert draft.published_at is None
    assert len(warnings) == 2


@pytest.mark.integration
def test_extract_stage_is_deterministic_for_semantic_output():
    """Same Parsed input should yield same draft and evidence semantics."""
    def _normalize(items):
        """Normalize evidence list for deterministic semantic comparison."""
        return [
            (
                item.claim_path,
                item.evidence_type.value,
                item.extraction_method,
                item.extracted_text,
                item.source_url,
                item.metadata,
            )
            for item in items
        ]

    parser_stage = HtmlParseStage()
    parsed = parser_stage.parse(_fixture_doc("malformed.html"), run_id="run-1")
    extract_stage = ArticleExtractStage(source_id="rss:malformed")

    draft_a, evidence_a = extract_stage.extract(parsed, run_id="run-1")
    draft_b, evidence_b = extract_stage.extract(parsed, run_id="run-1")

    assert draft_a.model_dump() == draft_b.model_dump()
    assert _normalize(evidence_a) == _normalize(evidence_b)
    assert draft_a.snippet is None or len(draft_a.snippet) <= 1500


@pytest.mark.integration
def test_extract_stage_handles_missing_title_regression():
    """Missing-title payload should remain valid without phantom evidence."""
    parsed = Parsed(
        url="https://example.com/no-title",
        text="Body only text.",
        title=None,
        date_published=None,
        author_names=[],
        html_title=None,
        meta_tags={},
        json_ld_blocks=[],
        canonical_url="https://example.com/no-title",
    )
    extract_stage = ArticleExtractStage(source_id="rss:missing")

    draft, evidence_list = extract_stage.extract(parsed, run_id="run-1")

    assert draft.title is None
    assert not any(item.claim_path == "/title" for item in evidence_list)
    _assert_non_null_claims_have_evidence(draft, evidence_list)
