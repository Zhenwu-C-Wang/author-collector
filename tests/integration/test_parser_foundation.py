"""Integration tests for parser/html.py and parser/jsonld.py."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.models import FetchedDoc
from parser.html import HtmlParseStage
from parser.jsonld import extract_structured_metadata


FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "html"


def _fixture_text(name: str) -> str:
    """Load an HTML fixture file."""
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def _fixture_doc(name: str, url: str = "https://example.com/source") -> FetchedDoc:
    """Create a FetchedDoc from fixture content."""
    body = _fixture_text(name).encode("utf-8")
    return FetchedDoc(
        status_code=200,
        final_url=url,
        headers={"content-type": "text/html; charset=utf-8"},
        body_bytes=body,
        body_sha256=None,
        latency_ms=12,
    )


@pytest.mark.integration
def test_jsonld_metadata_extraction_from_normal_fixture():
    """Structured metadata should include JSON-LD, meta tags, and canonical URL."""
    metadata = extract_structured_metadata(
        _fixture_text("normal.html"),
        page_url="https://example.com/source",
    )

    assert metadata["html_title"] == "Normal Fixture Title"
    assert metadata["meta_tags"]["og:title"] == "OG Fixture Title"
    assert metadata["canonical_url"] == "https://example.com/posts/normal"
    assert metadata["json_ld_title"] == "JSON-LD Headline"
    assert metadata["json_ld_author_names"] == ["Jane Doe", "John Roe"]
    assert metadata["json_ld_date_published"] == "2026-02-19T08:00:00Z"


@pytest.mark.integration
def test_html_parse_stage_prefers_jsonld_over_meta_title():
    """Title/date/author should prefer JSON-LD when available."""
    stage = HtmlParseStage()
    parsed = stage.parse(_fixture_doc("normal.html"), run_id="run-1")

    assert parsed.title == "JSON-LD Headline"
    assert parsed.canonical_url == "https://example.com/posts/normal"
    assert parsed.date_published is not None
    assert parsed.date_published.isoformat().startswith("2026-02-19T08:00:00")
    assert parsed.author_names[:2] == ["Jane Doe", "John Roe"]
    assert "Meta Author" in parsed.author_names
    assert parsed.html_title == "Normal Fixture Title"
    assert parsed.text is not None
    assert "First paragraph of the fixture article." in parsed.text
    assert "do-not-include" not in parsed.text


@pytest.mark.integration
def test_html_parse_stage_edge_fixture_meta_fallback():
    """Edge fixture should parse canonical/meta fields without JSON-LD."""
    stage = HtmlParseStage()
    parsed = stage.parse(
        _fixture_doc("edge.html", url="https://example.org/source"),
        run_id="run-1",
    )

    assert parsed.title == "Edge Fixture Title"
    assert parsed.canonical_url == "https://example.org/edge-case"
    assert parsed.date_published is not None
    assert parsed.date_published.isoformat().startswith("2026-02-17")
    assert "Edge Author A" in parsed.author_names
    assert "Edge Author B" in parsed.author_names


@pytest.mark.integration
def test_html_parse_stage_handles_malformed_jsonld_deterministically():
    """Malformed JSON-LD should not break parsing and output should be deterministic."""
    stage = HtmlParseStage()
    doc = _fixture_doc("malformed.html")

    first = stage.parse(doc, run_id="run-1")
    second = stage.parse(doc, run_id="run-2")

    assert first.model_dump() == second.model_dump()
    assert first.json_ld_blocks == []
    assert first.title == "Malformed OG Title"
    assert first.date_published is None
    assert first.text is not None
    assert "Malformed fixture still has readable text" in first.text


@pytest.mark.integration
def test_html_parse_stage_truncates_readable_text_with_ellipsis():
    """Readable text should respect max chars and end with ellipsis when truncated."""
    long_html = "<html><body><p>" + ("alpha " * 500) + "</p></body></html>"
    doc = FetchedDoc(
        status_code=200,
        final_url="https://example.com/long",
        headers={"content-type": "text/html; charset=utf-8"},
        body_bytes=long_html.encode("utf-8"),
        body_sha256=None,
        latency_ms=5,
    )
    stage = HtmlParseStage(readable_text_max_chars=120)
    parsed = stage.parse(doc, run_id="run-1")

    assert parsed.text is not None
    assert len(parsed.text) <= 121
    assert parsed.text.endswith("…")
