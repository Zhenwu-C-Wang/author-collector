"""JSON-LD and metadata extraction helpers for HTML documents."""

from __future__ import annotations

import html as html_lib
import json
import re
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin


_JSON_LD_SCRIPT_RE = re.compile(
    r"<script[^>]*type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
    flags=re.IGNORECASE | re.DOTALL,
)
_ARTICLE_TYPES = {"article", "newsarticle", "blogposting", "scholarlyarticle", "report"}


class _HeadMetadataParser(HTMLParser):
    """Capture title/meta/canonical metadata from HTML head."""

    def __init__(self) -> None:
        super().__init__()
        self.meta_tags: dict[str, str] = {}
        self.canonical_href: str | None = None
        self._capture_title = False
        self._title_chunks: list[str] = []

    @property
    def html_title(self) -> str | None:
        """Return normalized title text."""
        if not self._title_chunks:
            return None
        title = " ".join("".join(self._title_chunks).split())
        return title or None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_map = {k.lower(): (v or "").strip() for k, v in attrs}
        tag_lower = tag.lower()
        if tag_lower == "meta":
            key_source = attrs_map.get("property") or attrs_map.get("name") or ""
            key = key_source.lower().strip()
            content = attrs_map.get("content", "").strip()
            if key and content and key not in self.meta_tags:
                self.meta_tags[key] = content
            return

        if tag_lower == "link":
            rel = attrs_map.get("rel", "").lower()
            href = attrs_map.get("href", "").strip()
            if "canonical" in rel.split() and href and self.canonical_href is None:
                self.canonical_href = href
            return

        if tag_lower == "title":
            self._capture_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self._capture_title = False

    def handle_data(self, data: str) -> None:
        if self._capture_title:
            self._title_chunks.append(data)


def _iter_jsonld_objects(payload: Any) -> list[dict[str, Any]]:
    """Flatten JSON-LD payload to a list of dict objects."""
    if isinstance(payload, dict):
        objects: list[dict[str, Any]] = []
        graph = payload.get("@graph")
        if isinstance(graph, list):
            for entry in graph:
                objects.extend(_iter_jsonld_objects(entry))
        base_payload = {k: v for k, v in payload.items() if k != "@graph"}
        if base_payload:
            objects.append(base_payload)
        return objects

    if isinstance(payload, list):
        objects = []
        for item in payload:
            objects.extend(_iter_jsonld_objects(item))
        return objects

    return []


def _extract_author_names(author_value: Any) -> list[str]:
    """Normalize JSON-LD author field to a list of names."""
    names: list[str] = []

    def _add(name: str | None) -> None:
        if not name:
            return
        normalized = " ".join(name.split())
        if normalized and normalized not in names:
            names.append(normalized)

    if isinstance(author_value, str):
        _add(author_value)
    elif isinstance(author_value, dict):
        _add(author_value.get("name"))
    elif isinstance(author_value, list):
        for item in author_value:
            if isinstance(item, str):
                _add(item)
            elif isinstance(item, dict):
                _add(item.get("name"))

    return names


def _jsonld_type_score(block: dict[str, Any]) -> int:
    """Score JSON-LD nodes so article-like nodes are preferred."""
    raw_type = block.get("@type")
    types: list[str]
    if isinstance(raw_type, str):
        types = [raw_type.lower()]
    elif isinstance(raw_type, list):
        types = [str(item).lower() for item in raw_type]
    else:
        types = []
    return 1 if any(item in _ARTICLE_TYPES for item in types) else 0


def _pick_best_jsonld_block(blocks: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Pick the most relevant JSON-LD block for article metadata."""
    if not blocks:
        return None
    scored = sorted(blocks, key=_jsonld_type_score, reverse=True)
    return scored[0]


def extract_jsonld_blocks(html_text: str) -> list[dict[str, Any]]:
    """Extract and parse all valid JSON-LD script blocks."""
    blocks: list[dict[str, Any]] = []
    for match in _JSON_LD_SCRIPT_RE.findall(html_text):
        raw_json = html_lib.unescape(match).strip()
        if not raw_json:
            continue
        try:
            payload = json.loads(raw_json)
        except json.JSONDecodeError:
            continue
        blocks.extend(_iter_jsonld_objects(payload))
    return blocks


def extract_structured_metadata(html_text: str, page_url: str | None = None) -> dict[str, Any]:
    """Extract JSON-LD, meta tags, canonical URL, and high-signal fields."""
    head_parser = _HeadMetadataParser()
    head_parser.feed(html_text)

    canonical_url = head_parser.canonical_href
    if canonical_url and page_url:
        canonical_url = urljoin(page_url, canonical_url)

    json_ld_blocks = extract_jsonld_blocks(html_text)
    best_block = _pick_best_jsonld_block(json_ld_blocks) or {}

    headline = best_block.get("headline") or best_block.get("name")
    description = best_block.get("description")
    date_published = best_block.get("datePublished") or best_block.get("dateCreated")
    author_names = _extract_author_names(best_block.get("author"))
    ld_url = best_block.get("url")
    if isinstance(ld_url, str) and page_url:
        ld_url = urljoin(page_url, ld_url)

    return {
        "html_title": head_parser.html_title,
        "meta_tags": head_parser.meta_tags,
        "canonical_url": canonical_url,
        "json_ld_blocks": json_ld_blocks,
        "json_ld_title": headline,
        "json_ld_description": description,
        "json_ld_date_published": date_published,
        "json_ld_author_names": author_names,
        "json_ld_url": ld_url if isinstance(ld_url, str) else None,
    }
