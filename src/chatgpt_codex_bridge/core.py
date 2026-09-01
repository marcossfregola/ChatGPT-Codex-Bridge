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
    git_postflight_complete,
    git_preflight,
    postflight_payload,
    TRUNCATION_SENTINEL,
    WORKING_TREE_FINGERPRINT_VERSION,
    validate_continuation_snapshot,
)
from .persistence.sqlite_store import (
    CHECKPOINT_CREATED_EVENT,
    CHECKPOINT_FAILED_EVENT,
    CHECKPOINT_REF_UPDATED_EVENT,
    CHECKPOINT_STARTED_EVENT,
    D3_H3_CONTRACT,
    D3_R2_CONTRACT,
    RECONCILIATION_BASELINE_ADOPTED_EVENT,
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
RECONCILIATION_BASELINE_SCHEMA_VERSION = 1
RECONCILIATION_BASELINE_KIND = "reconciled_continuation"
ADOPTION_MODE_LEGACY = "legacy"
ADOPTION_MODE_DIRECT = "direct"
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

    @staticmethod
    def _normalize_adoption_mode(mode: str | None) -> str:
        """Normalize the explicit baseline-adoption mode fail-closed."""

        if mode is None:
            return ADOPTION_MODE_LEGACY
        if not isinstance(mode, str) or mode not in {
            ADOPTION_MODE_LEGACY,
            ADOPTION_MODE_DIRECT,
        }:
            raise ContinuationBaselineError("adoption mode is unsupported")
        return mode

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

    @staticmethod
    def _snapshot_error_is_incomplete(
        error: ContinuationBaselineError,
        payload: Mapping[str, Any] | None = None,
        *,
        expected_repo: str | None = None,
        expected_branch: str | None = None,
        expected_head: str | None = None,
    ) -> bool:
        """Return true only for a structurally valid snapshot with real truncation.

        The exception is deliberately ignored.  Recovery eligibility comes only
        from the postflight payload and the official Bridge sentinel; generic
        validation-error wording is never treated as evidence of truncation.
        """

        del error
        if not isinstance(payload, Mapping):
            return False

        sanitized = dict(payload)
        found_sentinel = False

        for key in ("status_porcelain", "diff", "cached_diff"):
            value = payload.get(key)
            if not isinstance(value, str):
                continue
            if TRUNCATION_SENTINEL not in value:
                continue
            if (
                not value.endswith(TRUNCATION_SENTINEL)
                or value.count(TRUNCATION_SENTINEL) != 1
            ):
                return False
            sanitized[key] = value[: -len(TRUNCATION_SENTINEL)]
            found_sentinel = True

        for key in ("changed_files", "untracked_files"):
            value = payload.get(key)
            if not isinstance(value, list):
                continue
            sentinel_indexes = [
                index
                for index, item in enumerate(value)
                if item == TRUNCATION_SENTINEL
            ]
            if sentinel_indexes and sentinel_indexes != [len(value) - 1]:
                return False
            if any(
                isinstance(item, str) and TRUNCATION_SENTINEL in item
                and item != TRUNCATION_SENTINEL
                for item in value
            ):
                return False
            if sentinel_indexes:
                sanitized[key] = value[:-1]
                found_sentinel = True

        raw_fingerprints = payload.get("content_fingerprints")
        if isinstance(raw_fingerprints, list):
            sentinel_indexes: list[int] = []
            for index, item in enumerate(raw_fingerprints):
                if isinstance(item, Mapping):
                    path = item.get("path")
                    if isinstance(path, str) and TRUNCATION_SENTINEL in path:
                        if item != {
                            "path": TRUNCATION_SENTINEL,
                            "state": "",
                            "sha256": "",
                        }:
                            return False
                        sentinel_indexes.append(index)
                    if any(
                        isinstance(value, str)
                        and TRUNCATION_SENTINEL in value
                        and not (
                            path == TRUNCATION_SENTINEL
                            and item == {
                                "path": TRUNCATION_SENTINEL,
                                "state": "",
                                "sha256": "",
                            }
                        )
                        for value in item.values()
                    ):
                        return False
                elif isinstance(item, (list, tuple)):
                    if any(
                        isinstance(value, str) and TRUNCATION_SENTINEL in value
                        for value in item
                    ):
                        return False
                elif isinstance(item, str) and TRUNCATION_SENTINEL in item:
                    return False
            if sentinel_indexes and sentinel_indexes != [len(raw_fingerprints) - 1]:
                return False
            if sentinel_indexes:
                sanitized["content_fingerprints"] = raw_fingerprints[:-1]
                found_sentinel = True

        # The historical postflight serializer never emits a truncation marker
        # in the legacy untracked-fingerprint field.  Any marker there is
        # therefore malformed evidence, not an adoption trigger.
        raw_untracked_fingerprints = payload.get("untracked_fingerprints")
        if isinstance(raw_untracked_fingerprints, list):
            for item in raw_untracked_fingerprints:
                values = item.values() if isinstance(item, Mapping) else item
                if isinstance(values, (list, tuple)) and any(
                    isinstance(value, str) and TRUNCATION_SENTINEL in value
                    for value in values
                ):
                    return False

        for key in (
            "repo_path",
            "baseline_branch",
            "baseline_head",
            "final_branch",
            "final_head",
        ):
            value = payload.get(key)
            if (
                not isinstance(value, str)
                or not value
                or TRUNCATION_SENTINEL in value
            ):
                return False

        if not found_sentinel:
            return False
        try:
            validate_continuation_snapshot(
                sanitized,
                expected_repo=expected_repo,
                expected_branch=expected_branch,
                expected_head=expected_head,
            )
        except ContinuationBaselineError:
            return False
        return True

    @staticmethod
    def _canonical_path_key(value: str) -> str:
        """Normalize a validated repository path for platform comparison."""

        return str(value).replace("/", "\\").rstrip("\\").casefold()

    @staticmethod
    def _event_id(event: Any) -> int:
        value = getattr(event, "event_id", None)
        return value if isinstance(value, int) and value > 0 else 0

    @staticmethod
    def _preflight_only_failure(task: Task, events: list[Any]) -> bool:
        """Return whether an AW failure has only an explicit preflight marker."""

        if task.execution_status is not ExecutionStatus.FAILED:
            return False
        if any(
            event.source == "bridge" and event.kind == "policy.postflight"
            for event in events
        ):
            return False
        return any(
            event.source == "bridge"
            and event.kind == "policy.violation"
            and isinstance(event.payload, Mapping)
            and event.payload.get("phase") == "preflight"
            for event in events
        )

    def _assert_source_is_latest_continuation_candidate(
        self,
        source_task: Task,
        source_postflight_event_id: int,
        *,
        excluded_task_id: str | None = None,
    ) -> None:
        """Reject a source that is hidden by a newer autonomous execution."""

        for candidate in self.store.list_tasks(source_task.project_id):
            if (
                candidate.task_id == source_task.task_id
                or (
                    excluded_task_id is not None
                    and candidate.task_id == excluded_task_id
                )
                or candidate.project_id != source_task.project_id
                or candidate.mode is not TaskMode.AUTONOMOUS_WRITE
            ):
                continue
            events = self.store.list_task_events(candidate.task_id)
            postflights = [
                event
                for event in events
                if event.source == "bridge" and event.kind == "policy.postflight"
            ]
            adoptions = [
                event
                for event in events
                if event.source == "bridge"
                and event.kind == RECONCILIATION_BASELINE_ADOPTED_EVENT
            ]
            violations = [
                event
                for event in events
                if event.source == "bridge" and event.kind == "policy.violation"
            ]
            latest_relevant = max(
                [self._event_id(event) for event in (*postflights, *adoptions, *violations)],
                default=0,
            )
            if postflights:
                latest_relevant = max(
                    latest_relevant,
                    self._event_id(max(postflights, key=self._event_id)),
                )
                if latest_relevant > source_postflight_event_id:
                    raise ContinuationBaselineError(
                        "source task is not the latest continuation candidate"
                    )
                # A non-final autonomous task with a later journal entry is a
                # fail-closed barrier even if its postflight is historical.
                if (
                    candidate.execution_status is not ExecutionStatus.FINISHED
                    and max((self._event_id(event) for event in events), default=0)
                    > source_postflight_event_id
                ):
                    raise ContinuationBaselineError(
                        "source task is not the latest continuation candidate"
                    )
                continue
            if adoptions:
                if max(self._event_id(event) for event in adoptions) > source_postflight_event_id:
                    raise ContinuationBaselineError(
                        "source task is not the latest continuation candidate"
                    )
                continue
            if self._preflight_only_failure(candidate, events):
                continue
            if candidate.execution_status is ExecutionStatus.QUEUED:
                continue
            if max((self._event_id(event) for event in events), default=0) > source_postflight_event_id:
                raise ContinuationBaselineError(
                    "source task is not the latest continuation candidate"
                )

    def _validated_baseline_adoption_context(
        self,
        source_task: Task,
        inspection_task_id: str | None,
        *,
        adoption_mode: str = ADOPTION_MODE_LEGACY,
        excluded_task_id: str | None = None,
    ) -> dict[str, Any]:
        """Validate source and inspection provenance for explicit adoption."""

        adoption_mode = self._normalize_adoption_mode(adoption_mode)

        if source_task.mode is not TaskMode.AUTONOMOUS_WRITE:
            raise ContinuationBaselineError(
                "reconciliation baseline source must be AUTONOMOUS_WRITE"
            )
        if source_task.execution_status is not ExecutionStatus.FINISHED:
            raise ContinuationBaselineError(
                "reconciliation baseline source must be FINISHED"
            )
        if adoption_mode == ADOPTION_MODE_LEGACY and (
            not isinstance(inspection_task_id, str) or not inspection_task_id.strip()
        ):
            raise ContinuationBaselineError("inspection_task_id must be non-empty text")
        if adoption_mode == ADOPTION_MODE_DIRECT and inspection_task_id is not None:
            raise ContinuationBaselineError(
                "direct adoption cannot include inspection_task_id"
            )

        project = self.store.get_project(source_task.project_id)
        if project is None:
            raise ContinuationBaselineError("source project does not exist")
        project_root = ensure_autonomous_workspace(project.repo_path)

        source_events = self.store.list_task_events(source_task.task_id)
        if any(
            event.source == "bridge" and event.kind == "policy.violation"
            for event in source_events
        ):
            raise ContinuationBaselineError("source task has a policy violation")
        postflight_events = [
            event
            for event in source_events
            if event.source == "bridge" and event.kind == "policy.postflight"
        ]
        if not postflight_events:
            raise ContinuationBaselineError("source task has no policy.postflight")
        postflight_event = max(postflight_events, key=self._event_id)
        postflight_event_id = self._event_id(postflight_event)
        if not postflight_event_id or not isinstance(postflight_event.payload, Mapping):
            raise ContinuationBaselineError("source policy.postflight evidence is invalid")
        source_terminal = max(
            (
                event
                for event in source_events
                if event.source == "bridge" and event.kind == "task.finished"
            ),
            key=self._event_id,
            default=None,
        )
        if (
            source_terminal is None
            or self._event_id(source_terminal) <= postflight_event_id
        ):
            raise ContinuationBaselineError(
                "source terminal evidence is not posterior to policy.postflight"
            )
        if (
            isinstance(source_terminal.payload, Mapping)
            and source_terminal.payload.get("policy_violation") is True
        ):
            raise ContinuationBaselineError("source terminal evidence has a policy violation")
        source_payload = dict(postflight_event.payload)
        if source_payload.get("policy_violation") is not False:
            raise ContinuationBaselineError("source policy.postflight has a policy violation")
        identity_fields = (
            "repo_path",
            "baseline_branch",
            "baseline_head",
            "final_branch",
            "final_head",
        )
        for key in identity_fields:
            value = source_payload.get(key)
            if not isinstance(value, str) or not value.strip() or "[TRUNCATED]" in value:
                raise ContinuationBaselineError(
                    f"source policy.postflight {key} identity is incomplete"
                )
        if (
            source_payload["final_branch"] != source_payload["baseline_branch"]
            or source_payload["final_head"] != source_payload["baseline_head"]
        ):
            raise ContinuationBaselineError(
                "source policy.postflight branch or HEAD changed"
            )
        source_root = ensure_autonomous_workspace(source_payload["repo_path"])
        if self._canonical_path_key(str(source_root)) != self._canonical_path_key(
            str(project_root)
        ):
            raise ContinuationBaselineError("source policy.postflight repo path differs")

        if adoption_mode == ADOPTION_MODE_DIRECT:
            fingerprint_present = (
                "working_tree_fingerprint_version" in source_payload
                or "working_tree_fingerprint" in source_payload
            )
            if not fingerprint_present:
                raise ContinuationBaselineError(
                    "direct adoption requires working-tree fingerprint v1"
                )
            normalized_source = validate_continuation_snapshot(
                source_payload,
                expected_repo=project.repo_path,
                expected_branch=source_payload["baseline_branch"],
                expected_head=source_payload["baseline_head"],
                allow_fingerprint_truncation=True,
            )
            if (
                isinstance(
                    normalized_source.get("working_tree_fingerprint_version"), bool
                )
                or not isinstance(
                    normalized_source.get("working_tree_fingerprint_version"), int
                )
                or normalized_source.get("working_tree_fingerprint_version")
                != WORKING_TREE_FINGERPRINT_VERSION
                or not isinstance(
                    normalized_source.get("working_tree_fingerprint"), str
                )
            ):
                raise ContinuationBaselineError(
                    "direct adoption requires working-tree fingerprint v1"
                )

            self._assert_source_is_latest_continuation_candidate(
                source_task,
                postflight_event_id,
                excluded_task_id=excluded_task_id,
            )
            return {
                "adoption_mode": adoption_mode,
                "project": project,
                "source_events": source_events,
                "inspection_events": [],
                "source_postflight": postflight_event,
                "source_postflight_payload": source_payload,
                "source_postflight_validation_error": None,
                "inspection_task": None,
                "inspection_terminal": None,
                "checkpoint": GitCheckpoint(
                    repo_path=str(source_root),
                    baseline_branch=source_payload["baseline_branch"],
                    baseline_head=source_payload["baseline_head"],
                    status_porcelain="",
                    staged_paths=(),
                    unstaged_paths=(),
                    untracked_paths=(),
                    baseline_kind=RECONCILIATION_BASELINE_KIND,
                ),
                "source_high_water": max(
                    (self._event_id(event) for event in source_events), default=0
                ),
                "inspection_high_water": 0,
            }

        try:
            validate_continuation_snapshot(
                source_payload,
                expected_repo=project.repo_path,
                expected_branch=source_payload["baseline_branch"],
                expected_head=source_payload["baseline_head"],
            )
        except ContinuationBaselineError as error:
            if not self._snapshot_error_is_incomplete(
                error,
                source_payload,
                expected_repo=project.repo_path,
                expected_branch=source_payload["baseline_branch"],
                expected_head=source_payload["baseline_head"],
            ):
                raise
            source_validation_error = error
        else:
            raise ContinuationBaselineError(
                "source policy.postflight evidence is already complete"
            )

        self._assert_source_is_latest_continuation_candidate(
            source_task,
            postflight_event_id,
            excluded_task_id=excluded_task_id,
        )

        inspection_task = self.store.get_task(inspection_task_id)
        if inspection_task is None:
            raise KeyError(f"task does not exist: {inspection_task_id}")
        if inspection_task.mode is not TaskMode.READ_ONLY:
            raise ContinuationBaselineError("inspection task must be READ_ONLY")
        if inspection_task.execution_status is not ExecutionStatus.FINISHED:
            raise ContinuationBaselineError("inspection task must be FINISHED")
        if inspection_task.project_id != source_task.project_id:
            raise ContinuationBaselineError(
                "inspection task belongs to another project"
            )
        inspection_project = self.store.get_project(inspection_task.project_id)
        if inspection_project is None:
            raise ContinuationBaselineError("inspection project does not exist")
        inspection_root = ensure_autonomous_workspace(inspection_project.repo_path)
        if self._canonical_path_key(str(inspection_root)) != self._canonical_path_key(
            str(project_root)
        ):
            raise ContinuationBaselineError("inspection repository differs")
        inspection_events = self.store.list_task_events(inspection_task_id)
        if any(
            event.source == "bridge" and event.kind == "policy.violation"
            for event in inspection_events
        ):
            raise ContinuationBaselineError("inspection task has a policy violation")
        inspection_terminal = max(
            (
                event
                for event in inspection_events
                if event.source == "bridge" and event.kind == "task.finished"
            ),
            key=self._event_id,
            default=None,
        )
        inspection_terminal_event_id = self._event_id(inspection_terminal)
        if (
            inspection_terminal is None
            or not inspection_terminal_event_id
            or inspection_terminal_event_id <= postflight_event_id
        ):
            raise ContinuationBaselineError(
                "inspection terminal evidence is not posterior to source postflight"
            )
        if (
            isinstance(inspection_terminal.payload, Mapping)
            and inspection_terminal.payload.get("policy_violation") is True
        ):
            raise ContinuationBaselineError(
                "inspection terminal evidence has a policy violation"
            )

        checkpoint = GitCheckpoint(
            repo_path=str(source_root),
            baseline_branch=source_payload["baseline_branch"],
            baseline_head=source_payload["baseline_head"],
            status_porcelain="",
            staged_paths=(),
            unstaged_paths=(),
            untracked_paths=(),
            baseline_kind="reconciled_continuation",
        )
        return {
            "adoption_mode": adoption_mode,
            "project": project,
            "source_events": source_events,
            "inspection_events": inspection_events,
            "source_postflight": postflight_event,
            "source_postflight_payload": source_payload,
            "source_postflight_validation_error": source_validation_error,
            "inspection_task": inspection_task,
            "inspection_terminal": inspection_terminal,
            "checkpoint": checkpoint,
            "source_high_water": max(
                (self._event_id(event) for event in source_events), default=0
            ),
            "inspection_high_water": max(
                (self._event_id(event) for event in inspection_events), default=0
            ),
        }

    @staticmethod
    def _baseline_adoption_fingerprint(
        *,
        source_task_id: str,
        source_postflight_event_id: int,
        inspection_task_id: str | None,
        inspection_terminal_event_id: int | None,
        project_id: str,
        source_high_water_event_id: int,
        inspection_high_water_event_id: int,
        snapshot: Mapping[str, Any],
        adoption_mode: str = ADOPTION_MODE_LEGACY,
    ) -> str:
        """Hash all durable adoption provenance and the complete Git snapshot."""

        material = {
            "schema_version": RECONCILIATION_BASELINE_SCHEMA_VERSION,
            "baseline_kind": RECONCILIATION_BASELINE_KIND,
            "source_task_id": source_task_id,
            "source_postflight_event_id": source_postflight_event_id,
            "inspection_task_id": inspection_task_id,
            "inspection_terminal_event_id": inspection_terminal_event_id,
            "project_id": project_id,
            "source_high_water_event_id": source_high_water_event_id,
            "inspection_high_water_event_id": inspection_high_water_event_id,
            "snapshot": snapshot,
        }
        if not isinstance(adoption_mode, str) or adoption_mode not in {
            ADOPTION_MODE_LEGACY,
            ADOPTION_MODE_DIRECT,
        }:
            raise ContinuationBaselineError("adoption mode is unsupported")
        # Legacy payloads predate adoption_mode.  Keep their fingerprint
        # material byte-for-byte compatible while binding the new direct mode
        # explicitly in every direct adoption fingerprint.
        if adoption_mode == ADOPTION_MODE_DIRECT:
            material["adoption_mode"] = ADOPTION_MODE_DIRECT
        encoded = json.dumps(
            material,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _capture_reconciled_snapshot(
        self, checkpoint: GitCheckpoint, project: Project
    ) -> dict[str, Any]:
        """Capture one complete, branch-stable Git snapshot for adoption."""

        try:
            postflight = git_postflight_complete(checkpoint)
            payload = postflight_payload(postflight)
            return validate_continuation_snapshot(
                payload,
                expected_repo=project.repo_path,
                expected_branch=checkpoint.baseline_branch,
                expected_head=checkpoint.baseline_head,
            )
        except ContinuationBaselineError:
            raise
        except Exception as error:
            raise ContinuationBaselineError(
                "Git state could not be captured for reconciliation adoption"
            ) from error

    def adopt_reconciled_continuation_baseline(
        self,
        source_task_id: str,
        inspection_task_id: str | None = None,
        *,
        mode: str | None = None,
    ) -> dict[str, Any]:
        """Adopt two identical, Bridge-captured Git snapshots explicitly.

        ``mode`` defaults to the established inspection-backed flow.  The
        direct mode is an explicit administrative invocation over the same
        durable adoption event; it never infers authorization from a dirty
        worktree.
        """

        adoption_mode = self._normalize_adoption_mode(mode)
        if adoption_mode == ADOPTION_MODE_DIRECT and inspection_task_id is not None:
            raise ContinuationBaselineError(
                "direct adoption cannot include inspection_task_id"
            )
        source_task = self.store.get_task(source_task_id)
        if source_task is None:
            raise KeyError(f"task does not exist: {source_task_id}")
        context = self._validated_baseline_adoption_context(
            source_task,
            inspection_task_id,
            adoption_mode=adoption_mode,
        )
        project = context["project"]
        checkpoint = context["checkpoint"]

        first_snapshot = self._capture_reconciled_snapshot(checkpoint, project)
        second_snapshot = self._capture_reconciled_snapshot(checkpoint, project)
        if first_snapshot != second_snapshot:
            raise ContinuationBaselineError(
                "Git state changed during reconciliation baseline adoption"
            )
        if adoption_mode == ADOPTION_MODE_DIRECT and (
            isinstance(
                first_snapshot.get("working_tree_fingerprint_version"), bool
            )
            or not isinstance(
                first_snapshot.get("working_tree_fingerprint_version"), int
            )
            or first_snapshot.get("working_tree_fingerprint_version")
            != WORKING_TREE_FINGERPRINT_VERSION
            or not isinstance(first_snapshot.get("working_tree_fingerprint"), str)
        ):
            raise ContinuationBaselineError(
                "direct adoption requires working-tree fingerprint v1"
            )

        source_postflight_event_id = self._event_id(context["source_postflight"])
        inspection_terminal_event_id = (
            None
            if adoption_mode == ADOPTION_MODE_DIRECT
            else self._event_id(context["inspection_terminal"])
        )
        source_high_water = context["source_high_water"]
        inspection_high_water = context["inspection_high_water"]
        # A retry must reproduce the exact original durable payload.  Reuse
        # the original high-water marks rather than making a second event look
        # different merely because the first adoption is now in the journal.
        existing_adoptions = [
            event
            for event in context["source_events"]
            if event.source == "bridge"
            and event.kind == RECONCILIATION_BASELINE_ADOPTED_EVENT
        ]
        if len(existing_adoptions) == 1 and isinstance(
            existing_adoptions[0].payload, Mapping
        ):
            existing_payload = existing_adoptions[0].payload
            for key, fallback in (
                ("source_high_water_event_id", source_high_water),
                ("inspection_high_water_event_id", inspection_high_water),
            ):
                value = existing_payload.get(key)
                if isinstance(value, int) and value > 0:
                    if key == "source_high_water_event_id":
                        source_high_water = value
                    else:
                        inspection_high_water = value

        evidence_fingerprint = self._baseline_adoption_fingerprint(
            source_task_id=source_task.task_id,
            source_postflight_event_id=source_postflight_event_id,
            inspection_task_id=inspection_task_id,
            inspection_terminal_event_id=inspection_terminal_event_id,
            project_id=source_task.project_id,
            source_high_water_event_id=source_high_water,
            inspection_high_water_event_id=inspection_high_water,
            snapshot=first_snapshot,
            adoption_mode=adoption_mode,
        )
        adoption_payload: dict[str, Any] = {
            "schema_version": RECONCILIATION_BASELINE_SCHEMA_VERSION,
            "baseline_kind": RECONCILIATION_BASELINE_KIND,
            "adoption_mode": adoption_mode,
            "source_task_id": source_task.task_id,
            "source_postflight_event_id": source_postflight_event_id,
            "inspection_task_id": inspection_task_id,
            "inspection_terminal_event_id": inspection_terminal_event_id,
            "project_id": source_task.project_id,
            "repo_path": first_snapshot["repo_path"],
            "baseline_branch": first_snapshot["baseline_branch"],
            "baseline_head": first_snapshot["baseline_head"],
            "final_branch": first_snapshot["final_branch"],
            "final_head": first_snapshot["final_head"],
            "source_high_water_event_id": source_high_water,
            "inspection_high_water_event_id": inspection_high_water,
            "snapshot": first_snapshot,
            "fingerprint": evidence_fingerprint,
            "evidence_fingerprint": evidence_fingerprint,
        }
        try:
            event, created = self.store.persist_reconciliation_baseline_adoption(
                source_task.task_id,
                adoption_payload,
                source_high_water=context["source_high_water"],
                inspection_task_id=inspection_task_id,
                inspection_high_water=context["inspection_high_water"],
                adoption_mode=adoption_mode,
            )
        except TaskStateError as error:
            raise ContinuationBaselineError(str(error)) from error
        return {
            "adopted": True,
            "idempotent": not created,
            "adoption_mode": adoption_mode,
            "source_task_id": source_task.task_id,
            "inspection_task_id": inspection_task_id,
            "adoption_event_id": event.event_id,
            "baseline_kind": RECONCILIATION_BASELINE_KIND,
            "fingerprint": evidence_fingerprint,
        }

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

    @staticmethod
    def _strict_checkpoint_from_event(event: Any) -> GitCheckpoint:
        """Rehydrate one complete initial ``policy.git_checkpoint`` event."""

        if (
            getattr(event, "source", None) != "bridge"
            or getattr(event, "kind", None) != "policy.git_checkpoint"
            or getattr(event, "event_id", None) is None
        ):
            raise ContinuationBaselineError("source Git checkpoint evidence is missing")
        payload = getattr(event, "payload", None)
        if not isinstance(payload, Mapping):
            raise ContinuationBaselineError("source Git checkpoint evidence is invalid")
        if payload.get("mode") != TaskMode.AUTONOMOUS_WRITE.value:
            raise ContinuationBaselineError("source Git checkpoint mode is invalid")

        required = (
            "repo_path",
            "baseline_branch",
            "baseline_head",
            "status_porcelain",
            "staged_paths",
            "unstaged_paths",
            "untracked_paths",
            "baseline_kind",
        )
        if not all(key in payload for key in required):
            raise ContinuationBaselineError("source Git checkpoint evidence is incomplete")
        if not all(
            isinstance(payload[key], str) and bool(payload[key])
            for key in ("repo_path", "baseline_branch", "baseline_head", "baseline_kind")
        ):
            raise ContinuationBaselineError("source Git checkpoint identity is incomplete")
        status = payload["status_porcelain"]
        if not isinstance(status, str) or status.endswith("[TRUNCATED]"):
            raise ContinuationBaselineError("source Git checkpoint evidence was truncated")

        def paths(name: str) -> tuple[str, ...]:
            value = payload.get(name)
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise ContinuationBaselineError(
                    f"source Git checkpoint {name} evidence is incomplete"
                )
            if any(item == "[TRUNCATED]" for item in value):
                raise ContinuationBaselineError("source Git checkpoint paths were truncated")
            return tuple(value)

        previous_task_id = payload.get("previous_task_id")
        if previous_task_id is not None and (
            not isinstance(previous_task_id, str) or not previous_task_id
        ):
            raise ContinuationBaselineError("source Git checkpoint previous task is invalid")
        return GitCheckpoint(
            repo_path=payload["repo_path"],
            baseline_branch=payload["baseline_branch"],
            baseline_head=payload["baseline_head"],
            status_porcelain=status,
            staged_paths=paths("staged_paths"),
            unstaged_paths=paths("unstaged_paths"),
            untracked_paths=paths("untracked_paths"),
            baseline_kind=payload["baseline_kind"],
            previous_task_id=previous_task_id,
        )

    def _validated_reconciliation_context(
        self,
        source_task: Task,
        reconciliation_id: str,
        inspection_task_id: str,
    ) -> dict[str, Any]:
        """Validate source/inspection provenance for reconciliation adoption."""

        if source_task.mode is not TaskMode.AUTONOMOUS_WRITE:
            raise ContinuationBaselineError(
                "reconciliation baseline source must be AUTONOMOUS_WRITE"
            )
        if source_task.execution_status is not ExecutionStatus.FAILED:
            raise ContinuationBaselineError(
                "reconciliation baseline source must be FAILED"
            )
        if not isinstance(reconciliation_id, str) or not reconciliation_id.strip():
            raise ContinuationBaselineError("reconciliation_id must be non-empty text")
        if not isinstance(inspection_task_id, str) or not inspection_task_id.strip():
            raise ContinuationBaselineError("inspection_task_id must be non-empty text")

        source_events = self.store.list_task_events(source_task.task_id)
        state = self.store.get_reconciliation_state(source_task.task_id)
        if state is None or not state.get("required") or not state.get("resolved"):
            raise ContinuationBaselineError(
                "source task does not have a resolved reconciliation"
            )
        if state.get("reconciliation_id") != reconciliation_id:
            raise ContinuationBaselineError("reconciliation_id is stale or incorrect")
        required_event = state.get("required_event")
        resolved_event = state.get("resolved_event")
        required_event_id = getattr(required_event, "event_id", None)
        resolved_event_id = getattr(resolved_event, "event_id", None)
        if (
            not isinstance(required_event_id, int)
            or required_event_id <= 0
            or not isinstance(resolved_event_id, int)
            or resolved_event_id <= required_event_id
        ):
            raise ContinuationBaselineError("reconciliation provenance is incomplete")
        resolved_payload = getattr(resolved_event, "payload", None)
        if not isinstance(resolved_payload, Mapping):
            raise ContinuationBaselineError("reconciliation resolution evidence is invalid")
        if (
            resolved_payload.get("reconciliation_id") != reconciliation_id
            or resolved_payload.get("resolution") != ExecutionStatus.FAILED.value
            or resolved_payload.get("resolver") != "mcp"
        ):
            raise ContinuationBaselineError(
                "reconciliation was not resolved by resolve_task_reconciliation"
            )
        failed_event = next(
            (
                event
                for event in source_events
                if event.source == "bridge"
                and event.kind == "task.failed"
                and (event.event_id or 0) > resolved_event_id
                and isinstance(event.payload, Mapping)
                and event.payload.get("reconciliation_id") == reconciliation_id
            ),
            None,
        )
        if failed_event is None:
            raise ContinuationBaselineError(
                "resolved reconciliation lacks its FAILED terminal evidence"
            )

        checkpoint_events = [
            event
            for event in source_events
            if event.source == "bridge" and event.kind == "policy.git_checkpoint"
        ]
        if len(checkpoint_events) != 1:
            raise ContinuationBaselineError(
                "source task must have exactly one initial Git checkpoint"
            )
        checkpoint_event = checkpoint_events[0]
        checkpoint = self._strict_checkpoint_from_event(checkpoint_event)
        if (checkpoint_event.event_id or 0) >= required_event_id:
            raise ContinuationBaselineError("source Git checkpoint evidence is not initial")

        project = self.store.get_project(source_task.project_id)
        if project is None:
            raise ContinuationBaselineError("source project does not exist")

        inspection_task = self.store.get_task(inspection_task_id)
        if inspection_task is None:
            raise KeyError(f"task does not exist: {inspection_task_id}")
        if inspection_task.mode is not TaskMode.READ_ONLY:
            raise ContinuationBaselineError("inspection task must be READ_ONLY")
        if inspection_task.execution_status is not ExecutionStatus.FINISHED:
            raise ContinuationBaselineError("inspection task must be FINISHED")
        if inspection_task.project_id != source_task.project_id:
            raise ContinuationBaselineError(
                "inspection task belongs to another Project"
            )
        inspection_events = self.store.list_task_events(inspection_task_id)
        inspection_terminal = max(
            (
                event
                for event in inspection_events
                if event.source == "bridge" and event.kind == "task.finished"
            ),
            key=lambda event: event.event_id or 0,
            default=None,
        )
        if inspection_terminal is None or (inspection_terminal.event_id or 0) <= required_event_id:
            raise ContinuationBaselineError(
                "inspection terminal evidence is not posterior to reconciliation"
            )

        return {
            "project": project,
            "source_events": source_events,
            "inspection_events": inspection_events,
            "required_event": required_event,
            "resolved_event": resolved_event,
            "failed_event": failed_event,
            "checkpoint_event": checkpoint_event,
            "checkpoint": checkpoint,
            "inspection_task": inspection_task,
            "inspection_terminal": inspection_terminal,
            "source_high_water": max(
                (event.event_id or 0 for event in source_events), default=0
            ),
            "inspection_high_water": max(
                (event.event_id or 0 for event in inspection_events), default=0
            ),
        }

    @staticmethod
    def _adoption_fingerprint(
        *,
        source_task_id: str,
        reconciliation_id: str,
        inspection_task_id: str,
        project_id: str,
        source_checkpoint_event_id: int,
        reconciliation_required_event_id: int,
        reconciliation_resolved_event_id: int,
        inspection_terminal_event_id: int,
        snapshot: Mapping[str, Any],
    ) -> str:
        material = {
            "schema_version": RECONCILIATION_BASELINE_SCHEMA_VERSION,
            "baseline_kind": RECONCILIATION_BASELINE_KIND,
            "source_task_id": source_task_id,
            "reconciliation_id": reconciliation_id,
            "inspection_task_id": inspection_task_id,
            "project_id": project_id,
            "source_checkpoint_event_id": source_checkpoint_event_id,
            "reconciliation_required_event_id": reconciliation_required_event_id,
            "reconciliation_resolved_event_id": reconciliation_resolved_event_id,
            "inspection_terminal_event_id": inspection_terminal_event_id,
            "snapshot": snapshot,
        }
        encoded = json.dumps(
            material,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @classmethod
    def _adoption_snapshot_from_event(
        cls,
        event: Any,
        candidate: Task,
        context: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Validate one durable adoption event and return its Git snapshot."""

        if event.source != "bridge" or event.kind != RECONCILIATION_BASELINE_ADOPTED_EVENT:
            raise ContinuationBaselineError("adoption event kind is invalid")
        payload = event.payload
        if not isinstance(payload, Mapping):
            raise ContinuationBaselineError("adoption event payload is invalid")
        if payload.get("schema_version") != RECONCILIATION_BASELINE_SCHEMA_VERSION:
            raise ContinuationBaselineError("adoption event schema is unsupported")
        if payload.get("baseline_kind") != RECONCILIATION_BASELINE_KIND:
            raise ContinuationBaselineError("adoption event baseline kind is invalid")
        source_task_id = payload.get("source_task_id")
        reconciliation_id = payload.get("reconciliation_id")
        inspection_task_id = payload.get("inspection_task_id")
        project_id = payload.get("project_id")
        if (
            source_task_id != candidate.task_id
            or not isinstance(reconciliation_id, str)
            or not reconciliation_id
            or not isinstance(inspection_task_id, str)
            or not inspection_task_id
            or project_id != candidate.project_id
        ):
            raise ContinuationBaselineError("adoption event provenance is invalid")
        checkpoint_event = context["checkpoint_event"]
        required_event = context["required_event"]
        resolved_event = context["resolved_event"]
        inspection_terminal = context["inspection_terminal"]

        def event_ref(name: str, expected: Any) -> int:
            value = payload.get(name)
            expected_id = getattr(expected, "event_id", None)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value <= 0
                or value != expected_id
            ):
                raise ContinuationBaselineError(f"adoption event {name} is invalid")
            return value

        checkpoint_id = event_ref("source_checkpoint_event_id", checkpoint_event)
        required_id = event_ref("reconciliation_required_event_id", required_event)
        resolved_id = event_ref("reconciliation_resolved_event_id", resolved_event)
        inspection_id = event_ref("inspection_terminal_event_id", inspection_terminal)
        if (event.event_id or 0) <= max(resolved_id, inspection_id):
            raise ContinuationBaselineError("adoption event ordering is invalid")

        snapshot = payload.get("snapshot")
        normalized = validate_continuation_snapshot(
            snapshot,
            expected_repo=context["checkpoint"].repo_path,
            expected_branch=context["checkpoint"].baseline_branch,
            expected_head=context["checkpoint"].baseline_head,
        )
        expected_fingerprint = cls._adoption_fingerprint(
            source_task_id=source_task_id,
            reconciliation_id=reconciliation_id,
            inspection_task_id=inspection_task_id,
            project_id=project_id,
            source_checkpoint_event_id=checkpoint_id,
            reconciliation_required_event_id=required_id,
            reconciliation_resolved_event_id=resolved_id,
            inspection_terminal_event_id=inspection_id,
            snapshot=normalized,
        )
        if payload.get("evidence_fingerprint") != expected_fingerprint:
            raise ContinuationBaselineError("adoption evidence fingerprint is invalid")
        for key in ("repo_path", "baseline_branch", "baseline_head", "final_branch", "final_head"):
            if key in payload and payload[key] != normalized[key]:
                raise ContinuationBaselineError("adoption event summary differs from snapshot")
        return normalized

    def _baseline_adoption_snapshot_from_event(
        self,
        event: Any,
        candidate: Task,
        context: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Validate one explicit adoption against its exact source postflight."""

        if event.source != "bridge" or event.kind != RECONCILIATION_BASELINE_ADOPTED_EVENT:
            raise ContinuationBaselineError("adoption event kind is invalid")
        payload = event.payload
        if not isinstance(payload, Mapping):
            raise ContinuationBaselineError("adoption event payload is invalid")
        if payload.get("schema_version") != RECONCILIATION_BASELINE_SCHEMA_VERSION:
            raise ContinuationBaselineError("adoption event schema is unsupported")
        if payload.get("baseline_kind") != RECONCILIATION_BASELINE_KIND:
            raise ContinuationBaselineError("adoption event baseline kind is invalid")
        adoption_mode = payload.get("adoption_mode", ADOPTION_MODE_LEGACY)
        if not isinstance(adoption_mode, str) or adoption_mode not in {
            ADOPTION_MODE_LEGACY,
            ADOPTION_MODE_DIRECT,
        }:
            raise ContinuationBaselineError("adoption event mode is unsupported")

        if adoption_mode == ADOPTION_MODE_DIRECT:
            source_postflight = context["source_postflight"]
            source_postflight_id = self._event_id(source_postflight)
            if (
                payload.get("source_task_id") != candidate.task_id
                or payload.get("project_id") != candidate.project_id
                or payload.get("inspection_task_id") is not None
                or payload.get("inspection_terminal_event_id") is not None
            ):
                raise ContinuationBaselineError("direct adoption provenance is invalid")

            source_ref = payload.get("source_postflight_event_id")
            if (
                isinstance(source_ref, bool)
                or not isinstance(source_ref, int)
                or source_ref <= 0
                or source_ref != source_postflight_id
            ):
                raise ContinuationBaselineError(
                    "adoption event source_postflight_event_id is invalid"
                )
            adoption_id = self._event_id(event)
            if adoption_id <= source_ref:
                raise ContinuationBaselineError("adoption event ordering is invalid")

            source_high_water = payload.get("source_high_water_event_id")
            inspection_high_water = payload.get("inspection_high_water_event_id")
            if (
                isinstance(source_high_water, bool)
                or not isinstance(source_high_water, int)
                or source_high_water < source_ref
                or source_high_water >= adoption_id
                or isinstance(inspection_high_water, bool)
                or not isinstance(inspection_high_water, int)
                or inspection_high_water != 0
            ):
                raise ContinuationBaselineError("adoption event high-water is invalid")
            if any(
                self._event_id(item) > adoption_id
                for item in context["source_events"]
            ):
                raise ContinuationBaselineError("source task changed after baseline adoption")
            source_prior_high_water = max(
                (
                    self._event_id(item)
                    for item in context["source_events"]
                    if self._event_id(item) < adoption_id
                ),
                default=0,
            )
            if source_high_water != source_prior_high_water:
                raise ContinuationBaselineError("source task high-water is not exact")

            snapshot = payload.get("snapshot")
            normalized = validate_continuation_snapshot(
                snapshot,
                expected_repo=context["project"].repo_path,
                expected_branch=context["source_postflight_payload"]["baseline_branch"],
                expected_head=context["source_postflight_payload"]["baseline_head"],
            )
            if (
                isinstance(
                    normalized.get("working_tree_fingerprint_version"), bool
                )
                or not isinstance(
                    normalized.get("working_tree_fingerprint_version"), int
                )
                or normalized.get("working_tree_fingerprint_version")
                != WORKING_TREE_FINGERPRINT_VERSION
                or not isinstance(normalized.get("working_tree_fingerprint"), str)
            ):
                raise ContinuationBaselineError(
                    "direct adoption snapshot lacks fingerprint v1"
                )
            expected_fingerprint = self._baseline_adoption_fingerprint(
                source_task_id=candidate.task_id,
                source_postflight_event_id=source_ref,
                inspection_task_id=None,
                inspection_terminal_event_id=None,
                project_id=candidate.project_id,
                source_high_water_event_id=source_high_water,
                inspection_high_water_event_id=0,
                snapshot=normalized,
                adoption_mode=ADOPTION_MODE_DIRECT,
            )
            if payload.get("fingerprint") != expected_fingerprint or payload.get(
                "evidence_fingerprint"
            ) != expected_fingerprint:
                raise ContinuationBaselineError("adoption evidence fingerprint is invalid")
            for key in (
                "repo_path",
                "baseline_branch",
                "baseline_head",
                "final_branch",
                "final_head",
            ):
                if payload.get(key) != normalized[key]:
                    raise ContinuationBaselineError(
                        "adoption event summary differs from snapshot"
                    )
            return normalized

        source_postflight = context["source_postflight"]
        inspection_terminal = context["inspection_terminal"]
        source_postflight_id = self._event_id(source_postflight)
        inspection_terminal_id = self._event_id(inspection_terminal)
        if (
            payload.get("source_task_id") != candidate.task_id
            or payload.get("project_id") != candidate.project_id
            or payload.get("inspection_task_id") != context["inspection_task"].task_id
        ):
            raise ContinuationBaselineError("adoption event provenance is invalid")

        def event_ref(name: str, expected: int) -> int:
            value = payload.get(name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value <= 0
                or value != expected
            ):
                raise ContinuationBaselineError(f"adoption event {name} is invalid")
            return value

        source_ref = event_ref("source_postflight_event_id", source_postflight_id)
        inspection_ref = event_ref("inspection_terminal_event_id", inspection_terminal_id)
        adoption_id = self._event_id(event)
        if adoption_id <= max(source_ref, inspection_ref):
            raise ContinuationBaselineError("adoption event ordering is invalid")

        source_high_water = payload.get("source_high_water_event_id")
        inspection_high_water = payload.get("inspection_high_water_event_id")
        if (
            isinstance(source_high_water, bool)
            or not isinstance(source_high_water, int)
            or source_high_water < source_ref
            or source_high_water >= adoption_id
            or isinstance(inspection_high_water, bool)
            or not isinstance(inspection_high_water, int)
            or inspection_high_water < inspection_ref
        ):
            raise ContinuationBaselineError("adoption event high-water is invalid")
        if any(
            self._event_id(item) > adoption_id
            for item in context["source_events"]
        ):
            raise ContinuationBaselineError("source task changed after baseline adoption")
        source_prior_high_water = max(
            (
                self._event_id(item)
                for item in context["source_events"]
                if self._event_id(item) < adoption_id
            ),
            default=0,
        )
        if source_high_water != source_prior_high_water:
            raise ContinuationBaselineError("source task high-water is not exact")
        inspection_actual_high_water = max(
            (self._event_id(item) for item in context["inspection_events"]),
            default=0,
        )
        if inspection_high_water != inspection_actual_high_water:
            raise ContinuationBaselineError("inspection task high-water is not exact")
        if any(
            self._event_id(item) > inspection_high_water
            for item in context["inspection_events"]
        ):
            raise ContinuationBaselineError(
                "inspection task changed after baseline adoption"
            )

        snapshot = payload.get("snapshot")
        normalized = validate_continuation_snapshot(
            snapshot,
            expected_repo=context["project"].repo_path,
            expected_branch=context["source_postflight_payload"]["baseline_branch"],
            expected_head=context["source_postflight_payload"]["baseline_head"],
        )
        expected_fingerprint = self._baseline_adoption_fingerprint(
            source_task_id=candidate.task_id,
            source_postflight_event_id=source_ref,
            inspection_task_id=context["inspection_task"].task_id,
            inspection_terminal_event_id=inspection_ref,
            project_id=candidate.project_id,
            source_high_water_event_id=source_high_water,
            inspection_high_water_event_id=inspection_high_water,
            snapshot=normalized,
        )
        if payload.get("fingerprint") != expected_fingerprint or payload.get(
            "evidence_fingerprint"
        ) != expected_fingerprint:
            raise ContinuationBaselineError("adoption evidence fingerprint is invalid")
        for key in (
            "repo_path",
            "baseline_branch",
            "baseline_head",
            "final_branch",
            "final_head",
        ):
            if payload.get(key) != normalized[key]:
                raise ContinuationBaselineError("adoption event summary differs from snapshot")
        return normalized

    def _previous_continuation_baseline(
        self, task: Task
    ) -> tuple[str, dict[str, Any]] | None:
        """Return the latest eligible explicit continuation baseline."""

        latest: tuple[int, str, dict[str, Any]] | None = None
        blocker: tuple[int, str, ContinuationBaselineError | None] | None = None

        def add_blocker(
            event_id: int, reason: str, error: ContinuationBaselineError | None = None
        ) -> None:
            nonlocal blocker
            if event_id <= 0:
                return
            if blocker is None or event_id > blocker[0]:
                blocker = (event_id, reason, error)

        for candidate in self.store.list_tasks(task.project_id):
            if (
                candidate.task_id == task.task_id
                or candidate.project_id != task.project_id
                or candidate.mode is not TaskMode.AUTONOMOUS_WRITE
            ):
                continue
            events = self.store.list_task_events(candidate.task_id)
            postflights = [
                event
                for event in events
                if event.source == "bridge" and event.kind == "policy.postflight"
            ]
            adoptions = [
                event
                for event in events
                if event.source == "bridge"
                and event.kind == RECONCILIATION_BASELINE_ADOPTED_EVENT
            ]
            violations = [
                event
                for event in events
                if event.source == "bridge" and event.kind == "policy.violation"
            ]
            latest_event_id = max((self._event_id(event) for event in events), default=0)
            candidate_baseline: tuple[int, str, dict[str, Any]] | None = None

            if adoptions:
                if len(adoptions) != 1:
                    add_blocker(max(self._event_id(event) for event in adoptions), "invalid")
                else:
                    adoption = adoptions[0]
                    try:
                        adoption_payload = adoption.payload
                        if not isinstance(adoption_payload, Mapping):
                            raise ContinuationBaselineError(
                                "adoption event payload is invalid"
                            )
                        adoption_mode = adoption_payload.get(
                            "adoption_mode", ADOPTION_MODE_LEGACY
                        )
                        if not isinstance(adoption_mode, str) or adoption_mode not in {
                            ADOPTION_MODE_LEGACY,
                            ADOPTION_MODE_DIRECT,
                        }:
                            raise ContinuationBaselineError(
                                "adoption event mode is unsupported"
                            )
                        inspection_id = adoption_payload.get("inspection_task_id")
                        if adoption_mode == ADOPTION_MODE_LEGACY and (
                            not isinstance(inspection_id, str) or not inspection_id
                        ):
                            raise ContinuationBaselineError(
                                "adoption event provenance is invalid"
                            )
                        if adoption_mode == ADOPTION_MODE_DIRECT and inspection_id is not None:
                            raise ContinuationBaselineError(
                                "direct adoption provenance is invalid"
                            )
                        context = self._validated_baseline_adoption_context(
                            candidate,
                            inspection_id,
                            adoption_mode=adoption_mode,
                            excluded_task_id=task.task_id,
                        )
                        snapshot = self._baseline_adoption_snapshot_from_event(
                            adoption, candidate, context
                        )
                        candidate_baseline = (
                            self._event_id(adoption),
                            candidate.task_id,
                            snapshot,
                        )
                    except (ContinuationBaselineError, KeyError) as error:
                        add_blocker(
                            self._event_id(adoption) or latest_event_id,
                            "invalid",
                            error if isinstance(error, ContinuationBaselineError) else None,
                        )

            if candidate_baseline is None and postflights and not adoptions:
                postflight = max(postflights, key=self._event_id)
                payload = postflight.payload
                if (
                    candidate.execution_status is ExecutionStatus.FINISHED
                    and self._event_id(postflight)
                    and isinstance(payload, Mapping)
                    and not violations
                    and payload.get("policy_violation") is False
                ):
                    try:
                        fingerprint_present = (
                            "working_tree_fingerprint_version" in payload
                            or "working_tree_fingerprint" in payload
                        )
                        normalized = validate_continuation_snapshot(
                            payload,
                            expected_repo=payload.get("repo_path"),
                            expected_branch=payload.get("baseline_branch"),
                            expected_head=payload.get("baseline_head"),
                            allow_fingerprint_truncation=fingerprint_present,
                        )
                    except ContinuationBaselineError as error:
                        reason = (
                            "incomplete"
                            if self._snapshot_error_is_incomplete(
                                error,
                                payload,
                                expected_repo=payload.get("repo_path"),
                                expected_branch=payload.get("baseline_branch"),
                                expected_head=payload.get("baseline_head"),
                            )
                            else "invalid"
                        )
                        add_blocker(self._event_id(postflight), reason, error)
                    else:
                        candidate_baseline = (
                            self._event_id(postflight),
                            candidate.task_id,
                            normalized,
                        )
                else:
                    add_blocker(latest_event_id, "invalid")
            elif candidate_baseline is None and adoptions:
                # An adoption is an explicit attempted replacement.  A
                # malformed one must not silently expose the historical
                # postflight or an older task as a baseline.
                if not postflights:
                    add_blocker(latest_event_id, "invalid")

            if candidate_baseline is None and not postflights and not adoptions:
                if self._preflight_only_failure(candidate, events):
                    continue
                if candidate.execution_status is ExecutionStatus.QUEUED:
                    continue
                add_blocker(latest_event_id, "invalid")

            if candidate_baseline is not None:
                if latest is None or candidate_baseline[0] > latest[0]:
                    latest = candidate_baseline

        if latest is None:
            if blocker is not None and blocker[2] is not None:
                raise blocker[2]
            return None
        if blocker is not None and blocker[0] > latest[0]:
            if blocker[2] is not None:
                raise blocker[2]
            return None
        _, previous_task_id, previous_postflight = latest
        return previous_task_id, previous_postflight

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
        tasks = [
            candidate
            for candidate in self.store.list_tasks(task.project_id)
            if candidate.mode is TaskMode.AUTONOMOUS_WRITE
        ]
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
