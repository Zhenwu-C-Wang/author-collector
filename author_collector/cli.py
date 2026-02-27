"""Minimal CLI entrypoint for author-collector."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from typing import Sequence
from uuid import uuid4

from connectors import ArxivDiscoverStage, HtmlAuthorPageDiscoverStage, RssDiscoverStage
from core.structured_logging import emit_json_event
from core.models import MergeDecision, RunLog, RunStatus
from core.pipeline import Pipeline
from extractor import ArticleExtractStage
from fetcher import HttpFetchStage
from parser import HtmlParseStage
from resolution import build_candidates
from storage.sqlite import SQLiteExportStage, SQLiteRunStore
from storage import SQLiteStoreStage


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCHEMAS_DIR = PROJECT_ROOT / "schemas"


def _resolve_command_run_id(args: argparse.Namespace) -> str:
    """Resolve run_id from CLI args or create one for command-level tracing."""
    explicit = getattr(args, "run_id", None)
    if explicit:
        return str(explicit)

    rollback_run = getattr(args, "run", None)
    if rollback_run:
        return str(rollback_run)

    return str(uuid4())


def _emit_cli_event(
    event_type: str,
    *,
    run_id: str,
    command: str,
    **payload: Any,
) -> str:
    """Emit one structured CLI event line with standard fields."""
    return emit_json_event(
        event_type=event_type,
        run_id=run_id,
        command=command,
        **payload,
    )


def _validate_schema_file(path: Path) -> None:
    """Validate that a JSON schema file is well-formed and has required top-level keys."""
    data = json.loads(path.read_text(encoding="utf-8"))
    required_keys = {"$schema", "type", "properties", "required"}
    missing = required_keys.difference(data)
    if missing:
        missing_str = ", ".join(sorted(missing))
        raise ValueError(f"{path.name} missing required schema keys: {missing_str}")


def _cmd_validate_schemas(_: argparse.Namespace) -> int:
    """Validate schema files for basic structural correctness."""
    run_id = str(uuid4())
    article_schema = SCHEMAS_DIR / "article.schema.json"
    evidence_schema = SCHEMAS_DIR / "evidence.schema.json"
    for path in (article_schema, evidence_schema):
        if not path.exists():
            raise FileNotFoundError(f"Schema file not found: {path}")
        _validate_schema_file(path)
    _emit_cli_event(
        "cli_validate_schemas_completed",
        run_id=run_id,
        command="validate-schemas",
        schema_files=[str(article_schema), str(evidence_schema)],
    )
    return 0


def _cmd_export(args: argparse.Namespace) -> int:
    """Export articles from SQLite with per-row schema validation."""
    run_id = args.run_id or str(uuid4())
    output = Path(args.output)
    run_store = SQLiteRunStore(args.db)
    exporter = SQLiteExportStage(run_store)
    count = exporter.export(str(output))
    _emit_cli_event(
        "cli_export_completed",
        run_id=run_id,
        command="export",
        output=str(output),
        db=str(args.db),
        exported_rows=count,
    )
    return 0


def _cmd_rollback(args: argparse.Namespace) -> int:
    """Rollback all persisted artifacts for a run_id."""
    run_id = str(args.run)
    db_path = Path(args.db)
    if not db_path.exists():
        raise FileNotFoundError(f"Database file not found: {db_path}")

    run_store = SQLiteRunStore(db_path, initialize=False)
    summary = run_store.rollback_run(args.run)
    _emit_cli_event(
        "cli_rollback_completed",
        run_id=run_id,
        command="rollback",
        db=str(db_path),
        target_run_id=str(args.run),
        **summary,
    )
    return 0


def _cmd_review_queue(args: argparse.Namespace) -> int:
    """Generate review queue candidates and write review.json."""
    run_id = args.run_id or str(uuid4())
    run_store = SQLiteRunStore(args.db)
    profiles = run_store.list_resolution_author_profiles()
    candidates = [item.to_dict() for item in build_candidates(profiles, min_score=args.min_score)]

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "min_score": args.min_score,
        "candidates": candidates,
    }
    output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    _emit_cli_event(
        "cli_review_queue_completed",
        run_id=run_id,
        command="review-queue",
        db=str(args.db),
        output=str(output_path),
        min_score=args.min_score,
        candidate_count=len(candidates),
    )
    return 0


def _cmd_review_apply(args: argparse.Namespace) -> int:
    """Apply manual review decisions and persist merge_decisions rows."""
    review_path = Path(args.review_file)
    if not review_path.exists():
        raise FileNotFoundError(f"Review file not found: {review_path}")

    raw = json.loads(review_path.read_text(encoding="utf-8"))
    candidates = raw.get("candidates", []) if isinstance(raw, dict) else []
    if not isinstance(candidates, list):
        raise ValueError("Invalid review file: 'candidates' must be a list")

    run_id = args.run_id or str(uuid4())
    run_store = SQLiteRunStore(args.db)
    run_log = RunLog(id=run_id, source_id="review:apply")
    run_store.create_run_log(run_log)

    accepted = 0
    duplicates = 0
    rejected = 0
    held = 0
    invalid = 0

    for item in candidates:
        if not isinstance(item, dict):
            invalid += 1
            continue

        decision = str(item.get("decision") or "").strip().lower()
        if decision == "reject":
            rejected += 1
            continue
        if decision in {"", "hold"}:
            held += 1
            continue
        if decision != "accept":
            invalid += 1
            continue

        from_author = item.get("from_author") if isinstance(item.get("from_author"), dict) else {}
        to_author = item.get("to_author") if isinstance(item.get("to_author"), dict) else {}
        from_id = str(from_author.get("id") or "")
        to_id = str(to_author.get("id") or "")
        from_name = str(from_author.get("canonical_name") or from_author.get("name") or "").strip()
        to_name = str(to_author.get("canonical_name") or to_author.get("name") or "").strip()
        if not from_id or not to_id:
            invalid += 1
            continue
        if not from_name:
            from_name = from_id
        if not to_name:
            to_name = to_id

        run_store.ensure_author(from_id, from_name)
        run_store.ensure_author(to_id, to_name)

        candidate_id = str(item.get("id") or f"{from_id}:{to_id}")
        score_payload = {
            "score": item.get("score"),
            "confidence": item.get("confidence"),
            "scoring_breakdown": item.get("scoring_breakdown"),
        }
        decision_record = MergeDecision(
            id=candidate_id,
            from_author_id=from_id,
            to_author_id=to_id,
            evidence_ids=[str(entry) for entry in item.get("evidence", []) if entry],
            decision_criteria=json.dumps(score_payload, sort_keys=True, ensure_ascii=True),
            created_by=args.created_by,
            run_id=run_id,
        )
        inserted = run_store.save_merge_decision(decision_record)
        if inserted:
            accepted += 1
        else:
            duplicates += 1

    run_log.ended_at = datetime.now(UTC)
    run_log.error_count = invalid
    if invalid:
        run_log.error_message = f"{invalid} invalid candidate rows skipped"
    run_log.status = RunStatus.COMPLETED
    run_store.update_run_log(run_log)

    _emit_cli_event(
        "cli_review_apply_completed",
        run_id=run_id,
        command="review apply",
        db=str(args.db),
        review_file=str(review_path),
        accepted=accepted,
        duplicates=duplicates,
        rejected=rejected,
        held=held,
        invalid=invalid,
    )
    return 0 if invalid == 0 else 1


def _build_discover_stage(source_id: str) -> object:
    """Build discover stage for a source_id."""
    if source_id.startswith("rss:"):
        return RssDiscoverStage()
    if source_id.startswith("html:"):
        return HtmlAuthorPageDiscoverStage()
    if source_id.startswith("arxiv:"):
        return ArxivDiscoverStage()
    raise ValueError(f"Unsupported source_id for sync: {source_id}")


def _cmd_sync(args: argparse.Namespace) -> int:
    """Run one sync job for the given source/seed."""
    run_id = args.run_id or str(uuid4())
    run_store = SQLiteRunStore(args.db)

    discover_stage = _build_discover_stage(args.source_id)
    fetch_stage = HttpFetchStage()
    parse_stage = HtmlParseStage()
    extract_stage = ArticleExtractStage(source_id=args.source_id)
    store_stage = SQLiteStoreStage(run_store)
    export_stage = SQLiteExportStage(run_store)

    pipeline = Pipeline(
        discover=discover_stage,
        fetch=fetch_stage,
        parse=parse_stage,
        extract=extract_stage,
        store=store_stage,
        export=export_stage,
        run_store=run_store,
    )
    run_log = pipeline.run(
        seed=args.seed,
        source_id=args.source_id,
        run_id=run_id,
        dry_run=args.dry_run,
    )
    _emit_cli_event(
        "cli_sync_completed",
        run_id=run_log.id,
        command="sync",
        source_id=args.source_id,
        seed=args.seed,
        db=str(args.db),
        status=run_log.status.value,
        fetched=run_log.fetched_count,
        new=run_log.new_articles_count,
        updated=run_log.updated_articles_count,
        errors=run_log.error_count,
        note=run_log.error_message,
    )
    return 0 if run_log.status == RunStatus.COMPLETED else 1


def build_parser() -> argparse.ArgumentParser:
    """Create argument parser for the author-collector CLI."""
    parser = argparse.ArgumentParser(
        prog="author-collector",
        description="Compliance-first author indexing pipeline (v0 baseline)",
    )
    parser.add_argument("--version", action="version", version="author-collector 0.1.0")

    subparsers = parser.add_subparsers(dest="command")

    validate_parser = subparsers.add_parser(
        "validate-schemas",
        help="Validate JSON schemas used by contract tests",
    )
    validate_parser.set_defaults(func=_cmd_validate_schemas)

    export_parser = subparsers.add_parser(
        "export",
        help="Write JSONL export from SQLite with schema validation",
    )
    export_parser.add_argument("--output", required=True, help="Output JSONL path")
    export_parser.add_argument("--db", default="collector.db", help="SQLite DB path")
    export_parser.add_argument("--run-id", help="Optional explicit run ID for logging")
    export_parser.set_defaults(func=_cmd_export)

    rollback_parser = subparsers.add_parser(
        "rollback",
        help="Rollback persisted artifacts for a run_id",
    )
    rollback_parser.add_argument("--run", required=True, help="Run ID to rollback")
    rollback_parser.add_argument("--db", default="collector.db", help="SQLite DB path")
    rollback_parser.set_defaults(func=_cmd_rollback)

    review_queue_parser = subparsers.add_parser(
        "review-queue",
        help="Generate merge-candidate review queue JSON",
    )
    review_queue_parser.add_argument("--output", default="review.json", help="Output review JSON path")
    review_queue_parser.add_argument("--db", default="collector.db", help="SQLite DB path")
    review_queue_parser.add_argument("--run-id", help="Optional explicit run ID for logging")
    review_queue_parser.add_argument(
        "--min-score",
        type=float,
        default=0.6,
        help="Minimum candidate score included in review queue",
    )
    review_queue_parser.set_defaults(func=_cmd_review_queue)

    review_parser = subparsers.add_parser(
        "review",
        help="Manual review operations",
    )
    review_subparsers = review_parser.add_subparsers(dest="review_command")
    review_subparsers.required = True

    review_apply_parser = review_subparsers.add_parser(
        "apply",
        help="Apply decisions from a review queue JSON file",
    )
    review_apply_parser.add_argument("review_file", help="Path to review JSON file")
    review_apply_parser.add_argument("--db", default="collector.db", help="SQLite DB path")
    review_apply_parser.add_argument("--run-id", help="Optional explicit run ID for this apply run")
    review_apply_parser.add_argument(
        "--created-by",
        default="manual-review",
        help="Human/operator identifier written to merge_decisions.created_by",
    )
    review_apply_parser.set_defaults(func=_cmd_review_apply)

    sync_parser = subparsers.add_parser(
        "sync",
        help="Run sync pipeline for a connector source",
    )
    sync_parser.add_argument("--source-id", required=True, help="Source ID, e.g. rss:example_feed")
    sync_parser.add_argument("--seed", required=True, help="Seed input (URL or local file path)")
    sync_parser.add_argument("--db", default="collector.db", help="SQLite DB path")
    sync_parser.add_argument("--run-id", help="Optional explicit run ID")
    sync_parser.add_argument("--dry-run", action="store_true", help="Discover/fetch/parse/extract only")
    sync_parser.set_defaults(func=_cmd_sync)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Execute CLI and return process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if not hasattr(args, "func"):
        parser.print_help()
        return 0

    try:
        return int(args.func(args))
    except Exception as exc:
        run_id = _resolve_command_run_id(args)
        _emit_cli_event(
            "cli_error",
            run_id=run_id,
            command=str(getattr(args, "command", "unknown")),
            error_type=type(exc).__name__,
            error=str(exc),
        )
        return 1


def cli() -> None:
    """Console-script entrypoint."""
    raise SystemExit(main())


if __name__ == "__main__":
    raise SystemExit(main())
