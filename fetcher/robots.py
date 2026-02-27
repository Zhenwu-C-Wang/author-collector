"""Robots.txt checker with TTL cache and conservative failure strategy."""

from __future__ import annotations

import time
from dataclasses import dataclass
from urllib import robotparser
from urllib.parse import urlparse

import requests


BLOCKED_BY_ROBOTS = "BLOCKED_BY_ROBOTS"


@dataclass(slots=True)
class RobotsCacheEntry:
    """Cached robots policy for a domain."""

    mode: str
    expires_at: float
    delay_multiplier: float
    parser: robotparser.RobotFileParser | None = None
    status_code: int | None = None
    warning: str | None = None


@dataclass(slots=True)
class RobotsDecision:
    """Decision payload for one URL robots check."""

    allowed: bool
    error_code: str | None
    delay_multiplier: float
    mode: str
    warning: str | None
    robots_url: str
    status_code: int | None
    cache_hit: bool


class RobotsTxtChecker:
    """Evaluate robots policy for URLs with domain-level caching."""

    def __init__(
        self,
        user_agent: str = "author-collector",
        timeout_seconds: int = 30,
        max_redirects: int = 5,
        session: requests.Session | None = None,
        clock_fn: callable | None = None,
    ) -> None:
        """Initialize robots checker, cache, and request-time policy defaults."""
        self.user_agent = user_agent
        self.timeout_seconds = timeout_seconds
        self.max_redirects = max_redirects
        self._session = session or requests.Session()
        self._clock = clock_fn or time.monotonic
        self._cache: dict[str, RobotsCacheEntry] = {}

        self._ttl_success_seconds = 3600
        self._ttl_not_found_seconds = 4 * 3600
        self._ttl_5xx_seconds = 15 * 60
        self._ttl_timeout_seconds = 3600

    def clear_cache(self) -> None:
        """Clear all cached robots decisions."""
        self._cache.clear()

    def evaluate(self, url: str) -> RobotsDecision:
        """Return a full robots decision for observability and rate control."""
        parsed = urlparse(url)
        domain = parsed.netloc.lower()
        scheme = parsed.scheme or "https"

        if not domain:
            return RobotsDecision(
                allowed=False,
                error_code=BLOCKED_BY_ROBOTS,
                delay_multiplier=1.0,
                mode="invalid",
                warning="Invalid URL for robots check: missing domain",
                robots_url="",
                status_code=None,
                cache_hit=False,
            )

        robots_url = f"{scheme}://{domain}/robots.txt"
        entry, cache_hit = self._get_or_fetch(domain, scheme)

        if entry.mode in {"allow_all", "allow_with_caution"}:
            return RobotsDecision(
                allowed=True,
                error_code=None,
                delay_multiplier=entry.delay_multiplier,
                mode=entry.mode,
                warning=entry.warning,
                robots_url=robots_url,
                status_code=entry.status_code,
                cache_hit=cache_hit,
            )

        if entry.parser and not entry.parser.can_fetch(self.user_agent, url):
            return RobotsDecision(
                allowed=False,
                error_code=BLOCKED_BY_ROBOTS,
                delay_multiplier=1.0,
                mode=entry.mode,
                warning=entry.warning,
                robots_url=robots_url,
                status_code=entry.status_code,
                cache_hit=cache_hit,
            )

        return RobotsDecision(
            allowed=True,
            error_code=None,
            delay_multiplier=entry.delay_multiplier,
            mode=entry.mode,
            warning=entry.warning,
            robots_url=robots_url,
            status_code=entry.status_code,
            cache_hit=cache_hit,
        )

    def can_fetch(self, url: str) -> tuple[bool, str | None, float]:
        """Return (is_allowed, error_code, delay_multiplier)."""
        decision = self.evaluate(url)
        return decision.allowed, decision.error_code, decision.delay_multiplier

    def _get_or_fetch(self, domain: str, scheme: str) -> tuple[RobotsCacheEntry, bool]:
        now = self._clock()
        cached = self._cache.get(domain)
        if cached and cached.expires_at > now:
            return cached, True

        robots_url = f"{scheme}://{domain}/robots.txt"
        entry = self._fetch_entry(robots_url)
        self._cache[domain] = entry
        return entry, False

    def _fetch_entry(self, robots_url: str) -> RobotsCacheEntry:
        now = self._clock()

        try:
            response = self._session.get(
                robots_url,
                headers={"User-Agent": self.user_agent},
                allow_redirects=True,
                timeout=self.timeout_seconds,
            )
            if len(response.history) > self.max_redirects:
                return RobotsCacheEntry(
                    mode="allow_all",
                    expires_at=now + self._ttl_timeout_seconds,
                    delay_multiplier=1.0,
                    warning=f"robots.txt redirect loop for {robots_url}; allowing",
                )
        except (requests.Timeout, requests.TooManyRedirects):
            return RobotsCacheEntry(
                mode="allow_all",
                expires_at=now + self._ttl_timeout_seconds,
                delay_multiplier=1.0,
                warning=f"robots.txt timeout for {robots_url}; allowing",
            )
        except requests.RequestException:
            return RobotsCacheEntry(
                mode="allow_with_caution",
                expires_at=now + self._ttl_5xx_seconds,
                delay_multiplier=2.0,
                warning=f"robots.txt request error for {robots_url}; allowing with reduced rate",
            )

        if response.status_code == 200:
            parser = robotparser.RobotFileParser()
            parser.set_url(robots_url)
            parser.parse(response.text.splitlines())
            return RobotsCacheEntry(
                mode="parsed",
                parser=parser,
                expires_at=now + self._ttl_success_seconds,
                delay_multiplier=1.0,
                status_code=response.status_code,
            )

        if response.status_code == 404:
            return RobotsCacheEntry(
                mode="allow_all",
                expires_at=now + self._ttl_not_found_seconds,
                delay_multiplier=1.0,
                status_code=response.status_code,
                warning=f"robots.txt not found for {robots_url}; allowing",
            )

        if 500 <= response.status_code <= 599:
            return RobotsCacheEntry(
                mode="allow_with_caution",
                expires_at=now + self._ttl_5xx_seconds,
                delay_multiplier=2.0,
                status_code=response.status_code,
                warning=f"robots.txt returned {response.status_code} for {robots_url}; allowing with reduced rate",
            )

        return RobotsCacheEntry(
            mode="allow_all",
            expires_at=now + self._ttl_timeout_seconds,
            delay_multiplier=1.0,
            status_code=response.status_code,
            warning=f"robots.txt returned {response.status_code} for {robots_url}; allowing",
        )
