"""Extract stage: convert Parsed -> ArticleDraft + Evidence list."""

from __future__ import annotations

from datetime import datetime
import re
from typing import Any, Callable

from core.config import ComplianceConfig
from core.evidence import create_evidence
from core.models import ArticleDraft, Evidence, EvidenceType, Parsed
from core.pipeline import ExtractStage


DRAFT_ARTICLE_ID = "__draft_article__"

CLAIM_PATH_BY_FIELD: dict[str, str] = {
    "title": "/title",
    "author_hint": "/author_hint",
    "published_at": "/published_at",
}

_ARTICLE_TYPES = {"article", "newsarticle", "blogposting", "scholarlyarticle", "report"}


def _truncate_with_ellipsis(text: str, max_chars: int) -> str:
    """Trim content to max chars on word boundary and append ellipsis."""
    normalized = " ".join(text.split())
    if len(normalized) <= max_chars:
        return normalized
    trimmed = normalized[: max_chars + 1][:max_chars]
    if not trimmed.endswith(" ") and " " in trimmed:
        trimmed = trimmed.rsplit(" ", 1)[0]
    return trimmed.rstrip() + "…"


def _parse_datetime(value: str | None) -> datetime | None:
    """Parse ISO-like datetime string with UTC Z support."""
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


def _score_jsonld_type(raw_type: Any) -> int:
    """Score JSON-LD block by article relevance."""
    if isinstance(raw_type, str):
        types = [raw_type.lower()]
    elif isinstance(raw_type, list):
        types = [str(item).lower() for item in raw_type]
    else:
        types = []
    return 1 if any(item in _ARTICLE_TYPES for item in types) else 0


def _pick_best_jsonld_block(blocks: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Pick best JSON-LD block for article-level claims."""
    if not blocks:
        return None
    return sorted(blocks, key=lambda item: _score_jsonld_type(item.get("@type")), reverse=True)[0]


def _extract_jsonld_author_names(block: dict[str, Any] | None) -> list[str]:
    """Normalize author values in JSON-LD block."""
    if not block:
        return []
    raw = block.get("author")
    names: list[str] = []

    def _add(name: str | None) -> None:
        if not name:
            return
        normalized = " ".join(name.split())
        if normalized and normalized not in names:
            names.append(normalized)

    if isinstance(raw, str):
        _add(raw)
    elif isinstance(raw, dict):
        _add(raw.get("name"))
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, str):
                _add(item)
            elif isinstance(item, dict):
                _add(item.get("name"))

    return names


def _pick_meta(meta_tags: dict[str, str], keys: tuple[str, ...]) -> tuple[str | None, str | None]:
    """Pick first available meta value and return (value, key)."""
    for key in keys:
        value = meta_tags.get(key)
        if value:
            return value, key
    return None, None


def enforce_evidence_coverage(draft: ArticleDraft, evidence_list: list[Evidence]) -> list[str]:
    """
    Ensure every non-null core field has at least one evidence entry.

    If coverage is missing, field value is nulled out to keep output compliant.
    """
    warnings: list[str] = []

    for field_name, claim_path in CLAIM_PATH_BY_FIELD.items():
        field_value = getattr(draft, field_name)
        if field_value is None:
            continue
        has_evidence = any(item.claim_path == claim_path for item in evidence_list)
        if has_evidence:
            continue
        setattr(draft, field_name, None)
        warnings.append(
            f"Field '{field_name}' had no evidence for claim_path '{claim_path}', value dropped"
        )

    return warnings


class ArticleExtractStage(ExtractStage):
    """Deterministic ExtractStage for article metadata + evidence generation."""

    def __init__(
        self,
        source_id: str,
        snippet_max_chars: int = ComplianceConfig.SNIPPET_MAX_CHARS,
        evidence_snippet_max_chars: int = ComplianceConfig.EVIDENCE_SNIPPET_MAX_CHARS,
        warning_hook: Callable[[str], None] | None = None,
    ) -> None:
        """Initialize extraction policy knobs and optional warning sink."""
        self.source_id = source_id
        self.snippet_max_chars = snippet_max_chars
        self.evidence_snippet_max_chars = evidence_snippet_max_chars
        self.warning_hook = warning_hook

    def _build_evidence(
        self,
        *,
        claim_path: str,
        evidence_type: EvidenceType,
        source_url: str,
        extracted_text: str,
        run_id: str,
        extraction_method: str,
        metadata: dict[str, Any] | None = None,
    ) -> Evidence:
        """Create a normalized Evidence object with standard limits."""
        clipped_text = _truncate_with_ellipsis(extracted_text, self.evidence_snippet_max_chars)
        return create_evidence(
            article_id=DRAFT_ARTICLE_ID,
            claim_path=claim_path,
            evidence_type=evidence_type,
            source_url=source_url,
            extracted_text=clipped_text,
            run_id=run_id,
            extraction_method=extraction_method,
            metadata=metadata,
        )

    def extract(self, parsed: Parsed, run_id: str) -> tuple[ArticleDraft, list[Evidence]]:
        """Extract ArticleDraft and evidence chain from Parsed content."""
        source_url = parsed.canonical_url or parsed.url
        json_ld_block = _pick_best_jsonld_block(parsed.json_ld_blocks)
        meta_tags = parsed.meta_tags or {}
        evidence_list: list[Evidence] = []

        # Title: JSON-LD -> meta -> parsed title/html title
        json_ld_title = None
        if json_ld_block:
            raw_title = json_ld_block.get("headline") or json_ld_block.get("name")
            if isinstance(raw_title, str) and raw_title.strip():
                json_ld_title = " ".join(raw_title.split())
        meta_title, meta_title_key = _pick_meta(meta_tags, ("og:title", "twitter:title"))
        fallback_title = parsed.title or parsed.html_title

        title: str | None = None
        if json_ld_title:
            title = json_ld_title
            evidence_list.append(
                self._build_evidence(
                    claim_path=CLAIM_PATH_BY_FIELD["title"],
                    evidence_type=EvidenceType.JSON_LD,
                    source_url=source_url,
                    extracted_text=json_ld_title,
                    run_id=run_id,
                    extraction_method="json_ld.headline",
                    metadata={"field": "headline"},
                )
            )
        elif meta_title:
            title = " ".join(meta_title.split())
            evidence_list.append(
                self._build_evidence(
                    claim_path=CLAIM_PATH_BY_FIELD["title"],
                    evidence_type=EvidenceType.META_TAG,
                    source_url=source_url,
                    extracted_text=title,
                    run_id=run_id,
                    extraction_method=f"meta.{meta_title_key}",
                    metadata={"field": meta_title_key},
                )
            )
        elif fallback_title:
            title = " ".join(fallback_title.split())
            evidence_list.append(
                self._build_evidence(
                    claim_path=CLAIM_PATH_BY_FIELD["title"],
                    evidence_type=EvidenceType.EXTRACTED,
                    source_url=source_url,
                    extracted_text=title,
                    run_id=run_id,
                    extraction_method="parsed.title",
                    metadata={"field": "title"},
                )
            )

        # Author: JSON-LD -> meta -> parsed author names
        json_ld_author_names = _extract_jsonld_author_names(json_ld_block)
        meta_author, meta_author_key = _pick_meta(
            meta_tags, ("author", "article:author", "og:article:author")
        )
        parsed_author = parsed.author_names[0] if parsed.author_names else None

        author_hint: str | None = None
        if json_ld_author_names:
            author_hint = json_ld_author_names[0]
            evidence_list.append(
                self._build_evidence(
                    claim_path=CLAIM_PATH_BY_FIELD["author_hint"],
                    evidence_type=EvidenceType.JSON_LD,
                    source_url=source_url,
                    extracted_text=", ".join(json_ld_author_names),
                    run_id=run_id,
                    extraction_method="json_ld.author",
                    metadata={"field": "author"},
                )
            )
        elif meta_author:
            author_candidates = [part.strip() for part in re.split(r",|\||\band\b", meta_author)]
            author_hint = next((item for item in author_candidates if item), None)
            if author_hint:
                evidence_list.append(
                    self._build_evidence(
                        claim_path=CLAIM_PATH_BY_FIELD["author_hint"],
                        evidence_type=EvidenceType.META_TAG,
                        source_url=source_url,
                        extracted_text=meta_author,
                        run_id=run_id,
                        extraction_method=f"meta.{meta_author_key}",
                        metadata={"field": meta_author_key},
                    )
                )
        elif parsed_author:
            author_hint = parsed_author
            evidence_list.append(
                self._build_evidence(
                    claim_path=CLAIM_PATH_BY_FIELD["author_hint"],
                    evidence_type=EvidenceType.EXTRACTED,
                    source_url=source_url,
                    extracted_text=parsed_author,
                    run_id=run_id,
                    extraction_method="parsed.author_names",
                    metadata={"field": "author_names"},
                )
            )

        # Published date: JSON-LD -> meta -> parsed date
        json_ld_date = None
        if json_ld_block:
            raw_date = json_ld_block.get("datePublished") or json_ld_block.get("dateCreated")
            if isinstance(raw_date, str):
                json_ld_date = _parse_datetime(raw_date)
        meta_date, meta_date_key = _pick_meta(
            meta_tags,
            ("article:published_time", "pubdate", "publish-date", "dc.date", "date"),
        )
        meta_date_parsed = _parse_datetime(meta_date)

        published_at: datetime | None = None
        if json_ld_date is not None:
            published_at = json_ld_date
            evidence_list.append(
                self._build_evidence(
                    claim_path=CLAIM_PATH_BY_FIELD["published_at"],
                    evidence_type=EvidenceType.JSON_LD,
                    source_url=source_url,
                    extracted_text=json_ld_date.isoformat(),
                    run_id=run_id,
                    extraction_method="json_ld.datePublished",
                    metadata={"field": "datePublished"},
                )
            )
        elif meta_date_parsed is not None and meta_date_key:
            published_at = meta_date_parsed
            evidence_list.append(
                self._build_evidence(
                    claim_path=CLAIM_PATH_BY_FIELD["published_at"],
                    evidence_type=EvidenceType.META_TAG,
                    source_url=source_url,
                    extracted_text=meta_date or meta_date_parsed.isoformat(),
                    run_id=run_id,
                    extraction_method=f"meta.{meta_date_key}",
                    metadata={"field": meta_date_key},
                )
            )
        elif parsed.date_published is not None:
            published_at = parsed.date_published
            evidence_list.append(
                self._build_evidence(
                    claim_path=CLAIM_PATH_BY_FIELD["published_at"],
                    evidence_type=EvidenceType.EXTRACTED,
                    source_url=source_url,
                    extracted_text=parsed.date_published.isoformat(),
                    run_id=run_id,
                    extraction_method="parsed.date_published",
                    metadata={"field": "date_published"},
                )
            )

        snippet = None
        if parsed.text:
            snippet = _truncate_with_ellipsis(parsed.text, self.snippet_max_chars)

        draft = ArticleDraft(
            canonical_url=source_url,
            source_id=self.source_id,
            title=title,
            author_hint=author_hint,
            published_at=published_at,
            snippet=snippet,
        )

        warnings = enforce_evidence_coverage(draft, evidence_list)
        if self.warning_hook:
            for warning in warnings:
                self.warning_hook(warning)

        return draft, evidence_list
