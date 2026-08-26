"""Bridge-owned orchestration over the small executor contract."""

from __future__ import annotations

from collections.abc import Mapping
import uuid
from typing import Any

from .domain.models import AuditStatus, ExecutionStatus, Project, Task
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

    async def run_task(self, task_id: str) -> Task:
        task = self.store.get_task(task_id)
        if task is None:
            raise KeyError(f"task does not exist: {task_id}")
        project = self.store.get_project(task.project_id)
        if project is None:
            raise RuntimeError(f"project does not exist: {task.project_id}")

        self.store.update_task_runtime(
            task_id,
            execution_status=ExecutionStatus.RUNNING,
            audit_status=AuditStatus.PENDING,
        )
        self.store.append_task_event(
            task_id,
            "bridge",
            "task.started",
            {"project_id": project.project_id},
        )

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
            correlation_updates["execution_status"] = ExecutionStatus.FINISHED
            correlation_updates["audit_status"] = AuditStatus.PENDING
            self.store.update_task_runtime(task_id, **correlation_updates)
            self.store.append_task_event(
                task_id,
                "bridge",
                "task.finished",
                {
                    "thread_id": result.thread_id,
                    "turn_id": result.turn_id,
                    "status": ExecutionStatus.FINISHED.value,
                    "final_response": result.final_response,
                },
            )
            finished = self.store.get_task(task_id)
            if finished is None:
                raise RuntimeError("task disappeared after completion")
            return finished
        except Exception as error:
            try:
                self.store.update_task_runtime(
                    task_id,
                    execution_status=ExecutionStatus.FAILED,
                    audit_status=AuditStatus.PENDING,
                )
                self.store.append_task_event(
                    task_id,
                    "bridge",
                    "task.failed",
                    {
                        "error_type": type(error).__name__,
                        "message": _bounded_error_message(error),
                    },
                )
            except Exception as persistence_error:
                raise persistence_error from error
            raise


__all__ = ["BridgeCore"]
