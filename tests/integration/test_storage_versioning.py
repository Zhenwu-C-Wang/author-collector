"""Integration tests for URL normalization and storage upsert/versioning."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest

from core.models import ArticleDraft, EvidenceType, RunLog
from core.evidence import create_evidence
from quality.urlnorm import canonicalize_url
from storage.sqlite import SQLiteRunStore


def _create_run(store: SQLiteRunStore, run_id: str, source_id: str = "rss:test") -> None:
    """Insert a run row required by evidence/versions foreign keys."""
    store.create_run_log(RunLog(id=run_id, source_id=source_id))


@pytest.mark.integration
def test_canonicalize_url_rules():
    """URL canonicalization should apply dedupe rules deterministically."""
    url = (
        "http://Example.COM/News/Item?b=2&utm_source=newsletter"
        "&a=1&sessionid=abc#section"
    )
    canonical = canonicalize_url(url)
    assert canonical == "https://example.com/news/item?a=1&b=2"
    assert canonicalize_url("http://example.com") == "https://example.com/"


@pytest.mark.integration
def test_storage_upsert_dedup_and_versioning(tmp_path):
    """Upsert should dedupe by (canonical_url, source_id) and version on content change."""
    db_path = tmp_path / "collector.db"
    store = SQLiteRunStore(db_path)

    _create_run(store, "run-1")
    _create_run(store, "run-2")
    _create_run(store, "run-3")
    _create_run(store, "run-4", source_id="rss:other")

    draft_1 = ArticleDraft(
        canonical_url=(
            "http://EXAMPLE.com/News/Item?b=2&utm_source=feed&a=1&sessionid=abc#frag"
        ),
        source_id="rss:test",
        title="Version One",
        author_hint="Jane Doe",
        published_at=datetime(2026, 2, 20, 9, 0, tzinfo=UTC),
        snippet="Initial snippet",
    )
    evidence_1 = create_evidence(
        article_id="draft",
        claim_path="/title",
        evidence_type=EvidenceType.META_TAG,
        source_url="https://example.com/news/item",
        extracted_text="Version One",
        run_id="run-1",
        extraction_method="meta.og:title",
    )

    article_1, created_1, updated_1 = store.upsert_article(draft_1, [evidence_1], "run-1")
    assert created_1 is True
    assert updated_1 is False
    assert article_1.version == 1
    assert article_1.canonical_url == "https://example.com/news/item?a=1&b=2"

    # Same logical URL + same content should dedupe and avoid version bump.
    draft_2 = ArticleDraft(
        canonical_url="https://example.com/news/item?a=1&b=2",
        source_id="rss:test",
        title="Version One",
        author_hint="Jane Doe",
        published_at=datetime(2026, 2, 20, 9, 0, tzinfo=UTC),
        snippet="Initial snippet",
    )
    evidence_2 = create_evidence(
        article_id="draft",
        claim_path="/title",
        evidence_type=EvidenceType.JSON_LD,
        source_url="https://example.com/news/item",
        extracted_text="Version One",
        run_id="run-2",
        extraction_method="json_ld.headline",
    )

    article_2, created_2, updated_2 = store.upsert_article(draft_2, [evidence_2], "run-2")
    assert created_2 is False
    assert updated_2 is False
    assert article_2.id == article_1.id
    assert article_2.version == 1

    # Content change should bump version and write new versions row.
    draft_3 = ArticleDraft(
        canonical_url="https://example.com/news/item?b=2&a=1",
        source_id="rss:test",
        title="Version Two",
        author_hint="Jane Doe",
        published_at=datetime(2026, 2, 20, 9, 0, tzinfo=UTC),
        snippet="Updated snippet",
    )
    evidence_3 = create_evidence(
        article_id="draft",
        claim_path="/title",
        evidence_type=EvidenceType.META_TAG,
        source_url="https://example.com/news/item",
        extracted_text="Version Two",
        run_id="run-3",
        extraction_method="meta.og:title",
    )

    article_3, created_3, updated_3 = store.upsert_article(draft_3, [evidence_3], "run-3")
    assert created_3 is False
    assert updated_3 is True
    assert article_3.id == article_1.id
    assert article_3.version == 2

    # Same canonical URL with different source_id should create a new article.
    draft_4 = ArticleDraft(
        canonical_url="https://example.com/news/item?a=1&b=2",
        source_id="rss:other",
        title="Other Source",
        author_hint="Jane Doe",
        published_at=datetime(2026, 2, 20, 9, 0, tzinfo=UTC),
        snippet="From another source",
    )
    evidence_4 = create_evidence(
        article_id="draft",
        claim_path="/title",
        evidence_type=EvidenceType.META_TAG,
        source_url="https://example.com/news/item",
        extracted_text="Other Source",
        run_id="run-4",
        extraction_method="meta.og:title",
    )
    article_4, created_4, updated_4 = store.upsert_article(draft_4, [evidence_4], "run-4")
    assert created_4 is True
    assert updated_4 is False
    assert article_4.id != article_1.id

    connection = sqlite3.connect(db_path)
    article_count = connection.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    version_rows = connection.execute(
        "SELECT version, run_id FROM versions WHERE article_id = ? ORDER BY version",
        (article_1.id,),
    ).fetchall()
    evidence_count_latest = connection.execute(
        "SELECT COUNT(*) FROM evidence WHERE article_id = ?",
        (article_1.id,),
    ).fetchone()[0]
    connection.close()

    assert article_count == 2
    assert version_rows == [(1, "run-1"), (2, "run-3")]
    assert evidence_count_latest == 1

