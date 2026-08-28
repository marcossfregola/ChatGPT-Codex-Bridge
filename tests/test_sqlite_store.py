from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


_VALID_PROJECTS_SQL = """
CREATE TABLE projects (
    project_id TEXT PRIMARY KEY NOT NULL,
    name TEXT NOT NULL CHECK (length(trim(name)) > 0),
    repo_path TEXT NOT NULL CHECK (length(trim(repo_path)) > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

_VALID_TASKS_SQL = """
CREATE TABLE tasks (
    task_id TEXT PRIMARY KEY NOT NULL,
    project_id TEXT NOT NULL,
    objective TEXT NOT NULL CHECK (length(trim(objective)) > 0),
    executor TEXT NOT NULL CHECK (length(trim(executor)) > 0),
    model TEXT NOT NULL CHECK (length(trim(model)) > 0),
    execution_status TEXT NOT NULL CHECK (
        execution_status IN (
            'QUEUED', 'RUNNING', 'WAITING_USER',
            'FINISHED', 'FAILED', 'CANCELLED'
        )
    ),
    audit_status TEXT NOT NULL CHECK (
        audit_status IN (
            'PENDING', 'APPROVED', 'CORRECTION_REQUIRED'
        )
    ),
    thread_id TEXT,
    turn_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(project_id)
)
"""

_VALID_TASK_EVENTS_SQL = """
CREATE TABLE task_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    source TEXT NOT NULL CHECK (length(trim(source)) > 0),
    kind TEXT NOT NULL CHECK (length(trim(kind)) > 0),
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(task_id)
)
"""


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from chatgpt_codex_bridge.domain.events import TaskEvent  # noqa: E402
from chatgpt_codex_bridge.domain.models import (  # noqa: E402
    AuditStatus,
    ExecutionStatus,
    Project,
    Task,
    TaskStateError,
    timestamp_to_text,
)
from chatgpt_codex_bridge.persistence import sqlite_store as sqlite_store_module  # noqa: E402
from chatgpt_codex_bridge.persistence.sqlite_store import (  # noqa: E402
    CANCELLATION_REQUEST_EVENT,
    D3_H3_CONTRACT,
    D3_R2_CONTRACT,
    EXECUTION_CLAIM_EVENT,
    EXECUTION_REQUEST_EVENT,
    SCHEMA_VERSION,
    SQLiteBridgeStore,
    SchemaVersionError,
)


class FailingTerminalStore(SQLiteBridgeStore):
    def _insert_task_event_in_transaction(
        self, connection, task_id, source, kind, payload, *, created_at=None
    ):
        if kind == "task.finished":
            raise RuntimeError("terminal event insert failed")
        return super()._insert_task_event_in_transaction(
            connection,
            task_id,
            source,
            kind,
            payload,
            created_at=created_at,
        )


class FailingPreflightStore(SQLiteBridgeStore):
    def _insert_task_event_in_transaction(
        self, connection, task_id, source, kind, payload, *, created_at=None
    ):
        if kind == "task.failed":
            raise RuntimeError("preflight terminal event insert failed")
        return super()._insert_task_event_in_transaction(
            connection,
            task_id,
            source,
            kind,
            payload,
            created_at=created_at,
        )


class SQLiteBridgeStoreTests(unittest.TestCase):
    timestamp = datetime(2026, 8, 26, 12, 0, 0, 123456, tzinfo=timezone.utc)

    def make_project(self) -> Project:
        return Project(
            project_id="project-1",
            name="Bridge",
            repo_path="C:/workspace/bridge",
            created_at=self.timestamp,
            updated_at=self.timestamp,
        )

    def make_task(self, *, task_id: str = "task-1", project_id: str = "project-1") -> Task:
        return Task(
            task_id=task_id,
            project_id=project_id,
            objective="Persist Bridge state",
            model="gpt-5.6-luna",
            created_at=self.timestamp,
            updated_at=self.timestamp,
        )

    @staticmethod
    def write_schema_database(
        db_path: Path,
        *,
        projects_sql: str | None = _VALID_PROJECTS_SQL,
        tasks_sql: str | None = _VALID_TASKS_SQL,
        task_events_sql: str | None = None,
        user_version: int = 1,
    ) -> None:
        connection = sqlite3.connect(db_path)
        try:
            if projects_sql is not None:
                connection.execute(projects_sql)
            if tasks_sql is not None:
                connection.execute(tasks_sql)
            if task_events_sql is not None:
                connection.execute(task_events_sql)
            connection.execute(f"PRAGMA user_version = {user_version}")
            connection.commit()
        finally:
            connection.close()

    def create_store_with_task(self, db_path: Path) -> SQLiteBridgeStore:
        store = SQLiteBridgeStore(db_path)
        store.create_project(self.make_project())
        store.create_task(self.make_task())
        return store

    def test_create_reopen_schema_and_foreign_keys(self) -> None:
        with TemporaryDirectory() as directory:
            db_path = Path(directory) / "bridge.sqlite3"
            store = SQLiteBridgeStore(db_path)
            self.assertEqual(store.connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(
                store.connection.execute("PRAGMA user_version").fetchone()[0],
                SCHEMA_VERSION,
            )
            tables = {
                row[0]
                for row in store.connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
                if not str(row[0]).startswith("sqlite_")
            }
            self.assertEqual(tables, {"projects", "tasks", "task_events"})
            store.close()

            reopened = SQLiteBridgeStore(db_path)
            self.assertEqual(reopened.list_projects(), [])
            self.assertEqual(reopened.list_tasks("project-1"), [])
            self.assertEqual(reopened.list_task_events("task-1"), [])
            reopened.close()

    def test_project_roundtrip_across_new_connection(self) -> None:
        with TemporaryDirectory() as directory:
            db_path = Path(directory) / "bridge.sqlite3"
            project = self.make_project()
            store = SQLiteBridgeStore(db_path)
            store.create_project(project)
            store.close()

            reopened = SQLiteBridgeStore(db_path)
            self.assertEqual(reopened.get_project(project.project_id), project)
            self.assertEqual(reopened.list_projects(), [project])
            reopened.close()

    def test_task_roundtrip_and_optional_codex_references(self) -> None:
        with TemporaryDirectory() as directory:
            db_path = Path(directory) / "bridge.sqlite3"
            project = self.make_project()
            task = self.make_task()
            store = SQLiteBridgeStore(db_path)
            store.create_project(project)
            store.create_task(task)
            store.close()

            reopened = SQLiteBridgeStore(db_path)
            self.assertEqual(reopened.get_task(task.task_id), task)
            self.assertEqual(reopened.list_tasks(project.project_id), [task])
            reopened.close()

    def test_runtime_update_persists_statuses_and_codex_correlation(self) -> None:
        with TemporaryDirectory() as directory:
            db_path = Path(directory) / "bridge.sqlite3"
            store = self.create_store_with_task(db_path)
            updated = store.update_task_runtime(
                "task-1",
                execution_status=ExecutionStatus.FINISHED,
                audit_status=AuditStatus.PENDING,
                thread_id="thread-example",
                turn_id="turn-example",
            )
            self.assertEqual(updated.execution_status, ExecutionStatus.FINISHED)
            self.assertEqual(updated.audit_status, AuditStatus.PENDING)
            self.assertEqual(updated.thread_id, "thread-example")
            self.assertEqual(updated.turn_id, "turn-example")
            store.close()

            reopened = SQLiteBridgeStore(db_path)
            recovered = reopened.get_task("task-1")
            self.assertIsNotNone(recovered)
            assert recovered is not None
            self.assertEqual(recovered.execution_status, ExecutionStatus.FINISHED)
            self.assertEqual(recovered.audit_status, AuditStatus.PENDING)
            self.assertEqual(recovered.thread_id, "thread-example")
            self.assertEqual(recovered.turn_id, "turn-example")
            reopened.close()

    def test_d3_r2_request_is_atomic_and_idempotent(self) -> None:
        with TemporaryDirectory() as directory:
            store = self.create_store_with_task(Path(directory) / "dispatch.sqlite3")
            payload = {
                "contract": D3_R2_CONTRACT,
                "requested_via": "run_task",
                "request_id": "request-1",
                "requested_at": "2026-08-27T00:00:00Z",
                "requested_by": "mcp",
            }
            first_task, first_event, first_created = store.request_task_execution(
                "task-1", payload
            )
            second_task, second_event, second_created = store.request_task_execution(
                "task-1", {**payload, "request_id": "request-2"}
            )

            self.assertEqual(first_task.execution_status, ExecutionStatus.QUEUED)
            self.assertTrue(first_created)
            self.assertFalse(second_created)
            self.assertEqual(first_event, second_event)
            self.assertEqual(second_task, first_task)
            self.assertEqual(
                [event.kind for event in store.list_task_events("task-1")],
                [EXECUTION_REQUEST_EVENT],
            )
            store.close()

    def test_d3_r2_claim_requires_request_and_starts_once(self) -> None:
        with TemporaryDirectory() as directory:
            store = self.create_store_with_task(Path(directory) / "claim.sqlite3")
            with self.assertRaises(TaskStateError):
                store.claim_task_execution(
                    "task-1",
                    {
                        "owner_kind": "persistent_worker",
                        "owner_id": "worker-1",
                        "pid": 1,
                    },
                )
            store.request_task_execution(
                "task-1",
                {
                    "contract": D3_R2_CONTRACT,
                    "requested_via": "run_task",
                    "request_id": "request-1",
                    "requested_at": "2026-08-27T00:00:00Z",
                    "requested_by": "mcp",
                },
            )
            claimed, claim_event = store.claim_task_execution(
                "task-1",
                {
                    "owner_kind": "persistent_worker",
                    "owner_id": "worker-1",
                    "pid": 1,
                    "claimed_at": "caller-value",
                },
            )
            self.assertEqual(claimed.execution_status, ExecutionStatus.RUNNING)
            self.assertEqual(claim_event.kind, EXECUTION_CLAIM_EVENT)
            self.assertIsInstance(claim_event.payload["claimed_at"], str)
            with self.assertRaises(TaskStateError):
                store.claim_task_execution(
                    "task-1",
                    {
                        "owner_kind": "persistent_worker",
                        "owner_id": "worker-2",
                        "pid": 2,
                    },
                )
            self.assertEqual(
                [event.kind for event in store.list_task_events("task-1")],
                [EXECUTION_REQUEST_EVENT, EXECUTION_CLAIM_EVENT, "task.started"],
            )
            store.close()

    def test_d3_h3_queued_cancellation_is_atomic_and_idempotent(self) -> None:
        with TemporaryDirectory() as directory:
            store = self.create_store_with_task(Path(directory) / "cancel.sqlite3")
            payload = {
                "contract": D3_H3_CONTRACT,
                "requested_via": "cancel_task",
                "request_id": "cancel-1",
                "requested_at": "2026-08-27T00:00:00Z",
                "requested_by": "mcp",
            }

            first_task, first_event, first_created = store.request_task_cancellation(
                "task-1", payload
            )
            second_task, second_event, second_created = store.request_task_cancellation(
                "task-1", {**payload, "request_id": "cancel-2"}
            )

            self.assertEqual(first_task.execution_status, ExecutionStatus.CANCELLED)
            self.assertTrue(first_created)
            self.assertFalse(second_created)
            self.assertEqual(first_event, second_event)
            self.assertEqual(second_task, first_task)
            self.assertEqual(
                [event.kind for event in store.list_task_events("task-1")],
                [CANCELLATION_REQUEST_EVENT, "task.cancelled"],
            )
            self.assertIsNotNone(store.get_cancellation_request("task-1"))
            store.close()

    def test_d3_h3_running_cancellation_only_records_one_request(self) -> None:
        with TemporaryDirectory() as directory:
            store = self.create_store_with_task(Path(directory) / "cancel-running.sqlite3")
            store.transition_task_running("task-1", project_id="project-1")
            payload = {
                "contract": D3_H3_CONTRACT,
                "requested_via": "cancel_task",
                "request_id": "cancel-1",
                "requested_at": "2026-08-27T00:00:00Z",
                "requested_by": "mcp",
            }

            first_task, first_event, first_created = store.request_task_cancellation(
                "task-1", payload
            )
            second_task, second_event, second_created = store.request_task_cancellation(
                "task-1", {**payload, "request_id": "cancel-2"}
            )

            self.assertEqual(first_task.execution_status, ExecutionStatus.RUNNING)
            self.assertEqual(second_task.execution_status, ExecutionStatus.RUNNING)
            self.assertTrue(first_created)
            self.assertFalse(second_created)
            self.assertEqual(first_event, second_event)
            self.assertEqual(
                [event.kind for event in store.list_task_events("task-1")],
                ["task.started", CANCELLATION_REQUEST_EVENT],
            )
            store.close()

    def test_runtime_update_distinguishes_omitted_and_none(self) -> None:
        with TemporaryDirectory() as directory:
            db_path = Path(directory) / "bridge.sqlite3"
            store = self.create_store_with_task(db_path)
            initial = store.get_task("task-1")
            self.assertIsNotNone(initial)
            assert initial is not None

            store.update_task_runtime(
                "task-1",
                thread_id="thread-example",
                turn_id="turn-example",
            )
            unchanged = store.update_task_runtime(
                "task-1",
                execution_status=ExecutionStatus.FINISHED,
            )
            self.assertEqual(unchanged.thread_id, "thread-example")
            self.assertEqual(unchanged.turn_id, "turn-example")
            self.assertEqual(unchanged.created_at, initial.created_at)
            self.assertNotEqual(unchanged.updated_at, initial.updated_at)

            cleared = store.update_task_runtime(
                "task-1",
                thread_id=None,
                turn_id=None,
            )
            self.assertIsNone(cleared.thread_id)
            self.assertIsNone(cleared.turn_id)
            self.assertEqual(cleared.execution_status, ExecutionStatus.FINISHED)
            self.assertEqual(cleared.created_at, initial.created_at)
            store.close()

    def test_terminal_transition_rolls_back_state_when_event_insert_fails(self) -> None:
        with TemporaryDirectory() as directory:
            store = FailingTerminalStore(Path(directory) / "bridge.sqlite3")
            project = self.make_project()
            task = self.make_task()
            store.create_project(project)
            store.create_task(task)
            store.append_task_event(
                task.task_id,
                "bridge",
                "task.created",
                {"project_id": project.project_id},
            )
            store.transition_task_running(task.task_id, project_id=project.project_id)

            with self.assertRaisesRegex(RuntimeError, "terminal event insert failed"):
                store.transition_task_terminal(
                    task.task_id,
                    execution_status=ExecutionStatus.FINISHED,
                    event_kind="task.finished",
                    payload={"final_response": "ok"},
                )

            recovered = store.get_task(task.task_id)
            assert recovered is not None
            self.assertEqual(recovered.execution_status, ExecutionStatus.RUNNING)
            terminal = [
                event
                for event in store.list_task_events(task.task_id)
                if event.kind in {"task.finished", "task.failed", "task.cancelled"}
            ]
            self.assertEqual(terminal, [])
            store.close()

    def test_preflight_failure_transition_is_atomic_and_terminal(self) -> None:
        with TemporaryDirectory() as directory:
            store = self.create_store_with_task(Path(directory) / "bridge.sqlite3")
            store.append_task_event(
                "task-1",
                "bridge",
                "task.created",
                {"project_id": "project-1"},
            )

            failed = store.transition_task_preflight_failed(
                "task-1",
                policy_payload={"phase": "preflight", "policy_violation": True},
                failed_payload={"error_type": "DirtyWorkingTreeError"},
            )
            self.assertEqual(failed.execution_status, ExecutionStatus.FAILED)
            self.assertEqual(
                [event.kind for event in store.list_task_events("task-1")],
                ["task.created", "policy.violation", "task.failed"],
            )
            with self.assertRaises(TaskStateError):
                store.transition_task_preflight_failed(
                    "task-1",
                    policy_payload={"phase": "preflight"},
                    failed_payload={"error_type": "Duplicate"},
                )
            store.close()

    def test_preflight_failure_transition_rolls_back_on_event_insert_failure(self) -> None:
        with TemporaryDirectory() as directory:
            store = FailingPreflightStore(Path(directory) / "bridge.sqlite3")
            store.create_project(self.make_project())
            store.create_task(self.make_task())
            store.append_task_event(
                "task-1",
                "bridge",
                "task.created",
                {"project_id": "project-1"},
            )

            with self.assertRaisesRegex(
                RuntimeError, "preflight terminal event insert failed"
            ):
                store.transition_task_preflight_failed(
                    "task-1",
                    policy_payload={"phase": "preflight"},
                    failed_payload={"error_type": "DirtyWorkingTreeError"},
                )

            task = store.get_task("task-1")
            assert task is not None
            self.assertEqual(task.execution_status, ExecutionStatus.QUEUED)
            self.assertEqual(
                [event.kind for event in store.list_task_events("task-1")],
                ["task.created"],
            )
            store.close()

    def test_foreign_key_rejects_task_without_project(self) -> None:
        with TemporaryDirectory() as directory:
            store = SQLiteBridgeStore(Path(directory) / "bridge.sqlite3")
            with self.assertRaises(sqlite3.IntegrityError):
                store.create_task(self.make_task(project_id="missing-project"))
            self.assertIsNone(store.get_task("task-1"))
            store.close()

    def test_duplicate_project_and_task_ids_fail_explicitly(self) -> None:
        with TemporaryDirectory() as directory:
            store = SQLiteBridgeStore(Path(directory) / "bridge.sqlite3")
            project = self.make_project()
            store.create_project(project)
            with self.assertRaises(sqlite3.IntegrityError):
                store.create_project(project)

            task = self.make_task()
            store.create_task(task)
            with self.assertRaises(sqlite3.IntegrityError):
                store.create_task(task)
            store.close()

    def test_future_schema_version_is_rejected(self) -> None:
        with TemporaryDirectory() as directory:
            db_path = Path(directory) / "future.sqlite3"
            connection = sqlite3.connect(db_path)
            connection.execute("PRAGMA user_version = 999")
            connection.commit()
            connection.close()
            with self.assertRaises(SchemaVersionError):
                SQLiteBridgeStore(db_path)

    def test_composite_primary_key_is_rejected(self) -> None:
        with TemporaryDirectory() as directory:
            db_path = Path(directory) / "composite-key.sqlite3"
            projects_sql = _VALID_PROJECTS_SQL.replace(
                "    project_id TEXT PRIMARY KEY NOT NULL,",
                "    project_id TEXT NOT NULL,",
            ).replace(
                "    updated_at TEXT NOT NULL\n)",
                "    updated_at TEXT NOT NULL,\n"
                "    PRIMARY KEY (project_id, name)\n)",
            )
            self.write_schema_database(db_path, projects_sql=projects_sql)
            with self.assertRaises(SchemaVersionError):
                SQLiteBridgeStore(db_path)

    def test_wrong_declared_type_is_rejected(self) -> None:
        with TemporaryDirectory() as directory:
            db_path = Path(directory) / "wrong-type.sqlite3"
            projects_sql = _VALID_PROJECTS_SQL.replace(
                "    updated_at TEXT NOT NULL",
                "    updated_at INTEGER NOT NULL",
            )
            self.write_schema_database(db_path, projects_sql=projects_sql)
            with self.assertRaises(SchemaVersionError):
                SQLiteBridgeStore(db_path)

    def test_wrong_check_constraint_is_rejected(self) -> None:
        with TemporaryDirectory() as directory:
            db_path = Path(directory) / "wrong-check.sqlite3"
            projects_sql = _VALID_PROJECTS_SQL.replace(
                "CHECK (length(trim(name)) > 0)",
                "CHECK (length(trim(name)) >= 0)",
            )
            self.write_schema_database(db_path, projects_sql=projects_sql)
            with self.assertRaises(SchemaVersionError):
                SQLiteBridgeStore(db_path)

    def test_nonempty_unversioned_database_is_rejected(self) -> None:
        with TemporaryDirectory() as directory:
            db_path = Path(directory) / "unversioned.sqlite3"
            self.write_schema_database(
                db_path,
                projects_sql="CREATE TABLE legacy (id TEXT)",
                tasks_sql=None,
                user_version=0,
            )
            with self.assertRaises(SchemaVersionError):
                SQLiteBridgeStore(db_path)

    def test_v1_incomplete_schema_is_rejected_without_migration(self) -> None:
        with TemporaryDirectory() as directory:
            db_path = Path(directory) / "incomplete.sqlite3"
            self.write_schema_database(
                db_path,
                projects_sql="CREATE TABLE projects (project_id TEXT PRIMARY KEY)",
                tasks_sql="CREATE TABLE tasks (task_id TEXT PRIMARY KEY)",
                user_version=1,
            )
            with self.assertRaises(SchemaVersionError):
                SQLiteBridgeStore(db_path)

            connection = sqlite3.connect(db_path)
            try:
                self.assertEqual(
                    connection.execute("PRAGMA user_version").fetchone()[0],
                    1,
                )
                self.assertIsNone(
                    connection.execute(
                        "SELECT name FROM sqlite_master WHERE name = 'task_events'"
                    ).fetchone()
                )
            finally:
                connection.close()

    def test_v1_valid_migrates_to_v3_and_preserves_data(self) -> None:
        with TemporaryDirectory() as directory:
            db_path = Path(directory) / "v1.sqlite3"
            project = self.make_project()
            task = self.make_task()
            self.write_schema_database(db_path, user_version=1)

            connection = sqlite3.connect(db_path)
            try:
                connection.execute(
                    """
                    INSERT INTO projects
                        (project_id, name, repo_path, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        project.project_id,
                        project.name,
                        project.repo_path,
                        timestamp_to_text(project.created_at),
                        timestamp_to_text(project.updated_at),
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO tasks
                        (task_id, project_id, objective, executor, model,
                         execution_status, audit_status, thread_id, turn_id,
                         created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task.task_id,
                        task.project_id,
                        task.objective,
                        task.executor,
                        task.model,
                        task.execution_status.value,
                        task.audit_status.value,
                        task.thread_id,
                        task.turn_id,
                        timestamp_to_text(task.created_at),
                        timestamp_to_text(task.updated_at),
                    ),
                )
                connection.commit()
            finally:
                connection.close()

            store = SQLiteBridgeStore(db_path)
            self.assertEqual(
                store.connection.execute("PRAGMA user_version").fetchone()[0],
                SCHEMA_VERSION,
            )
            self.assertEqual(store.get_task(task.task_id).mode.value, "READ_ONLY")
            self.assertEqual(store.get_project(project.project_id), project)
            self.assertEqual(store.get_task(task.task_id), task)
            self.assertEqual(store.list_task_events(task.task_id), [])
            store.close()

            reopened = SQLiteBridgeStore(db_path)
            self.assertEqual(reopened.get_project(project.project_id), project)
            self.assertEqual(reopened.get_task(task.task_id), task)
            self.assertEqual(reopened.count_task_events(task.task_id), 0)
            reopened.close()

    def test_failed_v1_to_v2_migration_rolls_back(self) -> None:
        with TemporaryDirectory() as directory:
            db_path = Path(directory) / "migration-rollback.sqlite3"
            self.write_schema_database(db_path, user_version=1)
            invalid_events_sql = """
            CREATE TABLE task_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT
            )
            """
            with patch.object(
                sqlite_store_module,
                "_CREATE_TASK_EVENTS_SQL",
                invalid_events_sql,
            ):
                with self.assertRaises(SchemaVersionError):
                    SQLiteBridgeStore(db_path)

            connection = sqlite3.connect(db_path)
            try:
                self.assertEqual(
                    connection.execute("PRAGMA user_version").fetchone()[0],
                    1,
                )
                self.assertIsNone(
                    connection.execute(
                        "SELECT name FROM sqlite_master WHERE name = 'task_events'"
                    ).fetchone()
                )
            finally:
                connection.close()

    def test_append_task_events_roundtrip_order_last_count_and_reopen(self) -> None:
        with TemporaryDirectory() as directory:
            db_path = Path(directory) / "events.sqlite3"
            store = self.create_store_with_task(db_path)
            first = store.append_task_event(
                "task-1",
                "bridge",
                "task.created",
                {"objective": "ejemplo ñ 🚀", "nested": {"items": [1, True, None]}},
            )
            second = store.append_task_event(
                "task-1",
                "codex",
                "item.commandExecution.started",
                {"command": "python -m unittest"},
            )
            third = store.append_task_event(
                "task-1",
                "codex",
                "turn.diff.updated",
                {"files": ["example.py"]},
            )

            self.assertIsInstance(first, TaskEvent)
            self.assertIsNotNone(first.event_id)
            self.assertLess(first.event_id, second.event_id)
            self.assertLess(second.event_id, third.event_id)
            self.assertEqual(first.payload["nested"]["items"], [1, True, None])
            self.assertEqual(first.payload["objective"], "ejemplo ñ 🚀")
            self.assertEqual(
                store.connection.execute(
                    "SELECT payload_json FROM task_events WHERE event_id = ?",
                    (first.event_id,),
                ).fetchone()[0],
                '{"nested":{"items":[1,true,null]},"objective":"ejemplo ñ 🚀"}',
            )

            events = store.list_task_events("task-1")
            self.assertEqual([event.event_id for event in events], [
                first.event_id,
                second.event_id,
                third.event_id,
            ])
            self.assertEqual(
                [event.kind for event in events],
                [
                    "task.created",
                    "item.commandExecution.started",
                    "turn.diff.updated",
                ],
            )
            self.assertEqual(store.get_last_task_event("task-1"), third)
            self.assertEqual(store.count_task_events("task-1"), 3)
            self.assertIsNotNone(first.created_at.tzinfo)
            store.close()

            reopened = SQLiteBridgeStore(db_path)
            self.assertEqual(reopened.list_task_events("task-1"), events)
            self.assertEqual(reopened.get_last_task_event("task-1"), third)
            self.assertEqual(reopened.count_task_events("task-1"), 3)
            reopened.close()

    def test_bounded_event_window_uses_sql_limits_and_deserializes_only_selected_rows(self) -> None:
        with TemporaryDirectory() as directory:
            store = self.create_store_with_task(Path(directory) / "bounded.sqlite3")
            try:
                store.connection.executemany(
                    """
                    INSERT INTO task_events (task_id, source, kind, payload_json, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        (
                            "task-1",
                            "codex",
                            "noise",
                            f'{{"index":{index}}}',
                            "2026-08-27T00:00:00Z",
                        )
                        for index in range(10_001)
                    ),
                )
                store.connection.commit()

                with patch.object(store, "_event_from_row", wraps=store._event_from_row) as decode:
                    events, total = store.list_task_events_window(
                        "task-1",
                        100,
                        critical_kinds={"task.created", "task.finished"},
                        critical_limit=8,
                    )

                self.assertEqual(total, 10_001)
                self.assertLessEqual(len(events), 108)
                self.assertEqual(events[0].kind, "noise")
                self.assertLessEqual(decode.call_count, 108)

                with patch.object(store, "_event_from_row", wraps=store._event_from_row) as decode_latest:
                    latest = store.get_latest_task_events(
                        "task-1", {"task.created", "task.finished", "noise"}
                    )
                self.assertEqual(len(latest), 1)
                self.assertLessEqual(decode_latest.call_count, 3)
            finally:
                store.close()

    def test_incremental_event_window_is_exclusive_and_ordered(self) -> None:
        with TemporaryDirectory() as directory:
            store = self.create_store_with_task(Path(directory) / "incremental.sqlite3")
            try:
                events = [
                    store.append_task_event(
                        "task-1", "codex", "incremental", {"index": index}
                    )
                    for index in range(5)
                ]
                cursor = events[1].event_id
                self.assertIsNotNone(cursor)
                page, total = store.list_task_events_window(
                    "task-1",
                    2,
                    since_event_id=cursor,
                    critical_kinds={"task.created", "task.finished"},
                )
                self.assertEqual(
                    [event.event_id for event in page],
                    [events[2].event_id, events[3].event_id],
                )
                self.assertEqual(total, 3)
                self.assertTrue(all(event.event_id > cursor for event in page))

                next_page, next_total = store.list_task_events_window(
                    "task-1", 2, since_event_id=page[-1].event_id
                )
                self.assertEqual([event.event_id for event in next_page], [events[4].event_id])
                self.assertEqual(next_total, 1)
            finally:
                store.close()

    def test_incremental_event_window_rejects_invalid_cursor(self) -> None:
        with TemporaryDirectory() as directory:
            store = self.create_store_with_task(Path(directory) / "invalid-cursor.sqlite3")
            try:
                for value in (-1, True, "1"):
                    with self.assertRaises(ValueError):
                        store.list_task_events_window(
                            "task-1", 10, since_event_id=value  # type: ignore[arg-type]
                        )
            finally:
                store.close()

    def test_append_event_foreign_key_rejects_missing_task(self) -> None:
        with TemporaryDirectory() as directory:
            store = SQLiteBridgeStore(Path(directory) / "bridge.sqlite3")
            with self.assertRaises(sqlite3.IntegrityError):
                store.append_task_event(
                    "missing-task",
                    "bridge",
                    "task.created",
                    {"objective": "example"},
                )
            self.assertEqual(store.count_task_events("missing-task"), 0)
            store.close()

    def test_non_serializable_payload_rejected_before_persist(self) -> None:
        with TemporaryDirectory() as directory:
            store = self.create_store_with_task(Path(directory) / "bridge.sqlite3")
            with self.assertRaises(ValueError):
                store.append_task_event(
                    "task-1",
                    "bridge",
                    "task.created",
                    object(),
                )
            self.assertEqual(store.count_task_events("task-1"), 0)
            store.close()

    def test_corrupt_payload_is_rejected_on_read(self) -> None:
        with TemporaryDirectory() as directory:
            store = self.create_store_with_task(Path(directory) / "bridge.sqlite3")
            event = store.append_task_event(
                "task-1",
                "bridge",
                "task.created",
                {"objective": "example"},
            )
            store.connection.execute(
                "UPDATE task_events SET payload_json = ? WHERE event_id = ?",
                ("{not-json", event.event_id),
            )
            store.connection.commit()
            with self.assertRaises(ValueError):
                store.get_last_task_event("task-1")
            store.close()

    def test_v2_altered_schema_is_rejected(self) -> None:
        with TemporaryDirectory() as directory:
            db_path = Path(directory) / "altered-v2.sqlite3"
            altered_events_sql = _VALID_TASK_EVENTS_SQL.replace(
                "payload_json TEXT NOT NULL",
                "payload_json INTEGER NOT NULL",
            )
            self.write_schema_database(
                db_path,
                task_events_sql=altered_events_sql,
                user_version=2,
            )
            with self.assertRaises(SchemaVersionError):
                SQLiteBridgeStore(db_path)

    def test_invalid_runtime_state_is_rejected_before_write(self) -> None:
        with TemporaryDirectory() as directory:
            store = self.create_store_with_task(Path(directory) / "bridge.sqlite3")
            with self.assertRaises(ValueError):
                store.update_task_runtime("task-1", execution_status="NOT_A_STATUS")
            recovered = store.get_task("task-1")
            self.assertIsNotNone(recovered)
            assert recovered is not None
            self.assertEqual(recovered.execution_status, ExecutionStatus.QUEUED)
            store.close()

    def test_close_is_idempotent_and_closed_store_rejects_operations(self) -> None:
        with TemporaryDirectory() as directory:
            store = SQLiteBridgeStore(Path(directory) / "bridge.sqlite3")
            store.close()
            store.close()
            with self.assertRaises(RuntimeError):
                store.list_projects()
            with self.assertRaises(RuntimeError):
                store.list_task_events("task-1")


if __name__ == "__main__":
    unittest.main()
