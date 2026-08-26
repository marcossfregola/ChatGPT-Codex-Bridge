"""Bridge-owned orchestration over the small executor contract."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import inspect
import uuid
from typing import Any

from .domain.models import (
    AuditStatus,
    ExecutionStatus,
    Project,
    Task,
    TaskMode,
    TaskStateError,
)
from .executors.base import ExecutionRequest, ExecutionResult, Executor
from .policy import (
    ContinuationBaselineError,
    GitCheckpoint,
    GitPostflight,
    GitPostflightError,
    DirtyWorkingTreeError,
    PolicyError,
    PolicyViolationError,
    augment_objective,
    checkpoint_payload,
    ensure_autonomous_workspace,
    git_continuation_preflight,
    git_postflight,
    git_preflight,
    postflight_payload,
)
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
        mode: TaskMode | str = TaskMode.READ_ONLY,
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
            mode=mode,
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
                "mode": task.mode.value,
            },
        )
        return created

    def recover_orphaned_tasks(self) -> list[Task]:
        """Fail closed for RUNNING tasks left by an earlier Bridge process."""

        recovered: list[Task] = []
        for task in self.store.list_tasks_by_execution_status(ExecutionStatus.RUNNING):
            checkpoint = self._checkpoint_for_task(task.task_id)
            if checkpoint is not None:
                try:
                    self._persist_postflight(task.task_id, checkpoint)
                except BaseException:
                    # Recovery must remain deterministic even when the project
                    # was removed or Git evidence is no longer available.
                    pass
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

    def _checkpoint_for_task(self, task_id: str) -> GitCheckpoint | None:
        """Rehydrate the last autonomous checkpoint for orphan recovery."""

        events = self.store.list_task_events(task_id)
        for event in reversed(events):
            if event.source != "bridge" or event.kind != "policy.git_checkpoint":
                continue
            payload = event.payload
            if not isinstance(payload, dict):
                return None
            required = (
                payload.get("repo_path"),
                payload.get("baseline_branch"),
                payload.get("baseline_head"),
            )
            if not all(isinstance(value, str) and value for value in required):
                return None

            def paths(name: str) -> tuple[str, ...]:
                value = payload.get(name, [])
                if not isinstance(value, list):
                    return ()
                return tuple(item for item in value if isinstance(item, str))

            return GitCheckpoint(
                repo_path=required[0],
                baseline_branch=required[1],
                baseline_head=required[2],
                status_porcelain=(
                    payload.get("status_porcelain")
                    if isinstance(payload.get("status_porcelain"), str)
                    else ""
                ),
                staged_paths=paths("staged_paths"),
                unstaged_paths=paths("unstaged_paths"),
                untracked_paths=paths("untracked_paths"),
                baseline_kind=(
                    payload.get("baseline_kind")
                    if isinstance(payload.get("baseline_kind"), str)
                    else "clean"
                ),
                previous_task_id=(
                    payload.get("previous_task_id")
                    if isinstance(payload.get("previous_task_id"), str)
                    else None
                ),
            )
        return None

    def _previous_continuation_baseline(
        self, task: Task
    ) -> tuple[str, dict[str, Any]] | None:
        """Return the latest prior task only when it is an eligible baseline."""

        latest: tuple[int, Task, list[Any]] | None = None
        for candidate in self.store.list_tasks(task.project_id):
            if candidate.task_id == task.task_id:
                continue
            events = self.store.list_task_events(candidate.task_id)
            event_id = max((event.event_id or 0 for event in events), default=0)
            if latest is None or event_id > latest[0]:
                latest = (event_id, candidate, events)
        if latest is None:
            return None
        _, candidate, events = latest
        if (
            candidate.mode is not TaskMode.AUTONOMOUS_WRITE
            or candidate.execution_status is not ExecutionStatus.FINISHED
        ):
            return None
        for event in reversed(events):
            if event.source != "bridge" or event.kind != "policy.postflight":
                continue
            payload = event.payload
            if isinstance(payload, dict) and payload.get("policy_violation") is False:
                return candidate.task_id, payload
            return None
        return None

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

        checkpoint: GitCheckpoint | None = None
        if task.mode is TaskMode.AUTONOMOUS_WRITE:
            try:
                safe_repo = ensure_autonomous_workspace(project.repo_path)
                try:
                    checkpoint = git_preflight(safe_repo)
                except DirtyWorkingTreeError:
                    previous = self._previous_continuation_baseline(task)
                    if previous is None:
                        raise
                    previous_task_id, previous_postflight = previous
                    checkpoint = git_continuation_preflight(
                        safe_repo,
                        previous_task_id=previous_task_id,
                        previous_postflight=previous_postflight,
                    )
            except Exception as error:
                if not isinstance(error, PolicyError):
                    raise
                # A definitive policy/preflight rejection is a failed Task,
                # not a retryable queued Task.  Persist the violation and the
                # single terminal event atomically without invoking Codex.
                violation_payload: dict[str, Any] = {
                    "phase": "preflight",
                    "error_type": type(error).__name__,
                    "message": _bounded_error_message(error),
                    "policy_violation": True,
                }
                if isinstance(error, DirtyWorkingTreeError):
                    violation_payload.update(
                        {
                            "status_porcelain": error.status_porcelain,
                            "staged_paths": list(error.staged_paths),
                            "unstaged_paths": list(error.unstaged_paths),
                            "untracked_paths": list(error.untracked_paths),
                        }
                    )
                elif isinstance(error, ContinuationBaselineError):
                    violation_payload["baseline_kind"] = "continuation"
                self.store.transition_task_preflight_failed(
                    task_id,
                    policy_payload=violation_payload,
                    failed_payload={
                        "error_type": type(error).__name__,
                        "message": _bounded_error_message(error),
                    },
                )
                raise
            self.store.append_task_event(
                task_id,
                "bridge",
                "policy.git_checkpoint",
                {"mode": task.mode.value, **checkpoint_payload(checkpoint)},
            )

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

        postflight_recorded = False
        try:
            execution_cwd = (
                checkpoint.repo_path if checkpoint is not None else project.repo_path
            )
            request = ExecutionRequest(
                task_id=task.task_id,
                cwd=execution_cwd,
                objective=augment_objective(task.objective, task.mode),
                model=task.model,
                mode=task.mode,
            )
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
            if checkpoint is not None:
                try:
                    postflight = self._persist_postflight(task_id, checkpoint)
                except GitPostflightError:
                    postflight_recorded = True
                    raise
                postflight_recorded = True
                if postflight.policy_violation:
                    raise PolicyViolationError(postflight)
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
            if checkpoint is not None and not postflight_recorded:
                try:
                    self._persist_postflight(task_id, checkpoint)
                except BaseException:
                    pass
            self.store.transition_task_terminal(
                task_id,
                execution_status=ExecutionStatus.CANCELLED,
                event_kind="task.cancelled",
                payload={"reason": "execution handler cancelled"},
            )
            raise
        except Exception as error:
            if checkpoint is not None and not postflight_recorded:
                try:
                    self._persist_postflight(task_id, checkpoint)
                except BaseException:
                    pass
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

    def _persist_postflight(
        self, task_id: str, checkpoint: GitCheckpoint
    ) -> GitPostflight:
        """Persist bounded postflight evidence and return its classification."""

        try:
            postflight = git_postflight(checkpoint)
        except Exception as error:
            failure_payload = {
                "mode": TaskMode.AUTONOMOUS_WRITE.value,
                "repo_path": checkpoint.repo_path,
                "ok": False,
                "policy_violation": True,
                "error_type": type(error).__name__,
                "message": _bounded_error_message(error),
            }
            try:
                self.store.append_task_event(
                    task_id, "bridge", "policy.postflight", failure_payload
                )
                self.store.append_task_event(
                    task_id,
                    "bridge",
                    "policy.violation",
                    {"phase": "postflight", **failure_payload},
                )
            except Exception:
                pass
            raise GitPostflightError("Git postflight evidence could not be collected") from error

        payload = {"mode": TaskMode.AUTONOMOUS_WRITE.value, **postflight_payload(postflight)}
        self.store.append_task_event(task_id, "bridge", "policy.postflight", payload)
        if postflight.policy_violation:
            self.store.append_task_event(
                task_id,
                "bridge",
                "policy.violation",
                {"phase": "postflight", **payload},
            )
        return postflight


__all__ = ["BridgeCore", "TaskStateError"]
