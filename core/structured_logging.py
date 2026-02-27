"""Shared structured JSON logging helpers."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any


def emit_json_event(
    event_type: str,
    *,
    run_id: str | None,
    level: str = "info",
    **payload: Any,
) -> str:
    """Emit one JSON event line to stdout and return the rendered line."""
    event: dict[str, Any] = {
        "event_type": event_type,
        "level": level,
        "timestamp": datetime.now(UTC).isoformat(),
        "run_id": run_id,
    }
    event.update(payload)
    line = json.dumps(event, ensure_ascii=True, sort_keys=True, default=str)
    print(line)
    return line
