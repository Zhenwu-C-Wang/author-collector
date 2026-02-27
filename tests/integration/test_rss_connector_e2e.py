"""End-to-end integration test for RSS connector + sync command."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import jsonschema
import pytest

from author_collector.cli import main as cli_main
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


def _json_lines(stdout: str) -> list[dict]:
    """Parse JSON log lines emitted by CLI commands."""
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    return [json.loads(line) for line in lines]


@pytest.mark.integration
def test_rss_sync_command_e2e(tmp_path, monkeypatch, capsys):
    """`author-collector sync` should complete full RSS connector pipeline."""
    fixture_feed = Path(__file__).resolve().parents[1] / "fixtures" / "rss" / "example.xml"
    db_path = tmp_path / "collector.db"
    run_id = "run-rss-e2e"
    output_file = tmp_path / f"export_{run_id}.jsonl"

    robots_session = DummySession([DummyResponse(404, body=b"")])
    content_session = DummySession(
        [
            DummyResponse(
                200,
                headers={"content-type": "text/html; charset=utf-8"},
                body=(
                    b"<html><head>"
                    b"<title>Article One</title>"
                    b"<meta property='og:title' content='Article One'/>"
                    b"<meta name='author' content='Jane Doe'/>"
                    b"<meta property='article:published_time' content='2026-02-27T10:00:00Z'/>"
                    b"</head><body><article><p>Article one body.</p></article></body></html>"
                ),
            ),
            DummyResponse(
                200,
                headers={"content-type": "text/html; charset=utf-8"},
                body=(
                    b"<html><head>"
                    b"<title>Article Two</title>"
                    b"<meta property='og:title' content='Article Two'/>"
                    b"<meta name='author' content='Jane Doe'/>"
                    b"<meta property='article:published_time' content='2026-02-27T11:00:00Z'/>"
                    b"</head><body><article><p>Article two body.</p></article></body></html>"
                ),
            ),
            DummyResponse(
                200,
                headers={"content-type": "text/html; charset=utf-8"},
                body=(
                    b"<html><head>"
                    b"<title>Article Three</title>"
                    b"<meta property='og:title' content='Article Three'/>"
                    b"<meta name='author' content='Jane Doe'/>"
                    b"<meta property='article:published_time' content='2026-02-27T12:00:00Z'/>"
                    b"</head><body><article><p>Article three body.</p></article></body></html>"
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
            "rss:example_feed",
            "--seed",
            str(fixture_feed),
            "--db",
            str(db_path),
            "--run-id",
            run_id,
        ]
    )
    assert exit_code == 0
    sync_events = _json_lines(capsys.readouterr().out)
    sync_summary = [item for item in sync_events if item.get("event_type") == "cli_sync_completed"]
    assert sync_summary
    assert sync_summary[-1]["run_id"] == run_id
    assert sync_summary[-1]["status"] == "COMPLETED"
    assert output_file.exists()

    lines = [line for line in output_file.read_text(encoding="utf-8").splitlines() if line]
    assert len(lines) == 3

    article_schema = _load_article_schema()
    for line in lines:
        payload = json.loads(line)
        jsonschema.validate(payload, article_schema)

    connection = sqlite3.connect(db_path)
    article_count = connection.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    evidence_count = connection.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]
    version_count = connection.execute("SELECT COUNT(*) FROM versions").fetchone()[0]
    fetch_count = connection.execute("SELECT COUNT(*) FROM fetch_log").fetchone()[0]
    run_status = connection.execute("SELECT status FROM run_log WHERE id = ?", (run_id,)).fetchone()[0]
    run_counts = connection.execute(
        "SELECT new_articles_count, updated_articles_count FROM run_log WHERE id = ?",
        (run_id,),
    ).fetchone()
    connection.close()

    assert article_count == 3
    assert evidence_count >= 3
    assert version_count == 3
    assert fetch_count == 3
    assert run_status == "COMPLETED"
    assert run_counts == (3, 0)

    # Robots should be fetched once due to per-domain cache.
    assert len(robots_session.calls) == 1


@pytest.mark.integration
def test_rss_sync_run_counts_track_new_and_updated(tmp_path, monkeypatch):
    """Two sync runs should track new vs updated article counts accurately."""
    fixture_feed = Path(__file__).resolve().parents[1] / "fixtures" / "rss" / "example.xml"
    db_path = tmp_path / "collector.db"
    run_1 = "run-rss-counts-1"
    run_2 = "run-rss-counts-2"

    robots_session = DummySession([DummyResponse(404, body=b""), DummyResponse(404, body=b"")])
    content_session_run_1 = DummySession(
        [
            DummyResponse(
                200,
                headers={"content-type": "text/html; charset=utf-8"},
                body=(
                    b"<html><head>"
                    b"<title>Article One</title>"
                    b"<meta property='og:title' content='Article One'/>"
                    b"<meta name='author' content='Jane Doe'/>"
                    b"<meta property='article:published_time' content='2026-02-27T10:00:00Z'/>"
                    b"</head><body><article><p>Article one body.</p></article></body></html>"
                ),
            ),
            DummyResponse(
                200,
                headers={"content-type": "text/html; charset=utf-8"},
                body=(
                    b"<html><head>"
                    b"<title>Article Two</title>"
                    b"<meta property='og:title' content='Article Two'/>"
                    b"<meta name='author' content='Jane Doe'/>"
                    b"<meta property='article:published_time' content='2026-02-27T11:00:00Z'/>"
                    b"</head><body><article><p>Article two body.</p></article></body></html>"
                ),
            ),
            DummyResponse(
                200,
                headers={"content-type": "text/html; charset=utf-8"},
                body=(
                    b"<html><head>"
                    b"<title>Article Three</title>"
                    b"<meta property='og:title' content='Article Three'/>"
                    b"<meta name='author' content='Jane Doe'/>"
                    b"<meta property='article:published_time' content='2026-02-27T12:00:00Z'/>"
                    b"</head><body><article><p>Article three body.</p></article></body></html>"
                ),
            ),
        ]
    )
    content_session_run_2 = DummySession(
        [
            DummyResponse(
                200,
                headers={"content-type": "text/html; charset=utf-8"},
                body=(
                    b"<html><head>"
                    b"<title>Article One</title>"
                    b"<meta property='og:title' content='Article One'/>"
                    b"<meta name='author' content='Jane Doe'/>"
                    b"<meta property='article:published_time' content='2026-02-27T10:00:00Z'/>"
                    b"</head><body><article><p>Article one body.</p></article></body></html>"
                ),
            ),
            DummyResponse(
                200,
                headers={"content-type": "text/html; charset=utf-8"},
                body=(
                    b"<html><head>"
                    b"<title>Article Two Updated</title>"
                    b"<meta property='og:title' content='Article Two Updated'/>"
                    b"<meta name='author' content='Jane Doe'/>"
                    b"<meta property='article:published_time' content='2026-02-27T11:00:00Z'/>"
                    b"</head><body><article><p>Article two body updated.</p></article></body></html>"
                ),
            ),
            DummyResponse(
                200,
                headers={"content-type": "text/html; charset=utf-8"},
                body=(
                    b"<html><head>"
                    b"<title>Article Three</title>"
                    b"<meta property='og:title' content='Article Three'/>"
                    b"<meta name='author' content='Jane Doe'/>"
                    b"<meta property='article:published_time' content='2026-02-27T12:00:00Z'/>"
                    b"</head><body><article><p>Article three body.</p></article></body></html>"
                ),
            ),
        ]
    )
    fetch_factories = iter([content_session_run_1, content_session_run_2])

    def _make_fetch_stage():
        content_session = next(fetch_factories)
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

    exit_code_1 = cli_main(
        [
            "sync",
            "--source-id",
            "rss:example_feed",
            "--seed",
            str(fixture_feed),
            "--db",
            str(db_path),
            "--run-id",
            run_1,
        ]
    )
    exit_code_2 = cli_main(
        [
            "sync",
            "--source-id",
            "rss:example_feed",
            "--seed",
            str(fixture_feed),
            "--db",
            str(db_path),
            "--run-id",
            run_2,
        ]
    )
    assert exit_code_1 == 0
    assert exit_code_2 == 0

    connection = sqlite3.connect(db_path)
    run_1_counts = connection.execute(
        "SELECT new_articles_count, updated_articles_count FROM run_log WHERE id = ?",
        (run_1,),
    ).fetchone()
    run_2_counts = connection.execute(
        "SELECT new_articles_count, updated_articles_count FROM run_log WHERE id = ?",
        (run_2,),
    ).fetchone()
    connection.close()

    assert run_1_counts == (3, 0)
    assert run_2_counts == (0, 1)
