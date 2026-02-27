"""Integration tests for Pipeline + SQLite run/fetch log persistence."""

from __future__ import annotations

import sqlite3

from core.models import ArticleDraft, FetchedDoc, Parsed
from core.pipeline import DiscoverStage, ExportStage, ExtractStage, ParseStage, Pipeline, StoreStage
from fetcher.http import HttpFetchStage
from fetcher.politeness import PolitenessController
from storage.sqlite import SQLiteRunStore


class DummyDiscoverStage(DiscoverStage):
    """Discover two deterministic URLs."""

    def discover(self, seed: str, run_id: str):
        _ = seed, run_id
        yield "https://example.com/a"
        yield "https://example.com/b"


class DummyParseStage(ParseStage):
    """Convert fetched doc to minimal Parsed payload."""

    def parse(self, fetched: FetchedDoc, run_id: str) -> Parsed:
        _ = run_id
        return Parsed(url=fetched.final_url, title="parsed")


class DummyExtractStage(ExtractStage):
    """Return minimal draft and no evidence for dry-run tests."""

    def extract(self, parsed: Parsed, run_id: str):
        _ = parsed, run_id
        return (
            ArticleDraft(canonical_url="https://example.com/a", source_id="rss:test", title="draft"),
            [],
        )


class DummyStoreStage(StoreStage):
    """Store stub should not be called in dry_run tests."""

    def store(self, draft: ArticleDraft, evidence_list, run_id: str):
        raise AssertionError("store() should not be called in dry_run mode")


class DummyExportStage(ExportStage):
    """Export stub should not be called in dry_run tests."""

    def export(self, output_path: str) -> int:
        raise AssertionError("export() should not be called in dry_run mode")


class DummyResponse:
    """Minimal response object used by HttpFetchStage session."""

    def __init__(self, status_code: int, body: bytes = b"ok") -> None:
        self.status_code = status_code
        self._body = body
        self.headers = {"content-type": "text/html"}

    def iter_content(self, chunk_size: int = 8192):
        for idx in range(0, len(self._body), chunk_size):
            yield self._body[idx : idx + chunk_size]

    def close(self) -> None:
        return None


class DummySession:
    """Deterministic session used by HttpFetchStage."""

    def __init__(self) -> None:
        self.calls = 0

    def get(self, url: str, **kwargs):
        _ = url, kwargs
        self.calls += 1
        return DummyResponse(status_code=200, body=b"content")


def test_pipeline_persists_run_and_fetch_logs(monkeypatch, tmp_path):
    """Pipeline should persist run_log and per-request fetch_log rows."""
    db_path = tmp_path / "collector.db"
    run_store = SQLiteRunStore(db_path)

    monkeypatch.setattr("fetcher.http._resolve_ip_addresses", lambda host: {"93.184.216.34"})

    fetch_stage = HttpFetchStage(
        session=DummySession(),
        politeness=PolitenessController(per_domain_delay_seconds=0.0, max_global_concurrency=1),
        log_fetches=False,
        event_logger=lambda event_type, payload: None,
    )

    pipeline = Pipeline(
        discover=DummyDiscoverStage(),
        fetch=fetch_stage,
        parse=DummyParseStage(),
        extract=DummyExtractStage(),
        store=DummyStoreStage(),
        export=DummyExportStage(),
        run_store=run_store,
    )

    run_log = pipeline.run(
        seed="https://example.com/feed",
        source_id="rss:test",
        run_id="run-123",
        dry_run=True,
    )

    assert run_log.id == "run-123"
    assert run_log.fetched_count == 2
    assert run_log.error_count == 0
    assert run_log.ended_at is not None

    connection = sqlite3.connect(db_path)
    run_row = connection.execute(
        "SELECT status, fetched_count, error_count, ended_at FROM run_log WHERE id = ?",
        ("run-123",),
    ).fetchone()
    fetch_rows = connection.execute(
        """
        SELECT url, status_code, error_code, bytes_received, run_id
        FROM fetch_log
        WHERE run_id = ?
        ORDER BY url
        """,
        ("run-123",),
    ).fetchall()
    connection.close()

    assert run_row is not None
    assert run_row[0] == "COMPLETED"
    assert run_row[1] == 2
    assert run_row[2] == 0
    assert run_row[3] is not None
    assert len(fetch_rows) == 2
    assert [row[0] for row in fetch_rows] == ["https://example.com/a", "https://example.com/b"]
    assert all(row[1] == 200 for row in fetch_rows)
    assert all(row[2] is None for row in fetch_rows)
    assert all(row[3] == 7 for row in fetch_rows)
    assert all(row[4] == "run-123" for row in fetch_rows)
