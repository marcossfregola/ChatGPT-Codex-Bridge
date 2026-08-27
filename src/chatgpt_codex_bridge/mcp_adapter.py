"""Small MCP-facing adapter over Bridge Core and Bridge persistence."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from . import BRIDGE_STAGE, __version__
from .core import BridgeCore, TaskStateError
from .domain.events import TaskEvent
from .domain.models import Project, Task, TaskMode, timestamp_to_text
from .persistence.sqlite_store import SQLiteBridgeStore
from .policy import PolicyError


DEFAULT_MODEL = "gpt-5.6-luna"
STAGE = BRIDGE_STAGE
MAX_EVENT_LIMIT = 1000


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
        "objective": task.objective,
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
        "payload": event.payload,
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
        self._run_lock = asyncio.Lock()

    def _all_tasks(self) -> list[Task]:
        tasks: list[Task] = []
        for project in self.store.list_projects():
            tasks.extend(self.store.list_tasks(project.project_id))
        return tasks

    def _latest_event(self, task_id: str) -> TaskEvent | None:
        return self.store.get_last_task_event(task_id)

    def _result_for_task(self, task: Task) -> dict[str, Any]:
        finished_events = [
            event
            for event in self.store.list_task_events(task.task_id)
            if event.source == "bridge" and event.kind == "task.finished"
        ]
        payload = finished_events[-1].payload if finished_events else {}
        if not isinstance(payload, dict):
            payload = {}
        events = self.store.list_task_events(task.task_id)
        checkpoint_events = [
            event
            for event in events
            if event.source == "bridge" and event.kind == "policy.git_checkpoint"
        ]
        postflight_events = [
            event
            for event in events
            if event.source == "bridge" and event.kind == "policy.postflight"
        ]
        violation_events = [
            event
            for event in events
            if event.source == "bridge" and event.kind == "policy.violation"
        ]
        checkpoint = checkpoint_events[-1].payload if checkpoint_events else {}
        postflight = postflight_events[-1].payload if postflight_events else {}
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
            "available": bool(finished_events),
            "final_response": payload.get("final_response"),
            "event_id": finished_events[-1].event_id if finished_events else None,
            "baseline_branch": checkpoint.get("baseline_branch"),
            "baseline_head": checkpoint.get("baseline_head"),
            "final_branch": postflight.get("final_branch"),
            "final_head": postflight.get("final_head"),
            "changed_files": postflight.get("changed_files", []),
            "untracked_files": postflight.get("untracked_files", []),
            "policy_violation": bool(
                violation_events
                or postflight.get("policy_violation", False)
            ),
        }

    @staticmethod
    def _critical_event_kinds() -> set[str]:
        return {
            "task.created",
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

    def _status(self) -> dict[str, Any]:
        tasks = self._all_tasks()
        active_statuses = {"QUEUED", "RUNNING", "WAITING_USER"}
        active = [task for task in tasks if task.execution_status.value in active_statuses]
        active.sort(key=lambda task: task.updated_at)
        active_task = active[-1] if active else None
        project = (
            self.store.get_project(active_task.project_id) if active_task is not None else None
        )
        latest_task = max(tasks, key=lambda task: task.updated_at) if tasks else None
        last_event = (
            self._latest_event(active_task.task_id)
            if active_task is not None
            else self._latest_event(latest_task.task_id) if latest_task is not None else None
        )
        return {
            "bridge_version": self.bridge_version,
            "stage": self.stage,
            "executor": self.executor_name,
            "active_project": _project_dict(project) if project is not None else None,
            "active_task": _task_dict(active_task) if active_task is not None else None,
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
            events = self.store.list_task_events(task_id)
            if limit is None or len(events) <= limit:
                selected = events
            else:
                selected = events[:limit]
                selected_ids = {event.event_id for event in selected}
                selected.extend(
                    event
                    for event in events
                    if event.kind in self._critical_event_kinds()
                    and event.event_id not in selected_ids
                )
                selected.sort(key=lambda event: event.event_id or 0)
            return {
                "task_id": task_id,
                "events": [_event_dict(event) for event in selected],
                "count": len(events),
                "truncated": len(selected) < len(events),
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
            if self._run_lock.locked():
                raise MCPConcurrencyError("another task is already running")
            async with self._run_lock:
                task = await self.core.run_task(task_id)
                result = _task_dict(task)
                result["final_response"] = self._result_for_task(task)["final_response"]
                return result
        raise MCPToolError(f"unknown tool: {name}")


__all__ = [
    "DEFAULT_MODEL",
    "MAX_EVENT_LIMIT",
    "MCPAdapter",
    "MCPConcurrencyError",
    "MCPToolError",
    "STAGE",
]
