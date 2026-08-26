"""Small domain models for Bridge-owned project and task state."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import TypeVar


class ExecutionStatus(str, Enum):
    """Execution lifecycle states approved for the 1C model."""

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING_USER = "WAITING_USER"
    FINISHED = "FINISHED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class AuditStatus(str, Enum):
    """Independent review state for a Bridge task."""

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    CORRECTION_REQUIRED = "CORRECTION_REQUIRED"


class TaskMode(str, Enum):
    """Execution policy selected for one Bridge task."""

    READ_ONLY = "READ_ONLY"
    AUTONOMOUS_WRITE = "AUTONOMOUS_WRITE"


class TaskStateError(RuntimeError):
    """Raised when a task lifecycle transition is not allowed."""


def utc_now() -> datetime:
    """Return an aware UTC timestamp."""

    return datetime.now(timezone.utc)


def ensure_utc(value: datetime, field_name: str = "timestamp") -> datetime:
    """Require an aware timestamp and normalize it to UTC."""

    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def timestamp_to_text(value: datetime) -> str:
    """Serialize an aware timestamp as stable UTC ISO 8601 text."""

    return ensure_utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def timestamp_from_text(value: str) -> datetime:
    """Parse a stored UTC ISO 8601 timestamp."""

    if not isinstance(value, str) or not value:
        raise ValueError("stored timestamp must be non-empty text")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError("stored timestamp is not valid ISO 8601") from exc
    return ensure_utc(parsed, "stored timestamp")


def _required_text(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be non-empty")
    return value


def _optional_text(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, field_name)


EnumT = TypeVar("EnumT", bound=Enum)


def _coerce_enum(value: EnumT | str, enum_type: type[EnumT], field_name: str) -> EnumT:
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid {field_name}: {value!r}") from exc


@dataclass
class Project:
    """A Bridge-owned project boundary."""

    project_id: str
    name: str
    repo_path: str
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        self.project_id = _required_text(self.project_id, "project_id")
        self.name = _required_text(self.name, "name")
        self.repo_path = _required_text(self.repo_path, "repo_path")
        self.created_at = ensure_utc(self.created_at, "created_at")
        self.updated_at = ensure_utc(self.updated_at, "updated_at")


@dataclass
class Task:
    """A Bridge task, distinct from Codex thread and turn identities."""

    task_id: str
    project_id: str
    objective: str
    executor: str = "codex"
    model: str = "gpt-5.6-luna"
    mode: TaskMode = field(default=TaskMode.READ_ONLY, kw_only=True)
    execution_status: ExecutionStatus = ExecutionStatus.QUEUED
    audit_status: AuditStatus = AuditStatus.PENDING
    thread_id: str | None = None
    turn_id: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        self.task_id = _required_text(self.task_id, "task_id")
        self.project_id = _required_text(self.project_id, "project_id")
        self.objective = _required_text(self.objective, "objective")
        self.executor = _required_text(self.executor, "executor")
        self.model = _required_text(self.model, "model")
        self.mode = _coerce_enum(self.mode, TaskMode, "mode")
        self.execution_status = _coerce_enum(
            self.execution_status, ExecutionStatus, "execution_status"
        )
        self.audit_status = _coerce_enum(self.audit_status, AuditStatus, "audit_status")
        self.thread_id = _optional_text(self.thread_id, "thread_id")
        self.turn_id = _optional_text(self.turn_id, "turn_id")
        self.created_at = ensure_utc(self.created_at, "created_at")
        self.updated_at = ensure_utc(self.updated_at, "updated_at")


__all__ = [
    "AuditStatus",
    "ExecutionStatus",
    "Project",
    "TaskMode",
    "TaskStateError",
    "Task",
    "ensure_utc",
    "timestamp_from_text",
    "timestamp_to_text",
    "utc_now",
]
