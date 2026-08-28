"""Bridge-owned orchestration over the small executor contract."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
import inspect
import json
import uuid
from typing import Any

from .domain.models import (
    AuditStatus,
    ExecutionStatus,
    Project,
    Task,
    TaskMode,
    TaskStateError,
    timestamp_to_text,
    utc_now,
)
from .executors.base import ExecutionRequest, ExecutionResult, Executor
from .readonly_git_index import (
    ReadOnlyGitIndexError,
    ReadOnlyGitIndexResult,
    preflight_read_only_git_index,
)
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
    CheckpointAlreadyCommittedError,
    ensure_autonomous_workspace,
    git_continuation_preflight,
    git_checkpoint_commit,
    git_postflight,
    git_preflight,
    postflight_payload,
)
from .persistence.sqlite_store import D3_R2_CONTRACT, SQLiteBridgeStore


@dataclass(frozen=True)
class ExecutionDispatch:
    """Durable acceptance returned by the request/dispatch boundary."""

    task: Task
    request_id: str | None
    accepted: bool
    already_requested: bool


_MAX_NOTIFICATION_DEPTH = 4
_MAX_NOTIFICATION_ITEMS = 64
_MAX_NOTIFICATION_TEXT = 4096
# These are byte limits for data that crosses or is persisted by the Bridge.
# The structural limits above remain useful for readable previews, while this
# aggregate limit prevents a broad payload from expanding the journal or MCP
# response without bound.
MAX_EVIDENCE_PAYLOAD_BYTES = 16 * 1024
MAX_FINAL_RESPONSE_BYTES = 12 * 1024
_CRITICAL_EVIDENCE_KEYS = (
    "threadId",
    "turnId",
    "itemId",
    "status",
    "type",
    "phase",
    "id",
    "method",
    "final_response",
)
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
        items = list(value.items())
        priority = [
            item
            for item in items
            if str(item[0]) in _CRITICAL_EVIDENCE_KEYS
        ]
        regular = [
            item
            for item in items
            if str(item[0]) not in _CRITICAL_EVIDENCE_KEYS
        ]
        for index, (key, child) in enumerate((*priority, *regular)):
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


def _compact_json_size(value: Any) -> int:
    """Return the UTF-8 size of the JSON representation used by SQLite."""

    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    )


def _bounded_evidence_value(
    value: Any, *, max_bytes: int = MAX_EVIDENCE_PAYLOAD_BYTES
) -> Any:
    """Apply structural and aggregate bounds while retaining identifiers.

    A large structured notification is represented by a small, valid JSON
    summary.  Known correlation/status fields are retained when they fit; the
    original payload is never embedded in the summary.
    """

    bounded = _bounded_notification_value(value)
    encoded_size = _compact_json_size(bounded)
    if encoded_size <= max_bytes:
        return bounded

    summary: dict[str, Any] = {
        "_truncated": True,
        "_original_bytes": encoded_size,
    }
    if isinstance(bounded, Mapping):
        for key in _CRITICAL_EVIDENCE_KEYS:
            if key not in bounded:
                continue
            candidate = dict(summary)
            candidate[key] = bounded[key]
            if _compact_json_size(candidate) <= max_bytes:
                summary = candidate
    return summary


def _bounded_text_bytes(value: str, max_bytes: int) -> str:
    """Truncate UTF-8 text without splitting a code point."""

    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    marker = "[TRUNCATED]"
    marker_bytes = marker.encode("utf-8")
    if max_bytes <= len(marker_bytes):
        return marker[:max_bytes]
    prefix = encoded[: max_bytes - len(marker_bytes)].decode("utf-8", errors="ignore")
    return prefix + marker


def _bounded_final_response(value: str | None) -> str | None:
    """Keep the public final-response type while enforcing a byte limit."""

    if value is None:
        return None
    if not isinstance(value, str):
        return None
    return _bounded_text_bytes(value, MAX_FINAL_RESPONSE_BYTES)


def _bounded_response_text(value: str) -> str:
    """Bound text fields returned by task/status MCP representations."""

    return _bounded_text_bytes(value, MAX_FINAL_RESPONSE_BYTES)


def _bounded_error_message(error: Exception) -> str:
    return str(_bounded_notification_value(str(error)))


class BridgeCore:
    """Create Bridge entities and run tasks through an injected executor."""

    def __init__(self, store: SQLiteBridgeStore, executor: Executor | None = None) -> None:
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

    @staticmethod
    def _request_id_from_event(event: Any) -> str | None:
        payload = getattr(event, "payload", None)
        request_id = payload.get("request_id") if isinstance(payload, Mapping) else None
        return request_id if isinstance(request_id, str) and request_id else None

    def _dispatch_for_nonqueued_task(self, task: Task) -> ExecutionDispatch:
        event = self.store.get_execution_request(task.task_id)
        return ExecutionDispatch(
            task=task,
            request_id=self._request_id_from_event(event),
            accepted=task.execution_status is ExecutionStatus.RUNNING,
            already_requested=(
                task.execution_status is ExecutionStatus.RUNNING or event is not None
            ),
        )

    def request_execution(self, task_id: str) -> ExecutionDispatch:
        """Validate a task and durably accept one asynchronous execution request."""

        task = self.store.get_task(task_id)
        if task is None:
            raise KeyError(f"task does not exist: {task_id}")
        if task.execution_status is ExecutionStatus.QUEUED:
            if self.store.get_project(task.project_id) is None:
                raise RuntimeError(f"project does not exist: {task.project_id}")
            request_id = str(uuid.uuid4())
            requested_at = timestamp_to_text(utc_now())
            try:
                updated, event, created = self.store.request_task_execution(
                    task_id,
                    {
                        "contract": D3_R2_CONTRACT,
                        "requested_via": "run_task",
                        "request_id": request_id,
                        "requested_at": requested_at,
                        "requested_by": "mcp",
                    },
                )
            except TaskStateError:
                # A worker may claim between the initial read and the
                # BEGIN-IMMEDIATE request transaction.  Reflect the durable
                # state instead of turning that benign race into an MCP error.
                current = self.store.get_task(task_id)
                if current is None or current.execution_status is ExecutionStatus.QUEUED:
                    raise
                return self._dispatch_for_nonqueued_task(current)
            # On an idempotent retry, return the durable request's identifier,
            # not the newly generated transient candidate.
            return ExecutionDispatch(
                task=updated,
                request_id=self._request_id_from_event(event),
                accepted=True,
                already_requested=not created,
            )
        # A RUNNING task is already owned; terminal and user-waiting tasks are
        # observable but never relaunchable.
        return self._dispatch_for_nonqueued_task(task)

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

    @staticmethod
    def _task_creation_order(store: SQLiteBridgeStore, task: Task) -> tuple[Any, ...]:
        """Return a durable creation order without relying on task_id sorting."""

        created_ids = [
            event.event_id
            for event in store.list_task_events(task.task_id)
            if event.source == "bridge" and event.kind == "task.created"
        ]
        if created_ids:
            return (0, min(created_ids), task.task_id)
        return (1, task.created_at, task.task_id)

    def _require_last_task(self, task: Task) -> None:
        tasks = self.store.list_tasks(task.project_id)
        if not tasks:
            raise PolicyError("task is not present in its project")
        latest = max(
            tasks,
            key=lambda candidate: self._task_creation_order(self.store, candidate),
        )
        if latest.task_id != task.task_id:
            raise PolicyError("checkpoint task is not the last applicable task")

    def commit_checkpoint(self, task_id: str, message: str) -> dict[str, Any]:
        """Create one local checkpoint for a finished autonomous task."""

        task = self.store.get_task(task_id)
        if task is None:
            raise PolicyError(f"task does not exist: {task_id}")
        if task.mode is not TaskMode.AUTONOMOUS_WRITE:
            raise PolicyError("checkpoint commits require AUTONOMOUS_WRITE mode")
        if task.execution_status is not ExecutionStatus.FINISHED:
            raise TaskStateError("checkpoint commits require a FINISHED task")
        if task.audit_status is AuditStatus.CORRECTION_REQUIRED:
            raise PolicyError("checkpoint commit is blocked by audit correction")
        project = self.store.get_project(task.project_id)
        if project is None:
            raise PolicyError(f"project does not exist: {task.project_id}")
        events = self.store.list_task_events(task_id)
        if any(
            event.source == "bridge" and event.kind == "checkpoint.commit.created"
            for event in events
        ):
            raise CheckpointAlreadyCommittedError(
                "checkpoint commit already exists for task"
            )
        if any(
            event.source == "bridge" and event.kind == "policy.violation"
            for event in events
        ):
            raise PolicyError("checkpoint commit is blocked by policy violation")
        postflight_events = [
            event
            for event in events
            if event.source == "bridge" and event.kind == "policy.postflight"
        ]
        if not postflight_events or not isinstance(postflight_events[-1].payload, dict):
            raise PolicyError("durable autonomous postflight is required")
        postflight = postflight_events[-1].payload
        if postflight.get("policy_violation") is not False:
            raise PolicyError(
                "durable autonomous postflight contains a policy violation"
            )
        self._require_last_task(task)
        safe_repo = ensure_autonomous_workspace(project.repo_path)
        result = git_checkpoint_commit(
            safe_repo,
            postflight=postflight,
            message=message,
        )
        payload = {
            "task_id": task.task_id,
            "project_id": project.project_id,
            "previous_head": result.previous_head,
            "commit_head": result.commit_head,
            "branch": result.branch,
            "message": result.message,
            "paths": list(result.paths),
            "clean": result.clean,
        }
        # Do not attempt a Git rollback if SQLite persistence fails here.  The
        # new HEAD remains the durable proof and a retry is rejected by the
        # branch/HEAD and idempotency preconditions.
        self.store.append_task_event(
            task_id,
            "bridge",
            "checkpoint.commit.created",
            payload,
        )
        return payload

    async def _cancel_active_execution(self) -> None:
        if self.executor is None:
            return
        cancel_active = getattr(self.executor, "cancel_active", None)
        if not callable(cancel_active):
            return
        result = cancel_active()
        if inspect.isawaitable(result):
            await result

    def _require_executor(self) -> Executor:
        if self.executor is None:
            raise RuntimeError("task execution belongs to the persistent execution worker")
        return self.executor

    def _preflight_checkpoint(self, task: Task, project: Project) -> GitCheckpoint | None:
        if task.mode is not TaskMode.AUTONOMOUS_WRITE:
            return None
        safe_repo = ensure_autonomous_workspace(project.repo_path)
        try:
            return git_preflight(safe_repo)
        except DirtyWorkingTreeError:
            previous = self._previous_continuation_baseline(task)
            if previous is None:
                raise
            previous_task_id, previous_postflight = previous
            return git_continuation_preflight(
                safe_repo,
                previous_task_id=previous_task_id,
                previous_postflight=previous_postflight,
            )

    @staticmethod
    def _preflight_violation_payload(error: PolicyError) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "phase": "preflight",
            "error_type": type(error).__name__,
            "message": _bounded_error_message(error),
            "policy_violation": True,
        }
        if isinstance(error, DirtyWorkingTreeError):
            payload.update(
                {
                    "status_porcelain": error.status_porcelain,
                    "staged_paths": list(error.staged_paths),
                    "unstaged_paths": list(error.unstaged_paths),
                    "untracked_paths": list(error.untracked_paths),
                }
            )
        elif isinstance(error, ContinuationBaselineError):
            payload["baseline_kind"] = "continuation"
        return payload

    def _record_preflight_failure(
        self,
        task_id: str,
        error: PolicyError,
        *,
        expected_status: ExecutionStatus,
    ) -> None:
        self.store.transition_task_preflight_failed(
            task_id,
            policy_payload=self._preflight_violation_payload(error),
            failed_payload={
                "error_type": type(error).__name__,
                "message": _bounded_error_message(error),
            },
            expected_status=expected_status,
        )

    @staticmethod
    def _read_only_index_event_payload(
        result: ReadOnlyGitIndexResult | ReadOnlyGitIndexError,
    ) -> dict[str, str]:
        """Return the bounded, path-free payload for the dedicated event."""

        payload = {"outcome": result.outcome, "reason": result.reason}
        if isinstance(result, ReadOnlyGitIndexError) and result.rollback_status:
            payload["rollback_status"] = result.rollback_status
        return payload

    def _preflight_read_only_index(
        self,
        task: Task,
        project: Project,
        *,
        expected_status: ExecutionStatus,
    ) -> None:
        """Run the Windows READ_ONLY index preflight before executor.run."""

        if task.mode is not TaskMode.READ_ONLY:
            return
        try:
            result = preflight_read_only_git_index(project.repo_path)
        except ReadOnlyGitIndexError as error:
            # This event is deliberately appended before the generic terminal
            # failure transition, so an executor can never be called without
            # durable evidence of the fail-closed decision.
            self.store.append_task_event(
                task.task_id,
                "bridge",
                "policy.read_only_git_index_access",
                self._read_only_index_event_payload(error),
            )
            self._record_preflight_failure(
                task.task_id,
                error,
                expected_status=expected_status,
            )
            raise
        except Exception as error:
            safe_error = ReadOnlyGitIndexError("internal_error")
            self.store.append_task_event(
                task.task_id,
                "bridge",
                "policy.read_only_git_index_access",
                self._read_only_index_event_payload(safe_error),
            )
            self._record_preflight_failure(
                task.task_id,
                safe_error,
                expected_status=expected_status,
            )
            raise safe_error from error
        if result.outcome != "noop":
            self.store.append_task_event(
                task.task_id,
                "bridge",
                "policy.read_only_git_index_access",
                self._read_only_index_event_payload(result),
            )

    async def _execute_running_task(
        self,
        task: Task,
        project: Project,
        checkpoint: GitCheckpoint | None,
    ) -> Task:
        """Execute one task whose durable owner already set it RUNNING."""

        executor = self._require_executor()
        task_id = task.task_id

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
                _bounded_evidence_value(params),
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
            result = await executor.run(
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
            return self.store.transition_task_terminal(
                task_id,
                execution_status=ExecutionStatus.FINISHED,
                event_kind="task.finished",
                payload={
                    "thread_id": result.thread_id,
                    "turn_id": result.turn_id,
                    "status": ExecutionStatus.FINISHED.value,
                    "final_response": _bounded_final_response(result.final_response),
                },
            )
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

    async def run_task(self, task_id: str) -> Task:
        """Legacy synchronous execution path retained for direct Core callers."""

        self._require_executor()
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
        self._preflight_read_only_index(
            task,
            project,
            expected_status=ExecutionStatus.QUEUED,
        )
        if task.mode is TaskMode.AUTONOMOUS_WRITE:
            try:
                checkpoint = self._preflight_checkpoint(task, project)
            except PolicyError as error:
                # Direct Core callers retain the original queue/preflight
                # contract; the worker-owned path uses expected_status=RUNNING.
                self._record_preflight_failure(
                    task_id, error, expected_status=ExecutionStatus.QUEUED
                )
                raise
            self.store.append_task_event(
                task_id,
                "bridge",
                "policy.git_checkpoint",
                {"mode": task.mode.value, **checkpoint_payload(checkpoint)},
            )

        self.store.transition_task_running(task_id, project_id=project.project_id)
        return await self._execute_running_task(task, project, checkpoint)

    async def execute_claimed_task(self, task_id: str) -> Task:
        """Run one task after a persistent worker atomically claimed it."""

        self._require_executor()
        task = self.store.get_task(task_id)
        if task is None:
            raise KeyError(f"task does not exist: {task_id}")
        if task.execution_status is not ExecutionStatus.RUNNING:
            raise TaskStateError(
                f"task {task_id} cannot execute from state "
                f"{task.execution_status.value}; expected RUNNING"
            )
        if self.store.get_execution_claim(task_id) is None:
            raise TaskStateError(f"task {task_id} has no persistent worker claim")
        project = self.store.get_project(task.project_id)
        if project is None:
            raise RuntimeError(f"project does not exist: {task.project_id}")

        checkpoint: GitCheckpoint | None = None
        self._preflight_read_only_index(
            task,
            project,
            expected_status=ExecutionStatus.RUNNING,
        )
        if task.mode is TaskMode.AUTONOMOUS_WRITE:
            try:
                checkpoint = self._preflight_checkpoint(task, project)
            except PolicyError as error:
                self._record_preflight_failure(
                    task_id, error, expected_status=ExecutionStatus.RUNNING
                )
                raise
            self.store.append_task_event(
                task_id,
                "bridge",
                "policy.git_checkpoint",
                {"mode": task.mode.value, **checkpoint_payload(checkpoint)},
            )

        return await self._execute_running_task(task, project, checkpoint)

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


__all__ = ["BridgeCore", "ExecutionDispatch", "TaskStateError"]
