"""HTML author-page connector: discover article links from one listing page."""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

from core.config import ComplianceConfig
from core.pipeline import DiscoverStage


def _is_http_url(value: str) -> bool:
    """Return True when URL uses HTTP(S)."""
    parsed = urlparse(value)
    return parsed.scheme.lower() in {"http", "https"} and bool(parsed.netloc)


class _AnchorCollector(HTMLParser):
    """Collect anchor href values in document order."""

    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        attrs_map = {k.lower(): (v or "").strip() for k, v in attrs}
        href = attrs_map.get("href", "").strip()
        if href:
            self.hrefs.append(href)


class HtmlAuthorPageDiscoverStage(DiscoverStage):
    """Discover article URLs from a single HTML author page seed."""

    def __init__(
        self,
        session: requests.Session | None = None,
        timeout_seconds: int = ComplianceConfig.FETCH_TIMEOUT_SECONDS,
        user_agent: str = ComplianceConfig.USER_AGENT,
    ) -> None:
        """Initialize HTML author-page discovery dependencies and HTTP settings."""
        self.session = session or requests.Session()
        self.timeout_seconds = timeout_seconds
        self.user_agent = user_agent

    def _load_seed(self, seed: str) -> tuple[str, str | None]:
        """Load HTML from local file or HTTP(S) URL; return (html, base_url)."""
        seed_path = Path(seed)
        if seed_path.exists():
            return seed_path.read_text(encoding="utf-8"), None

        parsed = urlparse(seed)
        if parsed.scheme.lower() not in {"http", "https"}:
            raise ValueError(f"Unsupported seed for HTML author connector: {seed}")

        response = self.session.get(
            seed,
            headers={"User-Agent": self.user_agent},
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        return response.text, seed

    def discover(self, seed: str, run_id: str):
        """Yield unique HTTP(S) article links from one listing page."""
        _ = run_id
        html_text, base_url = self._load_seed(seed)

        parser = _AnchorCollector()
        parser.feed(html_text)

        seen: set[str] = set()
        for href in parser.hrefs:
            candidate = href
            if base_url:
                candidate = urljoin(base_url, href)
            if not _is_http_url(candidate) or candidate in seen:
                continue
            seen.add(candidate)
            yield candidate
