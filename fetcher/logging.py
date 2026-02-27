"""Structured logging helpers for fetch operations."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from core.models import FetchLog


def _isoformat(value: datetime | None) -> str | None:
    """Serialize datetimes for logs."""
    if value is None:
        return None
    return value.isoformat()


def fetch_log_to_dict(fetch_log: FetchLog) -> dict[str, Any]:
    """Convert FetchLog to a JSON-safe dictionary."""
    return {
        "id": fetch_log.id,
        "url": fetch_log.url,
        "status_code": fetch_log.status_code,
        "latency_ms": fetch_log.latency_ms,
        "bytes_received": fetch_log.bytes_received,
        "error_code": fetch_log.error_code.value if fetch_log.error_code else None,
        "timestamp": _isoformat(fetch_log.created_at),
        "run_id": fetch_log.run_id,
    }


def emit_event(event_type: str, **payload: Any) -> str:
    """Emit a structured event log line and return it for testability."""
    event = {
        "event_type": event_type,
        "timestamp": datetime.now(UTC).isoformat(),
    }
    event.update(payload)
    line = json.dumps(event, ensure_ascii=True, sort_keys=True, default=str)
    print(line)
    return line


def emit_fetch_log(fetch_log: FetchLog) -> str:
    """Emit a structured JSON log line and return it for testability."""
    line = json.dumps(fetch_log_to_dict(fetch_log), ensure_ascii=True, sort_keys=True)
    print(line)
    return line
