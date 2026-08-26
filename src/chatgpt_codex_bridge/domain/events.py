"""Durable Bridge-owned task event model."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .models import ensure_utc, utc_now


def _required_text(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be non-empty")
    return value


@dataclass
class TaskEvent:
    """An append-only observable event owned by the Bridge."""

    event_id: int | None
    task_id: str
    source: str
    kind: str
    payload: Any
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if self.event_id is not None:
            if (
                isinstance(self.event_id, bool)
                or not isinstance(self.event_id, int)
                or self.event_id <= 0
            ):
                raise ValueError("event_id must be a positive integer or None")
        self.task_id = _required_text(self.task_id, "task_id")
        self.source = _required_text(self.source, "source")
        self.kind = _required_text(self.kind, "kind")
        self.created_at = ensure_utc(self.created_at, "created_at")


__all__ = ["TaskEvent"]
