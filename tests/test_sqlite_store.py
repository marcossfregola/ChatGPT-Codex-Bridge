from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import unittest


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


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from chatgpt_codex_bridge.domain.models import (  # noqa: E402
    AuditStatus,
    ExecutionStatus,
    Project,
    Task,
)
from chatgpt_codex_bridge.persistence.sqlite_store import (  # noqa: E402
    SCHEMA_VERSION,
    SQLiteBridgeStore,
    SchemaVersionError,
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
        user_version: int = SCHEMA_VERSION,
    ) -> None:
        connection = sqlite3.connect(db_path)
        try:
            if projects_sql is not None:
                connection.execute(projects_sql)
            if tasks_sql is not None:
                connection.execute(tasks_sql)
            connection.execute(f"PRAGMA user_version = {user_version}")
            connection.commit()
        finally:
            connection.close()

    def test_create_reopen_schema_and_foreign_keys(self) -> None:
        with TemporaryDirectory() as directory:
            db_path = Path(directory) / "bridge.sqlite3"
            store = SQLiteBridgeStore(db_path)
            self.assertEqual(store.connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(store.connection.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
            store.close()

            reopened = SQLiteBridgeStore(db_path)
            self.assertEqual(reopened.list_projects(), [])
            self.assertEqual(reopened.list_tasks("project-1"), [])
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
            store = SQLiteBridgeStore(db_path)
            store.create_project(self.make_project())
            store.create_task(self.make_task())
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

    def test_runtime_update_distinguishes_omitted_and_none(self) -> None:
        with TemporaryDirectory() as directory:
            db_path = Path(directory) / "bridge.sqlite3"
            store = SQLiteBridgeStore(db_path)
            store.create_project(self.make_project())
            store.create_task(self.make_task())
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

    def test_v1_incomplete_schema_is_rejected(self) -> None:
        with TemporaryDirectory() as directory:
            db_path = Path(directory) / "incomplete.sqlite3"
            connection = sqlite3.connect(db_path)
            connection.execute("CREATE TABLE projects (project_id TEXT PRIMARY KEY)")
            connection.execute("CREATE TABLE tasks (task_id TEXT PRIMARY KEY)")
            connection.execute("PRAGMA user_version = 1")
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

    def test_invalid_runtime_state_is_rejected_before_write(self) -> None:
        with TemporaryDirectory() as directory:
            store = SQLiteBridgeStore(Path(directory) / "bridge.sqlite3")
            store.create_project(self.make_project())
            store.create_task(self.make_task())
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


if __name__ == "__main__":
    unittest.main()
