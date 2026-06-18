from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import UUID

from engine.events.types import BaseEvent


class EventAuditLogger:
    """
    Append-only NDJSON event logger.

    Register `on_any` against BaseEvent after all business handlers so the
    audit trail records every event that was published during an engine run.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @classmethod
    def for_run(cls, log_dir: str | Path, run_id: str) -> "EventAuditLogger":
        if not run_id:
            raise ValueError("run_id is required for event audit logging")
        return cls(Path(log_dir) / "events" / f"{run_id}.ndjson")

    def on_any(self, event: BaseEvent) -> None:
        record = _event_to_record(event)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")


def _event_to_record(event: BaseEvent) -> dict[str, Any]:
    record = _jsonable(event)
    if not isinstance(record, dict):
        raise TypeError(f"event serialization returned {type(record).__name__}")
    record["event_type"] = type(event).__name__
    record["schema_version"] = event.schema_version
    return record


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseEvent):
        return {
            key: _jsonable(item)
            for key, item in asdict(value).items()
            if not key.startswith("_")
        }
    if is_dataclass(value):
        return {
            key: _jsonable(item)
            for key, item in asdict(value).items()
            if not key.startswith("_")
        }
    if isinstance(value, dict):
        return {
            str(key): _jsonable(item)
            for key, item in value.items()
            if not str(key).startswith("_")
        }
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value
