"""HTTP fetcher with robots, politeness, and SSRF protections."""

from __future__ import annotations

import hashlib
import socket
import time
from collections.abc import Callable
from ipaddress import ip_address, ip_network
from typing import Iterable
from urllib.parse import urljoin, urlparse

import requests

from core.config import ComplianceConfig
from core.models import FetchErrorCode, FetchedDoc, FetchLog
from core.pipeline import FetchStage
from fetcher.logging import emit_event, emit_fetch_log
from fetcher.politeness import PolitenessController
from fetcher.robots import BLOCKED_BY_ROBOTS, RobotsDecision, RobotsTxtChecker


class RedirectLimitExceeded(Exception):
    """Raised when a URL exceeds the configured redirect limit."""


class BodyLimitExceeded(Exception):
    """Raised when response body exceeds configured limits."""


def _blocked_networks() -> list:
    """Build blocked network list from compliance config."""
    return [ip_network(cidr, strict=False) for cidr in ComplianceConfig.BLOCKED_IP_RANGES]


def _resolve_ip_addresses(hostname: str) -> set[str]:
    """Resolve hostname to a set of IP addresses."""
    try:
        infos = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return set()
    return {item[4][0] for item in infos}


def _is_blocked_ip(ip_text: str, blocked_networks: Iterable) -> bool:
    """Check if an IP is inside blocked ranges."""
    ip_obj = ip_address(ip_text)
    return any(ip_obj in network for network in blocked_networks)


def _validate_url_scheme(url: str) -> bool:
    """Validate that URL uses allowed protocols."""
    parsed = urlparse(url)
    return parsed.scheme.lower() in ComplianceConfig.ALLOWED_PROTOCOLS


def _content_limit_for_response(content_type: str | None) -> int:
    """Compute byte limit for a response content-type."""
    if not content_type:
        return ComplianceConfig.MAX_BODY_BYTES_DEFAULT
    normalized = content_type.split(";", 1)[0].strip().lower()
    return ComplianceConfig.MAX_BODY_BYTES_BY_TYPE.get(
        normalized,
        ComplianceConfig.MAX_BODY_BYTES_DEFAULT,
    )


def _read_body_with_limit(response: requests.Response, max_bytes: int) -> bytes:
    """Read response body up to the configured maximum size."""
    if max_bytes == 0:
        raise BodyLimitExceeded("content type is disabled by policy")

    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_content(chunk_size=8192):
        if not chunk:
            continue
        total += len(chunk)
        if total > max_bytes:
            raise BodyLimitExceeded(f"response exceeds {max_bytes} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


def _follow_redirects(
    session: requests.Session,
    url: str,
    timeout_seconds: int,
    max_redirects: int,
    user_agent: str,
    blocked_networks: Iterable,
) -> tuple[requests.Response, str]:
    """Fetch a URL while enforcing redirect constraints."""
    current_url = url

    for hop in range(max_redirects + 1):
        response = session.get(
            current_url,
            headers={"User-Agent": user_agent},
            timeout=timeout_seconds,
            allow_redirects=False,
            stream=True,
        )

        if 300 <= response.status_code < 400 and response.headers.get("location"):
            if hop >= max_redirects:
                response.close()
                raise RedirectLimitExceeded(f"redirects exceeded {max_redirects}")

            next_url = urljoin(current_url, response.headers["location"])
            if not _validate_url_scheme(next_url):
                response.close()
                raise RedirectLimitExceeded("redirected to disallowed protocol")

            host = urlparse(next_url).hostname or ""
            resolved = _resolve_ip_addresses(host)
            if any(_is_blocked_ip(ip_text, blocked_networks) for ip_text in resolved):
                response.close()
                raise RedirectLimitExceeded("redirected to blocked IP range")

            response.close()
            current_url = next_url
            continue

        return response, current_url

    raise RedirectLimitExceeded(f"redirects exceeded {max_redirects}")


def fetch_url(
    url: str,
    run_id: str,
    robots_checker: RobotsTxtChecker | None = None,
    politeness: PolitenessController | None = None,
    session: requests.Session | None = None,
    event_hook: Callable[[str, dict[str, object]], None] | None = None,
) -> tuple[FetchedDoc | None, FetchLog]:
    """Fetch a URL using compliance and safety constraints."""
    start = time.monotonic()
    blocked_networks = _blocked_networks()

    def _emit(event_type: str, payload: dict[str, object]) -> None:
        if event_hook:
            event_hook(event_type, payload)

    if not _validate_url_scheme(url):
        return None, FetchLog(
            url=url,
            error_code=FetchErrorCode.SECURITY_BLOCKED,
            run_id=run_id,
            latency_ms=int((time.monotonic() - start) * 1000),
        )

    parsed = urlparse(url)
    hostname = parsed.hostname or ""
    if not hostname:
        return None, FetchLog(
            url=url,
            error_code=FetchErrorCode.FETCH_ERROR,
            run_id=run_id,
            latency_ms=int((time.monotonic() - start) * 1000),
        )

    resolved_ips = _resolve_ip_addresses(hostname)
    if any(_is_blocked_ip(ip_text, blocked_networks) for ip_text in resolved_ips):
        return None, FetchLog(
            url=url,
            error_code=FetchErrorCode.SECURITY_BLOCKED,
            run_id=run_id,
            latency_ms=int((time.monotonic() - start) * 1000),
        )

    delay_multiplier = 1.0
    if robots_checker:
        decision: RobotsDecision = robots_checker.evaluate(url)
        delay_multiplier = decision.delay_multiplier

        if decision.warning:
            _emit(
                "robots_warning",
                {
                    "run_id": run_id,
                    "url": url,
                    "robots_url": decision.robots_url,
                    "robots_mode": decision.mode,
                    "robots_status_code": decision.status_code,
                    "delay_multiplier": decision.delay_multiplier,
                    "cache_hit": decision.cache_hit,
                    "message": decision.warning,
                },
            )

        if decision.delay_multiplier > 1.0:
            _emit(
                "robots_slowdown",
                {
                    "run_id": run_id,
                    "url": url,
                    "domain": hostname,
                    "robots_mode": decision.mode,
                    "delay_multiplier": decision.delay_multiplier,
                },
            )

        if not decision.allowed and decision.error_code == BLOCKED_BY_ROBOTS:
            return None, FetchLog(
                url=url,
                error_code=FetchErrorCode.BLOCKED_BY_ROBOTS,
                run_id=run_id,
                latency_ms=int((time.monotonic() - start) * 1000),
            )

    http_session = session or requests.Session()

    try:
        if politeness:
            with politeness.request_slot(hostname, delay_multiplier=delay_multiplier):
                response, final_url = _follow_redirects(
                    http_session,
                    url,
                    timeout_seconds=ComplianceConfig.FETCH_TIMEOUT_SECONDS,
                    max_redirects=ComplianceConfig.MAX_REDIRECTS,
                    user_agent=ComplianceConfig.USER_AGENT,
                    blocked_networks=blocked_networks,
                )
        else:
            response, final_url = _follow_redirects(
                http_session,
                url,
                timeout_seconds=ComplianceConfig.FETCH_TIMEOUT_SECONDS,
                max_redirects=ComplianceConfig.MAX_REDIRECTS,
                user_agent=ComplianceConfig.USER_AGENT,
                blocked_networks=blocked_networks,
            )

        if response.status_code == 304:
            doc = FetchedDoc(
                status_code=304,
                final_url=final_url,
                headers={k.lower(): v for k, v in response.headers.items()},
                body_bytes=None,
                body_sha256=None,
                latency_ms=int((time.monotonic() - start) * 1000),
            )
            log = FetchLog(
                url=url,
                status_code=304,
                latency_ms=doc.latency_ms,
                bytes_received=0,
                run_id=run_id,
            )
            response.close()
            return doc, log

        content_limit = _content_limit_for_response(response.headers.get("content-type"))
        body = _read_body_with_limit(response, content_limit)
        body_hash = hashlib.sha256(body).hexdigest() if body else None

        doc = FetchedDoc(
            status_code=response.status_code,
            final_url=final_url,
            headers={k.lower(): v for k, v in response.headers.items()},
            body_bytes=body,
            body_sha256=body_hash,
            latency_ms=int((time.monotonic() - start) * 1000),
        )
        log = FetchLog(
            url=url,
            status_code=response.status_code,
            latency_ms=doc.latency_ms,
            bytes_received=len(body),
            run_id=run_id,
        )
        response.close()
        return doc, log

    except RedirectLimitExceeded:
        return None, FetchLog(
            url=url,
            error_code=FetchErrorCode.REDIRECT_LIMIT,
            run_id=run_id,
            latency_ms=int((time.monotonic() - start) * 1000),
        )
    except BodyLimitExceeded:
        return None, FetchLog(
            url=url,
            error_code=FetchErrorCode.BODY_TOO_LARGE,
            run_id=run_id,
            latency_ms=int((time.monotonic() - start) * 1000),
        )
    except requests.Timeout:
        return None, FetchLog(
            url=url,
            error_code=FetchErrorCode.TIMEOUT,
            run_id=run_id,
            latency_ms=int((time.monotonic() - start) * 1000),
        )
    except requests.RequestException:
        return None, FetchLog(
            url=url,
            error_code=FetchErrorCode.FETCH_ERROR,
            run_id=run_id,
            latency_ms=int((time.monotonic() - start) * 1000),
        )


class HttpFetchStage(FetchStage):
    """FetchStage implementation backed by fetch_url + structured logging."""

    def __init__(
        self,
        robots_checker: RobotsTxtChecker | None = None,
        politeness: PolitenessController | None = None,
        session: requests.Session | None = None,
        log_fetches: bool = True,
        event_logger: Callable[[str, dict[str, object]], None] | None = None,
    ) -> None:
        """Initialize fetch dependencies, politeness policy, and event sinks."""
        self.robots_checker = robots_checker or RobotsTxtChecker(
            user_agent=ComplianceConfig.USER_AGENT
        )
        self.politeness = politeness or PolitenessController(
            per_domain_delay_seconds=ComplianceConfig.PER_DOMAIN_DELAY_SECONDS,
            max_global_concurrency=ComplianceConfig.MAX_GLOBAL_CONCURRENCY,
        )
        self.session = session
        self.log_fetches = log_fetches
        self.event_logger = event_logger or self._default_event_logger

    @staticmethod
    def _default_event_logger(event_type: str, payload: dict[str, object]) -> None:
        """Default event sink writing to structured JSON stdout."""
        emit_event(event_type, **payload)

    def fetch(self, url: str, run_id: str) -> tuple[FetchedDoc | None, FetchLog]:
        """Fetch one URL and emit structured logs."""
        fetched_doc, fetch_log = fetch_url(
            url=url,
            run_id=run_id,
            robots_checker=self.robots_checker,
            politeness=self.politeness,
            session=self.session,
            event_hook=self.event_logger,
        )
        if self.log_fetches:
            emit_fetch_log(fetch_log)
        return fetched_doc, fetch_log
