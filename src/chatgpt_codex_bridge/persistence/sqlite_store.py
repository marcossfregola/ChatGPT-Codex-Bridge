"""Small transactional SQLite store for Project, Task, and event state."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import json
import sqlite3
from typing import Any

from ..domain.events import TaskEvent
from ..domain.models import (
    AuditStatus,
    ExecutionStatus,
    Project,
    Task,
    TaskMode,
    TaskStateError,
    timestamp_from_text,
    timestamp_to_text,
    utc_now,
)


SCHEMA_VERSION = 3


class SchemaVersionError(RuntimeError):
    """Raised when a database schema is newer or incomplete."""


_EXECUTION_VALUES = ", ".join(f"'{status.value}'" for status in ExecutionStatus)
_AUDIT_VALUES = ", ".join(f"'{status.value}'" for status in AuditStatus)
_TASK_MODE_VALUES = ", ".join(f"'{mode.value}'" for mode in TaskMode)


_CREATE_PROJECTS_SQL = """
CREATE TABLE projects (
    project_id TEXT PRIMARY KEY NOT NULL,
    name TEXT NOT NULL CHECK (length(trim(name)) > 0),
    repo_path TEXT NOT NULL CHECK (length(trim(repo_path)) > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

_CREATE_TASKS_V1_SQL = f"""
CREATE TABLE tasks (
    task_id TEXT PRIMARY KEY NOT NULL,
    project_id TEXT NOT NULL,
    objective TEXT NOT NULL CHECK (length(trim(objective)) > 0),
    executor TEXT NOT NULL CHECK (length(trim(executor)) > 0),
    model TEXT NOT NULL CHECK (length(trim(model)) > 0),
    execution_status TEXT NOT NULL CHECK (execution_status IN ({_EXECUTION_VALUES})),
    audit_status TEXT NOT NULL CHECK (audit_status IN ({_AUDIT_VALUES})),
    thread_id TEXT,
    turn_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(project_id)
)
"""

_CREATE_TASKS_SQL = f"""
CREATE TABLE tasks (
    task_id TEXT PRIMARY KEY NOT NULL,
    project_id TEXT NOT NULL,
    objective TEXT NOT NULL CHECK (length(trim(objective)) > 0),
    executor TEXT NOT NULL CHECK (length(trim(executor)) > 0),
    model TEXT NOT NULL CHECK (length(trim(model)) > 0),
    execution_status TEXT NOT NULL CHECK (execution_status IN ({_EXECUTION_VALUES})),
    audit_status TEXT NOT NULL CHECK (audit_status IN ({_AUDIT_VALUES})),
    thread_id TEXT,
    turn_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'READ_ONLY' CHECK (mode IN ({_TASK_MODE_VALUES})),
    FOREIGN KEY (project_id) REFERENCES projects(project_id)
)
"""

_CREATE_TASK_EVENTS_SQL = """
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


def _normalize_schema_sql(sql: str) -> str:
    """Normalize only case, whitespace, and a trailing semicolon."""

    return "".join(sql.split()).rstrip(";").upper()


_EXPECTED_COLUMNS_V1 = {
    "projects": (
        ("project_id", "TEXT", 1, 1),
        ("name", "TEXT", 1, 0),
        ("repo_path", "TEXT", 1, 0),
        ("created_at", "TEXT", 1, 0),
        ("updated_at", "TEXT", 1, 0),
    ),
    "tasks": (
        ("task_id", "TEXT", 1, 1),
        ("project_id", "TEXT", 1, 0),
        ("objective", "TEXT", 1, 0),
        ("executor", "TEXT", 1, 0),
        ("model", "TEXT", 1, 0),
        ("execution_status", "TEXT", 1, 0),
        ("audit_status", "TEXT", 1, 0),
        ("thread_id", "TEXT", 0, 0),
        ("turn_id", "TEXT", 0, 0),
        ("created_at", "TEXT", 1, 0),
        ("updated_at", "TEXT", 1, 0),
    ),
}

_EXPECTED_COLUMNS_V2 = {
    **_EXPECTED_COLUMNS_V1,
    "task_events": (
        ("event_id", "INTEGER", 0, 1),
        ("task_id", "TEXT", 1, 0),
        ("source", "TEXT", 1, 0),
        ("kind", "TEXT", 1, 0),
        ("payload_json", "TEXT", 1, 0),
        ("created_at", "TEXT", 1, 0),
    ),
}

_EXPECTED_COLUMNS_V3 = {
    **_EXPECTED_COLUMNS_V2,
    "tasks": (
        ("task_id", "TEXT", 1, 1),
        ("project_id", "TEXT", 1, 0),
        ("objective", "TEXT", 1, 0),
        ("executor", "TEXT", 1, 0),
        ("model", "TEXT", 1, 0),
        ("execution_status", "TEXT", 1, 0),
        ("audit_status", "TEXT", 1, 0),
        ("thread_id", "TEXT", 0, 0),
        ("turn_id", "TEXT", 0, 0),
        ("created_at", "TEXT", 1, 0),
        ("updated_at", "TEXT", 1, 0),
        ("mode", "TEXT", 1, 0),
    ),
}

_EXPECTED_COLUMNS_BY_VERSION = {
    1: _EXPECTED_COLUMNS_V1,
    2: _EXPECTED_COLUMNS_V2,
    3: _EXPECTED_COLUMNS_V3,
}

_EXPECTED_TABLES_BY_VERSION = {
    1: frozenset(_EXPECTED_COLUMNS_V1),
    2: frozenset(_EXPECTED_COLUMNS_V2),
    3: frozenset(_EXPECTED_COLUMNS_V3),
}

_EXPECTED_FOREIGN_KEYS_V1 = {
    "projects": (),
    "tasks": (("projects", "project_id", "project_id"),),
}

_EXPECTED_FOREIGN_KEYS_V2 = {
    **_EXPECTED_FOREIGN_KEYS_V1,
    "task_events": (("tasks", "task_id", "task_id"),),
}

_EXPECTED_FOREIGN_KEYS_BY_VERSION = {
    1: _EXPECTED_FOREIGN_KEYS_V1,
    2: _EXPECTED_FOREIGN_KEYS_V2,
    3: _EXPECTED_FOREIGN_KEYS_V2,
}

_EXPECTED_CHECKS_V1 = {
    "projects": (
        _normalize_schema_sql("CHECK (length(trim(name)) > 0)"),
        _normalize_schema_sql("CHECK (length(trim(repo_path)) > 0)"),
    ),
    "tasks": (
        _normalize_schema_sql("CHECK (length(trim(objective)) > 0)"),
        _normalize_schema_sql("CHECK (length(trim(executor)) > 0)"),
        _normalize_schema_sql("CHECK (length(trim(model)) > 0)"),
        _normalize_schema_sql(
            f"CHECK (execution_status IN ({_EXECUTION_VALUES}))"
        ),
        _normalize_schema_sql(f"CHECK (audit_status IN ({_AUDIT_VALUES}))"),
    ),
}

_EXPECTED_CHECKS_V2 = {
    **_EXPECTED_CHECKS_V1,
    "task_events": (
        _normalize_schema_sql("CHECK (length(trim(source)) > 0)"),
        _normalize_schema_sql("CHECK (length(trim(kind)) > 0)"),
    ),
}

_EXPECTED_CHECKS_V3 = {
    **_EXPECTED_CHECKS_V2,
    "tasks": (
        *_EXPECTED_CHECKS_V1["tasks"],
        _normalize_schema_sql(f"CHECK (mode IN ({_TASK_MODE_VALUES}))"),
    ),
}

_EXPECTED_CHECKS_BY_VERSION = {
    1: _EXPECTED_CHECKS_V1,
    2: _EXPECTED_CHECKS_V2,
    3: _EXPECTED_CHECKS_V3,
}

_EXPECTED_SCHEMA_SQL_V1 = {
    "projects": _normalize_schema_sql(_CREATE_PROJECTS_SQL),
    "tasks": _normalize_schema_sql(_CREATE_TASKS_V1_SQL),
}

_EXPECTED_SCHEMA_SQL_V2 = {
    **_EXPECTED_SCHEMA_SQL_V1,
    "task_events": _normalize_schema_sql(_CREATE_TASK_EVENTS_SQL),
}

_EXPECTED_SCHEMA_SQL_V3 = {
    "projects": _normalize_schema_sql(_CREATE_PROJECTS_SQL),
    "tasks": _normalize_schema_sql(_CREATE_TASKS_SQL),
    "task_events": _normalize_schema_sql(_CREATE_TASK_EVENTS_SQL),
}

_EXPECTED_SCHEMA_SQL_BY_VERSION = {
    1: _EXPECTED_SCHEMA_SQL_V1,
    2: _EXPECTED_SCHEMA_SQL_V2,
    3: _EXPECTED_SCHEMA_SQL_V3,
}

_UNSET = object()
_TERMINAL_EVENT_BY_STATUS = {
    ExecutionStatus.FINISHED: "task.finished",
    ExecutionStatus.FAILED: "task.failed",
    ExecutionStatus.CANCELLED: "task.cancelled",
}
_TERMINAL_EVENT_KINDS = tuple(_TERMINAL_EVENT_BY_STATUS.values())


class SQLiteBridgeStore:
    """Synchronous, single-connection persistence for the 1D domain model."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self._connection: sqlite3.Connection | None = sqlite3.connect(str(self.db_path))
        self._connection.row_factory = sqlite3.Row
        try:
            self._enable_foreign_keys()
            self._ensure_schema()
        except Exception:
            self.close()
            raise

    @property
    def connection(self) -> sqlite3.Connection:
        """Expose the active connection for small operational checks/tests."""

        return self._require_connection()

    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("SQLiteBridgeStore is closed")
        return self._connection

    def _enable_foreign_keys(self) -> None:
        connection = self._require_connection()
        connection.execute("PRAGMA foreign_keys = ON")
        enabled = connection.execute("PRAGMA foreign_keys").fetchone()[0]
        if enabled != 1:
            raise RuntimeError("SQLite foreign keys could not be enabled")

    def _ensure_schema(self) -> None:
        connection = self._require_connection()
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version < 0 or version > SCHEMA_VERSION:
            raise SchemaVersionError(
                f"database schema version {version} is not supported "
                f"(maximum supported is {SCHEMA_VERSION})"
            )
        if version == 0:
            existing_objects = connection.execute(
                "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
            ).fetchall()
            if existing_objects:
                names = ", ".join(sorted(str(row[0]) for row in existing_objects))
                raise SchemaVersionError(
                    "database schema version 0 is not empty; "
                    f"refusing implicit initialization: {names}"
                )
            try:
                connection.execute("BEGIN")
                connection.execute(_CREATE_PROJECTS_SQL)
                connection.execute(_CREATE_TASKS_SQL)
                connection.execute(_CREATE_TASK_EVENTS_SQL)
                self._verify_schema(SCHEMA_VERSION)
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                connection.commit()
            except Exception:
                connection.rollback()
                connection.execute("PRAGMA user_version = 0")
                connection.commit()
                raise
            return
        if version == 1:
            self._migrate_v1_to_v2()
            version = 2
        if version == 2:
            self._migrate_v2_to_v3()
            return
        self._verify_schema(SCHEMA_VERSION)

    def _migrate_v1_to_v2(self) -> None:
        """Validate v1, then atomically add the event journal table."""

        self._verify_schema(1)
        connection = self._require_connection()
        try:
            connection.execute("BEGIN")
            connection.execute(_CREATE_TASK_EVENTS_SQL)
            self._verify_schema(2)
            connection.execute("PRAGMA user_version = 2")
            connection.commit()
        except Exception:
            connection.rollback()
            connection.execute("PRAGMA user_version = 1")
            connection.commit()
            raise

    def _migrate_v2_to_v3(self) -> None:
        """Validate v2, then atomically add the durable task mode."""

        self._verify_schema(2)
        connection = self._require_connection()
        try:
            connection.execute("BEGIN")
            connection.execute(
                "ALTER TABLE tasks ADD COLUMN mode TEXT NOT NULL "
                "DEFAULT 'READ_ONLY' "
                "CHECK (mode IN ('READ_ONLY', 'AUTONOMOUS_WRITE'))"
            )
            self._verify_schema(3)
            connection.execute("PRAGMA user_version = 3")
            connection.commit()
        except Exception:
            connection.rollback()
            connection.execute("PRAGMA user_version = 2")
            connection.commit()
            raise

    def _verify_schema(self, version: int) -> None:
        if version not in _EXPECTED_TABLES_BY_VERSION:
            raise SchemaVersionError(f"schema v{version} is not supported")
        connection = self._require_connection()
        rows = connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        tables = {str(row[0]) for row in rows}
        expected_tables = _EXPECTED_TABLES_BY_VERSION[version]
        missing = expected_tables - tables
        if missing:
            names = ", ".join(sorted(missing))
            raise SchemaVersionError(f"schema v{version} is missing tables: {names}")
        unexpected = tables - expected_tables
        if unexpected:
            names = ", ".join(sorted(unexpected))
            raise SchemaVersionError(f"schema v{version} has unexpected tables: {names}")

        for table, expected_columns in _EXPECTED_COLUMNS_BY_VERSION[version].items():
            info_rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
            actual_columns = tuple(
                (
                    str(row["name"]),
                    str(row["type"]).strip().upper(),
                    int(row["notnull"]),
                    int(row["pk"]),
                )
                for row in info_rows
            )
            if actual_columns != expected_columns:
                raise SchemaVersionError(
                    f"schema v{version} table {table} has unexpected column metadata: "
                    f"{actual_columns!r}"
                )

        for table, expected_foreign_keys in _EXPECTED_FOREIGN_KEYS_BY_VERSION[version].items():
            foreign_keys = connection.execute(
                f"PRAGMA foreign_key_list({table})"
            ).fetchall()
            actual_foreign_keys = tuple(
                sorted(
                    (
                        str(row["table"]),
                        str(row["from"]),
                        str(row["to"]),
                    )
                    for row in foreign_keys
                )
            )
            if actual_foreign_keys != tuple(sorted(expected_foreign_keys)):
                raise SchemaVersionError(
                    f"schema v{version} table {table} has unexpected foreign keys: "
                    f"{actual_foreign_keys!r}"
                )

        table_rows = connection.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        sql_by_table = {
            str(row["name"]): str(row["sql"] or "") for row in table_rows
        }
        for table, expected_sql in _EXPECTED_SCHEMA_SQL_BY_VERSION[version].items():
            normalized_sql = _normalize_schema_sql(sql_by_table[table])
            expected_checks = _EXPECTED_CHECKS_BY_VERSION[version][table]
            if (
                normalized_sql.count("CHECK(") != len(expected_checks)
                or any(check not in normalized_sql for check in expected_checks)
            ):
                raise SchemaVersionError(
                    f"schema v{version} table {table} has incorrect CHECK constraints"
                )
            if normalized_sql != expected_sql:
                raise SchemaVersionError(
                    f"schema v{version} table {table} has an unexpected SQL definition"
                )
        triggers = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger'"
        ).fetchall()
        if triggers:
            names = ", ".join(str(row[0]) for row in triggers)
            raise SchemaVersionError(f"schema v{version} has unexpected triggers: {names}")

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def __enter__(self) -> "SQLiteBridgeStore":
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()

    def create_project(self, project: Project) -> Project:
        connection = self._require_connection()
        with connection:
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
        return project

    def get_project(self, project_id: str) -> Project | None:
        connection = self._require_connection()
        row = connection.execute(
            """
            SELECT project_id, name, repo_path, created_at, updated_at
            FROM projects WHERE project_id = ?
            """,
            (project_id,),
        ).fetchone()
        return self._project_from_row(row) if row is not None else None

    def list_projects(self) -> list[Project]:
        connection = self._require_connection()
        rows = connection.execute(
            """
            SELECT project_id, name, repo_path, created_at, updated_at
            FROM projects ORDER BY project_id
            """
        ).fetchall()
        return [self._project_from_row(row) for row in rows]

    @staticmethod
    def _project_from_row(row: sqlite3.Row) -> Project:
        return Project(
            project_id=row["project_id"],
            name=row["name"],
            repo_path=row["repo_path"],
            created_at=timestamp_from_text(row["created_at"]),
            updated_at=timestamp_from_text(row["updated_at"]),
        )

    def create_task(self, task: Task) -> Task:
        connection = self._require_connection()
        with connection:
            connection.execute(
                """
                INSERT INTO tasks
                    (task_id, project_id, objective, executor, model, mode,
                     execution_status, audit_status, thread_id, turn_id,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task.task_id,
                    task.project_id,
                    task.objective,
                    task.executor,
                    task.model,
                    task.mode.value,
                    task.execution_status.value,
                    task.audit_status.value,
                    task.thread_id,
                    task.turn_id,
                    timestamp_to_text(task.created_at),
                    timestamp_to_text(task.updated_at),
                ),
            )
        return task

    def get_task(self, task_id: str) -> Task | None:
        connection = self._require_connection()
        row = connection.execute(
            """
            SELECT task_id, project_id, objective, executor, model,
                   mode,
                   execution_status, audit_status, thread_id, turn_id,
                   created_at, updated_at
            FROM tasks WHERE task_id = ?
            """,
            (task_id,),
        ).fetchone()
        return self._task_from_row(row) if row is not None else None

    def list_tasks(self, project_id: str) -> list[Task]:
        connection = self._require_connection()
        rows = connection.execute(
            """
            SELECT task_id, project_id, objective, executor, model,
                   mode,
                   execution_status, audit_status, thread_id, turn_id,
                   created_at, updated_at
            FROM tasks WHERE project_id = ? ORDER BY task_id
            """,
            (project_id,),
        ).fetchall()
        return [self._task_from_row(row) for row in rows]

    def list_tasks_by_execution_status(
        self, execution_status: ExecutionStatus | str
    ) -> list[Task]:
        """Return tasks in one lifecycle state, ordered deterministically."""

        try:
            status = (
                execution_status
                if isinstance(execution_status, ExecutionStatus)
                else ExecutionStatus(execution_status)
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid execution_status: {execution_status!r}") from exc
        connection = self._require_connection()
        rows = connection.execute(
            """
            SELECT task_id, project_id, objective, executor, model,
                   mode,
                   execution_status, audit_status, thread_id, turn_id,
                   created_at, updated_at
            FROM tasks WHERE execution_status = ? ORDER BY task_id
            """,
            (status.value,),
        ).fetchall()
        return [self._task_from_row(row) for row in rows]

    @staticmethod
    def _terminal_event_query() -> str:
        placeholders = ", ".join("?" for _ in _TERMINAL_EVENT_KINDS)
        return (
            "SELECT kind FROM task_events WHERE task_id = ? AND source = 'bridge' "
            f"AND kind IN ({placeholders}) LIMIT 1"
        )

    def _assert_no_terminal_event(
        self, connection: sqlite3.Connection, task_id: str
    ) -> None:
        row = connection.execute(
            self._terminal_event_query(),
            (task_id, *_TERMINAL_EVENT_KINDS),
        ).fetchone()
        if row is not None:
            raise TaskStateError(
                f"task {task_id} already has terminal event {row['kind']!r}"
            )

    def transition_task_running(self, task_id: str, *, project_id: str) -> Task:
        """Atomically move QUEUED to RUNNING and append task.started."""

        connection = self._require_connection()
        started_at = utc_now()
        with connection:
            row = connection.execute(
                "SELECT execution_status FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"task does not exist: {task_id}")
            try:
                current = ExecutionStatus(row["execution_status"])
            except (TypeError, ValueError) as exc:
                raise ValueError("invalid task state in database") from exc
            if current is not ExecutionStatus.QUEUED:
                raise TaskStateError(
                    f"task {task_id} cannot run from state {current.value}; "
                    "only QUEUED tasks may run"
                )
            self._assert_no_terminal_event(connection, task_id)
            cursor = connection.execute(
                """
                UPDATE tasks
                SET execution_status = ?, audit_status = ?, updated_at = ?
                WHERE task_id = ? AND execution_status = ?
                """,
                (
                    ExecutionStatus.RUNNING.value,
                    AuditStatus.PENDING.value,
                    timestamp_to_text(started_at),
                    task_id,
                    ExecutionStatus.QUEUED.value,
                ),
            )
            if cursor.rowcount != 1:
                raise TaskStateError(
                    f"task {task_id} could not transition to RUNNING"
                )
            self._insert_task_event_in_transaction(
                connection,
                task_id,
                "bridge",
                "task.started",
                {"project_id": project_id},
                created_at=started_at,
            )
        updated = self.get_task(task_id)
        if updated is None:
            raise RuntimeError("task disappeared after transition")
        return updated

    def transition_task_preflight_failed(
        self,
        task_id: str,
        *,
        policy_payload: Any,
        failed_payload: Any,
    ) -> Task:
        """Atomically fail a QUEUED task rejected by autonomous preflight."""

        connection = self._require_connection()
        failed_at = utc_now()
        with connection:
            row = connection.execute(
                "SELECT execution_status FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"task does not exist: {task_id}")
            try:
                current = ExecutionStatus(row["execution_status"])
            except (TypeError, ValueError) as exc:
                raise ValueError("invalid task state in database") from exc
            if current is not ExecutionStatus.QUEUED:
                if current in _TERMINAL_EVENT_BY_STATUS:
                    raise TaskStateError(
                        f"task {task_id} is already terminal ({current.value})"
                    )
                raise TaskStateError(
                    f"task {task_id} cannot transition from {current.value} "
                    "to FAILED; expected QUEUED"
                )
            self._assert_no_terminal_event(connection, task_id)
            cursor = connection.execute(
                """
                UPDATE tasks
                SET execution_status = ?, audit_status = ?, updated_at = ?
                WHERE task_id = ? AND execution_status = ?
                """,
                (
                    ExecutionStatus.FAILED.value,
                    AuditStatus.PENDING.value,
                    timestamp_to_text(failed_at),
                    task_id,
                    ExecutionStatus.QUEUED.value,
                ),
            )
            if cursor.rowcount != 1:
                raise TaskStateError(
                    f"task {task_id} could not transition to FAILED"
                )
            self._insert_task_event_in_transaction(
                connection,
                task_id,
                "bridge",
                "policy.violation",
                policy_payload,
                created_at=failed_at,
            )
            self._insert_task_event_in_transaction(
                connection,
                task_id,
                "bridge",
                "task.failed",
                failed_payload,
                created_at=failed_at,
            )
        updated = self.get_task(task_id)
        if updated is None:
            raise RuntimeError("task disappeared after preflight failure")
        return updated

    def transition_task_terminal(
        self,
        task_id: str,
        *,
        execution_status: ExecutionStatus | str,
        event_kind: str,
        payload: Any,
        recovery_payload: Any | None = None,
    ) -> Task:
        """Atomically update a RUNNING task and append its terminal event."""

        try:
            status = (
                execution_status
                if isinstance(execution_status, ExecutionStatus)
                else ExecutionStatus(execution_status)
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid execution_status: {execution_status!r}") from exc
        expected_kind = _TERMINAL_EVENT_BY_STATUS.get(status)
        if expected_kind is None or event_kind != expected_kind:
            raise ValueError(
                f"event_kind {event_kind!r} does not match terminal state {status.value}"
            )

        connection = self._require_connection()
        terminal_at = utc_now()
        with connection:
            row = connection.execute(
                "SELECT execution_status FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"task does not exist: {task_id}")
            try:
                current = ExecutionStatus(row["execution_status"])
            except (TypeError, ValueError) as exc:
                raise ValueError("invalid task state in database") from exc
            if current is not ExecutionStatus.RUNNING:
                if current in _TERMINAL_EVENT_BY_STATUS:
                    raise TaskStateError(
                        f"task {task_id} is already terminal ({current.value})"
                    )
                raise TaskStateError(
                    f"task {task_id} cannot transition from {current.value} "
                    f"to {status.value}; expected RUNNING"
                )
            self._assert_no_terminal_event(connection, task_id)
            cursor = connection.execute(
                """
                UPDATE tasks
                SET execution_status = ?, audit_status = ?, updated_at = ?
                WHERE task_id = ? AND execution_status = ?
                """,
                (
                    status.value,
                    AuditStatus.PENDING.value,
                    timestamp_to_text(terminal_at),
                    task_id,
                    ExecutionStatus.RUNNING.value,
                ),
            )
            if cursor.rowcount != 1:
                raise TaskStateError(
                    f"task {task_id} could not transition to {status.value}"
                )
            if recovery_payload is not None:
                self._insert_task_event_in_transaction(
                    connection,
                    task_id,
                    "bridge",
                    "task.recovered",
                    recovery_payload,
                    created_at=terminal_at,
                )
            self._insert_task_event_in_transaction(
                connection,
                task_id,
                "bridge",
                event_kind,
                payload,
                created_at=terminal_at,
            )
        updated = self.get_task(task_id)
        if updated is None:
            raise RuntimeError("task disappeared after terminal transition")
        return updated

    @staticmethod
    def _task_from_row(row: sqlite3.Row) -> Task:
        try:
            return Task(
                task_id=row["task_id"],
                project_id=row["project_id"],
                objective=row["objective"],
                executor=row["executor"],
                model=row["model"],
                mode=row["mode"],
                execution_status=row["execution_status"],
                audit_status=row["audit_status"],
                thread_id=row["thread_id"],
                turn_id=row["turn_id"],
                created_at=timestamp_from_text(row["created_at"]),
                updated_at=timestamp_from_text(row["updated_at"]),
            )
        except (TypeError, ValueError, KeyError) as exc:
            raise ValueError("invalid task state in database") from exc

    def update_task_runtime(
        self,
        task_id: str,
        *,
        execution_status: ExecutionStatus | str | None = None,
        audit_status: AuditStatus | str | None = None,
        thread_id: str | None | object = _UNSET,
        turn_id: str | None | object = _UNSET,
    ) -> Task:
        """Atomically update runtime status and Codex correlation references."""

        updates: list[str] = []
        values: list[Any] = []
        if execution_status is not None:
            try:
                execution_value = (
                    execution_status
                    if isinstance(execution_status, ExecutionStatus)
                    else ExecutionStatus(execution_status)
                )
            except (TypeError, ValueError) as exc:
                raise ValueError(f"invalid execution_status: {execution_status!r}") from exc
            updates.append("execution_status = ?")
            values.append(execution_value.value)
        if audit_status is not None:
            try:
                audit_value = (
                    audit_status
                    if isinstance(audit_status, AuditStatus)
                    else AuditStatus(audit_status)
                )
            except (TypeError, ValueError) as exc:
                raise ValueError(f"invalid audit_status: {audit_status!r}") from exc
            updates.append("audit_status = ?")
            values.append(audit_value.value)
        if thread_id is not _UNSET:
            updates.append("thread_id = ?")
            values.append(self._optional_reference(thread_id, "thread_id"))
        if turn_id is not _UNSET:
            updates.append("turn_id = ?")
            values.append(self._optional_reference(turn_id, "turn_id"))
        if not updates:
            raise ValueError("at least one runtime field must be supplied")

        updates.append("updated_at = ?")
        values.append(timestamp_to_text(utc_now()))
        values.append(task_id)
        connection = self._require_connection()
        with connection:
            cursor = connection.execute(
                f"UPDATE tasks SET {', '.join(updates)} WHERE task_id = ?",
                values,
            )
            if cursor.rowcount != 1:
                raise KeyError(f"task does not exist: {task_id}")
        updated = self.get_task(task_id)
        if updated is None:
            raise RuntimeError("task disappeared after update")
        return updated

    @staticmethod
    def _optional_reference(value: str | None | object, field_name: str) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field_name} must be non-empty text or None")
        return value

    @staticmethod
    def _serialize_payload(payload: Any) -> str:
        try:
            return json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("payload must be JSON-serializable") from exc

    @staticmethod
    def _reject_json_constant(value: str) -> None:
        raise ValueError(f"invalid JSON constant: {value}")

    @classmethod
    def _deserialize_payload(cls, payload_json: str) -> Any:
        if not isinstance(payload_json, str) or not payload_json:
            raise ValueError("stored event payload must be non-empty JSON text")
        try:
            return json.loads(payload_json, parse_constant=cls._reject_json_constant)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("stored event payload is invalid JSON") from exc

    def _insert_task_event_in_transaction(
        self,
        connection: sqlite3.Connection,
        task_id: str,
        source: str,
        kind: str,
        payload: Any,
        *,
        created_at: datetime | None = None,
    ) -> TaskEvent:
        event = TaskEvent(
            event_id=None,
            task_id=task_id,
            source=source,
            kind=kind,
            payload=payload,
            created_at=created_at if created_at is not None else utc_now(),
        )
        payload_json = self._serialize_payload(event.payload)
        cursor = connection.execute(
            """
            INSERT INTO task_events
                (task_id, source, kind, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                event.task_id,
                event.source,
                event.kind,
                payload_json,
                timestamp_to_text(event.created_at),
            ),
        )
        event_id = cursor.lastrowid
        if event_id is None:
            raise RuntimeError("SQLite did not return an event_id")
        row = connection.execute(
            """
            SELECT event_id, task_id, source, kind, payload_json, created_at
            FROM task_events WHERE event_id = ?
            """,
            (event_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError("event disappeared after insert")
        return self._event_from_row(row)

    @classmethod
    def _event_from_row(cls, row: sqlite3.Row) -> TaskEvent:
        try:
            return TaskEvent(
                event_id=row["event_id"],
                task_id=row["task_id"],
                source=row["source"],
                kind=row["kind"],
                payload=cls._deserialize_payload(row["payload_json"]),
                created_at=timestamp_from_text(row["created_at"]),
            )
        except (TypeError, ValueError, KeyError) as exc:
            raise ValueError("invalid task event state in database") from exc

    def append_task_event(
        self,
        task_id: str,
        source: str,
        kind: str,
        payload: Any,
        *,
        created_at: datetime | None = None,
    ) -> TaskEvent:
        connection = self._require_connection()
        with connection:
            return self._insert_task_event_in_transaction(
                connection,
                task_id,
                source,
                kind,
                payload,
                created_at=created_at,
            )

    def list_task_events(self, task_id: str) -> list[TaskEvent]:
        connection = self._require_connection()
        rows = connection.execute(
            """
            SELECT event_id, task_id, source, kind, payload_json, created_at
            FROM task_events
            WHERE task_id = ?
            ORDER BY event_id ASC
            """,
            (task_id,),
        ).fetchall()
        return [self._event_from_row(row) for row in rows]

    def get_last_task_event(self, task_id: str) -> TaskEvent | None:
        connection = self._require_connection()
        row = connection.execute(
            """
            SELECT event_id, task_id, source, kind, payload_json, created_at
            FROM task_events
            WHERE task_id = ?
            ORDER BY event_id DESC
            LIMIT 1
            """,
            (task_id,),
        ).fetchone()
        return self._event_from_row(row) if row is not None else None

    def count_task_events(self, task_id: str) -> int:
        connection = self._require_connection()
        row = connection.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        return int(row[0])


__all__ = ["SCHEMA_VERSION", "SQLiteBridgeStore", "SchemaVersionError"]
