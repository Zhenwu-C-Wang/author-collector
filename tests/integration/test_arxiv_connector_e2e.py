"""End-to-end integration test for arXiv connector + sync command."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import jsonschema
import pytest

from author_collector.cli import main as cli_main
from connectors.arxiv import ArxivDiscoverStage
from fetcher.http import HttpFetchStage as RealHttpFetchStage
from fetcher.politeness import PolitenessController
from fetcher.robots import RobotsTxtChecker


class DummyResponse:
    """Minimal HTTP response object for deterministic fetch behavior."""

    def __init__(
        self,
        status_code: int,
        headers: dict[str, str] | None = None,
        body: bytes = b"",
        history: list[object] | None = None,
    ) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self._body = body
        self.history = history or []

    @property
    def text(self) -> str:
        """Decode body as UTF-8 text."""
        return self._body.decode("utf-8", errors="replace")

    def iter_content(self, chunk_size: int = 8192):
        """Yield content chunks."""
        for index in range(0, len(self._body), chunk_size):
            yield self._body[index : index + chunk_size]

    def close(self) -> None:
        """Compatibility no-op."""
        return None

    def raise_for_status(self) -> None:
        """Mimic requests.Response.raise_for_status."""
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP error: {self.status_code}")


class DummySession:
    """Deterministic response queue for HTTP calls."""

    def __init__(self, responses: list[DummyResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[str] = []

    def get(self, url: str, **_: object):
        """Return next queued response."""
        self.calls.append(url)
        if not self.responses:
            raise AssertionError(f"No stubbed response left for URL: {url}")
        return self.responses.pop(0)


def _load_article_schema() -> dict:
    """Load article schema for export validation assertions."""
    schema_path = Path(__file__).resolve().parents[2] / "schemas" / "article.schema.json"
    return json.loads(schema_path.read_text(encoding="utf-8"))


@pytest.mark.integration
def test_arxiv_sync_command_e2e(tmp_path, monkeypatch):
    """`author-collector sync` should complete full arXiv connector pipeline."""
    fixture_feed = Path(__file__).resolve().parents[1] / "fixtures" / "arxiv" / "response.atom"
    db_path = tmp_path / "collector.db"
    run_id = "run-arxiv-e2e"
    output_file = tmp_path / f"export_{run_id}.jsonl"

    robots_session = DummySession([DummyResponse(404, body=b"")])
    content_session = DummySession(
        [
            DummyResponse(
                200,
                headers={"content-type": "text/html; charset=utf-8"},
                body=(
                    b"<html><head><title>Paper One</title>"
                    b"<meta property='og:title' content='Paper One'/>"
                    b"<meta name='author' content='Jane Doe'/>"
                    b"<meta property='article:published_time' content='2026-02-27T10:00:00Z'/>"
                    b"</head><body><article><p>Paper one abstract.</p></article></body></html>"
                ),
            ),
            DummyResponse(
                200,
                headers={"content-type": "text/html; charset=utf-8"},
                body=(
                    b"<html><head><title>Paper Two</title>"
                    b"<meta property='og:title' content='Paper Two'/>"
                    b"<meta name='author' content='Jane Doe'/>"
                    b"<meta property='article:published_time' content='2026-02-27T11:00:00Z'/>"
                    b"</head><body><article><p>Paper two abstract.</p></article></body></html>"
                ),
            ),
            DummyResponse(
                200,
                headers={"content-type": "text/html; charset=utf-8"},
                body=(
                    b"<html><head><title>Paper Three</title>"
                    b"<meta property='og:title' content='Paper Three'/>"
                    b"<meta name='author' content='Jane Doe'/>"
                    b"<meta property='article:published_time' content='2026-02-27T12:00:00Z'/>"
                    b"</head><body><article><p>Paper three abstract.</p></article></body></html>"
                ),
            ),
        ]
    )

    def _make_fetch_stage():
        """Build fetch stage with deterministic robots/content sessions."""
        return RealHttpFetchStage(
            robots_checker=RobotsTxtChecker(session=robots_session, user_agent="author-collector"),
            politeness=PolitenessController(per_domain_delay_seconds=0.0, max_global_concurrency=1),
            session=content_session,
            log_fetches=False,
            event_logger=lambda event_type, payload: None,
        )

    monkeypatch.setattr("author_collector.cli.HttpFetchStage", _make_fetch_stage)
    monkeypatch.setattr("fetcher.http._resolve_ip_addresses", lambda host: {"93.184.216.34"})
    monkeypatch.chdir(tmp_path)

    exit_code = cli_main(
        [
            "sync",
            "--source-id",
            "arxiv:query",
            "--seed",
            str(fixture_feed),
            "--db",
            str(db_path),
            "--run-id",
            run_id,
        ]
    )
    assert exit_code == 0
    assert output_file.exists()

    lines = [line for line in output_file.read_text(encoding="utf-8").splitlines() if line]
    assert len(lines) == 3

    article_schema = _load_article_schema()
    for line in lines:
        payload = json.loads(line)
        jsonschema.validate(payload, article_schema)
        assert payload["source_id"] == "arxiv:query"

    connection = sqlite3.connect(db_path)
    article_count = connection.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    version_count = connection.execute("SELECT COUNT(*) FROM versions").fetchone()[0]
    fetch_count = connection.execute("SELECT COUNT(*) FROM fetch_log").fetchone()[0]
    run_status = connection.execute("SELECT status FROM run_log WHERE id = ?", (run_id,)).fetchone()[0]
    connection.close()

    assert article_count == 3
    assert version_count == 3
    assert fetch_count == 3
    assert run_status == "COMPLETED"
    assert len(robots_session.calls) == 1
    assert all("/pdf/" not in url for url in content_session.calls)


@pytest.mark.integration
def test_arxiv_discover_accepts_query_seed(tmp_path):
    """ArXiv discover stage should accept raw query seed via official API URL."""
    fixture_feed = Path(__file__).resolve().parents[1] / "fixtures" / "arxiv" / "response.atom"
    atom_text = fixture_feed.read_text(encoding="utf-8")
    discovery_session = DummySession(
        [DummyResponse(200, headers={"content-type": "application/atom+xml"}, body=atom_text.encode("utf-8"))]
    )
    stage = ArxivDiscoverStage(session=discovery_session)

    urls = list(stage.discover("au:Jane Doe", run_id="run-arxiv-query"))

    assert urls == [
        "https://arxiv.org/abs/2602.00001",
        "https://arxiv.org/abs/2602.00002",
        "https://arxiv.org/abs/2602.00003",
    ]
    assert len(discovery_session.calls) == 1
    assert (
        discovery_session.calls[0]
        == "https://export.arxiv.org/api/query?search_query=au%3AJane+Doe&start=0&max_results=100"
    )
