"""RSS connector: discover article URLs from RSS/Atom feeds."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

import requests

from core.config import ComplianceConfig
from core.pipeline import DiscoverStage


def _local_name(tag: str) -> str:
    """Return lowercase local name for an XML tag."""
    if "}" in tag:
        return tag.split("}", 1)[1].lower()
    return tag.lower()


def _is_http_url(value: str) -> bool:
    """Return True when URL uses HTTP(S)."""
    parsed = urlparse(value)
    return parsed.scheme.lower() in {"http", "https"} and bool(parsed.netloc)


def _extract_entry_link(entry: ET.Element) -> str | None:
    """Extract canonical link from RSS item / Atom entry."""
    for child in entry:
        if _local_name(child.tag) != "link":
            continue

        text_link = (child.text or "").strip()
        if text_link and _is_http_url(text_link):
            return text_link

        href = (child.attrib.get("href") or "").strip()
        rel = (child.attrib.get("rel") or "alternate").strip().lower()
        if href and rel in {"alternate", ""} and _is_http_url(href):
            return href
    return None


def _iter_feed_entries(root: ET.Element) -> list[ET.Element]:
    """Return RSS item / Atom entry elements in document order."""
    entries: list[ET.Element] = []
    for element in root.iter():
        name = _local_name(element.tag)
        if name in {"item", "entry"}:
            entries.append(element)
    return entries


class RssDiscoverStage(DiscoverStage):
    """Discover article URLs from an RSS/Atom seed."""

    def __init__(
        self,
        session: requests.Session | None = None,
        timeout_seconds: int = ComplianceConfig.FETCH_TIMEOUT_SECONDS,
        user_agent: str = ComplianceConfig.USER_AGENT,
    ) -> None:
        """Initialize RSS discovery dependencies and HTTP settings."""
        self.session = session or requests.Session()
        self.timeout_seconds = timeout_seconds
        self.user_agent = user_agent

    def _load_seed(self, seed: str) -> str:
        """Load feed XML from local file path or HTTP(S) URL."""
        seed_path = Path(seed)
        if seed_path.exists():
            return seed_path.read_text(encoding="utf-8")

        parsed = urlparse(seed)
        if parsed.scheme.lower() not in {"http", "https"}:
            raise ValueError(f"Unsupported seed for RSS connector: {seed}")

        response = self.session.get(
            seed,
            headers={"User-Agent": self.user_agent},
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        return response.text

    def discover(self, seed: str, run_id: str):
        """Yield article URLs discovered in feed entries."""
        _ = run_id
        xml_text = self._load_seed(seed)
        root = ET.fromstring(xml_text)

        seen: set[str] = set()
        for entry in _iter_feed_entries(root):
            link = _extract_entry_link(entry)
            if not link or link in seen:
                continue
            seen.add(link)
            yield link
