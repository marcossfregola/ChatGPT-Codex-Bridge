"""Small transactional SQLite store for Project, Task, and event state."""

from __future__ import annotations

from datetime import datetime
from collections.abc import Iterable, Mapping
from contextlib import contextmanager
from pathlib import Path
import json
import hashlib
import sqlite3
import uuid
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
SQLITE_BUSY_TIMEOUT_SECONDS = 5.0


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

# D3-R2 uses the existing append-only journal as the durable dispatch queue.
# These names are deliberately constants so the MCP request path and the
# persistent worker cannot drift apart while the SQLite schema remains v3.
D3_R2_CONTRACT = "D3-R2"
EXECUTION_REQUEST_EVENT = "task.execution_requested"
EXECUTION_CLAIM_EVENT = "task.execution_claimed"
# P3-S1 records the durable handoff boundary separately from the worker claim.
# The protocol version on the claim lets recovery apply the strong meaning of
# marker absence only to claims created by workers that know about this event.
EXECUTOR_DISPATCH_STARTED_EVENT = "executor.dispatch_started"
EXECUTOR_DISPATCH_PROTOCOL_VERSION = 1
D3_H3_CONTRACT = "D3-H3"
CANCELLATION_REQUEST_EVENT = "task.cancel_requested"
CANCELLATION_INTERRUPT_SENT_EVENT = "task.cancel_interrupt_sent"
CANCELLATION_INTERRUPT_FAILED_EVENT = "task.cancel_interrupt_failed"
RECONCILIATION_REQUIRED_EVENT = "task.reconciliation_required"
RECONCILIATION_RESOLVED_EVENT = "task.reconciliation_resolved"
# Durable provenance event for an explicit continuation-baseline recovery.
# Keep the event name separate from ``policy.postflight``: this is an
# auditable adoption decision, never a replacement or mutation of history.
RECONCILIATION_BASELINE_ADOPTED_EVENT = "reconciliation.baseline_adopted"
ADOPTION_MODE_LEGACY = "legacy"
ADOPTION_MODE_DIRECT = "direct"

CHECKPOINT_STARTED_EVENT = "checkpoint.commit.started"
CHECKPOINT_REF_UPDATED_EVENT = "checkpoint.commit.ref_updated"
CHECKPOINT_CREATED_EVENT = "checkpoint.commit.created"
CHECKPOINT_FAILED_EVENT = "checkpoint.commit.failed"


class SQLiteBridgeStore:
    """Synchronous, single-connection persistence for the 1D domain model."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self._connection: sqlite3.Connection | None = sqlite3.connect(
            str(self.db_path), timeout=SQLITE_BUSY_TIMEOUT_SECONDS
        )
        self._connection.row_factory = sqlite3.Row
        try:
            self._enable_foreign_keys()
            self._connection.execute(
                f"PRAGMA busy_timeout = {int(SQLITE_BUSY_TIMEOUT_SECONDS * 1000)}"
            )
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

    @contextmanager
    def immediate_transaction(self):
        """Run one bounded SQLite writer transaction.

        ``BEGIN IMMEDIATE`` is intentionally explicit here.  Callers that
        need to close a Bridge-side race can hold the writer reservation only
        for the small decision window and rely on this context manager to
        rollback and release it on every failure path.
        """

        connection = self._require_connection()
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
        except BaseException:
            connection.rollback()
            raise
        else:
            try:
                connection.commit()
            except BaseException:
                # A failed COMMIT may leave the connection in a writable
                # transaction (notably under injected crash/fault tests).
                # Roll it back before the caller retries so the writer lock
                # cannot leak into the next operation.
                connection.rollback()
                raise

    def insert_task_event_in_transaction(
        self,
        connection: sqlite3.Connection,
        task_id: str,
        source: str,
        kind: str,
        payload: Any,
        *,
        created_at: datetime | None = None,
    ) -> TaskEvent:
        """Public transaction-scoped event insertion for Core protocols."""

        return self._insert_task_event_in_transaction(
            connection,
            task_id,
            source,
            kind,
            payload,
            created_at=created_at,
        )

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

    def latest_task_identity_in_transaction(
        self, connection: sqlite3.Connection, project_id: str
    ) -> tuple[str | None, int, int]:
        """Return latest task id, its creation event id, and non-checkpoint HWM."""

        rows = connection.execute(
            "SELECT task_id, created_at FROM tasks WHERE project_id = ? AND mode = ?",
            (project_id, TaskMode.AUTONOMOUS_WRITE.value),
        ).fetchall()
        latest: tuple[tuple[Any, ...], str, int] | None = None
        high_water = 0
        for row in rows:
            task_id = str(row["task_id"])
            event_rows = connection.execute(
                "SELECT event_id, kind FROM task_events WHERE task_id = ? ORDER BY event_id",
                (task_id,),
            ).fetchall()
            creation_ids: list[int] = []
            for event_row in event_rows:
                event_id = int(event_row["event_id"])
                kind = str(event_row["kind"])
                if kind == "task.created":
                    creation_ids.append(event_id)
                if not kind.startswith("checkpoint.commit."):
                    high_water = max(high_water, event_id)
            creation_id = min(creation_ids) if creation_ids else 0
            key: tuple[Any, ...]
            if creation_id:
                key = (0, creation_id, task_id)
            else:
                key = (1, str(row["created_at"]), task_id)
            if latest is None or key > latest[0]:
                latest = (key, task_id, creation_id)
        if latest is None:
            return None, 0, high_water
        return latest[1], latest[2], high_water

    def project_task_high_water(
        self, connection: sqlite3.Connection, project_id: str
    ) -> int:
        """Return the maximum non-checkpoint event id for a project."""

        _, _, high_water = self.latest_task_identity_in_transaction(
            connection, project_id
        )
        return high_water

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

    def _execution_request_in_transaction(
        self, connection: sqlite3.Connection, task_id: str
    ) -> TaskEvent | None:
        """Return the first explicit D3-R2 request for ``task_id``."""

        rows = connection.execute(
            """
            SELECT event_id, task_id, source, kind, payload_json, created_at
            FROM task_events
            WHERE task_id = ? AND source = 'bridge' AND kind = ?
            ORDER BY event_id ASC
            """,
            (task_id, EXECUTION_REQUEST_EVENT),
        ).fetchall()
        for row in rows:
            event = self._event_from_row(row)
            if isinstance(event.payload, Mapping) and event.payload.get("contract") == D3_R2_CONTRACT:
                return event
        return None

    def get_execution_request(self, task_id: str) -> TaskEvent | None:
        """Return the durable D3-R2 request, if one exists."""

        connection = self._require_connection()
        return self._execution_request_in_transaction(connection, task_id)

    def get_execution_claim(self, task_id: str) -> TaskEvent | None:
        """Return the latest durable worker claim, if one exists."""

        connection = self._require_connection()
        row = connection.execute(
            """
            SELECT event_id, task_id, source, kind, payload_json, created_at
            FROM task_events
            WHERE task_id = ? AND source = 'bridge' AND kind = ?
            ORDER BY event_id DESC
            LIMIT 1
            """,
            (task_id, EXECUTION_CLAIM_EVENT),
        ).fetchone()
        return self._event_from_row(row) if row is not None else None

    def request_task_execution(
        self, task_id: str, payload: Mapping[str, Any]
    ) -> tuple[Task, TaskEvent, bool]:
        """Durably enqueue one explicit D3-R2 execution request.

        The state check, idempotency lookup, and event insert are one
        ``BEGIN IMMEDIATE`` transaction.  ``bool`` is true only when this
        call inserted the request; a retry receives the original event.
        """

        if not isinstance(payload, Mapping):
            raise ValueError("execution request payload must be an object")
        normalized_payload = dict(payload)
        if normalized_payload.get("contract") != D3_R2_CONTRACT:
            raise ValueError("execution request payload must declare contract D3-R2")

        connection = self._require_connection()
        request_event: TaskEvent | None = None
        created = False
        requested_at = utc_now()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT task_id, project_id, objective, executor, model,
                       mode, execution_status, audit_status, thread_id, turn_id,
                       created_at, updated_at
                FROM tasks WHERE task_id = ?
                """,
                (task_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"task does not exist: {task_id}")
            task = self._task_from_row(row)
            if task.execution_status is not ExecutionStatus.QUEUED:
                raise TaskStateError(
                    f"task {task_id} cannot request execution from state "
                    f"{task.execution_status.value}; only QUEUED tasks may request"
                )

            request_event = self._execution_request_in_transaction(connection, task_id)
            if request_event is None:
                request_event = self._insert_task_event_in_transaction(
                    connection,
                    task_id,
                    "bridge",
                    EXECUTION_REQUEST_EVENT,
                    normalized_payload,
                    created_at=requested_at,
                )
                created = True
            connection.commit()
        except Exception:
            connection.rollback()
            raise

        updated = self.get_task(task_id)
        if updated is None or request_event is None:
            raise RuntimeError("task disappeared after execution request")
        return updated, request_event, created

    def find_next_requested_task(self) -> Task | None:
        """Return the oldest QUEUED task with an explicit D3-R2 request."""

        candidates: list[tuple[int, Task]] = []
        for task in self.list_tasks_by_execution_status(ExecutionStatus.QUEUED):
            event = self.get_execution_request(task.task_id)
            if event is not None:
                candidates.append((event.event_id or 0, task))
        if not candidates:
            return None
        return min(candidates, key=lambda item: (item[0], item[1].task_id))[1]

    def claim_task_execution(
        self, task_id: str, owner_payload: Mapping[str, Any]
    ) -> tuple[Task, TaskEvent]:
        """Atomically claim one requested task for the persistent worker."""

        if not isinstance(owner_payload, Mapping):
            raise ValueError("execution owner payload must be an object")
        owner_kind = owner_payload.get("owner_kind")
        owner_id = owner_payload.get("owner_id")
        pid = owner_payload.get("pid")
        if owner_kind != "persistent_worker":
            raise ValueError("execution owner must be a persistent_worker")
        if not isinstance(owner_id, str) or not owner_id.strip():
            raise ValueError("execution owner_id must be non-empty text")
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            raise ValueError("execution owner pid must be a positive integer")
        normalized_owner = {
            "owner_kind": owner_kind,
            "owner_id": owner_id,
            "pid": pid,
            "executor_dispatch_protocol_version": EXECUTOR_DISPATCH_PROTOCOL_VERSION,
        }
        connection = self._require_connection()
        claim_event: TaskEvent | None = None
        claimed_at = utc_now()
        normalized_owner["claimed_at"] = timestamp_to_text(claimed_at)
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT task_id, project_id, objective, executor, model,
                       mode, execution_status, audit_status, thread_id, turn_id,
                       created_at, updated_at
                FROM tasks WHERE task_id = ?
                """,
                (task_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"task does not exist: {task_id}")
            task = self._task_from_row(row)
            if task.execution_status is not ExecutionStatus.QUEUED:
                raise TaskStateError(
                    f"task {task_id} cannot be claimed from state "
                    f"{task.execution_status.value}; only QUEUED tasks may be claimed"
                )
            request_event = self._execution_request_in_transaction(connection, task_id)
            if request_event is None:
                raise TaskStateError(
                    f"task {task_id} has no explicit D3-R2 execution request"
                )
            started = connection.execute(
                """
                SELECT event_id FROM task_events
                WHERE task_id = ? AND source = 'bridge' AND kind = 'task.started'
                LIMIT 1
                """,
                (task_id,),
            ).fetchone()
            if started is not None:
                raise TaskStateError(f"task {task_id} already has task.started")
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
                    timestamp_to_text(claimed_at),
                    task_id,
                    ExecutionStatus.QUEUED.value,
                ),
            )
            if cursor.rowcount != 1:
                raise TaskStateError(f"task {task_id} could not be claimed")
            claim_event = self._insert_task_event_in_transaction(
                connection,
                task_id,
                "bridge",
                EXECUTION_CLAIM_EVENT,
                normalized_owner,
                created_at=claimed_at,
            )
            self._insert_task_event_in_transaction(
                connection,
                task_id,
                "bridge",
                "task.started",
                {"project_id": task.project_id},
                created_at=claimed_at,
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise

        updated = self.get_task(task_id)
        if updated is None or claim_event is None:
            raise RuntimeError("task disappeared after execution claim")
        return updated, claim_event

    def transition_task_preflight_failed(
        self,
        task_id: str,
        *,
        policy_payload: Any,
        failed_payload: Any,
        expected_status: ExecutionStatus | str = ExecutionStatus.QUEUED,
    ) -> Task:
        """Atomically fail a task rejected by autonomous preflight."""

        try:
            expected = (
                expected_status
                if isinstance(expected_status, ExecutionStatus)
                else ExecutionStatus(expected_status)
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid expected_status: {expected_status!r}") from exc

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
            if current is not expected:
                if current in _TERMINAL_EVENT_BY_STATUS:
                    raise TaskStateError(
                        f"task {task_id} is already terminal ({current.value})"
                    )
                raise TaskStateError(
                    f"task {task_id} cannot transition from {current.value} "
                    f"to FAILED; expected {expected.value}"
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
                    expected.value,
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

    @staticmethod
    def _reconciliation_fingerprint(payload: Mapping[str, Any]) -> str:
        """Return a stable digest for durable reconciliation evidence."""

        def stable(value: Any, *, parent_key: str | None = None) -> Any:
            if isinstance(value, Mapping):
                result: dict[str, Any] = {}
                for key, item in value.items():
                    key_text = str(key)
                    # Process identity is deliberately correlation-only.  It
                    # may change or become unavailable after a crash and must
                    # never alter the durable reconciliation identity.
                    if key_text.lower() in {
                        "pid",
                        "process_id",
                        "command_line",
                        "sidecar",
                    }:
                        continue
                    result[key_text] = stable(item, parent_key=key_text)
                return result
            if isinstance(value, (list, tuple)):
                return [stable(item, parent_key=parent_key) for item in value]
            return value

        material = stable(
            {
                key: value
                for key, value in payload.items()
                if key not in {"reconciliation_id", "evidence_fingerprint"}
            }
        )
        encoded = json.dumps(
            material,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @classmethod
    def _reconciliation_state_from_events(
        cls, events: Iterable[TaskEvent]
    ) -> dict[str, Any] | None:
        """Derive the latest pending/resolved reconciliation from the journal."""

        required: TaskEvent | None = None
        resolved: dict[str, TaskEvent] = {}
        for event in sorted(events, key=lambda item: item.event_id or 0):
            payload = event.payload
            if not isinstance(payload, Mapping):
                continue
            reconciliation_id = payload.get("reconciliation_id")
            if not isinstance(reconciliation_id, str) or not reconciliation_id:
                continue
            if event.source != "bridge":
                continue
            if event.kind == RECONCILIATION_REQUIRED_EVENT:
                required = event
            elif event.kind == RECONCILIATION_RESOLVED_EVENT:
                resolved[reconciliation_id] = event
        if required is None:
            return None
        required_payload = (
            dict(required.payload) if isinstance(required.payload, Mapping) else {}
        )
        reconciliation_id = required_payload.get("reconciliation_id")
        if not isinstance(reconciliation_id, str) or not reconciliation_id:
            return None
        resolved_event = resolved.get(reconciliation_id)
        return {
            "required": True,
            "resolved": resolved_event is not None,
            "reconciliation_id": reconciliation_id,
            "evidence_fingerprint": required_payload.get("evidence_fingerprint"),
            "required_event_id": required.event_id,
            "required_event": required,
            "required_payload": required_payload,
            "resolved_event": resolved_event,
        }

    def get_reconciliation_state(self, task_id: str) -> dict[str, Any] | None:
        """Return the journal-derived reconciliation projection for one task."""

        connection = self._require_connection()
        rows = connection.execute(
            """
            SELECT event_id, task_id, source, kind, payload_json, created_at
            FROM task_events
            WHERE task_id = ? AND source = 'bridge'
              AND kind IN (?, ?)
            ORDER BY event_id ASC
            """,
            (task_id, RECONCILIATION_REQUIRED_EVENT, RECONCILIATION_RESOLVED_EVENT),
        ).fetchall()
        return self._reconciliation_state_from_events(
            [self._event_from_row(row) for row in rows]
        )

    def ensure_reconciliation_required(
        self,
        task_id: str,
        payload: Mapping[str, Any],
    ) -> tuple[Task, TaskEvent | None, bool]:
        """Append one idempotent reconciliation marker while the task is RUNNING."""

        if not isinstance(payload, Mapping):
            raise ValueError("reconciliation payload must be an object")
        normalized = dict(payload)
        reconciliation_id = normalized.get("reconciliation_id")
        if reconciliation_id is None:
            reconciliation_id = str(uuid.uuid4())
        if not isinstance(reconciliation_id, str) or not reconciliation_id.strip():
            raise ValueError("reconciliation_id must be non-empty text")
        normalized["reconciliation_id"] = reconciliation_id
        fingerprint = normalized.get("evidence_fingerprint")
        if not isinstance(fingerprint, str) or not fingerprint:
            normalized["evidence_fingerprint"] = self._reconciliation_fingerprint(
                normalized
            )

        connection = self._require_connection()
        marker: TaskEvent | None = None
        created = False
        marked_at = utc_now()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT task_id, project_id, objective, executor, model, mode, "
                "execution_status, audit_status, thread_id, turn_id, created_at, updated_at "
                "FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"task does not exist: {task_id}")
            task = self._task_from_row(row)
            events = self._events_in_transaction(connection, task_id)
            state = self._reconciliation_state_from_events(events)
            if task.execution_status is not ExecutionStatus.RUNNING:
                connection.commit()
                return task, None, False
            if self._has_bridge_terminal_event(events):
                connection.commit()
                return task, None, False
            if state is not None and not state["resolved"]:
                connection.commit()
                return task, state["required_event"], False
            marker = self._insert_task_event_in_transaction(
                connection,
                task_id,
                "bridge",
                RECONCILIATION_REQUIRED_EVENT,
                normalized,
                created_at=marked_at,
            )
            created = True
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

        updated = self.get_task(task_id)
        if updated is None or marker is None:
            raise RuntimeError("task disappeared after reconciliation marker")
        return updated, marker, created

    @staticmethod
    def _has_bridge_terminal_event(events: Iterable[TaskEvent]) -> bool:
        return any(
            event.source == "bridge" and event.kind in _TERMINAL_EVENT_KINDS
            for event in events
        )

    @staticmethod
    def _codex_event_matches_task(task: Task, event: TaskEvent) -> bool:
        """Return whether a Codex event is exactly correlated to ``task``.

        Missing IDs are intentionally *not* treated as a match.  Recovery may
        therefore leave the task pending, but a malformed/unrelated terminal
        notification cannot block the narrow manual FAILED resolver.
        """

        if event.source != "codex" or not isinstance(event.payload, Mapping):
            return False
        payload = event.payload
        thread_id = payload.get("thread_id", payload.get("threadId"))
        turn_id = payload.get("turn_id", payload.get("turnId"))
        turn = payload.get("turn")
        if isinstance(turn, Mapping):
            if turn_id is None:
                turn_id = turn.get("id")
        return (
            isinstance(task.thread_id, str)
            and isinstance(task.turn_id, str)
            and thread_id == task.thread_id
            and turn_id == task.turn_id
        )

    def _events_in_transaction(
        self, connection: sqlite3.Connection, task_id: str
    ) -> list[TaskEvent]:
        rows = connection.execute(
            """
            SELECT event_id, task_id, source, kind, payload_json, created_at
            FROM task_events WHERE task_id = ? ORDER BY event_id ASC
            """,
            (task_id,),
        ).fetchall()
        return [self._event_from_row(row) for row in rows]

    def transition_task_recovered_terminal(
        self,
        task_id: str,
        *,
        execution_status: ExecutionStatus | str,
        event_kind: str,
        payload: Mapping[str, Any],
        recovery_payload: Mapping[str, Any],
    ) -> tuple[Task, bool]:
        """Apply one evidence-backed terminal transition during startup recovery."""

        try:
            status = (
                execution_status
                if isinstance(execution_status, ExecutionStatus)
                else ExecutionStatus(execution_status)
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid execution_status: {execution_status!r}") from exc
        expected_kind = _TERMINAL_EVENT_BY_STATUS.get(status)
        if expected_kind != event_kind:
            raise ValueError("recovery event does not match terminal status")
        connection = self._require_connection()
        recovered_at = utc_now()
        changed = False
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT task_id, project_id, objective, executor, model, mode, "
                "execution_status, audit_status, thread_id, turn_id, created_at, updated_at "
                "FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"task does not exist: {task_id}")
            task = self._task_from_row(row)
            events = self._events_in_transaction(connection, task_id)
            if task.execution_status is not ExecutionStatus.RUNNING:
                connection.commit()
                return task, False
            if self._has_bridge_terminal_event(events):
                connection.commit()
                return task, False
            cursor = connection.execute(
                "UPDATE tasks SET execution_status = ?, audit_status = ?, updated_at = ? "
                "WHERE task_id = ? AND execution_status = ?",
                (
                    status.value,
                    AuditStatus.PENDING.value,
                    timestamp_to_text(recovered_at),
                    task_id,
                    ExecutionStatus.RUNNING.value,
                ),
            )
            if cursor.rowcount != 1:
                raise TaskStateError(f"task {task_id} could not be recovered")
            self._insert_task_event_in_transaction(
                connection,
                task_id,
                "bridge",
                "task.recovered",
                dict(recovery_payload),
                created_at=recovered_at,
            )
            self._insert_task_event_in_transaction(
                connection,
                task_id,
                "bridge",
                event_kind,
                dict(payload),
                created_at=recovered_at,
            )
            changed = True
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        updated = self.get_task(task_id)
        if updated is None:
            raise RuntimeError("task disappeared after recovery")
        return updated, changed

    def resolve_task_reconciliation(
        self,
        task_id: str,
        reconciliation_id: str,
        *,
        resolution: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Resolve one pending reconciliation atomically to FAILED only."""

        if resolution != ExecutionStatus.FAILED.value:
            raise ValueError("only FAILED reconciliation resolution is supported")
        if not isinstance(reconciliation_id, str) or not reconciliation_id.strip():
            raise ValueError("reconciliation_id must be non-empty text")
        if not isinstance(payload, Mapping):
            raise ValueError("reconciliation payload must be an object")

        connection = self._require_connection()
        reason = "execution outcome could not be recovered after execution owner loss"
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT task_id, project_id, objective, executor, model, mode, "
                "execution_status, audit_status, thread_id, turn_id, created_at, updated_at "
                "FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"task does not exist: {task_id}")
            task = self._task_from_row(row)
            events = self._events_in_transaction(connection, task_id)
            state = self._reconciliation_state_from_events(events)
            if state is not None and state["resolved"] and state["reconciliation_id"] == reconciliation_id:
                connection.commit()
                return {
                    "task_id": task_id,
                    "execution_status": task.execution_status.value,
                    "reconciliation_id": reconciliation_id,
                    "resolved": True,
                    "already_resolved": True,
                    "resolution": resolution,
                    "terminal": task.execution_status in _TERMINAL_EVENT_BY_STATUS,
                }
            if state is None or not state["required"]:
                raise TaskStateError(f"task {task_id} has no pending reconciliation")
            if state["reconciliation_id"] != reconciliation_id or state["resolved"]:
                raise TaskStateError("reconciliation_id is stale or already resolved")
            if task.execution_status is not ExecutionStatus.RUNNING:
                raise TaskStateError(
                    f"task {task_id} cannot be resolved from state {task.execution_status.value}"
                )
            required_event_id = state.get("required_event_id") or 0
            for event in events:
                if (event.event_id or 0) <= required_event_id:
                    continue
                if event.source == "bridge" and event.kind in {
                    RECONCILIATION_REQUIRED_EVENT,
                    "task.execution_claimed",
                    "task.started",
                    "task.finished",
                    "task.failed",
                    "task.cancelled",
                }:
                    raise TaskStateError(
                        "reconciliation changed after the required marker"
                    )
                if event.kind in {
                    "turn/completed",
                    "turn/failed",
                    "turn/interrupted",
                    "turn/cancelled",
                    "turn/aborted",
                } and self._codex_event_matches_task(task, event):
                    raise TaskStateError(
                        "new Codex terminal evidence requires automatic reconciliation"
                    )
            now = utc_now()
            resolved_payload = dict(payload)
            resolved_payload.update(
                {
                    "reconciliation_id": reconciliation_id,
                    "evidence_fingerprint": state.get("evidence_fingerprint"),
                    "resolution": resolution,
                    "reason": reason,
                }
            )
            cursor = connection.execute(
                "UPDATE tasks SET execution_status = ?, audit_status = ?, updated_at = ? "
                "WHERE task_id = ? AND execution_status = ?",
                (
                    ExecutionStatus.FAILED.value,
                    AuditStatus.PENDING.value,
                    timestamp_to_text(now),
                    task_id,
                    ExecutionStatus.RUNNING.value,
                ),
            )
            if cursor.rowcount != 1:
                raise TaskStateError(f"task {task_id} could not be resolved")
            self._insert_task_event_in_transaction(
                connection,
                task_id,
                "bridge",
                RECONCILIATION_RESOLVED_EVENT,
                resolved_payload,
                created_at=now,
            )
            failed_payload = dict(resolved_payload)
            failed_payload["recovered_from"] = ExecutionStatus.RUNNING.value
            self._insert_task_event_in_transaction(
                connection,
                task_id,
                "bridge",
                "task.failed",
                failed_payload,
                created_at=now,
            )
            connection.commit()
            return {
                "task_id": task_id,
                "execution_status": ExecutionStatus.FAILED.value,
                "reconciliation_id": reconciliation_id,
                "resolved": True,
                "already_resolved": False,
                "resolution": resolution,
                "terminal": True,
            }
        except BaseException:
            connection.rollback()
            raise

    def persist_reconciliation_baseline_adoption(
        self,
        source_task_id: str,
        payload: Mapping[str, Any],
        *,
        source_high_water: int,
        inspection_task_id: str | None,
        inspection_high_water: int,
        adoption_mode: str | None = None,
    ) -> tuple[TaskEvent, bool]:
        """Atomically append one immutable, explicitly adopted baseline."""

        if not isinstance(source_task_id, str) or not source_task_id.strip():
            raise ValueError("source_task_id must be non-empty text")
        if not isinstance(payload, Mapping):
            raise ValueError("adoption payload must be an object")
        payload_mode = payload.get("adoption_mode", ADOPTION_MODE_LEGACY)
        if adoption_mode is None:
            adoption_mode = payload_mode
        if not isinstance(adoption_mode, str) or adoption_mode not in {
            ADOPTION_MODE_LEGACY,
            ADOPTION_MODE_DIRECT,
        }:
            raise ValueError("adoption mode is unsupported")
        if payload_mode != adoption_mode:
            raise ValueError("adoption payload adoption_mode differs")
        if adoption_mode == ADOPTION_MODE_LEGACY:
            if not isinstance(inspection_task_id, str) or not inspection_task_id.strip():
                raise ValueError("inspection_task_id must be non-empty text")
        elif inspection_task_id is not None:
            raise ValueError("direct adoption cannot include inspection_task_id")
        if (
            isinstance(source_high_water, bool)
            or not isinstance(source_high_water, int)
            or source_high_water < 0
            or isinstance(inspection_high_water, bool)
            or not isinstance(inspection_high_water, int)
            or inspection_high_water < 0
        ):
            raise ValueError("event high-water marks must be non-negative integers")
        if adoption_mode == ADOPTION_MODE_DIRECT and inspection_high_water != 0:
            raise ValueError("direct adoption inspection high-water must be zero")

        connection = self._require_connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            source_row = connection.execute(
                """
                SELECT task_id, project_id, objective, executor, model,
                       mode, execution_status, audit_status, thread_id, turn_id,
                       created_at, updated_at
                FROM tasks WHERE task_id = ?
                """,
                (source_task_id,),
            ).fetchone()
            if source_row is None:
                raise KeyError(f"task does not exist: {source_task_id}")
            source_task = self._task_from_row(source_row)
            if source_task.mode is not TaskMode.AUTONOMOUS_WRITE:
                raise TaskStateError(
                    "reconciliation baseline source must be AUTONOMOUS_WRITE"
                )
            if source_task.execution_status is not ExecutionStatus.FINISHED:
                raise TaskStateError(
                    "reconciliation baseline source must be FINISHED"
                )

            inspection_task = None
            if adoption_mode == ADOPTION_MODE_LEGACY:
                inspection_row = connection.execute(
                    """
                    SELECT task_id, project_id, objective, executor, model,
                           mode, execution_status, audit_status, thread_id, turn_id,
                           created_at, updated_at
                    FROM tasks WHERE task_id = ?
                    """,
                    (inspection_task_id,),
                ).fetchone()
                if inspection_row is None:
                    raise KeyError(f"task does not exist: {inspection_task_id}")
                inspection_task = self._task_from_row(inspection_row)
                if inspection_task.mode is not TaskMode.READ_ONLY:
                    raise TaskStateError("reconciliation inspection must be READ_ONLY")
                if inspection_task.execution_status is not ExecutionStatus.FINISHED:
                    raise TaskStateError("reconciliation inspection must be FINISHED")
                if inspection_task.project_id != source_task.project_id:
                    raise TaskStateError(
                        "reconciliation inspection belongs to another project"
                    )

            source_events = self._events_in_transaction(connection, source_task_id)
            inspection_events = (
                self._events_in_transaction(connection, inspection_task_id)
                if adoption_mode == ADOPTION_MODE_LEGACY
                else []
            )
            source_max = max((event.event_id or 0 for event in source_events), default=0)
            inspection_max = max(
                (event.event_id or 0 for event in inspection_events), default=0
            )
            candidate_json = self._serialize_payload(dict(payload))
            existing = [
                event
                for event in source_events
                if event.source == "bridge"
                and event.kind == RECONCILIATION_BASELINE_ADOPTED_EVENT
            ]
            if existing:
                existing_payload = existing[0].payload
                if isinstance(existing_payload, Mapping) and (
                    adoption_mode == ADOPTION_MODE_LEGACY
                    and "adoption_mode" not in existing_payload
                ):
                    existing_payload = dict(existing_payload)
                    existing_payload["adoption_mode"] = ADOPTION_MODE_LEGACY
                if len(existing) != 1 or self._serialize_payload(existing_payload) != candidate_json:
                    raise TaskStateError(
                        "reconciliation baseline adoption provenance or snapshot differs"
                    )
                if source_max > source_high_water or inspection_max > inspection_high_water:
                    raise TaskStateError("task journal changed during baseline adoption")
                connection.commit()
                return existing[0], False

            if source_max > source_high_water or inspection_max > inspection_high_water:
                raise TaskStateError("task journal changed during baseline adoption")

            if payload.get("source_task_id") != source_task_id:
                raise TaskStateError("adoption source_task_id differs")
            if adoption_mode == ADOPTION_MODE_LEGACY and payload.get(
                "inspection_task_id"
            ) != inspection_task_id:
                raise TaskStateError("adoption inspection_task_id differs")
            if adoption_mode == ADOPTION_MODE_DIRECT and payload.get(
                "inspection_task_id"
            ) is not None:
                raise TaskStateError("direct adoption inspection_task_id must be null")
            if payload.get("project_id") != source_task.project_id:
                raise TaskStateError("adoption project_id differs")

            def required_event(
                events: list[TaskEvent], key: str, expected_source: str, expected_kind: str
            ) -> TaskEvent:
                value = payload.get(key)
                if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                    raise TaskStateError(f"adoption {key} is invalid")
                match = next(
                    (
                        event
                        for event in events
                        if event.event_id == value
                        and event.source == expected_source
                        and event.kind == expected_kind
                    ),
                    None,
                )
                if match is None:
                    raise TaskStateError(
                        f"adoption {key} does not identify required evidence"
                    )
                return match

            source_postflight = required_event(
                source_events,
                "source_postflight_event_id",
                "bridge",
                "policy.postflight",
            )
            inspection_terminal = None
            if adoption_mode == ADOPTION_MODE_LEGACY:
                inspection_terminal = required_event(
                    inspection_events,
                    "inspection_terminal_event_id",
                    "bridge",
                    "task.finished",
                )
            source_terminal = max(
                (
                    event
                    for event in source_events
                    if event.source == "bridge" and event.kind == "task.finished"
                ),
                key=lambda event: event.event_id or 0,
                default=None,
            )
            if (
                source_terminal is None
                or (source_terminal.event_id or 0) <= (source_postflight.event_id or 0)
            ):
                raise TaskStateError("source terminal evidence is not posterior")
            if (
                source_terminal is not None
                and isinstance(source_terminal.payload, Mapping)
                and source_terminal.payload.get("policy_violation") is True
            ):
                raise TaskStateError("source terminal evidence has a policy violation")
            if adoption_mode == ADOPTION_MODE_LEGACY:
                if (
                    inspection_terminal is None
                    or (source_postflight.event_id or 0)
                    >= (inspection_terminal.event_id or 0)
                ):
                    raise TaskStateError("inspection terminal evidence is not posterior")
                if (
                    isinstance(inspection_terminal.payload, Mapping)
                    and inspection_terminal.payload.get("policy_violation") is True
                ):
                    raise TaskStateError(
                        "inspection terminal evidence has a policy violation"
                    )
            if adoption_mode == ADOPTION_MODE_DIRECT and any(
                payload.get(key) is not None
                for key in ("inspection_terminal_event_id",)
            ):
                raise TaskStateError(
                    "direct adoption inspection evidence must be absent"
                )
            source_high_water_payload = payload.get("source_high_water_event_id")
            inspection_high_water_payload = payload.get("inspection_high_water_event_id")
            if (
                isinstance(source_high_water_payload, bool)
                or not isinstance(source_high_water_payload, int)
                or source_high_water_payload != source_high_water
                or source_high_water_payload < (source_postflight.event_id or 0)
            ):
                raise TaskStateError("adoption high-water provenance is invalid")
            if adoption_mode == ADOPTION_MODE_LEGACY:
                if (
                    isinstance(inspection_high_water_payload, bool)
                    or not isinstance(inspection_high_water_payload, int)
                    or inspection_high_water_payload != inspection_high_water
                    or inspection_high_water_payload < (inspection_terminal.event_id or 0)
                ):
                    raise TaskStateError("adoption high-water provenance is invalid")
            elif (
                isinstance(inspection_high_water_payload, bool)
                or not isinstance(inspection_high_water_payload, int)
                or inspection_high_water_payload != 0
            ):
                raise TaskStateError("direct adoption inspection high-water must be zero")
            if any(
                event.source == "bridge" and event.kind == "policy.violation"
                for event in source_events
            ) or (
                adoption_mode == ADOPTION_MODE_LEGACY
                and any(
                    event.source == "bridge" and event.kind == "policy.violation"
                    for event in inspection_events
                )
            ):
                raise TaskStateError("adoption provenance contains a policy violation")
            if payload.get("schema_version") != 1 or payload.get("baseline_kind") != "reconciled_continuation":
                raise TaskStateError("adoption schema or baseline kind is invalid")

            event = self._insert_task_event_in_transaction(
                connection,
                source_task_id,
                "bridge",
                RECONCILIATION_BASELINE_ADOPTED_EVENT,
                dict(payload),
                created_at=utc_now(),
            )
            connection.commit()
            return event, True
        except BaseException:
            connection.rollback()
            raise

    @staticmethod
    def _validate_event_limit(limit: int) -> None:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("event limit must be a positive integer")

    @staticmethod
    def _validate_event_cursor(since_event_id: int) -> None:
        if (
            isinstance(since_event_id, bool)
            or not isinstance(since_event_id, int)
            or since_event_id < 0
        ):
            raise ValueError("since_event_id must be a non-negative integer")

    def list_task_events(
        self, task_id: str, limit: int | None = None
    ) -> list[TaskEvent]:
        connection = self._require_connection()
        query = """
            SELECT event_id, task_id, source, kind, payload_json, created_at
            FROM task_events
            WHERE task_id = ?
            ORDER BY event_id ASC
        """
        params: list[Any] = [task_id]
        if limit is not None:
            self._validate_event_limit(limit)
            query += " LIMIT ?"
            params.append(limit)
        rows = connection.execute(query, tuple(params)).fetchall()
        return [self._event_from_row(row) for row in rows]

    def list_task_events_window(
        self,
        task_id: str,
        limit: int,
        *,
        since_event_id: int | None = None,
        critical_kinds: Iterable[str] = (),
        critical_limit: int = 64,
    ) -> tuple[list[TaskEvent], int]:
        """Read a bounded event window in durable ``event_id`` order.

        Without ``since_event_id`` this preserves the legacy bounded head plus
        recent-critical-event view.  With a cursor, the selection is strictly
        ``event_id > since_event_id`` and never adds a critical tail; this
        cursor mode is the gap-free incremental journal contract.  The returned
        count is the total matching event count for the selected mode.
        """

        self._validate_event_limit(limit)
        if since_event_id is not None:
            self._validate_event_cursor(since_event_id)
        self._validate_event_limit(critical_limit)
        connection = self._require_connection()

        if since_event_id is not None:
            rows = connection.execute(
                """
                SELECT event_id, task_id, source, kind, payload_json, created_at
                FROM task_events
                WHERE task_id = ? AND event_id > ?
                ORDER BY event_id ASC
                LIMIT ?
                """,
                (task_id, since_event_id, limit),
            ).fetchall()
            # Count after selecting so an append between the page read and
            # this count makes the response conservatively truncated rather
            # than allowing a caller to stop before seeing the new event.
            total = int(
                connection.execute(
                    "SELECT COUNT(*) FROM task_events "
                    "WHERE task_id = ? AND event_id > ?",
                    (task_id, since_event_id),
                ).fetchone()[0]
            )
            return [self._event_from_row(row) for row in rows], total

        total = self.count_task_events(task_id)
        head_rows = connection.execute(
            """
            SELECT event_id, task_id, source, kind, payload_json, created_at
            FROM task_events
            WHERE task_id = ?
            ORDER BY event_id ASC
            LIMIT ?
            """,
            (task_id, limit),
        ).fetchall()
        events = [self._event_from_row(row) for row in head_rows]
        kinds = tuple(dict.fromkeys(kind for kind in critical_kinds if isinstance(kind, str)))
        if total <= limit or not kinds:
            return events, total

        placeholders = ", ".join("?" for _ in kinds)
        rows = connection.execute(
            f"""
            SELECT event_id, task_id, source, kind, payload_json, created_at
            FROM task_events
            WHERE task_id = ? AND kind IN ({placeholders})
            ORDER BY event_id DESC
            LIMIT ?
            """,
            (task_id, *kinds, critical_limit),
        ).fetchall()
        by_id = {event.event_id: event for event in events}
        for row in rows:
            event = self._event_from_row(row)
            by_id[event.event_id] = event
        return sorted(by_id.values(), key=lambda event: event.event_id or 0), total

    def get_latest_task_events(
        self, task_id: str, kinds: Iterable[str]
    ) -> list[TaskEvent]:
        """Read at most one latest event for each requested kind."""

        connection = self._require_connection()
        unique_kinds = tuple(dict.fromkeys(kind for kind in kinds if isinstance(kind, str)))
        events: list[TaskEvent] = []
        for kind in unique_kinds:
            row = connection.execute(
                """
                SELECT event_id, task_id, source, kind, payload_json, created_at
                FROM task_events
                WHERE task_id = ? AND kind = ?
                ORDER BY event_id DESC
                LIMIT 1
                """,
                (task_id, kind),
            ).fetchone()
            if row is not None:
                events.append(self._event_from_row(row))
        return sorted(events, key=lambda event: event.event_id or 0)

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

    def _cancellation_request_in_transaction(
        self, connection: sqlite3.Connection, task_id: str
    ) -> TaskEvent | None:
        """Return the first durable H3 cancellation request for ``task_id``."""

        rows = connection.execute(
            """
            SELECT event_id, task_id, source, kind, payload_json, created_at
            FROM task_events
            WHERE task_id = ? AND source = 'bridge' AND kind = ?
            ORDER BY event_id ASC
            """,
            (task_id, CANCELLATION_REQUEST_EVENT),
        ).fetchall()
        for row in rows:
            event = self._event_from_row(row)
            if (
                isinstance(event.payload, Mapping)
                and event.payload.get("contract") == D3_H3_CONTRACT
            ):
                return event
        return None

    def get_cancellation_request(self, task_id: str) -> TaskEvent | None:
        """Return the durable H3 cancellation request, if one exists."""

        connection = self._require_connection()
        return self._cancellation_request_in_transaction(connection, task_id)

    def get_cancellation_interrupt_sent(self, task_id: str) -> TaskEvent | None:
        """Return the durable interrupt dispatch evidence, if one exists."""

        connection = self._require_connection()
        row = connection.execute(
            """
            SELECT event_id, task_id, source, kind, payload_json, created_at
            FROM task_events
            WHERE task_id = ? AND source = 'bridge' AND kind = ?
            ORDER BY event_id ASC
            LIMIT 1
            """,
            (task_id, CANCELLATION_INTERRUPT_SENT_EVENT),
        ).fetchone()
        return self._event_from_row(row) if row is not None else None

    def get_cancellation_interrupt_failure(self, task_id: str) -> TaskEvent | None:
        """Return the durable interrupt failure evidence, if one exists."""

        connection = self._require_connection()
        row = connection.execute(
            """
            SELECT event_id, task_id, source, kind, payload_json, created_at
            FROM task_events
            WHERE task_id = ? AND source = 'bridge' AND kind = ?
            ORDER BY event_id DESC
            LIMIT 1
            """,
            (task_id, CANCELLATION_INTERRUPT_FAILED_EVENT),
        ).fetchone()
        return self._event_from_row(row) if row is not None else None

    def request_task_cancellation(
        self, task_id: str, payload: Mapping[str, Any]
    ) -> tuple[Task, TaskEvent | None, bool]:
        """Durably request cancellation and atomically cancel an untouched queue item.

        A QUEUED task is moved to CANCELLED in the same transaction as the
        request event, so a worker cannot claim it after cancellation.  A
        RUNNING task only records the request; its owner must obtain official
        turn-interruption evidence before writing the terminal event.  Terminal
        tasks are returned unchanged and never receive a new request event.
        """

        if not isinstance(payload, Mapping):
            raise ValueError("cancellation request payload must be an object")
        normalized_payload = dict(payload)
        if normalized_payload.get("contract") != D3_H3_CONTRACT:
            raise ValueError("cancellation request payload must declare contract D3-H3")

        connection = self._require_connection()
        request_event: TaskEvent | None = None
        created = False
        requested_at = utc_now()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT task_id, project_id, objective, executor, model,
                       mode, execution_status, audit_status, thread_id, turn_id,
                       created_at, updated_at
                FROM tasks WHERE task_id = ?
                """,
                (task_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"task does not exist: {task_id}")
            task = self._task_from_row(row)
            request_event = self._cancellation_request_in_transaction(
                connection, task_id
            )
            if task.execution_status in {
                ExecutionStatus.FINISHED,
                ExecutionStatus.FAILED,
                ExecutionStatus.CANCELLED,
            }:
                connection.commit()
                return task, request_event, False
            if task.execution_status not in {
                ExecutionStatus.QUEUED,
                ExecutionStatus.RUNNING,
            }:
                raise TaskStateError(
                    f"task {task_id} cannot be cancelled from state "
                    f"{task.execution_status.value}; only QUEUED or RUNNING tasks may be cancelled"
                )

            if request_event is None:
                request_event = self._insert_task_event_in_transaction(
                    connection,
                    task_id,
                    "bridge",
                    CANCELLATION_REQUEST_EVENT,
                    normalized_payload,
                    created_at=requested_at,
                )
                created = True

            if task.execution_status is ExecutionStatus.QUEUED:
                cancelled_at = utc_now()
                cancel_payload = {
                    "status": ExecutionStatus.CANCELLED.value,
                    "reason": "cancel requested before execution",
                    "requested_via": normalized_payload.get("requested_via", "mcp"),
                    "cancel_request_id": (
                        request_event.payload.get("request_id")
                        if isinstance(request_event.payload, Mapping)
                        else None
                    ),
                }
                cursor = connection.execute(
                    """
                    UPDATE tasks
                    SET execution_status = ?, audit_status = ?, updated_at = ?
                    WHERE task_id = ? AND execution_status = ?
                    """,
                    (
                        ExecutionStatus.CANCELLED.value,
                        AuditStatus.PENDING.value,
                        timestamp_to_text(cancelled_at),
                        task_id,
                        ExecutionStatus.QUEUED.value,
                    ),
                )
                if cursor.rowcount != 1:
                    raise TaskStateError(
                        f"task {task_id} could not be cancelled from QUEUED"
                    )
                self._insert_task_event_in_transaction(
                    connection,
                    task_id,
                    "bridge",
                    "task.cancelled",
                    cancel_payload,
                    created_at=cancelled_at,
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise

        updated = self.get_task(task_id)
        if updated is None:
            raise RuntimeError("task disappeared after cancellation request")
        return updated, request_event, created

    def count_task_events(self, task_id: str) -> int:
        connection = self._require_connection()
        row = connection.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        return int(row[0])


__all__ = [
    "CHECKPOINT_CREATED_EVENT",
    "CHECKPOINT_FAILED_EVENT",
    "CHECKPOINT_REF_UPDATED_EVENT",
    "CHECKPOINT_STARTED_EVENT",
    "CANCELLATION_INTERRUPT_FAILED_EVENT",
    "CANCELLATION_INTERRUPT_SENT_EVENT",
    "CANCELLATION_REQUEST_EVENT",
    "D3_H3_CONTRACT",
    "D3_R2_CONTRACT",
    "EXECUTION_CLAIM_EVENT",
    "EXECUTION_REQUEST_EVENT",
    "RECONCILIATION_BASELINE_ADOPTED_EVENT",
    "RECONCILIATION_REQUIRED_EVENT",
    "RECONCILIATION_RESOLVED_EVENT",
    "SCHEMA_VERSION",
    "SQLITE_BUSY_TIMEOUT_SECONDS",
    "SQLiteBridgeStore",
    "SchemaVersionError",
]
