"""Integration tests for M5 manual review queue + apply flow."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

import pytest

from author_collector.cli import main as cli_main
from core.evidence import create_evidence
from core.models import ArticleDraft, EvidenceType, RunLog
from resolution.scoring import normalized_levenshtein_distance
from storage.sqlite import SQLiteRunStore


def _create_run(store: SQLiteRunStore, run_id: str, source_id: str) -> None:
    """Insert run row for FK requirements."""
    store.create_run_log(RunLog(id=run_id, source_id=source_id))


def _json_lines(stdout: str) -> list[dict]:
    """Parse JSON log lines emitted by CLI commands."""
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    return [json.loads(line) for line in lines]


def _insert_article(
    store: SQLiteRunStore,
    *,
    run_id: str,
    source_id: str,
    canonical_url: str,
    title: str,
    author_hint: str,
) -> None:
    """Insert one article + evidence row through storage upsert."""
    draft = ArticleDraft(
        canonical_url=canonical_url,
        source_id=source_id,
        title=title,
        author_hint=author_hint,
        published_at=datetime(2026, 2, 27, 10, 0, tzinfo=UTC),
        snippet=f"{title} snippet",
    )
    evidence = create_evidence(
        article_id="draft",
        claim_path="/author_hint",
        evidence_type=EvidenceType.META_TAG,
        source_url=canonical_url,
        extracted_text=author_hint,
        run_id=run_id,
        extraction_method="meta.author",
    )
    store.upsert_article(draft, [evidence], run_id)


@pytest.mark.integration
def test_review_queue_includes_same_name_same_domain_candidates(tmp_path, capsys):
    """Same-name same-domain authors from different sources should appear in review queue."""
    db_path = tmp_path / "collector.db"
    review_path = tmp_path / "review.json"
    store = SQLiteRunStore(db_path)

    _create_run(store, "run-seed-rss", "rss:feed-a")
    _create_run(store, "run-seed-html", "html:author-a")
    _insert_article(
        store,
        run_id="run-seed-rss",
        source_id="rss:feed-a",
        canonical_url="https://techblog.com/posts/1",
        title="Post 1",
        author_hint="Jane Doe",
    )
    _insert_article(
        store,
        run_id="run-seed-html",
        source_id="html:author-a",
        canonical_url="https://techblog.com/posts/2",
        title="Post 2",
        author_hint="Jane Doe",
    )

    exit_code = cli_main(
        [
            "review-queue",
            "--db",
            str(db_path),
            "--output",
            str(review_path),
            "--min-score",
            "0.6",
        ]
    )
    assert exit_code == 0
    queue_events = _json_lines(capsys.readouterr().out)
    assert queue_events[-1]["event_type"] == "cli_review_queue_completed"
    assert queue_events[-1]["candidate_count"] >= 1
    assert queue_events[-1]["run_id"] is not None

    payload = json.loads(review_path.read_text(encoding="utf-8"))
    candidates = payload["candidates"]
    assert candidates, "Expected at least one review candidate"
    assert any(
        item["score"] >= 0.75
        and item["from_author"]["canonical_name"] == "Jane Doe"
        and item["to_author"]["canonical_name"] == "Jane Doe"
        for item in candidates
    )
    assert all(item["decision"] is None for item in candidates)

    # v0 manual-only rule: generating queue must not create merge decisions.
    connection = sqlite3.connect(db_path)
    merge_count = connection.execute("SELECT COUNT(*) FROM merge_decisions").fetchone()[0]
    connection.close()
    assert merge_count == 0


@pytest.mark.integration
def test_review_apply_is_replayable_and_rollbackable(tmp_path, capsys):
    """Accept decisions create merge_decisions; replay is idempotent; rollback removes run changes."""
    db_path = tmp_path / "collector.db"
    review_path = tmp_path / "review.json"
    store = SQLiteRunStore(db_path)

    _create_run(store, "run-seed-rss", "rss:feed-a")
    _create_run(store, "run-seed-html", "html:author-a")
    _insert_article(
        store,
        run_id="run-seed-rss",
        source_id="rss:feed-a",
        canonical_url="https://techblog.com/posts/1",
        title="Post 1",
        author_hint="Jane Doe",
    )
    _insert_article(
        store,
        run_id="run-seed-html",
        source_id="html:author-a",
        canonical_url="https://techblog.com/posts/2",
        title="Post 2",
        author_hint="Jane Doe",
    )

    assert (
        cli_main(
            [
                "review-queue",
                "--db",
                str(db_path),
                "--output",
                str(review_path),
            ]
        )
        == 0
    )
    queue_events = _json_lines(capsys.readouterr().out)
    assert queue_events[-1]["event_type"] == "cli_review_queue_completed"

    payload = json.loads(review_path.read_text(encoding="utf-8"))
    assert payload["candidates"], "Expected at least one candidate for apply flow"
    payload["candidates"][0]["decision"] = "accept"
    review_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    # First apply writes one merge_decisions row.
    exit_apply_1 = cli_main(
        [
            "review",
            "apply",
            str(review_path),
            "--db",
            str(db_path),
            "--run-id",
            "run-review-1",
            "--created-by",
            "tester",
        ]
    )
    assert exit_apply_1 == 0
    apply_1_events = _json_lines(capsys.readouterr().out)
    assert apply_1_events[-1]["event_type"] == "cli_review_apply_completed"
    assert apply_1_events[-1]["run_id"] == "run-review-1"
    assert apply_1_events[-1]["accepted"] == 1

    # Replay same file with another run_id should not duplicate (idempotent by decision id).
    exit_apply_2 = cli_main(
        [
            "review",
            "apply",
            str(review_path),
            "--db",
            str(db_path),
            "--run-id",
            "run-review-2",
            "--created-by",
            "tester",
        ]
    )
    assert exit_apply_2 == 0
    apply_2_events = _json_lines(capsys.readouterr().out)
    assert apply_2_events[-1]["event_type"] == "cli_review_apply_completed"
    assert apply_2_events[-1]["run_id"] == "run-review-2"
    assert apply_2_events[-1]["duplicates"] == 1

    connection = sqlite3.connect(db_path)
    merge_count_before_rollback = connection.execute(
        "SELECT COUNT(*) FROM merge_decisions"
    ).fetchone()[0]
    merge_run_ids = connection.execute(
        "SELECT DISTINCT run_id FROM merge_decisions ORDER BY run_id"
    ).fetchall()
    connection.close()
    assert merge_count_before_rollback == 1
    assert merge_run_ids == [("run-review-1",)]

    # Merge decisions are rollback-friendly via run_id.
    exit_rollback = cli_main(["rollback", "--run", "run-review-1", "--db", str(db_path)])
    assert exit_rollback == 0
    rollback_events = _json_lines(capsys.readouterr().out)
    assert rollback_events[-1]["event_type"] == "cli_rollback_completed"
    assert rollback_events[-1]["run_id"] == "run-review-1"

    connection = sqlite3.connect(db_path)
    merge_count_after_rollback = connection.execute(
        "SELECT COUNT(*) FROM merge_decisions"
    ).fetchone()[0]
    rollback_status = connection.execute(
        "SELECT status FROM run_log WHERE id = ?",
        ("run-review-1",),
    ).fetchone()[0]
    connection.close()

    assert merge_count_after_rollback == 0
    assert rollback_status == "CANCELLED"


@pytest.mark.integration
def test_normalized_levenshtein_examples_match_roadmap():
    """Scoring helper should match documented normalized Levenshtein examples."""
    assert normalized_levenshtein_distance("Jane Doe", "Jane Do") == pytest.approx(0.125)
    assert normalized_levenshtein_distance("Jane Doe", "John Smith") > 0.15
