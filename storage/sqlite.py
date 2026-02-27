"""SQLite persistence for run/fetch logs, storage, export, and rollback."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlparse
from uuid import NAMESPACE_URL, uuid4, uuid5

import jsonschema

from core.models import Article, ArticleDraft, Evidence, FetchLog, MergeDecision, RunLog
from core.structured_logging import emit_json_event
from core.pipeline import ExportStage, StoreStage
from quality.urlnorm import canonicalize_url

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCHEMAS_DIR = PROJECT_ROOT / "schemas"
ARTICLE_SCHEMA = json.loads((SCHEMAS_DIR / "article.schema.json").read_text(encoding="utf-8"))


def _utc_now() -> datetime:
    """Return timezone-aware UTC timestamp."""
    return datetime.now(UTC)


def _hash_article_fields(draft: ArticleDraft) -> str:
    """Compute stable hash for versioning-relevant article fields."""
    payload = {
        "title": draft.title,
        "author_hint": draft.author_hint,
        "snippet": draft.snippet,
        "published_at": draft.published_at.isoformat() if draft.published_at else None,
    }
    serialized = json.dumps(payload, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _parse_iso_datetime(value: str | None) -> datetime | None:
    """Parse ISO datetime string into datetime, preserving None."""
    if value is None:
        return None
    return datetime.fromisoformat(value)


def _normalize_author_hint(value: str) -> str:
    """Normalize author hint for deterministic grouping."""
    return " ".join(value.strip().lower().split())


def _extract_domain(url: str) -> str:
    """Extract lowercase host from URL; empty string when missing."""
    try:
        return (urlparse(url).hostname or "").strip().lower()
    except Exception as exc:
        emit_json_event(
            event_type="storage_domain_parse_error",
            run_id=None,
            component="storage",
            url=url,
            error_type=type(exc).__name__,
            error=str(exc),
        )
        return ""


def _review_author_id(source_id: str, normalized_name: str, domain: str) -> str:
    """Build deterministic review author ID from source/name/domain."""
    key = f"review-author|{source_id}|{normalized_name}|{domain}"
    return str(uuid5(NAMESPACE_URL, key))


def _serialize_evidence_snapshot(evidence_list: list[Evidence]) -> str:
    """Serialize evidence list into a deterministic JSON snapshot for rollback."""
    payload: list[dict[str, object]] = []
    for item in evidence_list:
        payload.append(
            {
                "id": item.id,
                "claim_path": item.claim_path,
                "evidence_type": item.evidence_type.value,
                "source_url": item.source_url,
                "extraction_method": item.extraction_method,
                "extracted_text": item.extracted_text,
                "confidence": item.confidence,
                "metadata": item.metadata,
                "retrieved_at": item.retrieved_at.isoformat(),
                "extractor_version": item.extractor_version,
                "input_ref": item.input_ref,
                "snippet_max_chars_applied": item.snippet_max_chars_applied,
                "created_at": item.created_at.isoformat(),
                "run_id": item.run_id,
            }
        )
    return json.dumps(payload, sort_keys=True, ensure_ascii=True)


def _deserialize_evidence_snapshot(raw_snapshot: str | None, article_id: str) -> list[Evidence]:
    """Parse a version evidence snapshot into Evidence objects for one article."""
    if not raw_snapshot:
        return []
    try:
        rows = json.loads(raw_snapshot)
    except json.JSONDecodeError as exc:
        emit_json_event(
            event_type="storage_evidence_snapshot_json_error",
            run_id=None,
            component="storage",
            article_id=article_id,
            error_type=type(exc).__name__,
            error=str(exc),
        )
        return []
    if not isinstance(rows, list):
        return []

    restored: list[Evidence] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        try:
            restored.append(
                Evidence(
                    id=str(row.get("id") or uuid4()),
                    article_id=article_id,
                    claim_path=str(row["claim_path"]),
                    evidence_type=str(row["evidence_type"]),
                    source_url=str(row["source_url"]),
                    extraction_method=row.get("extraction_method"),
                    extracted_text=str(row["extracted_text"]),
                    confidence=float(row.get("confidence", 1.0)),
                    metadata=row.get("metadata") or {},
                    retrieved_at=_parse_iso_datetime(row.get("retrieved_at")) or _utc_now(),
                    extractor_version=row.get("extractor_version"),
                    input_ref=row.get("input_ref"),
                    snippet_max_chars_applied=row.get("snippet_max_chars_applied"),
                    created_at=_parse_iso_datetime(row.get("created_at")) or _utc_now(),
                    run_id=str(row.get("run_id") or "snapshot"),
                )
            )
        except Exception as exc:
            emit_json_event(
                event_type="storage_evidence_snapshot_row_error",
                run_id=None,
                component="storage",
                article_id=article_id,
                row_index=index,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            continue
    return restored


def _insert_evidence_row(connection: sqlite3.Connection, item: Evidence) -> None:
    """Insert one evidence row into SQLite."""
    connection.execute(
        """
        INSERT INTO evidence (
            id, article_id, claim_path, evidence_type, source_url, extraction_method,
            extracted_text, confidence, metadata, retrieved_at, extractor_version,
            input_ref, snippet_max_chars_applied, created_at, run_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            item.id,
            item.article_id,
            item.claim_path,
            item.evidence_type.value,
            item.source_url,
            item.extraction_method,
            item.extracted_text,
            item.confidence,
            json.dumps(item.metadata, sort_keys=True, ensure_ascii=True),
            item.retrieved_at.isoformat(),
            item.extractor_version,
            item.input_ref,
            item.snippet_max_chars_applied,
            item.created_at.isoformat(),
            item.run_id,
        ),
    )


class SQLiteRunStore:
    """Persist run/fetch logs and article state to SQLite."""

    def __init__(self, db_path: str | Path, initialize: bool = True) -> None:
        """Initialize store and optionally apply startup schema upgrades."""
        self.db_path = Path(db_path)
        if initialize:
            self.initialize_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            yield connection
            connection.commit()
        finally:
            connection.close()

    def initialize_schema(self) -> None:
        """Apply initial migration schema to an empty database."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        migration_path = Path(__file__).resolve().parent / "migrations" / "0001_init.sql"
        sql = migration_path.read_text(encoding="utf-8")
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='run_log'"
            ).fetchone()
            if existing:
                self._ensure_additive_columns(connection)
                return
            connection.executescript(sql)
            self._ensure_additive_columns(connection)

    def _ensure_additive_columns(self, connection: sqlite3.Connection) -> None:
        """Apply additive schema updates for older databases."""
        version_columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(versions)").fetchall()
        }
        if "evidence_snapshot" not in version_columns:
            connection.execute("ALTER TABLE versions ADD COLUMN evidence_snapshot TEXT")

    def ensure_author(self, author_id: str, canonical_name: str) -> None:
        """Ensure a canonical author row exists (idempotent)."""
        now = _utc_now().isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO authors (id, canonical_name, metadata, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    canonical_name = excluded.canonical_name,
                    updated_at = excluded.updated_at
                """,
                (
                    author_id,
                    canonical_name,
                    json.dumps({}, sort_keys=True, ensure_ascii=True),
                    now,
                    now,
                ),
            )

    def list_resolution_author_profiles(self) -> list[dict[str, Any]]:
        """
        Build deterministic author profiles for review from stored articles.

        v0 policy:
        - Group by (source_id, normalized author_hint, domain)
        - Materialize deterministic author IDs in `authors` for merge FK integrity
        """
        grouped: dict[tuple[str, str, str], dict[str, Any]] = {}

        with self._connect() as connection:
            article_rows = connection.execute(
                """
                SELECT source_id, author_hint, canonical_url
                FROM articles
                WHERE author_hint IS NOT NULL AND TRIM(author_hint) <> ''
                ORDER BY source_id, canonical_url
                """
            ).fetchall()

            for row in article_rows:
                source_id = str(row["source_id"])
                raw_hint = str(row["author_hint"]).strip()
                normalized_hint = _normalize_author_hint(raw_hint)
                if not normalized_hint:
                    continue
                domain = _extract_domain(str(row["canonical_url"]))
                key = (source_id, normalized_hint, domain)
                bucket = grouped.setdefault(
                    key,
                    {
                        "source_id": source_id,
                        "canonical_name": raw_hint,
                        "normalized_name": normalized_hint,
                        "domains": set(),
                        "accounts": set(),
                        "profile_urls": set(),
                        "article_count": 0,
                    },
                )
                bucket["article_count"] += 1
                if domain:
                    bucket["domains"].add(domain)

                # Optional rule-1 seed from author_hint when it clearly encodes an account.
                if "@" in normalized_hint:
                    bucket["accounts"].add(normalized_hint)
                if normalized_hint.startswith("http://") or normalized_hint.startswith("https://"):
                    bucket["accounts"].add(normalized_hint)
                    parsed = urlparse(normalized_hint)
                    if any(seg in parsed.path.lower() for seg in ("/author/", "/people/", "/profile/", "/bio")):
                        bucket["profile_urls"].add(normalized_hint)

            now = _utc_now().isoformat()
            profiles: list[dict[str, Any]] = []
            for (source_id, normalized_hint, domain), bucket in sorted(grouped.items()):
                author_id = _review_author_id(source_id, normalized_hint, domain)
                metadata = {
                    "source_id": source_id,
                    "normalized_name": normalized_hint,
                    "domain": domain,
                    "article_count": bucket["article_count"],
                }
                connection.execute(
                    """
                    INSERT INTO authors (id, canonical_name, metadata, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        canonical_name = excluded.canonical_name,
                        metadata = excluded.metadata,
                        updated_at = excluded.updated_at
                    """,
                    (
                        author_id,
                        str(bucket["canonical_name"]),
                        json.dumps(metadata, sort_keys=True, ensure_ascii=True),
                        now,
                        now,
                    ),
                )
                profiles.append(
                    {
                        "id": author_id,
                        "canonical_name": str(bucket["canonical_name"]),
                        "source_id": source_id,
                        "domains": sorted(bucket["domains"]),
                        "accounts": sorted(bucket["accounts"]),
                        "profile_urls": sorted(bucket["profile_urls"]),
                    }
                )

            if not profiles:
                return []

            # Enrich with account identifiers already mapped in accounts table.
            author_ids = [str(profile["id"]) for profile in profiles]
            placeholders = ",".join(["?"] * len(author_ids))
            account_rows = connection.execute(
                f"""
                SELECT author_id, source_identifier
                FROM accounts
                WHERE author_id IN ({placeholders})
                """,
                author_ids,
            ).fetchall()
            by_author_id: dict[str, set[str]] = {author_id: set() for author_id in author_ids}
            for row in account_rows:
                by_author_id[str(row["author_id"])].add(str(row["source_identifier"]).strip().lower())
            for profile in profiles:
                profile_accounts = set(profile["accounts"]) | by_author_id.get(str(profile["id"]), set())
                profile["accounts"] = sorted(value for value in profile_accounts if value)

            return profiles

    def save_merge_decision(self, decision: MergeDecision) -> bool:
        """
        Persist one merge decision.

        Returns:
            True when inserted, False when already present (idempotent replay).
        """
        with self._connect() as connection:
            from_exists = connection.execute(
                "SELECT 1 FROM authors WHERE id = ?",
                (decision.from_author_id,),
            ).fetchone()
            to_exists = connection.execute(
                "SELECT 1 FROM authors WHERE id = ?",
                (decision.to_author_id,),
            ).fetchone()
            if from_exists is None or to_exists is None:
                raise ValueError("Cannot save merge decision: from/to author does not exist")

            rowcount = connection.execute(
                """
                INSERT OR IGNORE INTO merge_decisions (
                    id,
                    from_author_id,
                    to_author_id,
                    evidence_ids,
                    decision_criteria,
                    created_at,
                    created_by,
                    run_id,
                    reverted_at,
                    reverted_by,
                    reverted_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision.id,
                    decision.from_author_id,
                    decision.to_author_id,
                    json.dumps(decision.evidence_ids, sort_keys=True, ensure_ascii=True),
                    decision.decision_criteria,
                    decision.created_at.isoformat(),
                    decision.created_by,
                    decision.run_id,
                    decision.reverted_at.isoformat() if decision.reverted_at else None,
                    decision.reverted_by,
                    decision.reverted_reason,
                ),
            ).rowcount
            return bool(rowcount)

    def create_run_log(self, run_log: RunLog) -> None:
        """Insert a new run_log row."""
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO run_log (
                    id,
                    source_id,
                    started_at,
                    ended_at,
                    status,
                    error_message,
                    fetched_count,
                    new_articles_count,
                    updated_articles_count,
                    error_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_log.id,
                    run_log.source_id,
                    run_log.started_at.isoformat(),
                    run_log.ended_at.isoformat() if run_log.ended_at else None,
                    run_log.status.value,
                    run_log.error_message,
                    run_log.fetched_count,
                    run_log.new_articles_count,
                    run_log.updated_articles_count,
                    run_log.error_count,
                ),
            )

    def save_fetch_log(self, fetch_log: FetchLog) -> None:
        """Insert one fetch_log row."""
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO fetch_log (
                    id,
                    url,
                    status_code,
                    latency_ms,
                    bytes_received,
                    error_code,
                    created_at,
                    run_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fetch_log.id,
                    fetch_log.url,
                    fetch_log.status_code,
                    fetch_log.latency_ms,
                    fetch_log.bytes_received,
                    fetch_log.error_code.value if fetch_log.error_code else None,
                    fetch_log.created_at.isoformat(),
                    fetch_log.run_id,
                ),
            )

    def update_run_log(self, run_log: RunLog) -> None:
        """Update end-state metrics for a run."""
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE run_log
                SET
                    ended_at = ?,
                    status = ?,
                    error_message = ?,
                    fetched_count = ?,
                    new_articles_count = ?,
                    updated_articles_count = ?,
                    error_count = ?
                WHERE id = ?
                """,
                (
                    run_log.ended_at.isoformat() if run_log.ended_at else None,
                    run_log.status.value,
                    run_log.error_message,
                    run_log.fetched_count,
                    run_log.new_articles_count,
                    run_log.updated_articles_count,
                    run_log.error_count,
                    run_log.id,
                ),
            )

    def upsert_article(
        self,
        draft: ArticleDraft,
        evidence_list: list[Evidence],
        run_id: str,
    ) -> tuple[Article, bool, bool]:
        """
        Upsert article by dedupe key and apply minimal versioning.

        Returns:
            (article, created, updated)
        """
        canonical_url = canonicalize_url(draft.canonical_url)
        content_hash = _hash_article_fields(draft)
        now = _utc_now()

        with self._connect() as connection:
            existing_row = connection.execute(
                """
                SELECT id, version, created_at, updated_at
                FROM articles
                WHERE canonical_url = ? AND source_id = ?
                """,
                (canonical_url, draft.source_id),
            ).fetchone()

            created = False
            updated = False

            if existing_row is None:
                article_id = str(uuid4())
                version = 1
                persisted_evidence = [
                    item.model_copy(
                        update={
                            "article_id": article_id,
                            "run_id": run_id,
                        }
                    )
                    for item in evidence_list
                ]
                connection.execute(
                    """
                    INSERT INTO articles (
                        id, canonical_url, source_id, title, author_hint, published_at,
                        snippet, version, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        article_id,
                        canonical_url,
                        draft.source_id,
                        draft.title,
                        draft.author_hint,
                        draft.published_at.isoformat() if draft.published_at else None,
                        draft.snippet,
                        version,
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO versions (
                        id, article_id, version, content_hash,
                        title_snapshot, author_hint_snapshot, published_at_snapshot, snippet_snapshot,
                        evidence_snapshot, created_at, run_id
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid4()),
                        article_id,
                        version,
                        content_hash,
                        draft.title,
                        draft.author_hint,
                        draft.published_at.isoformat() if draft.published_at else None,
                        draft.snippet,
                        _serialize_evidence_snapshot(persisted_evidence),
                        now.isoformat(),
                        run_id,
                    ),
                )
                connection.execute("DELETE FROM evidence WHERE article_id = ?", (article_id,))
                for item in persisted_evidence:
                    _insert_evidence_row(connection, item)
                created = True
            else:
                article_id = str(existing_row["id"])
                current_version = int(existing_row["version"])
                latest_version_row = connection.execute(
                    """
                    SELECT content_hash
                    FROM versions
                    WHERE article_id = ?
                    ORDER BY version DESC
                    LIMIT 1
                    """,
                    (article_id,),
                ).fetchone()
                latest_hash = str(latest_version_row["content_hash"]) if latest_version_row else None

                version = current_version
                if latest_hash != content_hash:
                    version = current_version + 1
                    persisted_evidence = [
                        item.model_copy(
                            update={
                                "article_id": article_id,
                                "run_id": run_id,
                            }
                        )
                        for item in evidence_list
                    ]
                    connection.execute(
                        """
                        UPDATE articles
                        SET
                            title = ?,
                            author_hint = ?,
                            published_at = ?,
                            snippet = ?,
                            version = ?,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            draft.title,
                            draft.author_hint,
                            draft.published_at.isoformat() if draft.published_at else None,
                            draft.snippet,
                            version,
                            now.isoformat(),
                            article_id,
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO versions (
                            id, article_id, version, content_hash,
                            title_snapshot, author_hint_snapshot, published_at_snapshot, snippet_snapshot,
                            evidence_snapshot, created_at, run_id
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(uuid4()),
                            article_id,
                            version,
                            content_hash,
                            draft.title,
                            draft.author_hint,
                            draft.published_at.isoformat() if draft.published_at else None,
                            draft.snippet,
                            _serialize_evidence_snapshot(persisted_evidence),
                            now.isoformat(),
                            run_id,
                        ),
                    )
                    connection.execute("DELETE FROM evidence WHERE article_id = ?", (article_id,))
                    for item in persisted_evidence:
                        _insert_evidence_row(connection, item)
                    updated = True

            article = self._load_article(connection, article_id)
            return article, created, updated

    def _load_article(self, connection: sqlite3.Connection, article_id: str) -> Article:
        """Load one article with evidence rows from SQLite."""
        article_row = connection.execute(
            """
            SELECT id, canonical_url, source_id, title, author_hint, published_at, snippet,
                   version, created_at, updated_at
            FROM articles
            WHERE id = ?
            """,
            (article_id,),
        ).fetchone()
        if article_row is None:
            raise ValueError(f"Article not found: {article_id}")

        evidence_rows = connection.execute(
            """
            SELECT
                id, article_id, claim_path, evidence_type, source_url, extraction_method,
                extracted_text, confidence, metadata, retrieved_at, extractor_version,
                input_ref, snippet_max_chars_applied, created_at, run_id
            FROM evidence
            WHERE article_id = ?
            ORDER BY created_at, id
            """,
            (article_id,),
        ).fetchall()

        evidence_list = [
            Evidence(
                id=str(row["id"]),
                article_id=str(row["article_id"]),
                claim_path=str(row["claim_path"]),
                evidence_type=str(row["evidence_type"]),
                source_url=str(row["source_url"]),
                extraction_method=row["extraction_method"],
                extracted_text=str(row["extracted_text"]),
                confidence=float(row["confidence"]) if row["confidence"] is not None else 1.0,
                metadata=json.loads(row["metadata"]) if row["metadata"] else {},
                retrieved_at=_parse_iso_datetime(row["retrieved_at"]) or _utc_now(),
                extractor_version=row["extractor_version"],
                input_ref=row["input_ref"],
                snippet_max_chars_applied=row["snippet_max_chars_applied"],
                created_at=_parse_iso_datetime(row["created_at"]) or _utc_now(),
                run_id=str(row["run_id"]),
            )
            for row in evidence_rows
        ]

        return Article(
            id=str(article_row["id"]),
            canonical_url=str(article_row["canonical_url"]),
            source_id=str(article_row["source_id"]),
            title=article_row["title"],
            author_hint=article_row["author_hint"],
            published_at=_parse_iso_datetime(article_row["published_at"]),
            snippet=article_row["snippet"],
            evidence=evidence_list,
            version=int(article_row["version"]),
            created_at=_parse_iso_datetime(article_row["created_at"]) or _utc_now(),
            updated_at=_parse_iso_datetime(article_row["updated_at"]) or _utc_now(),
        )

    def iter_articles_for_export(self) -> Iterator[Article]:
        """Yield stored articles with evidence in deterministic order for export."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id
                FROM articles
                ORDER BY canonical_url, source_id
                """
            ).fetchall()
            for row in rows:
                yield self._load_article(connection, str(row["id"]))

    def rollback_run(self, run_id: str) -> dict[str, int]:
        """
        Roll back artifacts created by one run_id.

        Minimal rollback semantics for M3:
        - Delete fetch_log rows for run
        - Delete evidence rows for run
        - Delete version rows for run
        - If an article has no remaining versions after deletion, delete article
        - Otherwise restore article fields/version from latest remaining version snapshot
        - Mark run_log status as CANCELLED with rollback note
        """
        summary = {
            "fetch_log_deleted": 0,
            "evidence_deleted": 0,
            "versions_deleted": 0,
            "merge_decisions_deleted": 0,
            "articles_deleted": 0,
            "articles_reverted": 0,
        }
        now = _utc_now().isoformat()

        with self._connect() as connection:
            fetch_deleted = connection.execute(
                "DELETE FROM fetch_log WHERE run_id = ?",
                (run_id,),
            ).rowcount
            evidence_deleted = connection.execute(
                "DELETE FROM evidence WHERE run_id = ?",
                (run_id,),
            ).rowcount

            affected_article_rows = connection.execute(
                """
                SELECT DISTINCT article_id
                FROM versions
                WHERE run_id = ?
                """,
                (run_id,),
            ).fetchall()
            affected_article_ids = [str(row["article_id"]) for row in affected_article_rows]

            versions_deleted = connection.execute(
                "DELETE FROM versions WHERE run_id = ?",
                (run_id,),
            ).rowcount
            merge_decisions_deleted = connection.execute(
                "DELETE FROM merge_decisions WHERE run_id = ?",
                (run_id,),
            ).rowcount

            for article_id in affected_article_ids:
                latest_remaining = connection.execute(
                    """
                    SELECT
                        version,
                        title_snapshot,
                        author_hint_snapshot,
                        published_at_snapshot,
                        snippet_snapshot,
                        evidence_snapshot
                    FROM versions
                    WHERE article_id = ?
                    ORDER BY version DESC
                    LIMIT 1
                    """,
                    (article_id,),
                ).fetchone()

                if latest_remaining is None:
                    connection.execute("DELETE FROM evidence WHERE article_id = ?", (article_id,))
                    deleted = connection.execute(
                        "DELETE FROM articles WHERE id = ?",
                        (article_id,),
                    ).rowcount
                    if deleted:
                        summary["articles_deleted"] += 1
                    continue

                connection.execute(
                    """
                    UPDATE articles
                    SET
                        title = ?,
                        author_hint = ?,
                        published_at = ?,
                        snippet = ?,
                        version = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        latest_remaining["title_snapshot"],
                        latest_remaining["author_hint_snapshot"],
                        latest_remaining["published_at_snapshot"],
                        latest_remaining["snippet_snapshot"],
                        int(latest_remaining["version"]),
                        now,
                        article_id,
                    ),
                )
                connection.execute("DELETE FROM evidence WHERE article_id = ?", (article_id,))
                restored_evidence = _deserialize_evidence_snapshot(
                    latest_remaining["evidence_snapshot"],
                    article_id=article_id,
                )
                for item in restored_evidence:
                    _insert_evidence_row(connection, item)
                summary["articles_reverted"] += 1

            connection.execute(
                """
                UPDATE run_log
                SET status = 'CANCELLED',
                    ended_at = COALESCE(ended_at, ?),
                    error_message = ?
                WHERE id = ?
                """,
                (now, f"Rolled back run {run_id}", run_id),
            )

        summary["fetch_log_deleted"] = fetch_deleted
        summary["evidence_deleted"] = evidence_deleted
        summary["versions_deleted"] = versions_deleted
        summary["merge_decisions_deleted"] = merge_decisions_deleted
        return summary


class SQLiteStoreStage(StoreStage):
    """Store stage backed by SQLiteRunStore upsert/versioning methods."""

    def __init__(self, run_store: SQLiteRunStore) -> None:
        """Initialize store stage with shared SQLite run store."""
        self.run_store = run_store

    def store(
        self,
        draft: ArticleDraft,
        evidence_list: list[Evidence],
        run_id: str,
    ) -> tuple[Article, bool, bool]:
        """Persist one article draft and return `(article, created, updated)`."""
        return self.run_store.upsert_article(draft, evidence_list, run_id)


class SQLiteExportStage(ExportStage):
    """Export stage backed by SQLiteRunStore with row-by-row schema validation."""

    def __init__(self, run_store: SQLiteRunStore) -> None:
        """Initialize export stage with shared SQLite run store."""
        self.run_store = run_store

    def export(self, output_path: str) -> int:
        """
        Export all rows as JSONL.

        Validation is performed per row before writing that row.
        On first invalid row, raises ValueError and stops immediately.
        """
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)

        exported_count = 0
        with output.open("w", encoding="utf-8") as handle:
            for article in self.run_store.iter_articles_for_export():
                payload = article.model_dump(mode="json")
                try:
                    jsonschema.validate(payload, ARTICLE_SCHEMA)
                except jsonschema.ValidationError as exc:
                    raise ValueError(
                        f"Export validation failed for article {article.id}: {exc.message}"
                    ) from exc
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
                exported_count += 1
        return exported_count
