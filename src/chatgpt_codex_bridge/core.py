"""Bridge-owned orchestration over the small executor contract."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import inspect
import json
import sqlite3
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
    CheckpointCommitError,
    CheckpointPreconditionError,
    CHECKPOINT_PHASE_PRE_CAS,
    CHECKPOINT_PHASE_CREATED,
    CHECKPOINT_PHASE_REF_UPDATED,
    PreparedCheckpoint,
    ensure_autonomous_workspace,
    git_continuation_preflight,
    git_checkpoint_cas,
    git_checkpoint_finalize,
    git_checkpoint_head_relation,
    git_checkpoint_prepare,
    _h4_rehydrate_prepared,
    git_postflight,
    git_preflight,
    postflight_payload,
)
from .persistence.sqlite_store import (
    CHECKPOINT_CREATED_EVENT,
    CHECKPOINT_FAILED_EVENT,
    CHECKPOINT_REF_UPDATED_EVENT,
    CHECKPOINT_STARTED_EVENT,
    D3_H3_CONTRACT,
    D3_R2_CONTRACT,
    SQLiteBridgeStore,
)


@dataclass(frozen=True)
class ExecutionDispatch:
    """Durable acceptance returned by the request/dispatch boundary."""

    task: Task
    request_id: str | None
    accepted: bool
    already_requested: bool


@dataclass(frozen=True)
class CancellationDispatch:
    """Durable acceptance returned by the cancellation boundary."""

    task: Task
    request_id: str | None
    accepted: bool
    already_requested: bool
    terminal: bool


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

    def request_cancellation(self, task_id: str) -> CancellationDispatch:
        """Durably request one safe cancellation for a queued/running task."""

        task = self.store.get_task(task_id)
        if task is None:
            raise KeyError(f"task does not exist: {task_id}")
        original_status = task.execution_status
        request_id = str(uuid.uuid4())
        requested_at = timestamp_to_text(utc_now())
        updated, event, created = self.store.request_task_cancellation(
            task_id,
            {
                "contract": D3_H3_CONTRACT,
                "requested_via": "cancel_task",
                "request_id": request_id,
                "requested_at": requested_at,
                "requested_by": "mcp",
                # Keep the H3 target durable so recovery can distinguish a
                # cancellation for this exact thread/turn from unrelated
                # terminal Codex evidence.
                "thread_id": task.thread_id,
                "turn_id": task.turn_id,
            },
        )
        durable_request_id = self._request_id_from_event(event)
        accepted = updated.execution_status is ExecutionStatus.RUNNING or (
            original_status is ExecutionStatus.QUEUED
            and created
            and updated.execution_status is ExecutionStatus.CANCELLED
        )
        return CancellationDispatch(
            task=updated,
            request_id=durable_request_id,
            accepted=accepted,
            already_requested=(not created and event is not None),
            terminal=updated.execution_status
            in {
                ExecutionStatus.FINISHED,
                ExecutionStatus.FAILED,
                ExecutionStatus.CANCELLED,
            },
        )

    def _has_cancellation_confirmation(
        self,
        task_id: str,
        *,
        thread_id: str | None = None,
        turn_id: str | None = None,
    ) -> bool:
        """Require durable interrupt or Codex terminal-turn evidence."""

        task = self.store.get_task(task_id)
        expected_thread_id = thread_id or (task.thread_id if task is not None else None)
        expected_turn_id = turn_id or (task.turn_id if task is not None else None)
        sent = self.store.get_cancellation_interrupt_sent(task_id)
        if sent is not None:
            payload = sent.payload if sent is not None else None
            if not isinstance(payload, Mapping):
                return False
            sent_thread_id = payload.get("thread_id")
            sent_turn_id = payload.get("turn_id")
            if (
                expected_thread_id is not None
                and sent_thread_id != expected_thread_id
            ) or (
                expected_turn_id is not None
                and sent_turn_id != expected_turn_id
            ):
                return False
            return True
        terminal_events = self.store.get_latest_task_events(
            task_id,
            ("turn/completed", "turn/interrupted", "turn/cancelled", "turn/aborted"),
        )
        for event in terminal_events:
            if event.source != "codex":
                continue
            payload = event.payload
            event_thread_id = event_turn_id = None
            status: Any = None
            if isinstance(payload, Mapping):
                event_thread_id = payload.get("threadId", payload.get("thread_id"))
                event_turn_id = payload.get("turnId", payload.get("turn_id"))
                status = payload.get("status")
                turn = payload.get("turn")
                if isinstance(turn, Mapping):
                    if event_turn_id is None:
                        event_turn_id = turn.get("id")
                    status = turn.get("status", status)
                if (
                    expected_thread_id is not None
                    and event_thread_id is not None
                    and event_thread_id != expected_thread_id
                ) or (
                    expected_turn_id is not None
                    and event_turn_id is not None
                    and event_turn_id != expected_turn_id
                ):
                    continue
            if event.kind in {"turn/interrupted", "turn/cancelled", "turn/aborted"}:
                return True
            if not isinstance(payload, Mapping):
                continue
            if isinstance(status, str) and status.lower() in {
                "interrupted",
                "cancelled",
                "canceled",
                "aborted",
            }:
                return True
        return False

    @staticmethod
    def _event_correlation(event: Any) -> tuple[str | None, str | None, str | None]:
        """Extract exact thread/turn/status evidence from one Codex event."""

        payload = getattr(event, "payload", None)
        if not isinstance(payload, Mapping):
            return None, None, None
        thread_id = payload.get("thread_id", payload.get("threadId"))
        turn_id = payload.get("turn_id", payload.get("turnId"))
        status = payload.get("status")
        turn = payload.get("turn")
        if isinstance(turn, Mapping):
            if turn_id is None:
                turn_id = turn.get("id")
            if status is None:
                status = turn.get("status")
        if not isinstance(thread_id, str):
            thread_id = None
        if not isinstance(turn_id, str):
            turn_id = None
        if not isinstance(status, str):
            status = None
        return thread_id, turn_id, status

    @classmethod
    def _correlated_turn_events(
        cls, task: Task, events: list[Any]
    ) -> list[Any]:
        """Return only Codex terminal events with exact task correlation."""

        if not task.thread_id or not task.turn_id:
            return []
        terminal_kinds = {
            "turn/completed",
            "turn/failed",
            "turn/interrupted",
            "turn/cancelled",
            "turn/aborted",
        }
        correlated: list[Any] = []
        for event in events:
            if getattr(event, "source", None) != "codex":
                continue
            if getattr(event, "kind", None) not in terminal_kinds:
                continue
            thread_id, turn_id, _ = cls._event_correlation(event)
            if thread_id == task.thread_id and turn_id == task.turn_id:
                correlated.append(event)
        return correlated

    @classmethod
    def _recovery_classification(
        cls, task: Task, events: list[Any]
    ) -> tuple[str, dict[str, Any]] | None:
        """Classify one RUNNING task from durable Codex evidence only."""

        correlated = cls._correlated_turn_events(task, events)
        if not correlated:
            return None
        latest = correlated[-1]
        kind = latest.kind
        thread_id, turn_id, status = cls._event_correlation(latest)
        effective_kind = kind
        if kind == "turn/completed" and isinstance(status, str):
            normalized_status = status.replace("_", "").replace("-", "").lower()
            if normalized_status in {"interrupted", "cancelled", "canceled", "aborted"}:
                effective_kind = "turn/interrupted"
            elif normalized_status == "failed":
                effective_kind = "turn/failed"
        evidence_ids = [event.event_id for event in correlated if event.event_id is not None]
        base = {
            "thread_id": thread_id,
            "turn_id": turn_id,
            "evidence_event_ids": evidence_ids,
            "evidence_kind": effective_kind,
        }
        if effective_kind in {"turn/interrupted", "turn/cancelled", "turn/aborted"}:
            def compatible_cancel_request(event: Any) -> bool:
                if (
                    event.source != "bridge"
                    or event.kind != "task.cancel_requested"
                    or not isinstance(event.payload, Mapping)
                    or event.payload.get("contract") != D3_H3_CONTRACT
                    or (event.event_id or 0) >= (latest.event_id or 0)
                ):
                    return False
                request_thread = event.payload.get(
                    "thread_id", event.payload.get("threadId")
                )
                request_turn = event.payload.get(
                    "turn_id", event.payload.get("turnId")
                )
                return (
                    (request_thread is None or request_thread == thread_id)
                    and (request_turn is None or request_turn == turn_id)
                )

            cancellation = next(
                (
                    event
                    for event in events
                    if compatible_cancel_request(event)
                ),
                None,
            )
            if cancellation is not None:
                payload = {
                    **base,
                    "status": ExecutionStatus.CANCELLED.value,
                    "reason": "cancel request confirmed by durable Codex interruption",
                    "cancel_request_id": cls._request_id_from_event(cancellation),
                }
                return ExecutionStatus.CANCELLED.value, payload
            return ExecutionStatus.FAILED.value, {
                **base,
                "status": ExecutionStatus.FAILED.value,
                "reason": "unexpected execution interruption",
                "error_type": "UnexpectedExecutionInterruption",
            }
        if effective_kind == "turn/failed" and (
            status is not None
            or (
                isinstance(latest.payload, Mapping)
                and any(key in latest.payload for key in ("error", "error_type", "message"))
            )
        ):
            return ExecutionStatus.FAILED.value, {
                **base,
                "status": ExecutionStatus.FAILED.value,
                "reason": "Codex turn failed",
                "error": latest.payload if isinstance(latest.payload, Mapping) else None,
            }
        # A completed Codex turn is deliberately not enough to reconstruct the
        # Bridge result after a crash.  The caller creates a durable
        # reconciliation_required marker instead.
        return None

    @staticmethod
    def _reconciliation_payload(task: Task, events: list[Any]) -> dict[str, Any]:
        """Build stable evidence for a reconciliation marker."""

        relevant = []
        for event in events:
            if event.source not in {"bridge", "codex"}:
                continue
            if not (
                event.kind.startswith("turn/")
                or event.kind
                in {
                    "task.execution_claimed",
                    "task.started",
                    "task.cancel_requested",
                    "task.cancel_interrupt_sent",
                    "task.cancel_interrupt_failed",
                }
            ):
                continue
            payload = event.payload if isinstance(event.payload, Mapping) else {}
            # Owner PID/sidecar values are correlation hints, not authority;
            # deliberately exclude them from the stable fingerprint.
            if event.kind == "task.execution_claimed":
                fingerprint_payload = {
                    key: payload.get(key)
                    for key in ("owner_kind", "owner_id", "claimed_at")
                    if key in payload
                }
            elif event.kind.startswith("turn/"):
                fingerprint_payload = {
                    key: payload.get(key)
                    for key in ("threadId", "thread_id", "turnId", "turn_id", "status")
                    if key in payload
                }
            else:
                fingerprint_payload = {
                    key: payload.get(key)
                    for key in ("request_id", "contract", "thread_id", "turn_id")
                    if key in payload
                }
            relevant.append(
                {
                    "event_id": event.event_id,
                    "source": event.source,
                    "kind": event.kind,
                    "payload": fingerprint_payload,
                }
            )
        material = {
            "task_id": task.task_id,
            "thread_id": task.thread_id,
            "turn_id": task.turn_id,
            "events": relevant,
        }
        encoded = json.dumps(
            material,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return {
            "reason": "durable execution outcome is insufficient to recover safely",
            "thread_id": task.thread_id,
            "turn_id": task.turn_id,
            "claim": next(
                (
                    event.payload
                    for event in reversed(events)
                    if event.source == "bridge" and event.kind == "task.execution_claimed"
                ),
                None,
            ),
            "cancel_requested": any(
                event.source == "bridge" and event.kind == "task.cancel_requested"
                for event in events
            ),
            "evidence_event_ids": [
                item["event_id"] for item in relevant if item["event_id"] is not None
            ],
            "evidence_fingerprint": hashlib.sha256(encoded).hexdigest(),
        }

    def recover_orphaned_tasks(self) -> list[Task]:
        """Reconcile RUNNING tasks conservatively after worker loss."""

        recovered: list[Task] = []
        for task in self.store.list_tasks_by_execution_status(ExecutionStatus.RUNNING):
            events = self.store.list_task_events(task.task_id)
            classification = self._recovery_classification(task, events)
            if classification is not None:
                status_value, payload = classification
                event_kind = {
                    ExecutionStatus.FAILED.value: "task.failed",
                    ExecutionStatus.CANCELLED.value: "task.cancelled",
                }[status_value]
                updated, _ = self.store.transition_task_recovered_terminal(
                    task.task_id,
                    execution_status=status_value,
                    event_kind=event_kind,
                    payload=payload,
                    recovery_payload={
                        "previous_status": ExecutionStatus.RUNNING.value,
                        "reason": "durable_codex_terminal_evidence",
                        "evidence_event_ids": payload.get("evidence_event_ids", []),
                    },
                )
                recovered.append(updated)
                continue
            _, marker, _ = self.store.ensure_reconciliation_required(
                task.task_id, self._reconciliation_payload(task, events)
            )
            updated = self.store.get_task(task.task_id)
            if updated is not None:
                recovered.append(updated)
        return recovered

    def resolve_task_reconciliation(
        self,
        task_id: str,
        reconciliation_id: str,
        resolution: str,
    ) -> dict[str, Any]:
        """Resolve an unknown execution outcome to FAILED, and nothing else."""

        task = self.store.get_task(task_id)
        if task is None:
            raise KeyError(f"task does not exist: {task_id}")
        if resolution != ExecutionStatus.FAILED.value:
            raise ValueError("only FAILED reconciliation resolution is supported")
        events = self.store.list_task_events(task_id)
        claim = next(
            (
                event.payload
                for event in reversed(events)
                if event.source == "bridge" and event.kind == "task.execution_claimed"
            ),
            None,
        )
        cancel_request = self.store.get_cancellation_request(task_id)
        payload = {
            "reconciliation_id": reconciliation_id,
            "thread_id": task.thread_id,
            "turn_id": task.turn_id,
            "claim": claim,
            "cancel_requested": cancel_request is not None,
            "cancel_request_id": self._request_id_from_event(cancel_request),
            "evidence_event_ids": [
                event.event_id for event in events if event.event_id is not None
            ],
            "resolver": "mcp",
        }
        return self.store.resolve_task_reconciliation(
            task_id,
            reconciliation_id,
            resolution=resolution,
            payload=payload,
        )

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
        """Create or forward-repair one durable local checkpoint attempt."""

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

        created_events = [
            event
            for event in events
            if event.source == "bridge" and event.kind == CHECKPOINT_CREATED_EVENT
        ]
        if created_events:
            payload = created_events[-1].payload
            if isinstance(payload, dict):
                return dict(payload)
            raise CheckpointCommitError("stored checkpoint.created payload is invalid")
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

        started_events = [
            event
            for event in events
            if event.source == "bridge" and event.kind == CHECKPOINT_STARTED_EVENT
        ]
        failed_events = [
            event
            for event in events
            if event.source == "bridge" and event.kind == CHECKPOINT_FAILED_EVENT
        ]
        prepared: PreparedCheckpoint

        def persist_attempt_failed(
            payload: Mapping[str, Any] | None,
            *,
            reason: str,
            error: BaseException,
        ) -> None:
            """Best-effort durable classification for pre-CAS failures.

            STARTED is intentionally the authority before CAS.  If its
            rehydration or an early retry probe fails, retain that attempt's
            identity and classify it once instead of leaving an unbounded
            STARTED-only record.  A persistence failure is left to the next
            retry, which can still use the immutable STARTED payload.
            """

            normalized = dict(payload) if isinstance(payload, Mapping) else {}
            attempt_id = normalized.get("attempt_id")
            normalized.update(
                {
                    "phase": CHECKPOINT_PHASE_PRE_CAS,
                    "classification": "FAILED_PRE_CAS",
                    "reason": reason,
                    "error_type": type(error).__name__,
                    "message": str(error)[:500],
                }
            )
            try:
                with self.store.immediate_transaction() as connection:
                    existing = connection.execute(
                        "SELECT payload_json FROM task_events "
                        "WHERE task_id = ? AND source = 'bridge' AND kind = ?",
                        (task_id, CHECKPOINT_FAILED_EVENT),
                    ).fetchall()
                    for row in existing:
                        try:
                            existing_payload = json.loads(row["payload_json"])
                        except (TypeError, ValueError, json.JSONDecodeError):
                            continue
                        if (
                            isinstance(existing_payload, Mapping)
                            and existing_payload.get("attempt_id") == attempt_id
                        ):
                            return
                    self.store.insert_task_event_in_transaction(
                        connection,
                        task_id,
                        "bridge",
                        CHECKPOINT_FAILED_EVENT,
                        normalized,
                    )
            except Exception:
                # The durable STARTED record remains available for a bounded
                # retry; never mask the original pre-CAS failure here.
                pass

        if started_events:
            started_payload = started_events[-1].payload
            if not isinstance(started_payload, Mapping):
                error = CheckpointCommitError(
                    "stored checkpoint.started payload is invalid"
                )
                persist_attempt_failed(
                    None, reason="STARTED_PAYLOAD_INVALID", error=error
                )
                raise error
            attempt_id = started_payload.get("attempt_id")
            matching_failure = next(
                (
                    event
                    for event in reversed(failed_events)
                    if isinstance(event.payload, Mapping)
                    and event.payload.get("attempt_id") == attempt_id
                ),
                None,
            )
            if matching_failure is not None:
                raise CheckpointCommitError(
                    "checkpoint attempt is terminally classified before CAS"
                )
            try:
                prepared = _h4_rehydrate_prepared(started_payload)
            except BaseException as error:
                persist_attempt_failed(
                    started_payload,
                    reason="STARTED_REHYDRATION_FAILED",
                    error=error,
                )
                raise
        else:
            # Capture the task high-water before STARTED.  The immediate
            # transaction below revalidates both values immediately before CAS.
            connection = self.store.connection
            latest_id, latest_creation_id, high_water = (
                self.store.latest_task_identity_in_transaction(
                    connection, task.project_id
                )
            )
            prepared = git_checkpoint_prepare(
                safe_repo,
                postflight=postflight,
                message=message,
                task_id=task.task_id,
                project_id=project.project_id,
            )
            prepared.snapshot["latest_task_identity"] = {
                "task_id": latest_id,
                "creation_event_id": latest_creation_id,
            }
            prepared.snapshot["task_event_high_water"] = high_water
            prepared.snapshot["task_execution_status"] = task.execution_status.value
            prepared.snapshot["task_audit_status"] = task.audit_status.value
            try:
                self.store.append_task_event(
                    task.task_id,
                    "bridge",
                    CHECKPOINT_STARTED_EVENT,
                    prepared.started_payload(),
                )
            except BaseException:
                prepared.cleanup()
                raise

        def reject_incompatible_attempt(events_to_check: list[Any]) -> None:
            for event in events_to_check:
                if event.source != "bridge" or event.kind not in {
                    CHECKPOINT_STARTED_EVENT,
                    CHECKPOINT_REF_UPDATED_EVENT,
                    CHECKPOINT_CREATED_EVENT,
                }:
                    continue
                payload = event.payload
                if not isinstance(payload, Mapping):
                    raise CheckpointCommitError(
                        "stored checkpoint attempt payload is invalid"
                    )
                event_attempt = payload.get("attempt_id")
                if event_attempt != prepared.attempt_id:
                    raise CheckpointCommitError(
                        "another checkpoint attempt exists for this task"
                    )

        # A concurrent caller may have appended a different STARTED attempt
        # between our initial read and candidate preparation.  Do not let
        # either attempt silently incorporate the other's authorization.
        try:
            reject_incompatible_attempt(self.store.list_task_events(task_id))
        except BaseException as error:
            persist_attempt_failed(
                prepared.started_payload(),
                reason="CHECKPOINT_INCOMPATIBLE",
                error=error,
            )
            prepared.cleanup()
            raise

        attempt_payload = {
            "attempt_id": prepared.attempt_id,
            "snapshot_id": prepared.snapshot_id,
            "task_id": task.task_id,
            "project_id": project.project_id,
            "branch_ref": prepared.branch_ref,
            "expected_head": prepared.expected_head,
            "candidate_commit": prepared.candidate_commit,
            "candidate_parent": prepared.candidate_parent,
            "candidate_tree": prepared.candidate_tree,
            "paths": list(prepared.paths),
            "message_digest": prepared.message_digest,
            "task_execution_status": prepared.snapshot.get("task_execution_status"),
            "task_audit_status": prepared.snapshot.get("task_audit_status"),
        }

        def persist_created(finalization: Any) -> dict[str, Any]:
            payload = {
                "task_id": task.task_id,
                "project_id": project.project_id,
                "phase": CHECKPOINT_PHASE_CREATED,
                "previous_head": prepared.expected_head,
                "commit_head": prepared.candidate_commit,
                "branch": prepared.branch,
                "message": prepared.message,
                "paths": list(prepared.paths),
                "clean": finalization.clean,
                "attempt_id": prepared.attempt_id,
                "snapshot_id": prepared.snapshot_id,
                "commit_created": True,
                "finalization_status": finalization.finalization_status,
                "post_state": finalization.finalization_status,
                "conflict": finalization.conflict,
                "repaired": bool(started_events),
            }
            try:
                self.store.append_task_event(
                    task_id,
                    "bridge",
                    CHECKPOINT_CREATED_EVENT,
                    payload,
                )
            except Exception as error:
                raise CheckpointCommitError(
                    "checkpoint commit created but durable created event failed"
                ) from error
            return payload

        if started_events:
            try:
                relation, _ = git_checkpoint_head_relation(prepared)
            except BaseException as error:
                persist_attempt_failed(
                    attempt_payload,
                    reason="CHECKPOINT_OUTCOME_NOT_PROVABLE",
                    error=error,
                )
                prepared.cleanup()
                raise
            if relation in {"candidate", "descendant"}:
                try:
                    return persist_created(git_checkpoint_finalize(prepared))
                finally:
                    prepared.cleanup()
            if relation == "unknown":
                try:
                    with self.store.immediate_transaction() as connection:
                        self.store.insert_task_event_in_transaction(
                            connection,
                            task_id,
                            "bridge",
                            CHECKPOINT_FAILED_EVENT,
                            {
                                **attempt_payload,
                                "phase": CHECKPOINT_PHASE_PRE_CAS,
                                "classification": "FAILED_PRE_CAS",
                                "reason": "CHECKPOINT_OUTCOME_NOT_PROVABLE",
                            },
                        )
                finally:
                    prepared.cleanup()
                raise CheckpointCommitError("checkpoint outcome is not provable")

        cas_completed = False
        try:
            failure: CheckpointPreconditionError | None = None
            try:
                with self.store.immediate_transaction() as connection:
                    try:
                        reject_incompatible_attempt(self.store.list_task_events(task_id))
                    except CheckpointCommitError as error:
                        failure = CheckpointPreconditionError(
                            str(error), reason="CHECKPOINT_INCOMPATIBLE"
                        )
                    if failure is None:
                        current = self.store.get_task(task_id)
                        if current is None:
                            raise PolicyError(f"task does not exist: {task_id}")
                        if current.execution_status is not ExecutionStatus.FINISHED:
                            raise CheckpointPreconditionError(
                                "task changed before checkpoint CAS",
                                reason="TASK_STATE_CHANGED",
                            )
                        if current.audit_status is AuditStatus.CORRECTION_REQUIRED:
                            raise CheckpointPreconditionError(
                                "audit correction appeared before checkpoint CAS",
                                reason="AUDIT_CORRECTION_REQUIRED",
                            )
                        latest_id, latest_creation_id, high_water = (
                            self.store.latest_task_identity_in_transaction(
                                connection, project.project_id
                            )
                        )
                        expected_latest = prepared.snapshot.get("latest_task_identity")
                        expected_high_water = prepared.snapshot.get("task_event_high_water")
                        if (
                            latest_id != task.task_id
                            or (
                                isinstance(expected_latest, Mapping)
                                and (
                                    expected_latest.get("task_id") != latest_id
                                    or expected_latest.get("creation_event_id")
                                    != latest_creation_id
                                )
                            )
                            or (
                                expected_high_water is not None
                                and high_water != expected_high_water
                            )
                        ):
                            failure = CheckpointPreconditionError(
                                "checkpoint task is no longer the latest applicable task",
                                reason="LATEST_TASK_CHANGED",
                            )
                        else:
                            try:
                                git_checkpoint_cas(prepared)
                                cas_completed = True
                                self.store.insert_task_event_in_transaction(
                                    connection,
                                    task_id,
                                    "bridge",
                                    CHECKPOINT_REF_UPDATED_EVENT,
                                    {
                                        **attempt_payload,
                                        "phase": CHECKPOINT_PHASE_REF_UPDATED,
                                        "cas_result": "success",
                                    },
                                )
                            except CheckpointPreconditionError as error:
                                failure = error
                            except CheckpointCommitError as error:
                                failure = CheckpointPreconditionError(
                                    str(error), reason="PRE_CAS_VALIDATION_FAILED"
                                )
                    if failure is not None:
                        self.store.insert_task_event_in_transaction(
                            connection,
                            task_id,
                            "bridge",
                            CHECKPOINT_FAILED_EVENT,
                            {
                                **attempt_payload,
                                "phase": CHECKPOINT_PHASE_PRE_CAS,
                                "classification": "FAILED_PRE_CAS",
                                "reason": failure.reason,
                            },
                        )
            except Exception as error:
                if cas_completed:
                    raise CheckpointCommitError(
                        "CAS succeeded but SQLite could not commit ref_updated; forward repair required"
                    ) from error
                if isinstance(error, sqlite3.OperationalError):
                    raise CheckpointCommitError(
                        "SQLite writer lock could not be acquired within the configured timeout"
                    ) from error
                raise
            if failure is not None:
                raise failure

            return persist_created(git_checkpoint_finalize(prepared))
        finally:
            prepared.cleanup()

    async def _cancel_active_execution(
        self,
        *,
        thread_id: str | None = None,
        turn_id: str | None = None,
    ) -> bool:
        """Ask the injected executor to interrupt its current operation.

        The optional correlation IDs let a worker pass the durable target
        without making Core depend on app-server protocol details.  Legacy
        test/executor implementations with a zero-argument ``cancel_active``
        remain supported.
        """

        if self.executor is None:
            return False
        cancel_active = getattr(self.executor, "cancel_active", None)
        if not callable(cancel_active):
            return False
        kwargs: dict[str, Any] = {}
        try:
            parameters = inspect.signature(cancel_active).parameters
        except (TypeError, ValueError):
            parameters = {}
        if "thread_id" in parameters or any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        ):
            kwargs["thread_id"] = thread_id
        if "turn_id" in parameters or any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        ):
            kwargs["turn_id"] = turn_id
        result = cancel_active(**kwargs) if kwargs else cancel_active()
        if inspect.isawaitable(result):
            result = await result
        # A successful call with no explicit boolean is still evidence that
        # the executor accepted the dispatch; terminality is decided later by
        # the executor's confirmed result, never by this return value alone.
        return result is not False

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
            if result.status is ExecutionStatus.CANCELLED:
                cancellation = self.store.get_cancellation_request(task_id)
                if cancellation is None:
                    raise RuntimeError(
                        "executor returned CANCELLED without a durable cancellation request"
                    )
                if not self._has_cancellation_confirmation(
                    task_id,
                    thread_id=result.thread_id,
                    turn_id=result.turn_id,
                ):
                    raise RuntimeError(
                        "executor returned CANCELLED without interrupt confirmation"
                    )
                cancellation_payload: dict[str, Any] = {
                    "status": ExecutionStatus.CANCELLED.value,
                    "reason": "cancel request confirmed by executor",
                    "requested_via": "cancel_task",
                    "cancel_request_id": self._request_id_from_event(cancellation),
                    "thread_id": result.thread_id,
                    "turn_id": result.turn_id,
                }
                if result.final_response is not None:
                    cancellation_payload["final_response"] = _bounded_final_response(
                        result.final_response
                    )
                return self.store.transition_task_terminal(
                    task_id,
                    execution_status=ExecutionStatus.CANCELLED,
                    event_kind="task.cancelled",
                    payload=cancellation_payload,
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


__all__ = [
    "BridgeCore",
    "CancellationDispatch",
    "ExecutionDispatch",
    "TaskStateError",
]
