"""Codex-independent execution contract used by Bridge Core."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, runtime_checkable

from ..domain.models import ExecutionStatus, TaskMode


CorrelationCallback = Callable[[str | None, str | None], None]
NotificationCallback = Callable[[str, dict[str, Any]], None]


def _required_text(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be non-empty")
    return value


@dataclass(frozen=True)
class ExecutionRequest:
    """Small input boundary shared by Core and any executor implementation."""

    task_id: str
    cwd: str
    objective: str
    model: str
    mode: TaskMode = field(default=TaskMode.READ_ONLY, kw_only=True)

    def __post_init__(self) -> None:
        object.__setattr__(self, "task_id", _required_text(self.task_id, "task_id"))
        object.__setattr__(self, "cwd", _required_text(self.cwd, "cwd"))
        object.__setattr__(self, "objective", _required_text(self.objective, "objective"))
        object.__setattr__(self, "model", _required_text(self.model, "model"))
        try:
            selected_mode = (
                self.mode if isinstance(self.mode, TaskMode) else TaskMode(self.mode)
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid mode: {self.mode!r}") from exc
        object.__setattr__(self, "mode", selected_mode)


@dataclass
class ExecutionResult:
    """Small output boundary returned by an executor after a run."""

    thread_id: str | None
    turn_id: str | None
    status: ExecutionStatus | str
    final_response: str | None

    def __post_init__(self) -> None:
        if self.thread_id is not None:
            self.thread_id = _required_text(self.thread_id, "thread_id")
        if self.turn_id is not None:
            self.turn_id = _required_text(self.turn_id, "turn_id")
        try:
            self.status = (
                self.status
                if isinstance(self.status, ExecutionStatus)
                else ExecutionStatus(self.status)
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid execution result status: {self.status!r}") from exc
        if self.final_response is not None and not isinstance(self.final_response, str):
            raise ValueError("final_response must be text or None")


@runtime_checkable
class Executor(Protocol):
    """Executor boundary; it intentionally contains no app-server concepts."""

    async def run(
        self,
        request: ExecutionRequest,
        *,
        on_correlation: CorrelationCallback | None = None,
        on_notification: NotificationCallback | None = None,
    ) -> ExecutionResult:
        """Execute one request and return its durable result."""


__all__ = [
    "CorrelationCallback",
    "ExecutionRequest",
    "ExecutionResult",
    "Executor",
    "NotificationCallback",
]
