"""Small MCP-facing adapter over Bridge Core and Bridge persistence."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import json
from typing import Any

from . import BRIDGE_STAGE, __version__
from .core import (
    BridgeCore,
    TaskStateError,
    _bounded_evidence_value,
    _bounded_final_response,
    _bounded_response_text,
)
from .domain.events import TaskEvent
from .domain.models import ExecutionStatus, Project, Task, TaskMode, timestamp_to_text
from .execution_worker import read_worker_state
from .persistence.sqlite_store import SQLiteBridgeStore
from .policy import PolicyError


DEFAULT_MODEL = "gpt-5.6-luna"
STAGE = BRIDGE_STAGE
MAX_EVENT_LIMIT = 1000
DEFAULT_EVENT_LIMIT = 100
MAX_CRITICAL_EVENT_RESULTS = 64
MAX_EVENT_RESPONSE_BYTES = 512 * 1024
_RESULT_EVENT_KINDS = (
    "task.finished",
    "policy.git_checkpoint",
    "policy.postflight",
    "policy.violation",
)


class MCPToolError(RuntimeError):
    """Safe, user-facing error raised while handling one MCP tool call."""


class MCPConcurrencyError(MCPToolError):
    """Raised when a second task is requested during an active run."""


def _required_text(arguments: Mapping[str, Any], name: str) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value.strip():
        raise MCPToolError(f"argument {name!r} must be non-empty text")
    return value


def _optional_text(arguments: Mapping[str, Any], name: str) -> str | None:
    value = arguments.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise MCPToolError(f"argument {name!r} must be non-empty text or null")
    return value


def _project_dict(project: Project) -> dict[str, Any]:
    return {
        "project_id": project.project_id,
        "name": project.name,
        "repo_path": project.repo_path,
        "created_at": timestamp_to_text(project.created_at),
        "updated_at": timestamp_to_text(project.updated_at),
    }


def _task_dict(task: Task) -> dict[str, Any]:
    return {
        "task_id": task.task_id,
        "project_id": task.project_id,
        "objective": _bounded_response_text(task.objective),
        "executor": task.executor,
        "model": task.model,
        "mode": task.mode.value,
        "execution_status": task.execution_status.value,
        "audit_status": task.audit_status.value,
        "thread_id": task.thread_id,
        "turn_id": task.turn_id,
        "created_at": timestamp_to_text(task.created_at),
        "updated_at": timestamp_to_text(task.updated_at),
    }


def _event_dict(event: TaskEvent) -> dict[str, Any]:
    return {
        "event_id": event.event_id,
        "task_id": event.task_id,
        "source": event.source,
        "kind": event.kind,
        "payload": _bounded_evidence_value(event.payload),
        "created_at": timestamp_to_text(event.created_at),
    }


class MCPAdapter:
    """Expose Bridge-owned operations as a small set of MCP tools."""

    def __init__(
        self,
        core: BridgeCore,
        store: SQLiteBridgeStore,
        *,
        bridge_version: str = __version__,
        stage: str = STAGE,
        executor_name: str = "codex",
    ) -> None:
        self.core = core
        self.store = store
        self.bridge_version = bridge_version
        self.stage = stage
        self.executor_name = executor_name

    def _all_tasks(self) -> list[Task]:
        tasks: list[Task] = []
        for project in self.store.list_projects():
            tasks.extend(self.store.list_tasks(project.project_id))
        return tasks

    def _latest_event(self, task_id: str) -> TaskEvent | None:
        return self.store.get_last_task_event(task_id)

    def _result_for_task(self, task: Task) -> dict[str, Any]:
        relevant_events = self.store.get_latest_task_events(
            task.task_id, _RESULT_EVENT_KINDS
        )
        by_kind = {
            event.kind: event
            for event in relevant_events
            if event.source == "bridge"
        }
        finished_event = by_kind.get("task.finished")
        payload = finished_event.payload if finished_event is not None else {}
        if not isinstance(payload, dict):
            payload = {}
        checkpoint_event = by_kind.get("policy.git_checkpoint")
        postflight_event = by_kind.get("policy.postflight")
        violation_event = by_kind.get("policy.violation")
        checkpoint = checkpoint_event.payload if checkpoint_event is not None else {}
        postflight = postflight_event.payload if postflight_event is not None else {}
        if not isinstance(checkpoint, dict):
            checkpoint = {}
        if not isinstance(postflight, dict):
            postflight = {}
        return {
            "task_id": task.task_id,
            "mode": task.mode.value,
            "execution_status": task.execution_status.value,
            "audit_status": task.audit_status.value,
            "thread_id": task.thread_id,
            "turn_id": task.turn_id,
            "available": finished_event is not None,
            "final_response": _bounded_final_response(payload.get("final_response")),
            "event_id": finished_event.event_id if finished_event is not None else None,
            "baseline_branch": checkpoint.get("baseline_branch"),
            "baseline_head": checkpoint.get("baseline_head"),
            "final_branch": postflight.get("final_branch"),
            "final_head": postflight.get("final_head"),
            "changed_files": _bounded_evidence_value(postflight.get("changed_files", [])),
            "untracked_files": _bounded_evidence_value(postflight.get("untracked_files", [])),
            "policy_violation": bool(
                violation_event is not None
                or postflight.get("policy_violation", False)
            ),
        }

    @staticmethod
    def _critical_event_kinds() -> set[str]:
        return {
            "task.created",
            "task.execution_requested",
            "task.execution_claimed",
            "task.started",
            "task.finished",
            "task.failed",
            "task.cancelled",
            "task.recovered",
            "policy.git_checkpoint",
            "policy.postflight",
            "policy.violation",
            "checkpoint.commit.created",
            "thread/started",
            "turn/started",
            "turn/completed",
        }

    @staticmethod
    def _response_size(value: Any) -> int:
        return len(
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        )

    def _bounded_event_response(
        self,
        task_id: str,
        events: list[TaskEvent],
        total: int,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Bound event count and aggregate response bytes, retaining critical events."""

        critical_kinds = self._critical_event_kinds()
        priority = [event for event in events if event.kind in critical_kinds]
        remainder = [event for event in events if event.kind not in critical_kinds]
        selected: list[dict[str, Any]] = []
        omitted = False
        for event in (*priority, *remainder):
            event_value = _event_dict(event)
            candidate = {
                "task_id": task_id,
                "events": [*selected, event_value],
                "count": total,
                "truncated": True,
            }
            if self._response_size(candidate) > MAX_EVENT_RESPONSE_BYTES:
                omitted = True
                continue
            selected.append(event_value)
        selected.sort(key=lambda event: event.get("event_id") or 0)
        return selected, omitted

    def _status(self) -> dict[str, Any]:
        worker_state = read_worker_state(self.store.db_path)
        worker_status = (
            worker_state.get("status")
            if isinstance(worker_state, Mapping)
            and isinstance(worker_state.get("status"), str)
            else None
        )
        worker_pid = (
            worker_state.get("pid")
            if isinstance(worker_state, Mapping)
            and isinstance(worker_state.get("pid"), int)
            and not isinstance(worker_state.get("pid"), bool)
            and worker_state.get("pid") > 0
            else None
        )
        worker_id = (
            worker_state.get("worker_id")
            if isinstance(worker_state, Mapping)
            and isinstance(worker_state.get("worker_id"), str)
            and worker_state.get("worker_id")
            else None
        )
        worker_active = worker_status in {
            "starting",
            "idle",
            "claiming",
            "running",
            "stopping",
            "error",
        }
        worker_owner: dict[str, Any] | None = None
        if worker_active:
            worker_owner = {"owner_kind": "persistent_worker"}
            if worker_id is not None:
                worker_owner["owner_id"] = worker_id
            if worker_pid is not None:
                worker_owner["pid"] = worker_pid
        tasks = self._all_tasks()
        running = [
            task
            for task in tasks
            if task.execution_status is ExecutionStatus.RUNNING
        ]
        queued_requested = [
            task
            for task in tasks
            if task.execution_status is ExecutionStatus.QUEUED
            and self.store.get_execution_request(task.task_id) is not None
        ]
        waiting = [
            task
            for task in tasks
            if task.execution_status is ExecutionStatus.WAITING_USER
        ]
        if running:
            active_task = max(running, key=lambda task: task.updated_at)
            active_source = "running"
        elif queued_requested:
            active_task = max(queued_requested, key=lambda task: task.updated_at)
            active_source = "queued_request"
        elif waiting:
            active_task = max(waiting, key=lambda task: task.updated_at)
            active_source = "waiting_user"
        else:
            active_task = None
            active_source = "historical"
        project = (
            self.store.get_project(active_task.project_id) if active_task is not None else None
        )
        latest_task = max(tasks, key=lambda task: task.updated_at) if tasks else None
        if active_task is None:
            active_task = latest_task
            project = (
                self.store.get_project(active_task.project_id)
                if active_task is not None
                else None
            )
        last_event = (
            self._latest_event(active_task.task_id)
            if active_task is not None
            else self._latest_event(latest_task.task_id) if latest_task is not None else None
        )
        claim = (
            self.store.get_execution_claim(active_task.task_id)
            if active_task is not None and active_source == "running"
            else None
        )
        owner: dict[str, Any] | None = None
        if claim is not None and isinstance(claim.payload, Mapping):
            owner_kind = claim.payload.get("owner_kind")
            owner_id = claim.payload.get("owner_id")
            pid = claim.payload.get("pid")
            claimed_at = claim.payload.get("claimed_at")
            if owner_kind == "persistent_worker":
                owner = {"owner_kind": owner_kind}
                if isinstance(owner_id, str) and owner_id:
                    owner["owner_id"] = owner_id
                if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
                    owner["pid"] = pid
                if isinstance(claimed_at, str) and claimed_at:
                    owner["claimed_at"] = claimed_at
                worker_owner = dict(owner)
        requested_task_id = (
            active_task.task_id if active_source == "queued_request" and active_task else None
        )
        running_task_id = (
            active_task.task_id if active_source == "running" and active_task else None
        )
        return {
            "bridge_version": self.bridge_version,
            "stage": self.stage,
            "executor": self.executor_name,
            "active_project": _project_dict(project) if project is not None else None,
            "active_task": _task_dict(active_task) if active_task is not None else None,
            "active_task_source": active_source if active_task is not None else None,
            "worker_active": worker_active,
            "worker_status": worker_status,
            "worker_pid": worker_pid,
            "worker_owner": worker_owner,
            "requested_task_id": requested_task_id,
            "running_task_id": running_task_id,
            "owner": owner,
            "owner_kind": owner.get("owner_kind") if owner is not None else None,
            "owner_id": owner.get("owner_id") if owner is not None else None,
            "pid": owner.get("pid") if owner is not None else None,
            "claimed_at": owner.get("claimed_at") if owner is not None else None,
            "project_id": project.project_id if project is not None else None,
            "task_id": active_task.task_id if active_task is not None else None,
            "model": active_task.model if active_task is not None else None,
            "execution_status": (
                active_task.execution_status.value if active_task is not None else None
            ),
            "audit_status": active_task.audit_status.value if active_task is not None else None,
            "thread_id": active_task.thread_id if active_task is not None else None,
            "turn_id": active_task.turn_id if active_task is not None else None,
            "last_event": _event_dict(last_event) if last_event is not None else None,
        }

    async def call_tool(
        self, name: str, arguments: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        try:
            return await self._call_tool(name, arguments)
        except asyncio.CancelledError:
            # Core persists task.cancelled and deliberately re-raises.  The
            # adapter cannot distinguish a peer-cancelled request from the
            # MCP server's global shutdown: both arrive here as the same
            # cancellation family, while the high-level MCPServer tool
            # context does not expose the dispatcher's request cancel event.
            # Preserve cancellation so a server TaskGroup can terminate.
            raise
        except TaskStateError as exc:
            raise MCPToolError(str(exc)) from exc
        except PolicyError as exc:
            raise MCPToolError(str(exc)) from exc
        except MCPToolError:
            raise
        except Exception as exc:
            # Keep persistence/executor failures inside the application error
            # boundary. The SDK-facing server adds the safe tool semantics;
            # no traceback, secret, or path detail crosses this boundary.
            raise MCPToolError(f"tool {name!r} failed") from exc

    async def _call_tool(
        self, name: str, arguments: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        if not isinstance(name, str) or not name:
            raise MCPToolError("tool name must be non-empty text")
        args: Mapping[str, Any] = {} if arguments is None else arguments
        if not isinstance(args, Mapping):
            raise MCPToolError("tool arguments must be an object")

        if name == "get_status":
            return self._status()
        if name == "create_project":
            project = self.core.create_project(
                _required_text(args, "name"),
                _required_text(args, "repo_path"),
                project_id=_optional_text(args, "project_id"),
            )
            return _project_dict(project)
        if name == "create_task":
            mode_value = args.get("mode")
            if mode_value is None:
                mode = TaskMode.READ_ONLY
            elif not isinstance(mode_value, str):
                raise MCPToolError("argument 'mode' must be READ_ONLY or AUTONOMOUS_WRITE")
            else:
                try:
                    mode = TaskMode(mode_value)
                except ValueError as exc:
                    raise MCPToolError(
                        "argument 'mode' must be READ_ONLY or AUTONOMOUS_WRITE"
                    ) from exc
            task = self.core.create_task(
                _required_text(args, "project_id"),
                _required_text(args, "objective"),
                model=_optional_text(args, "model") or DEFAULT_MODEL,
                task_id=_optional_text(args, "task_id"),
                mode=mode,
            )
            return _task_dict(task)
        if name == "get_task":
            task = self.store.get_task(_required_text(args, "task_id"))
            if task is None:
                raise MCPToolError(f"task does not exist: {args.get('task_id')}")
            return _task_dict(task)
        if name == "get_task_events":
            task_id = _required_text(args, "task_id")
            if self.store.get_task(task_id) is None:
                raise MCPToolError(f"task does not exist: {task_id}")
            limit = args.get("limit")
            if limit is not None:
                if isinstance(limit, bool) or not isinstance(limit, int):
                    raise MCPToolError("argument 'limit' must be an integer")
                if limit < 1 or limit > MAX_EVENT_LIMIT:
                    raise MCPToolError(
                        f"argument 'limit' must be between 1 and {MAX_EVENT_LIMIT}"
                    )
            requested_limit = DEFAULT_EVENT_LIMIT if limit is None else limit
            selected, total = self.store.list_task_events_window(
                task_id,
                requested_limit,
                critical_kinds=self._critical_event_kinds(),
                critical_limit=MAX_CRITICAL_EVENT_RESULTS,
            )
            selected_values, response_omitted = self._bounded_event_response(
                task_id, selected, total
            )
            return {
                "task_id": task_id,
                "events": selected_values,
                "count": total,
                "truncated": response_omitted or len(selected_values) < total,
            }
        if name == "get_result":
            task = self.store.get_task(_required_text(args, "task_id"))
            if task is None:
                raise MCPToolError(f"task does not exist: {args.get('task_id')}")
            return self._result_for_task(task)
        if name == "commit_checkpoint":
            return self.core.commit_checkpoint(
                _required_text(args, "task_id"),
                _required_text(args, "message"),
            )
        if name == "run_task":
            task_id = _required_text(args, "task_id")
            dispatch = self.core.request_execution(task_id)
            result = _task_dict(dispatch.task)
            result.update(
                {
                    "accepted": dispatch.accepted,
                    "already_requested": dispatch.already_requested,
                    "request_id": dispatch.request_id,
                }
            )
            return result
        raise MCPToolError(f"unknown tool: {name}")


__all__ = [
    "DEFAULT_MODEL",
    "DEFAULT_EVENT_LIMIT",
    "MAX_EVENT_LIMIT",
    "MCPAdapter",
    "MCPConcurrencyError",
    "MCPToolError",
    "STAGE",
]
