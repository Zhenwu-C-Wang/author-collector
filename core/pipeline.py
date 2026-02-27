"""
Pipeline interface for author-collector.

Defines the contract for moving data through the stages:
discover → fetch → parse → extract → store → export

This is intentionally minimal and prescriptive:
- No skipping stages
- No out-of-order execution
- No retries or branching logic (handled by connector/orchestrator layer)
"""

from abc import ABC, abstractmethod
from datetime import UTC, datetime
from typing import Iterator, Optional

from core.structured_logging import emit_json_event
from core.models import (
    Parsed,
    ArticleDraft,
    Evidence,
    Article,
    FetchedDoc,
    FetchLog,
    RunLog,
    RunStatus,
)


# ============================================================================
# Stage Interfaces
# ============================================================================

class DiscoverStage(ABC):
    """
    Discover stage: given a seed, produce an iterator of URLs to fetch.

    Example:
      seed = "https://example.com/rss"
      → yields ["https://example.com/article1", "https://example.com/article2", ...]
    """

    @abstractmethod
    def discover(self, seed: str, run_id: str) -> Iterator[str]:
        """
        Discover URLs from seed.

        Args:
            seed: Starting point (feed URL, author page, etc.)
            run_id: Run ID for tracking

        Yields:
            URLs to fetch (canonical, validated)

        Raises:
            ValueError: If seed is invalid
            Exception: Any discovery error (logged, not fatal)
        """
        pass


class FetchStage(ABC):
    """
    Fetch stage: given a URL, download its content with safety constraints.

    Responsibilities:
    - Robots.txt enforcement (blocking if disallowed)
    - Rate limiting (per-domain + global concurrency)
    - Security: IP blocklist, protocol whitelist, redirect limits
    - Observability: log all fetches (success + error)
    """

    @abstractmethod
    def fetch(self, url: str, run_id: str) -> tuple[Optional[FetchedDoc], FetchLog]:
        """
        Fetch a single URL.

        Args:
            url: URL to fetch
            run_id: Run ID for tracking

        Returns:
            (FetchedDoc, FetchLog entry)
            - If fetch fails, FetchedDoc is None and FetchLog.error_code is set
            - If fetch succeeds, FetchedDoc contains status_code, headers, body_bytes, etc.
            - 304 Not Modified: FetchedDoc.body_bytes is None (use cached version)

        Always returns a FetchLog (never raises; errors logged)
        """
        pass


class ParseStage(ABC):
    """
    Parse stage: convert bytes → structured Parsed object.

    Responsibilities:
    - HTML → readable text (trafilatura/readability)
    - Extract metadata (JSON-LD, meta tags, canonical URL)
    - Sanity checks (content size, encoding, etc.)
    """

    @abstractmethod
    def parse(self, fetched: FetchedDoc, run_id: str) -> Parsed:
        """
        Parse downloaded content.

        Args:
            fetched: FetchedDoc from fetch stage
            run_id: Run ID for tracking

        Returns:
            Parsed object with extracted metadata + readable text

        Raises:
            Exception: Parse errors (logged, not fatal)
        """
        pass


class ExtractStage(ABC):
    """
    Extract stage: convert Parsed → ArticleDraft + Evidence[].

    Responsibilities:
    - Deterministic mapping of Parsed → ArticleDraft
    - Generate Evidence objects for each claim
    - Ensure every non-null claim has ≥1 evidence entry
    """

    @abstractmethod
    def extract(self, parsed: Parsed, run_id: str) -> tuple[ArticleDraft, list[Evidence]]:
        """
        Extract article draft + evidence from parsed content.

        Args:
            parsed: Parsed object from parse stage
            run_id: Run ID for tracking

        Returns:
            (ArticleDraft, [Evidence])
            - ArticleDraft: Core metadata ready for storage
            - Evidence: List of evidence entries (one per claim source)

        Raises:
            ValueError: If parsed content is invalid
        """
        pass


class StoreStage(ABC):
    """
    Store stage: save ArticleDraft → Article (with versioning + dedup).

    Responsibilities:
    - Canonicalize URL
    - Check for duplicates (same canonical_url + source_id)
    - Upsert (update if exists, insert if new)
    - Track versions (content hash changes)
    - Store evidence with run_id tracking
    """

    @abstractmethod
    def store(
        self,
        draft: ArticleDraft,
        evidence_list: list[Evidence],
        run_id: str,
    ) -> tuple[Article, bool, bool]:
        """
        Store article draft + evidence.

        Args:
            draft: ArticleDraft from extract stage
            evidence_list: Evidence entries
            run_id: Run ID for tracking (used for versioning + rollback)

        Returns:
            Tuple `(article, created, updated)`:
            - article: Stored Article (with assigned ID, version, etc.)
            - created: True when new article row was created
            - updated: True when existing article content version was updated

        Raises:
            Exception: Storage errors (logged, but fail the entire run)
        """
        pass


class ExportStage(ABC):
    """
    Export stage: serialize all articles to JSONL with schema validation.

    Responsibilities:
    - Query all articles from storage
    - Validate each against schema
    - Write JSONL (one article per line)
    - Fail fast if any row is invalid
    """

    @abstractmethod
    def export(self, output_path: str) -> int:
        """
        Export all articles to JSONL.

        Args:
            output_path: Where to write JSONL file

        Returns:
            Number of articles exported

        Raises:
            ValueError: If any article fails schema validation
            IOError: If write fails
        """
        pass


# ============================================================================
# Pipeline Orchestrator
# ============================================================================

class Pipeline:
    """
    Main orchestrator: coordinates all stages in sequence.

    Usage:
        pipeline = Pipeline(discover, fetch, parse, extract, store, export)
        run = pipeline.run(seed="https://example.com/rss", source_id="rss:example")
    """

    def __init__(
        self,
        discover: DiscoverStage,
        fetch: FetchStage,
        parse: ParseStage,
        extract: ExtractStage,
        store: StoreStage,
        export: ExportStage,
        run_store: object | None = None,
    ):
        """Initialize pipeline stage instances and optional persistence backend."""
        self.discover = discover
        self.fetch = fetch
        self.parse = parse
        self.extract = extract
        self.store = store
        self.export = export
        self.run_store = run_store

    def _persist_run_start(self, run_log: RunLog) -> None:
        """Persist run start if storage is configured."""
        if self.run_store and hasattr(self.run_store, "create_run_log"):
            self.run_store.create_run_log(run_log)

    def _persist_fetch_log(self, fetch_log: FetchLog) -> None:
        """Persist one fetch log entry if storage is configured."""
        if self.run_store and hasattr(self.run_store, "save_fetch_log"):
            self.run_store.save_fetch_log(fetch_log)

    def _persist_run_end(self, run_log: RunLog) -> None:
        """Persist run completion if storage is configured."""
        if self.run_store and hasattr(self.run_store, "update_run_log"):
            self.run_store.update_run_log(run_log)

    @staticmethod
    def _emit_pipeline_event(
        event_type: str,
        *,
        run_id: str,
        source_id: str,
        **payload: object,
    ) -> None:
        """Emit a structured pipeline event for recoverable/unrecoverable errors."""
        emit_json_event(
            event_type=event_type,
            run_id=run_id,
            source_id=source_id,
            component="pipeline",
            **payload,
        )

    def run(
        self,
        seed: str,
        source_id: str,
        run_id: str,
        dry_run: bool = False,
    ) -> RunLog:
        """
        Execute full pipeline for a seed.

        Stages executed in order:
        1. discover(seed) → URLs
        2. for each URL:
           a. fetch(url) → FetchedDoc
           b. parse(FetchedDoc) → Parsed
           c. extract(Parsed) → ArticleDraft + Evidence
           d. store(ArticleDraft) → (Article, created, updated)
        3. export() → JSONL

        Args:
            seed: Starting point (e.g., RSS feed URL)
            source_id: Identifier for this source
            run_id: Run ID (for tracking + rollback)
            dry_run: If True, don't store anything (just validate)

        Returns:
            RunLog with final status + counts

        Note:
            - Errors in any stage are logged, not fatal (run continues)
            - RunLog tracks all counts (fetched, created, updated, errors)
            - Export happens only if run succeeds
        """
        run_log = RunLog(id=run_id, source_id=source_id)
        self._persist_run_start(run_log)

        try:
            # Stage 1: Discover
            urls = list(self.discover.discover(seed, run_id))
            if not urls:
                run_log.status = RunStatus.COMPLETED
                run_log.error_message = "No URLs discovered"
                run_log.ended_at = datetime.now(UTC)
                self._persist_run_end(run_log)
                return run_log

            # Stage 2-5: For each URL, fetch → parse → extract → store
            for url in urls:
                stage = "fetch"
                try:
                    # Fetch
                    fetched_doc, fetch_log = self.fetch.fetch(url, run_id)
                    run_log.fetched_count += 1
                    self._persist_fetch_log(fetch_log)

                    if fetch_log.error_code or fetched_doc is None:
                        run_log.error_count += 1
                        continue  # Skip to next URL

                    # Parse
                    stage = "parse"
                    parsed = self.parse.parse(fetched_doc, run_id)

                    # Extract
                    stage = "extract"
                    draft, evidence_list = self.extract.extract(parsed, run_id)

                    # Store
                    if not dry_run:
                        stage = "store"
                        _, created, updated = self.store.store(draft, evidence_list, run_id)
                        if created:
                            run_log.new_articles_count += 1
                        if updated:
                            run_log.updated_articles_count += 1

                except Exception as exc:
                    run_log.error_count += 1
                    self._emit_pipeline_event(
                        "pipeline_stage_error",
                        run_id=run_id,
                        source_id=source_id,
                        url=url,
                        stage=stage,
                        error_type=type(exc).__name__,
                        error=str(exc),
                    )
                    # Log error and continue to next URL.
                    continue

            # Stage 6: Export
            if not dry_run:
                try:
                    self.export.export(f"export_{run_id}.jsonl")
                except Exception as e:
                    self._emit_pipeline_event(
                        "pipeline_export_error",
                        run_id=run_id,
                        source_id=source_id,
                        stage="export",
                        error_type=type(e).__name__,
                        error=str(e),
                    )
                    run_log.error_message = f"Export failed: {e}"
                    run_log.status = RunStatus.FAILED
                    run_log.ended_at = datetime.now(UTC)
                    self._persist_run_end(run_log)
                    return run_log

            run_log.status = RunStatus.COMPLETED
            run_log.ended_at = datetime.now(UTC)

        except Exception as e:
            self._emit_pipeline_event(
                "pipeline_run_error",
                run_id=run_id,
                source_id=source_id,
                stage="run",
                error_type=type(e).__name__,
                error=str(e),
            )
            run_log.status = RunStatus.FAILED
            run_log.error_message = str(e)
            run_log.ended_at = datetime.now(UTC)

        self._persist_run_end(run_log)

        return run_log
