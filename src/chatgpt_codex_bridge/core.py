"""Bridge-owned orchestration over the small executor contract."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import inspect
import uuid
from typing import Any

from .domain.models import AuditStatus, ExecutionStatus, Project, Task, TaskStateError
from .executors.base import ExecutionRequest, ExecutionResult, Executor
from .persistence.sqlite_store import SQLiteBridgeStore


_MAX_NOTIFICATION_DEPTH = 4
_MAX_NOTIFICATION_ITEMS = 64
_MAX_NOTIFICATION_TEXT = 4096
_SENSITIVE_MARKERS = (
    "auth",
    "cookie",
    "credential",
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "codex_home",
)


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower().replace("-", "_")
    return any(marker in lowered for marker in _SENSITIVE_MARKERS)


def _bounded_notification_value(value: Any, depth: int = 0) -> Any:
    """Keep notification evidence JSON-safe, bounded, and free of secrets."""

    if depth >= _MAX_NOTIFICATION_DEPTH:
        return "[TRUNCATED]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(value) <= _MAX_NOTIFICATION_TEXT:
            return value
        marker = "[TRUNCATED]"
        return value[: _MAX_NOTIFICATION_TEXT - len(marker)] + marker
    if isinstance(value, Mapping):
        bounded: dict[str, Any] = {}
        for index, (key, child) in enumerate(value.items()):
            if index >= _MAX_NOTIFICATION_ITEMS:
                bounded["_truncated"] = True
                break
            text_key = str(key)
            if _is_sensitive_key(text_key):
                continue
            bounded[text_key] = _bounded_notification_value(child, depth + 1)
        return bounded
    if isinstance(value, (list, tuple)):
        items = [
            _bounded_notification_value(item, depth + 1)
            for item in value[:_MAX_NOTIFICATION_ITEMS]
        ]
        if len(value) > _MAX_NOTIFICATION_ITEMS:
            items.append("[TRUNCATED]")
        return items
    return f"[{type(value).__name__}]"


def _bounded_error_message(error: Exception) -> str:
    return str(_bounded_notification_value(str(error)))


class BridgeCore:
    """Create Bridge entities and run tasks through an injected executor."""

    def __init__(self, store: SQLiteBridgeStore, executor: Executor) -> None:
        self.store = store
        self.executor = executor

    def create_project(
        self,
        name: str,
        repo_path: str,
        *,
        project_id: str | None = None,
    ) -> Project:
        project = Project(
            project_id=project_id or f"project-{uuid.uuid4().hex}",
            name=name,
            repo_path=repo_path,
        )
        return self.store.create_project(project)

    def create_task(
        self,
        project_id: str,
        objective: str,
        model: str = "gpt-5.6-luna",
        *,
        task_id: str | None = None,
        executor: str = "codex",
    ) -> Task:
        project = self.store.get_project(project_id)
        if project is None:
            raise KeyError(f"project does not exist: {project_id}")
        task = Task(
            task_id=task_id or f"task-{uuid.uuid4().hex}",
            project_id=project.project_id,
            objective=objective,
            executor=executor,
            model=model,
            execution_status=ExecutionStatus.QUEUED,
            audit_status=AuditStatus.PENDING,
        )
        created = self.store.create_task(task)
        self.store.append_task_event(
            task.task_id,
            "bridge",
            "task.created",
            {
                "project_id": task.project_id,
                "objective": task.objective,
                "model": task.model,
                "executor": task.executor,
            },
        )
        return created

    def recover_orphaned_tasks(self) -> list[Task]:
        """Fail closed for RUNNING tasks left by an earlier Bridge process."""

        recovered: list[Task] = []
        for task in self.store.list_tasks_by_execution_status(ExecutionStatus.RUNNING):
            recovered.append(
                self.store.transition_task_terminal(
                    task.task_id,
                    execution_status=ExecutionStatus.FAILED,
                    event_kind="task.failed",
                    payload={
                        "error_type": "OrphanedTaskRecovery",
                        "message": "task recovered after an interrupted Bridge execution",
                        "recovered_from": ExecutionStatus.RUNNING.value,
                    },
                    recovery_payload={
                        "previous_status": ExecutionStatus.RUNNING.value,
                        "reason": "bridge_startup_recovery",
                    },
                )
            )
        return recovered

    async def _cancel_active_execution(self) -> None:
        cancel_active = getattr(self.executor, "cancel_active", None)
        if not callable(cancel_active):
            return
        result = cancel_active()
        if inspect.isawaitable(result):
            await result

    async def run_task(self, task_id: str) -> Task:
        task = self.store.get_task(task_id)
        if task is None:
            raise KeyError(f"task does not exist: {task_id}")
        if task.execution_status is not ExecutionStatus.QUEUED:
            raise TaskStateError(
                f"task {task_id} cannot run from state "
                f"{task.execution_status.value}; only QUEUED tasks may run"
            )
        project = self.store.get_project(task.project_id)
        if project is None:
            raise RuntimeError(f"project does not exist: {task.project_id}")

        self.store.transition_task_running(task_id, project_id=project.project_id)

        def on_correlation(thread_id: str | None, turn_id: str | None) -> None:
            updates: dict[str, Any] = {}
            if thread_id is not None:
                updates["thread_id"] = thread_id
            if turn_id is not None:
                updates["turn_id"] = turn_id
            if updates:
                self.store.update_task_runtime(task_id, **updates)

        def on_notification(method: str, params: dict[str, Any]) -> None:
            if not isinstance(method, str) or not method.strip():
                raise ValueError("Codex notification method must be non-empty")
            self.store.append_task_event(
                task_id,
                "codex",
                method,
                _bounded_notification_value(params),
            )

        request = ExecutionRequest(
            task_id=task.task_id,
            cwd=project.repo_path,
            objective=task.objective,
            model=task.model,
        )
        try:
            result = await self.executor.run(
                request,
                on_correlation=on_correlation,
                on_notification=on_notification,
            )
            if result.status != ExecutionStatus.FINISHED:
                raise RuntimeError(
                    f"executor returned non-finished status: {result.status!r}"
                )
            correlation_updates: dict[str, Any] = {}
            if result.thread_id is not None:
                correlation_updates["thread_id"] = result.thread_id
            if result.turn_id is not None:
                correlation_updates["turn_id"] = result.turn_id
            if correlation_updates:
                self.store.update_task_runtime(task_id, **correlation_updates)
            finished = self.store.transition_task_terminal(
                task_id,
                execution_status=ExecutionStatus.FINISHED,
                event_kind="task.finished",
                payload={
                    "thread_id": result.thread_id,
                    "turn_id": result.turn_id,
                    "status": ExecutionStatus.FINISHED.value,
                    "final_response": result.final_response,
                },
            )
            return finished
        except asyncio.CancelledError:
            try:
                await self._cancel_active_execution()
            except BaseException:
                # Preserve cancellation semantics; terminal persistence below
                # is the Bridge's fail-closed obligation.
                pass
            self.store.transition_task_terminal(
                task_id,
                execution_status=ExecutionStatus.CANCELLED,
                event_kind="task.cancelled",
                payload={"reason": "execution handler cancelled"},
            )
            raise
        except Exception as error:
            self.store.transition_task_terminal(
                task_id,
                execution_status=ExecutionStatus.FAILED,
                event_kind="task.failed",
                payload={
                    "error_type": type(error).__name__,
                    "message": _bounded_error_message(error),
                },
            )
            raise


__all__ = ["BridgeCore", "TaskStateError"]
