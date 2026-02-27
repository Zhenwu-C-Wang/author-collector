"""arXiv connector: discover non-PDF article URLs from Atom feeds."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote_plus, urlparse
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


def _is_pdf_link(url: str) -> bool:
    """Return True when URL points to a PDF resource."""
    lowered = url.lower()
    return lowered.endswith(".pdf") or "/pdf/" in lowered


def _extract_entry_link(entry: ET.Element) -> str | None:
    """Extract preferred non-PDF link from one Atom entry."""
    fallback_id: str | None = None

    for child in entry:
        name = _local_name(child.tag)
        if name == "id":
            maybe_id = (child.text or "").strip()
            if maybe_id and _is_http_url(maybe_id) and not _is_pdf_link(maybe_id):
                fallback_id = maybe_id
            continue

        if name != "link":
            continue

        href = (child.attrib.get("href") or "").strip()
        rel = (child.attrib.get("rel") or "alternate").strip().lower()
        title = (child.attrib.get("title") or "").strip().lower()
        content_type = (child.attrib.get("type") or "").strip().lower()
        if not href or not _is_http_url(href):
            continue
        if _is_pdf_link(href) or title == "pdf" or content_type == "application/pdf":
            continue
        if rel in {"alternate", ""}:
            return href

    return fallback_id


def _iter_entries(root: ET.Element) -> list[ET.Element]:
    """Return Atom entry elements in document order."""
    entries: list[ET.Element] = []
    for element in root.iter():
        if _local_name(element.tag) == "entry":
            entries.append(element)
    return entries


class ArxivDiscoverStage(DiscoverStage):
    """Discover article URLs from arXiv Atom API feeds."""

    def __init__(
        self,
        session: requests.Session | None = None,
        timeout_seconds: int = ComplianceConfig.FETCH_TIMEOUT_SECONDS,
        user_agent: str = ComplianceConfig.USER_AGENT,
    ) -> None:
        """Initialize arXiv discovery dependencies and HTTP settings."""
        self.session = session or requests.Session()
        self.timeout_seconds = timeout_seconds
        self.user_agent = user_agent

    def _load_seed(self, seed: str) -> str:
        """Load Atom XML from local file path or HTTP(S) URL."""
        seed_path = Path(seed)
        if seed_path.exists():
            return seed_path.read_text(encoding="utf-8")

        parsed = urlparse(seed)
        if parsed.scheme.lower() not in {"http", "https"}:
            # v0 arXiv mode accepts raw query/author seed and maps to official API query URL.
            query_seed = seed.strip()
            if not query_seed:
                raise ValueError(f"Unsupported seed for arXiv connector: {seed}")
            encoded_query = quote_plus(query_seed)
            api_url = (
                "https://export.arxiv.org/api/query"
                f"?search_query={encoded_query}&start=0&max_results=100"
            )
            response = self.session.get(
                api_url,
                headers={"User-Agent": self.user_agent},
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            return response.text

        response = self.session.get(
            seed,
            headers={"User-Agent": self.user_agent},
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        return response.text

    def discover(self, seed: str, run_id: str):
        """Yield unique non-PDF entry links from Atom feed entries."""
        _ = run_id
        xml_text = self._load_seed(seed)
        root = ET.fromstring(xml_text)

        seen: set[str] = set()
        for entry in _iter_entries(root):
            link = _extract_entry_link(entry)
            if not link or link in seen:
                continue
            seen.add(link)
            yield link
