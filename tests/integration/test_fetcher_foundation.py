"""Integration-style tests for the Milestone 1 fetcher foundation."""

from __future__ import annotations

from contextlib import contextmanager
import json

import pytest
import requests

from core.models import FetchErrorCode
from fetcher.http import HttpFetchStage, fetch_url
from fetcher.politeness import PolitenessController
from fetcher.robots import RobotsTxtChecker


class DummyResponse:
    """Minimal response object for exercising fetcher logic."""

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
        return self._body.decode("utf-8", errors="replace")

    def iter_content(self, chunk_size: int = 8192):
        for index in range(0, len(self._body), chunk_size):
            yield self._body[index : index + chunk_size]

    def close(self) -> None:
        return None


class DummySession:
    """Sequence-driven session for deterministic HTTP behavior."""

    def __init__(self, responses: list[object] | None = None) -> None:
        self.responses = list(responses or [])
        self.calls: list[str] = []

    def get(self, url: str, **_: object):
        self.calls.append(url)
        if not self.responses:
            raise AssertionError("No more stubbed responses available")
        next_item = self.responses.pop(0)
        if isinstance(next_item, Exception):
            raise next_item
        return next_item


class StubPoliteness:
    """Capture delay multipliers passed from robots checks."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, float]] = []

    @contextmanager
    def request_slot(self, domain: str, delay_multiplier: float = 1.0):
        self.calls.append((domain, delay_multiplier))
        yield


def _parse_json_lines(captured: str) -> list[dict[str, object]]:
    """Decode structured log lines emitted to stdout."""
    lines = [line for line in captured.splitlines() if line.strip()]
    return [json.loads(line) for line in lines]


@pytest.mark.integration

def test_fetch_success_200(monkeypatch):
    """200 fetch returns FetchedDoc + populated FetchLog."""
    session = DummySession(
        [DummyResponse(200, headers={"content-type": "text/html"}, body=b"hello")]
    )
    monkeypatch.setattr("fetcher.http._resolve_ip_addresses", lambda host: {"93.184.216.34"})

    doc, log = fetch_url("https://example.com/post", run_id="run-1", session=session)

    assert doc is not None
    assert doc.status_code == 200
    assert doc.body_bytes == b"hello"
    assert log.status_code == 200
    assert log.bytes_received == 5
    assert log.error_code is None


@pytest.mark.integration

def test_fetch_supports_304(monkeypatch):
    """304 fetch returns no body and no body hash."""
    session = DummySession([DummyResponse(304, headers={"etag": '"v1"'})])
    monkeypatch.setattr("fetcher.http._resolve_ip_addresses", lambda host: {"93.184.216.34"})

    doc, log = fetch_url("https://example.com/post", run_id="run-1", session=session)

    assert doc is not None
    assert doc.status_code == 304
    assert doc.body_bytes is None
    assert doc.body_sha256 is None
    assert log.status_code == 304
    assert log.bytes_received == 0


@pytest.mark.integration
@pytest.mark.parametrize(
    ("url", "resolved_ip"),
    [
        ("http://localhost/resource", "127.0.0.1"),
        ("http://10.0.0.1", "10.0.0.1"),
        ("http://127.0.0.1", "127.0.0.1"),
        ("http://192.168.1.1", "192.168.1.1"),
    ],
)
def test_fetch_blocks_private_ip(monkeypatch, url: str, resolved_ip: str):
    """Private/internal hosts must be blocked as SECURITY_BLOCKED."""
    monkeypatch.setattr("fetcher.http._resolve_ip_addresses", lambda host: {resolved_ip})

    doc, log = fetch_url(url, run_id="run-1", session=DummySession())

    assert doc is None
    assert log.error_code == FetchErrorCode.SECURITY_BLOCKED


@pytest.mark.integration

def test_fetch_redirect_limit(monkeypatch):
    """More than 5 redirects should fail with REDIRECT_LIMIT."""
    redirect_responses = [
        DummyResponse(302, headers={"location": f"/hop-{index + 1}"})
        for index in range(6)
    ]
    session = DummySession(redirect_responses)
    monkeypatch.setattr("fetcher.http._resolve_ip_addresses", lambda host: {"93.184.216.34"})

    doc, log = fetch_url("https://example.com/start", run_id="run-1", session=session)

    assert doc is None
    assert log.error_code == FetchErrorCode.REDIRECT_LIMIT


@pytest.mark.integration
def test_fetch_redirect_limit_allows_five_hops(monkeypatch):
    """Exactly 5 redirects should still pass and return final response."""
    redirect_responses = [
        DummyResponse(302, headers={"location": f"/hop-{index + 1}"})
        for index in range(5)
    ]
    session = DummySession(
        redirect_responses
        + [DummyResponse(200, headers={"content-type": "text/html"}, body=b"done")]
    )
    monkeypatch.setattr("fetcher.http._resolve_ip_addresses", lambda host: {"93.184.216.34"})

    doc, log = fetch_url("https://example.com/start", run_id="run-1", session=session)

    assert doc is not None
    assert doc.status_code == 200
    assert log.status_code == 200
    assert log.error_code is None


@pytest.mark.integration

def test_fetch_timeout_maps_to_error_code(monkeypatch):
    """Network timeout should map to TIMEOUT."""
    session = DummySession([requests.Timeout("slow network")])
    monkeypatch.setattr("fetcher.http._resolve_ip_addresses", lambda host: {"93.184.216.34"})

    doc, log = fetch_url("https://example.com/slow", run_id="run-1", session=session)

    assert doc is None
    assert log.error_code == FetchErrorCode.TIMEOUT


@pytest.mark.integration

def test_fetch_blocks_by_robots(monkeypatch):
    """Robots disallow should block before content fetch."""
    robots_session = DummySession(
        [
            DummyResponse(
                200,
                body=b"User-agent: *\nDisallow: /private\n",
            )
        ]
    )
    content_session = DummySession([])
    checker = RobotsTxtChecker(session=robots_session)

    monkeypatch.setattr("fetcher.http._resolve_ip_addresses", lambda host: {"93.184.216.34"})

    doc, log = fetch_url(
        "https://example.com/private/post",
        run_id="run-1",
        robots_checker=checker,
        session=content_session,
    )

    assert doc is None
    assert log.error_code == FetchErrorCode.BLOCKED_BY_ROBOTS
    assert content_session.calls == []


@pytest.mark.integration

def test_robots_cache_second_lookup_is_instant(monkeypatch):
    """Second robots lookup on same domain should hit cache (single HTTP call)."""
    robots_session = DummySession([DummyResponse(404, body=b"")])
    checker = RobotsTxtChecker(session=robots_session)

    allowed_1, _, _ = checker.can_fetch("https://example.com/post-1")
    allowed_2, _, _ = checker.can_fetch("https://example.com/post-2")

    assert allowed_1 is True
    assert allowed_2 is True
    assert len(robots_session.calls) == 1


@pytest.mark.integration

def test_robots_5xx_applies_delay_multiplier(monkeypatch):
    """Robots 5xx should increase domain delay multiplier to 2x."""
    robots_session = DummySession([DummyResponse(503, body=b"")])
    checker = RobotsTxtChecker(session=robots_session)
    politeness = StubPoliteness()
    content_session = DummySession([DummyResponse(200, headers={"content-type": "text/html"}, body=b"ok")])

    monkeypatch.setattr("fetcher.http._resolve_ip_addresses", lambda host: {"93.184.216.34"})

    doc, log = fetch_url(
        "https://example.com/post",
        run_id="run-1",
        robots_checker=checker,
        politeness=politeness,
        session=content_session,
    )

    assert doc is not None
    assert log.error_code is None
    assert politeness.calls == [("example.com", 2.0)]


@pytest.mark.integration
def test_fetch_emits_robots_warning_event(monkeypatch):
    """Robots 404 should emit a structured warning event."""
    robots_session = DummySession([DummyResponse(404, body=b"")])
    checker = RobotsTxtChecker(session=robots_session)
    content_session = DummySession([DummyResponse(200, headers={"content-type": "text/html"}, body=b"ok")])
    events: list[tuple[str, dict[str, object]]] = []

    monkeypatch.setattr("fetcher.http._resolve_ip_addresses", lambda host: {"93.184.216.34"})

    def capture_event(event_type: str, payload: dict[str, object]) -> None:
        events.append((event_type, payload))

    doc, log = fetch_url(
        "https://example.com/post",
        run_id="run-1",
        robots_checker=checker,
        session=content_session,
        event_hook=capture_event,
    )

    assert doc is not None
    assert log.error_code is None
    warning_events = [payload for event, payload in events if event == "robots_warning"]
    assert warning_events, "Expected at least one robots_warning event"
    assert warning_events[0]["robots_mode"] == "allow_all"
    assert warning_events[0]["delay_multiplier"] == 1.0


@pytest.mark.integration
def test_fetch_emits_robots_slowdown_event(monkeypatch):
    """Robots 5xx should emit explicit slowdown status."""
    robots_session = DummySession([DummyResponse(503, body=b"")])
    checker = RobotsTxtChecker(session=robots_session)
    content_session = DummySession([DummyResponse(200, headers={"content-type": "text/html"}, body=b"ok")])
    events: list[tuple[str, dict[str, object]]] = []
    politeness = StubPoliteness()

    monkeypatch.setattr("fetcher.http._resolve_ip_addresses", lambda host: {"93.184.216.34"})

    def capture_event(event_type: str, payload: dict[str, object]) -> None:
        events.append((event_type, payload))

    doc, log = fetch_url(
        "https://example.com/post",
        run_id="run-1",
        robots_checker=checker,
        politeness=politeness,
        session=content_session,
        event_hook=capture_event,
    )

    assert doc is not None
    assert log.error_code is None
    slowdown_events = [payload for event, payload in events if event == "robots_slowdown"]
    assert slowdown_events, "Expected robots_slowdown event"
    assert slowdown_events[0]["delay_multiplier"] == 2.0


@pytest.mark.integration
def test_http_fetch_stage_logs_200(monkeypatch, capsys):
    """HttpFetchStage should emit structured fetch_log for 200."""
    monkeypatch.setattr("fetcher.http._resolve_ip_addresses", lambda host: {"93.184.216.34"})
    stage = HttpFetchStage(
        session=DummySession(
            [DummyResponse(200, headers={"content-type": "text/html"}, body=b"ok")]
        ),
        politeness=PolitenessController(per_domain_delay_seconds=0.0, max_global_concurrency=1),
        event_logger=lambda event_type, payload: None,
    )

    doc, log = stage.fetch("https://example.com/post", run_id="run-log-200")
    lines = _parse_json_lines(capsys.readouterr().out)

    assert doc is not None
    assert log.status_code == 200
    assert lines[-1]["run_id"] == "run-log-200"
    assert lines[-1]["status_code"] == 200
    assert lines[-1]["error_code"] is None


@pytest.mark.integration
def test_http_fetch_stage_logs_304(monkeypatch, capsys):
    """HttpFetchStage should emit structured fetch_log for 304."""
    monkeypatch.setattr("fetcher.http._resolve_ip_addresses", lambda host: {"93.184.216.34"})
    stage = HttpFetchStage(
        session=DummySession([DummyResponse(304, headers={"etag": '"v1"'})]),
        politeness=PolitenessController(per_domain_delay_seconds=0.0, max_global_concurrency=1),
        event_logger=lambda event_type, payload: None,
    )

    doc, log = stage.fetch("https://example.com/post", run_id="run-log-304")
    lines = _parse_json_lines(capsys.readouterr().out)

    assert doc is not None
    assert log.status_code == 304
    assert lines[-1]["status_code"] == 304
    assert lines[-1]["bytes_received"] == 0
    assert lines[-1]["error_code"] is None


@pytest.mark.integration
def test_http_fetch_stage_logs_robots_disallow(monkeypatch, capsys):
    """HttpFetchStage should emit structured fetch_log for robots disallow."""
    monkeypatch.setattr("fetcher.http._resolve_ip_addresses", lambda host: {"93.184.216.34"})
    checker = RobotsTxtChecker(
        session=DummySession(
            [DummyResponse(200, body=b"User-agent: *\nDisallow: /private\n")]
        )
    )
    stage = HttpFetchStage(
        robots_checker=checker,
        session=DummySession([]),
        politeness=PolitenessController(per_domain_delay_seconds=0.0, max_global_concurrency=1),
        event_logger=lambda event_type, payload: None,
    )

    doc, log = stage.fetch("https://example.com/private/post", run_id="run-log-robots")
    lines = _parse_json_lines(capsys.readouterr().out)

    assert doc is None
    assert log.error_code == FetchErrorCode.BLOCKED_BY_ROBOTS
    assert lines[-1]["status_code"] is None
    assert lines[-1]["error_code"] == "BLOCKED_BY_ROBOTS"


@pytest.mark.integration
def test_http_fetch_stage_logs_timeout(monkeypatch, capsys):
    """HttpFetchStage should emit structured fetch_log for timeout."""
    monkeypatch.setattr("fetcher.http._resolve_ip_addresses", lambda host: {"93.184.216.34"})
    stage = HttpFetchStage(
        session=DummySession([requests.Timeout("slow network")]),
        politeness=PolitenessController(per_domain_delay_seconds=0.0, max_global_concurrency=1),
        event_logger=lambda event_type, payload: None,
    )

    doc, log = stage.fetch("https://example.com/slow", run_id="run-log-timeout")
    lines = _parse_json_lines(capsys.readouterr().out)

    assert doc is None
    assert log.error_code == FetchErrorCode.TIMEOUT
    assert lines[-1]["status_code"] is None
    assert lines[-1]["error_code"] == "TIMEOUT"


@pytest.mark.integration
def test_http_fetch_stage_emits_robots_warning_log(monkeypatch, capsys):
    """Default event logger should emit robots warning as structured JSON."""
    monkeypatch.setattr("fetcher.http._resolve_ip_addresses", lambda host: {"93.184.216.34"})
    checker = RobotsTxtChecker(session=DummySession([DummyResponse(404, body=b"")]))
    stage = HttpFetchStage(
        robots_checker=checker,
        session=DummySession(
            [DummyResponse(200, headers={"content-type": "text/html"}, body=b"ok")]
        ),
        politeness=PolitenessController(per_domain_delay_seconds=0.0, max_global_concurrency=1),
        log_fetches=False,
    )

    doc, log = stage.fetch("https://example.com/post", run_id="run-robots-warning")
    lines = _parse_json_lines(capsys.readouterr().out)
    warning_events = [line for line in lines if line.get("event_type") == "robots_warning"]

    assert doc is not None
    assert log.error_code is None
    assert warning_events
    assert warning_events[0]["robots_mode"] == "allow_all"
    assert warning_events[0]["delay_multiplier"] == 1.0


@pytest.mark.integration
def test_http_fetch_stage_emits_robots_slowdown_log(monkeypatch, capsys):
    """Default event logger should emit robots slowdown as structured JSON."""
    monkeypatch.setattr("fetcher.http._resolve_ip_addresses", lambda host: {"93.184.216.34"})
    checker = RobotsTxtChecker(session=DummySession([DummyResponse(503, body=b"")]))
    stage = HttpFetchStage(
        robots_checker=checker,
        session=DummySession(
            [DummyResponse(200, headers={"content-type": "text/html"}, body=b"ok")]
        ),
        politeness=PolitenessController(per_domain_delay_seconds=0.0, max_global_concurrency=1),
        log_fetches=False,
    )

    doc, log = stage.fetch("https://example.com/post", run_id="run-robots-slowdown")
    lines = _parse_json_lines(capsys.readouterr().out)
    slowdown_events = [line for line in lines if line.get("event_type") == "robots_slowdown"]

    assert doc is not None
    assert log.error_code is None
    assert slowdown_events
    assert slowdown_events[0]["delay_multiplier"] == 2.0


@pytest.mark.integration
def test_politeness_enforces_minimum_domain_gap():
    """Politeness controller should sleep between same-domain requests."""
    now = [100.0]
    sleeps: list[float] = []

    def fake_clock() -> float:
        return now[0]

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    controller = PolitenessController(
        per_domain_delay_seconds=5.0,
        max_global_concurrency=1,
        sleep_fn=fake_sleep,
        clock_fn=fake_clock,
    )

    controller.wait_for_domain("example.com")
    controller.wait_for_domain("example.com")

    assert sleeps == [5.0]
