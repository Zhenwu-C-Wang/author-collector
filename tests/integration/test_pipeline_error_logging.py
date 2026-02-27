"""Regression tests for structured pipeline exception logging paths."""

from __future__ import annotations

import json

import pytest

from core.models import Article, ArticleDraft, FetchLog, FetchedDoc, Parsed, RunStatus
from core.pipeline import DiscoverStage, ExportStage, ExtractStage, FetchStage, ParseStage, Pipeline, StoreStage


def _json_lines(stdout: str) -> list[dict]:
    """Parse JSON log lines emitted by the pipeline."""
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    return [json.loads(line) for line in lines]


class OneUrlDiscoverStage(DiscoverStage):
    """Yield one URL then stop."""

    def discover(self, seed: str, run_id: str):
        """Yield deterministic URL."""
        _ = seed, run_id
        yield "https://example.com/article"


class FailingDiscoverStage(DiscoverStage):
    """Raise at discovery to exercise top-level pipeline error handling."""

    def discover(self, seed: str, run_id: str):
        """Raise a deterministic discovery failure."""
        _ = seed, run_id
        raise RuntimeError("discover boom")


class SuccessFetchStage(FetchStage):
    """Return deterministic fetched document."""

    def fetch(self, url: str, run_id: str):
        """Return success document and fetch log."""
        return (
            FetchedDoc(
                status_code=200,
                final_url=url,
                headers={"content-type": "text/html"},
                body_bytes=b"<html></html>",
                body_sha256="deadbeef",
                latency_ms=1,
            ),
            FetchLog(
                url=url,
                status_code=200,
                latency_ms=1,
                bytes_received=13,
                run_id=run_id,
            ),
        )


class FailingParseStage(ParseStage):
    """Raise during parse to exercise inner-loop exception path."""

    def parse(self, fetched: FetchedDoc, run_id: str) -> Parsed:
        """Raise parse exception for test coverage."""
        _ = fetched, run_id
        raise ValueError("parse boom")


class NoopExtractStage(ExtractStage):
    """Unused extract stage for tests where parse fails first."""

    def extract(self, parsed: Parsed, run_id: str):
        """Return minimal values when invoked."""
        _ = run_id
        return ArticleDraft(canonical_url=parsed.url, source_id="rss:test"), []


class NoopStoreStage(StoreStage):
    """No-op store stage."""

    def store(self, draft: ArticleDraft, evidence_list, run_id: str):
        """Return no-op stored tuple."""
        _ = draft, evidence_list, run_id
        return Article(canonical_url="https://example.com/article", source_id="rss:test"), False, False


class NoopExportStage(ExportStage):
    """No-op export stage used in dry-run tests."""

    def export(self, output_path: str) -> int:
        """Return zero exported rows."""
        _ = output_path
        return 0


@pytest.mark.integration
def test_pipeline_stage_exception_emits_structured_json(tmp_path, capsys):
    """Pipeline should log structured stage errors and continue gracefully."""
    pipeline = Pipeline(
        discover=OneUrlDiscoverStage(),
        fetch=SuccessFetchStage(),
        parse=FailingParseStage(),
        extract=NoopExtractStage(),
        store=NoopStoreStage(),
        export=NoopExportStage(),
        run_store=None,
    )

    run_log = pipeline.run(
        seed="seed",
        source_id="rss:test",
        run_id="run-pipeline-stage-error",
        dry_run=True,
    )

    assert run_log.status == RunStatus.COMPLETED
    assert run_log.error_count == 1

    events = _json_lines(capsys.readouterr().out)
    stage_events = [item for item in events if item.get("event_type") == "pipeline_stage_error"]
    assert stage_events
    assert stage_events[-1]["run_id"] == "run-pipeline-stage-error"
    assert stage_events[-1]["stage"] == "parse"
    assert stage_events[-1]["error_type"] == "ValueError"


@pytest.mark.integration
def test_pipeline_outer_exception_emits_structured_json(capsys):
    """Pipeline should log structured run errors when discover fails."""
    pipeline = Pipeline(
        discover=FailingDiscoverStage(),
        fetch=SuccessFetchStage(),
        parse=FailingParseStage(),
        extract=NoopExtractStage(),
        store=NoopStoreStage(),
        export=NoopExportStage(),
        run_store=None,
    )

    run_log = pipeline.run(
        seed="seed",
        source_id="rss:test",
        run_id="run-pipeline-fatal-error",
        dry_run=True,
    )

    assert run_log.status == RunStatus.FAILED
    assert run_log.error_message == "discover boom"

    events = _json_lines(capsys.readouterr().out)
    run_events = [item for item in events if item.get("event_type") == "pipeline_run_error"]
    assert run_events
    assert run_events[-1]["run_id"] == "run-pipeline-fatal-error"
    assert run_events[-1]["stage"] == "run"
    assert run_events[-1]["error_type"] == "RuntimeError"
