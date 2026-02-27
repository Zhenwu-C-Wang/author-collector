#!/usr/bin/env python3
"""Run daily canary sync/export/review checks against real public sources."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass
class CommandResult:
    """Captured result for one CLI command."""

    name: str
    run_id: str
    returncode: int
    argv: list[str]
    stdout: str
    stderr: str
    events: list[dict[str, Any]]


def _utc_stamp() -> str:
    """Return UTC timestamp suitable for run IDs and artifact names."""
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _parse_json_lines(text: str) -> list[dict[str, Any]]:
    """Parse newline-delimited JSON payloads from command stdout."""
    events: list[dict[str, Any]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            events.append(payload)
    return events


def _run_command(
    *,
    name: str,
    run_id: str,
    argv: list[str],
    logs_dir: Path,
) -> CommandResult:
    """Execute one command and persist stdout/stderr logs."""
    process = subprocess.run(
        argv,
        check=False,
        text=True,
        capture_output=True,
    )
    events = _parse_json_lines(process.stdout)

    prefix = f"{name}_{run_id}"
    (logs_dir / f"{prefix}.stdout.log").write_text(process.stdout, encoding="utf-8")
    (logs_dir / f"{prefix}.stderr.log").write_text(process.stderr, encoding="utf-8")
    (logs_dir / f"{prefix}.events.jsonl").write_text(
        "".join(json.dumps(event, ensure_ascii=True, sort_keys=True) + "\n" for event in events),
        encoding="utf-8",
    )

    return CommandResult(
        name=name,
        run_id=run_id,
        returncode=process.returncode,
        argv=argv,
        stdout=process.stdout,
        stderr=process.stderr,
        events=events,
    )


def _emit(level: str, message: str) -> None:
    """Emit human and GitHub Actions friendly diagnostic lines."""
    if level == "error":
        print(f"ERROR: {message}")
        if "GITHUB_ACTIONS" in os.environ:
            print(f"::error::{message}")
        return
    if level == "warning":
        print(f"WARNING: {message}")
        if "GITHUB_ACTIONS" in os.environ:
            print(f"::warning::{message}")
        return
    print(message)


def _summary_event(result: CommandResult, event_type: str) -> dict[str, Any] | None:
    """Return the last event of a given type from command results."""
    for event in reversed(result.events):
        if event.get("event_type") == event_type:
            return event
    return None


def _count_events(
    events: list[dict[str, Any]],
    *,
    predicate,
) -> int:
    """Count events satisfying predicate."""
    return sum(1 for item in events if predicate(item))


def _load_sources(path: Path) -> list[dict[str, str]]:
    """Load and validate canary source definitions."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("canary source file must be a JSON list")

    sources: list[dict[str, str]] = []
    for idx, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"source index {idx} must be an object")
        name = str(item.get("name") or "").strip()
        source_id = str(item.get("source_id") or "").strip()
        seed = str(item.get("seed") or "").strip()
        if not name or not source_id or not seed:
            raise ValueError(f"source index {idx} missing required fields: name/source_id/seed")
        sources.append({"name": name, "source_id": source_id, "seed": seed})
    return sources


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description="Run daily canary checks with real data sources.")
    parser.add_argument(
        "--sources-file",
        default="canary/sources.json",
        help="JSON file containing real source definitions",
    )
    parser.add_argument(
        "--workspace",
        default="artifacts/canary",
        help="Output workspace for logs and reports",
    )
    parser.add_argument(
        "--db-path",
        help="Optional sqlite DB path (defaults to <workspace>/canary.db)",
    )
    parser.add_argument(
        "--error-rate-fail-threshold",
        type=float,
        default=0.2,
        help="Fail canary when aggregate sync error rate exceeds this threshold",
    )
    parser.add_argument(
        "--blocked-by-robots-fail-threshold",
        type=int,
        default=20,
        help="Fail canary when BLOCKED_BY_ROBOTS count exceeds this threshold",
    )
    parser.add_argument(
        "--skip-rollback-check",
        action="store_true",
        help="Skip rollback functional check step",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run canary flow and emit report/alerts."""
    args = parse_args(argv)
    stamp = _utc_stamp()

    workspace = Path(args.workspace)
    logs_dir = workspace / "logs"
    workspace.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)

    db_path = Path(args.db_path) if args.db_path else workspace / "canary.db"
    export_path = workspace / f"canary_export_{stamp}.jsonl"
    review_path = workspace / f"canary_review_{stamp}.json"
    report_path = workspace / f"canary_report_{stamp}.json"

    sources = _load_sources(Path(args.sources_file))

    all_results: list[CommandResult] = []
    sync_summaries: list[dict[str, Any]] = []

    for source in sources:
        run_id = f"canary-sync-{source['name']}-{stamp}"
        argv_sync = [
            sys.executable,
            "-m",
            "author_collector.cli",
            "sync",
            "--source-id",
            source["source_id"],
            "--seed",
            source["seed"],
            "--db",
            str(db_path),
            "--run-id",
            run_id,
        ]
        result = _run_command(name=source["name"], run_id=run_id, argv=argv_sync, logs_dir=logs_dir)
        all_results.append(result)

        summary = _summary_event(result, "cli_sync_completed")
        if summary:
            sync_summaries.append(summary)

    export_run_id = f"canary-export-{stamp}"
    export_result = _run_command(
        name="export",
        run_id=export_run_id,
        argv=[
            sys.executable,
            "-m",
            "author_collector.cli",
            "export",
            "--output",
            str(export_path),
            "--db",
            str(db_path),
            "--run-id",
            export_run_id,
        ],
        logs_dir=logs_dir,
    )
    all_results.append(export_result)

    review_run_id = f"canary-review-queue-{stamp}"
    review_result = _run_command(
        name="review_queue",
        run_id=review_run_id,
        argv=[
            sys.executable,
            "-m",
            "author_collector.cli",
            "review-queue",
            "--output",
            str(review_path),
            "--db",
            str(db_path),
            "--run-id",
            review_run_id,
        ],
        logs_dir=logs_dir,
    )
    all_results.append(review_result)

    rollback_checked = False
    rollback_ok = None
    rollback_target_run_id = None
    if not args.skip_rollback_check and sync_summaries:
        rollback_checked = True
        rollback_target_run_id = str(sync_summaries[0].get("run_id"))
        rollback_result = _run_command(
            name="rollback_check",
            run_id=rollback_target_run_id,
            argv=[
                sys.executable,
                "-m",
                "author_collector.cli",
                "rollback",
                "--run",
                rollback_target_run_id,
                "--db",
                str(db_path),
            ],
            logs_dir=logs_dir,
        )
        all_results.append(rollback_result)
        rollback_ok = rollback_result.returncode == 0 and (
            _summary_event(rollback_result, "cli_rollback_completed") is not None
        )

    all_events = [event for result in all_results for event in result.events]
    command_failures = [result for result in all_results if result.returncode != 0]

    total_fetched = sum(int(item.get("fetched") or 0) for item in sync_summaries)
    total_errors = sum(int(item.get("errors") or 0) for item in sync_summaries)
    error_rate = float(total_errors / total_fetched) if total_fetched > 0 else 0.0

    cli_error_count = _count_events(
        all_events,
        predicate=lambda event: event.get("event_type") == "cli_error",
    )
    pipeline_error_count = _count_events(
        all_events,
        predicate=lambda event: str(event.get("event_type", "")).startswith("pipeline_")
        and str(event.get("event_type", "")).endswith("_error"),
    )
    blocked_by_robots_count = _count_events(
        all_events,
        predicate=lambda event: event.get("error_code") == "BLOCKED_BY_ROBOTS",
    )
    missing_run_id_count = _count_events(
        all_events,
        predicate=lambda event: ("run_id" not in event) or (event.get("run_id") in (None, "")),
    )

    critical_alerts: list[str] = []
    warning_alerts: list[str] = []

    if command_failures:
        critical_alerts.append(
            "one or more canary commands failed: "
            + ", ".join(f"{item.name}({item.returncode})" for item in command_failures)
        )
    if cli_error_count > 0:
        critical_alerts.append(f"cli_error count={cli_error_count}")
    if pipeline_error_count > 0:
        critical_alerts.append(f"pipeline_*_error count={pipeline_error_count}")
    if missing_run_id_count > 0:
        critical_alerts.append(f"missing run_id events count={missing_run_id_count}")
    if error_rate > args.error_rate_fail_threshold:
        critical_alerts.append(
            f"error_rate={error_rate:.4f} exceeded threshold={args.error_rate_fail_threshold:.4f}"
        )
    if rollback_checked and rollback_ok is not True:
        critical_alerts.append("rollback check failed")
    if blocked_by_robots_count > args.blocked_by_robots_fail_threshold:
        critical_alerts.append(
            "BLOCKED_BY_ROBOTS count="
            f"{blocked_by_robots_count} exceeded threshold={args.blocked_by_robots_fail_threshold}"
        )
    elif blocked_by_robots_count > 0:
        warning_alerts.append(f"BLOCKED_BY_ROBOTS count={blocked_by_robots_count}")

    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "workspace": str(workspace),
        "db_path": str(db_path),
        "sources": sources,
        "sync_runs": sync_summaries,
        "metrics": {
            "total_fetched": total_fetched,
            "total_errors": total_errors,
            "error_rate": error_rate,
            "cli_error_count": cli_error_count,
            "pipeline_error_count": pipeline_error_count,
            "blocked_by_robots_count": blocked_by_robots_count,
            "missing_run_id_count": missing_run_id_count,
        },
        "rollback_check": {
            "checked": rollback_checked,
            "target_run_id": rollback_target_run_id,
            "ok": rollback_ok,
        },
        "thresholds": {
            "error_rate_fail_threshold": args.error_rate_fail_threshold,
            "blocked_by_robots_fail_threshold": args.blocked_by_robots_fail_threshold,
        },
        "alerts": {
            "critical": critical_alerts,
            "warning": warning_alerts,
        },
        "commands": [
            {
                "name": result.name,
                "run_id": result.run_id,
                "returncode": result.returncode,
                "argv": result.argv,
            }
            for result in all_results
        ],
    }
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    for warning in warning_alerts:
        _emit("warning", warning)
    for critical in critical_alerts:
        _emit("error", critical)

    print(json.dumps({"event_type": "canary_report", "report_path": str(report_path)}, ensure_ascii=True))
    return 0 if not critical_alerts else 2


if __name__ == "__main__":
    raise SystemExit(main())
