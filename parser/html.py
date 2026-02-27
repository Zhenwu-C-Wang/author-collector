"""HTML parse stage for generating Parsed documents from fetched content."""

from __future__ import annotations

import re
from datetime import datetime
from html.parser import HTMLParser
from typing import Any

from core.config import ComplianceConfig
from core.models import FetchedDoc, Parsed
from core.pipeline import ParseStage
from parser.jsonld import extract_structured_metadata

try:
    import trafilatura
except ImportError:  # pragma: no cover - optional dependency
    trafilatura = None


_DATE_META_KEYS = (
    "article:published_time",
    "pubdate",
    "publish-date",
    "dc.date",
    "date",
)
_AUTHOR_META_KEYS = ("author", "article:author", "og:article:author")
_TITLE_META_KEYS = ("og:title", "twitter:title")


class _TextExtractor(HTMLParser):
    """Extract visible text from HTML while skipping script/style payloads."""

    def __init__(self) -> None:
        super().__init__()
        self._skip_depth = 0
        self._in_head = False
        self._chunks: list[str] = []

    @property
    def text(self) -> str:
        """Return normalized text content."""
        raw = "".join(self._chunks)
        return _normalize_whitespace(raw)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        _ = attrs
        tag_lower = tag.lower()
        if tag_lower == "head":
            self._in_head = True
            return
        if tag_lower in {"script", "style", "noscript", "template"}:
            self._skip_depth += 1
            return
        if tag_lower in {"p", "br", "li", "div", "section", "article", "h1", "h2", "h3"}:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag_lower = tag.lower()
        if tag_lower == "head":
            self._in_head = False
            return
        if tag_lower in {"script", "style", "noscript", "template"} and self._skip_depth > 0:
            self._skip_depth -= 1
            return
        if tag_lower in {"p", "li", "div", "section", "article"}:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_head or self._skip_depth > 0:
            return
        if data.strip():
            self._chunks.append(data)
            self._chunks.append(" ")


def _normalize_whitespace(value: str) -> str:
    """Collapse whitespace while preserving paragraph breaks."""
    normalized_lines: list[str] = []
    for line in value.splitlines():
        compact = " ".join(line.split())
        if compact:
            normalized_lines.append(compact)
    return "\n\n".join(normalized_lines)


def _truncate_with_ellipsis(text: str, max_chars: int) -> str:
    """Truncate string safely on word boundary and append ellipsis."""
    if len(text) <= max_chars:
        return text
    prefix = text[: max_chars + 1]
    trimmed = prefix[:max_chars]
    if not trimmed.endswith(" ") and " " in trimmed:
        trimmed = trimmed.rsplit(" ", 1)[0]
    return trimmed.rstrip() + "…"


def _parse_datetime(value: str | None) -> datetime | None:
    """Parse a best-effort ISO datetime string."""
    if not value:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    normalized = normalized.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def _decode_body_bytes(fetched: FetchedDoc) -> str:
    """Decode body bytes using response charset hints with safe fallback."""
    if fetched.body_bytes is None:
        return ""

    content_type = fetched.headers.get("content-type", "")
    charset_match = re.search(r"charset=([a-zA-Z0-9._-]+)", content_type)
    encodings = []
    if charset_match:
        encodings.append(charset_match.group(1))
    encodings.extend(["utf-8", "latin-1"])

    for encoding in encodings:
        try:
            return fetched.body_bytes.decode(encoding)
        except UnicodeDecodeError:
            continue

    return fetched.body_bytes.decode("utf-8", errors="replace")


def _fallback_text_extract(html_text: str) -> str:
    """Fallback visible-text extraction when trafilatura is unavailable."""
    parser = _TextExtractor()
    parser.feed(html_text)
    return parser.text


def _extract_readable_text(html_text: str) -> str:
    """Extract readable text via trafilatura if available, fallback otherwise."""
    if trafilatura is not None:
        extracted = trafilatura.extract(
            html_text,
            output_format="txt",
            include_comments=False,
            include_tables=False,
        )
        if extracted:
            return _normalize_whitespace(extracted)
    return _fallback_text_extract(html_text)


def _collect_author_names(metadata: dict[str, Any]) -> list[str]:
    """Merge author hints from JSON-LD and meta tags."""
    names: list[str] = []

    def _add(candidate: str | None) -> None:
        if not candidate:
            return
        for part in re.split(r",|\||\band\b", candidate):
            normalized = " ".join(part.split())
            if normalized and normalized not in names:
                names.append(normalized)

    for name in metadata.get("json_ld_author_names", []):
        _add(name)

    meta_tags = metadata.get("meta_tags", {})
    for key in _AUTHOR_META_KEYS:
        _add(meta_tags.get(key))

    return names


def _choose_title(metadata: dict[str, Any]) -> str | None:
    """Pick title by priority: JSON-LD, OG/Twitter, HTML title."""
    json_ld_title = metadata.get("json_ld_title")
    if isinstance(json_ld_title, str) and json_ld_title.strip():
        return " ".join(json_ld_title.split())

    meta_tags = metadata.get("meta_tags", {})
    for key in _TITLE_META_KEYS:
        value = meta_tags.get(key)
        if value:
            return " ".join(value.split())

    html_title = metadata.get("html_title")
    if isinstance(html_title, str) and html_title.strip():
        return " ".join(html_title.split())
    return None


def _choose_published_at(metadata: dict[str, Any]) -> datetime | None:
    """Pick publication datetime from JSON-LD first, then meta tags."""
    json_ld_date = metadata.get("json_ld_date_published")
    if isinstance(json_ld_date, str):
        parsed = _parse_datetime(json_ld_date)
        if parsed is not None:
            return parsed

    meta_tags = metadata.get("meta_tags", {})
    for key in _DATE_META_KEYS:
        parsed = _parse_datetime(meta_tags.get(key))
        if parsed is not None:
            return parsed
    return None


class HtmlParseStage(ParseStage):
    """Convert fetched HTML content into Parsed payload."""

    def __init__(self, readable_text_max_chars: int = ComplianceConfig.SNIPPET_MAX_CHARS) -> None:
        """Initialize parser truncation policy for readable content."""
        self.readable_text_max_chars = readable_text_max_chars

    def parse(self, fetched: FetchedDoc, run_id: str) -> Parsed:
        """Parse fetched document into deterministic Parsed fields."""
        _ = run_id
        html_text = _decode_body_bytes(fetched)
        metadata = extract_structured_metadata(html_text, page_url=fetched.final_url)

        readable_text = _extract_readable_text(html_text)
        readable_text = _truncate_with_ellipsis(readable_text, self.readable_text_max_chars)

        canonical_url = metadata.get("canonical_url")
        if not isinstance(canonical_url, str) or not canonical_url.strip():
            canonical_url = fetched.final_url

        return Parsed(
            url=fetched.final_url,
            text=readable_text or None,
            title=_choose_title(metadata),
            date_published=_choose_published_at(metadata),
            author_names=_collect_author_names(metadata),
            html_title=metadata.get("html_title"),
            meta_tags=metadata.get("meta_tags", {}),
            json_ld_blocks=metadata.get("json_ld_blocks", []),
            canonical_url=canonical_url,
            original_html=html_text,
        )
