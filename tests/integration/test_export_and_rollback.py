"""Integration tests for export fail-fast validation and run rollback."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

import pytest

from author_collector.cli import main as cli_main
from core.evidence import create_evidence
from core.models import ArticleDraft, EvidenceType, FetchLog, RunLog
from storage.sqlite import SQLiteRunStore


def _create_run(store: SQLiteRunStore, run_id: str, source_id: str = "rss:test") -> None:
    """Insert run row for foreign keys."""
    store.create_run_log(RunLog(id=run_id, source_id=source_id))


def _json_lines(stdout: str) -> list[dict]:
    """Parse JSON log lines emitted by CLI commands."""
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    return [json.loads(line) for line in lines]


@pytest.mark.integration
def test_export_fails_fast_on_invalid_row(tmp_path, capsys):
    """Export should stop on first invalid row and raise ValueError."""
    db_path = tmp_path / "collector.db"
    output_path = tmp_path / "export.jsonl"
    store = SQLiteRunStore(db_path)

    _create_run(store, "run-1")
    _create_run(store, "run-2")

    draft_valid = ArticleDraft(
        canonical_url="https://example.com/a",
        source_id="rss:test",
        title="Valid Title",
        author_hint="Jane Doe",
        published_at=datetime(2026, 2, 20, 9, 0, tzinfo=UTC),
        snippet="Valid snippet",
    )
    evidence_valid = create_evidence(
        article_id="draft",
        claim_path="/title",
        evidence_type=EvidenceType.META_TAG,
        source_url="https://example.com/a",
        extracted_text="Valid Title",
        run_id="run-1",
        extraction_method="meta.og:title",
    )
    article_valid, _, _ = store.upsert_article(draft_valid, [evidence_valid], "run-1")

    draft_invalid = ArticleDraft(
        canonical_url="https://example.com/b",
        source_id="rss:test",
        title="Will be corrupted",
        author_hint="Jane Doe",
        published_at=datetime(2026, 2, 20, 9, 5, tzinfo=UTC),
        snippet="Corrupt this row in DB",
    )
    evidence_invalid = create_evidence(
        article_id="draft",
        claim_path="/title",
        evidence_type=EvidenceType.META_TAG,
        source_url="https://example.com/b",
        extracted_text="Will be corrupted",
        run_id="run-2",
        extraction_method="meta.og:title",
    )
    article_invalid, _, _ = store.upsert_article(draft_invalid, [evidence_invalid], "run-2")

    # Corrupt second row to violate article schema (version must be >= 1).
    connection = sqlite3.connect(db_path)
    connection.execute("UPDATE articles SET version = 0 WHERE id = ?", (article_invalid.id,))
    connection.commit()
    connection.close()

    exit_code = cli_main(
        [
            "export",
            "--output",
            str(output_path),
            "--db",
            str(db_path),
            "--run-id",
            "run-export-1",
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 1
    events = _json_lines(captured.out)
    assert events[-1]["event_type"] == "cli_error"
    assert events[-1]["run_id"] == "run-export-1"
    assert "Export validation failed" in events[-1]["error"]
    assert article_invalid.id in events[-1]["error"]

    exported_lines = [line for line in output_path.read_text(encoding="utf-8").splitlines() if line]
    assert len(exported_lines) == 1
    first_row = json.loads(exported_lines[0])
    assert first_row["id"] == article_valid.id


@pytest.mark.integration
def test_rollback_run_minimal_restores_versions_and_deletes_new_articles(tmp_path, capsys):
    """Rollback by run_id should remove run artifacts and restore prior article snapshot."""
    db_path = tmp_path / "collector.db"
    store = SQLiteRunStore(db_path)

    _create_run(store, "run-1")
    _create_run(store, "run-2")

    draft_v1 = ArticleDraft(
        canonical_url="https://example.com/article",
        source_id="rss:test",
        title="Title V1",
        author_hint="Jane Doe",
        published_at=datetime(2026, 2, 20, 9, 0, tzinfo=UTC),
        snippet="Snippet V1",
    )
    ev_v1 = create_evidence(
        article_id="draft",
        claim_path="/title",
        evidence_type=EvidenceType.META_TAG,
        source_url="https://example.com/article",
        extracted_text="Title V1",
        run_id="run-1",
        extraction_method="meta.og:title",
    )
    article_v1, _, _ = store.upsert_article(draft_v1, [ev_v1], "run-1")

    draft_v2 = ArticleDraft(
        canonical_url="https://example.com/article",
        source_id="rss:test",
        title="Title V2",
        author_hint="Jane Doe",
        published_at=datetime(2026, 2, 20, 9, 0, tzinfo=UTC),
        snippet="Snippet V2",
    )
    ev_v2 = create_evidence(
        article_id="draft",
        claim_path="/title",
        evidence_type=EvidenceType.JSON_LD,
        source_url="https://example.com/article",
        extracted_text="Title V2",
        run_id="run-2",
        extraction_method="json_ld.headline",
    )
    store.upsert_article(draft_v2, [ev_v2], "run-2")

    draft_new = ArticleDraft(
        canonical_url="https://example.com/new",
        source_id="rss:test",
        title="Run2 New Article",
        author_hint="Jane Doe",
        published_at=datetime(2026, 2, 20, 10, 0, tzinfo=UTC),
        snippet="Created in run2",
    )
    ev_new = create_evidence(
        article_id="draft",
        claim_path="/title",
        evidence_type=EvidenceType.META_TAG,
        source_url="https://example.com/new",
        extracted_text="Run2 New Article",
        run_id="run-2",
        extraction_method="meta.og:title",
    )
    article_new, _, _ = store.upsert_article(draft_new, [ev_new], "run-2")

    store.save_fetch_log(
        FetchLog(
            url="https://example.com/new",
            status_code=200,
            latency_ms=10,
            bytes_received=100,
            run_id="run-2",
        )
    )

    exit_code = cli_main(["rollback", "--run", "run-2", "--db", str(db_path)])
    assert exit_code == 0
    rollback_events = _json_lines(capsys.readouterr().out)
    assert rollback_events[-1]["event_type"] == "cli_rollback_completed"
    assert rollback_events[-1]["run_id"] == "run-2"
    assert rollback_events[-1]["target_run_id"] == "run-2"

    connection = sqlite3.connect(db_path)
    remaining_article = connection.execute(
        "SELECT id, title, snippet, version FROM articles WHERE id = ?",
        (article_v1.id,),
    ).fetchone()
    deleted_article = connection.execute(
        "SELECT id FROM articles WHERE id = ?",
        (article_new.id,),
    ).fetchone()
    remaining_versions = connection.execute(
        "SELECT version, run_id FROM versions WHERE article_id = ? ORDER BY version",
        (article_v1.id,),
    ).fetchall()
    run2_fetch_logs = connection.execute(
        "SELECT COUNT(*) FROM fetch_log WHERE run_id = ?",
        ("run-2",),
    ).fetchone()[0]
    run2_evidence = connection.execute(
        "SELECT COUNT(*) FROM evidence WHERE run_id = ?",
        ("run-2",),
    ).fetchone()[0]
    restored_evidence = connection.execute(
        """
        SELECT claim_path, evidence_type, extracted_text, run_id
        FROM evidence
        WHERE article_id = ?
        ORDER BY id
        """,
        (article_v1.id,),
    ).fetchall()
    run2_status = connection.execute(
        "SELECT status FROM run_log WHERE id = ?",
        ("run-2",),
    ).fetchone()[0]
    connection.close()

    assert remaining_article == (article_v1.id, "Title V1", "Snippet V1", 1)
    assert deleted_article is None
    assert remaining_versions == [(1, "run-1")]
    assert run2_fetch_logs == 0
    assert run2_evidence == 0
    assert restored_evidence == [("/title", "meta_tag", "Title V1", "run-1")]
    assert run2_status == "CANCELLED"


@pytest.mark.integration
def test_rollback_preserves_previous_evidence_when_run_has_no_content_version_bump(tmp_path, capsys):
    """Rollback should keep earlier evidence when reverted run did not create a new version."""
    db_path = tmp_path / "collector.db"
    store = SQLiteRunStore(db_path)

    _create_run(store, "run-1")
    _create_run(store, "run-2")

    draft_v1 = ArticleDraft(
        canonical_url="https://example.com/article",
        source_id="rss:test",
        title="Stable Title",
        author_hint="Jane Doe",
        published_at=datetime(2026, 2, 20, 9, 0, tzinfo=UTC),
        snippet="Stable snippet",
    )
    ev_v1 = create_evidence(
        article_id="draft",
        claim_path="/title",
        evidence_type=EvidenceType.META_TAG,
        source_url="https://example.com/article",
        extracted_text="Stable Title",
        run_id="run-1",
        extraction_method="meta.og:title",
    )
    article_v1, created_1, updated_1 = store.upsert_article(draft_v1, [ev_v1], "run-1")
    assert created_1 is True
    assert updated_1 is False

    # Same content hash; should not create a new version and should keep existing evidence snapshot.
    draft_same = ArticleDraft(
        canonical_url="https://example.com/article",
        source_id="rss:test",
        title="Stable Title",
        author_hint="Jane Doe",
        published_at=datetime(2026, 2, 20, 9, 0, tzinfo=UTC),
        snippet="Stable snippet",
    )
    ev_run2 = create_evidence(
        article_id="draft",
        claim_path="/title",
        evidence_type=EvidenceType.JSON_LD,
        source_url="https://example.com/article",
        extracted_text="Stable Title (run2)",
        run_id="run-2",
        extraction_method="json_ld.headline",
    )
    article_v2, created_2, updated_2 = store.upsert_article(draft_same, [ev_run2], "run-2")
    assert article_v2.id == article_v1.id
    assert created_2 is False
    assert updated_2 is False

    exit_code = cli_main(["rollback", "--run", "run-2", "--db", str(db_path)])
    assert exit_code == 0
    rollback_events = _json_lines(capsys.readouterr().out)
    assert rollback_events[-1]["event_type"] == "cli_rollback_completed"
    assert rollback_events[-1]["run_id"] == "run-2"

    connection = sqlite3.connect(db_path)
    versions = connection.execute(
        "SELECT version, run_id FROM versions WHERE article_id = ? ORDER BY version",
        (article_v1.id,),
    ).fetchall()
    evidence_rows = connection.execute(
        """
        SELECT evidence_type, extracted_text, run_id
        FROM evidence
        WHERE article_id = ?
        ORDER BY id
        """,
        (article_v1.id,),
    ).fetchall()
    run2_evidence_count = connection.execute(
        "SELECT COUNT(*) FROM evidence WHERE run_id = ?",
        ("run-2",),
    ).fetchone()[0]
    connection.close()

    assert versions == [(1, "run-1")]
    assert run2_evidence_count == 0
    assert evidence_rows == [("meta_tag", "Stable Title", "run-1")]
