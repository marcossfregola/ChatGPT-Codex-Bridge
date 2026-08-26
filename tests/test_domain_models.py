from __future__ import annotations

from datetime import datetime, timezone
import sys
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from chatgpt_codex_bridge.domain.models import (  # noqa: E402
    AuditStatus,
    ExecutionStatus,
    Project,
    Task,
)


class DomainModelTests(unittest.TestCase):
    def test_project_valid_roundtrip_fields(self) -> None:
        timestamp = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
        project = Project(
            project_id="project-1",
            name="Bridge",
            repo_path="C:/workspace/bridge",
            created_at=timestamp,
            updated_at=timestamp,
        )
        self.assertEqual(project.project_id, "project-1")
        self.assertEqual(project.name, "Bridge")
        self.assertEqual(project.repo_path, "C:/workspace/bridge")
        self.assertEqual(project.created_at, timestamp)
        self.assertEqual(project.updated_at, timestamp)

    def test_task_defaults(self) -> None:
        task = Task("task-1", "project-1", "Demonstrate persistence")
        self.assertEqual(task.executor, "codex")
        self.assertEqual(task.execution_status, ExecutionStatus.QUEUED)
        self.assertEqual(task.audit_status, AuditStatus.PENDING)

    def test_execution_and_audit_states_are_separate(self) -> None:
        task = Task(
            "task-1",
            "project-1",
            "Completed but awaiting review",
            execution_status=ExecutionStatus.FINISHED,
            audit_status=AuditStatus.PENDING,
        )
        self.assertEqual(task.execution_status, ExecutionStatus.FINISHED)
        self.assertEqual(task.audit_status, AuditStatus.PENDING)

    def test_codex_ids_are_optional_and_distinct_fields(self) -> None:
        task = Task("task-1", "project-1", "No Codex run yet")
        self.assertIsNone(task.thread_id)
        self.assertIsNone(task.turn_id)
        self.assertNotEqual(task.task_id, task.thread_id)
        self.assertNotEqual(task.task_id, task.turn_id)

    def test_timestamps_require_timezone(self) -> None:
        with self.assertRaises(ValueError):
            Project(
                "project-1",
                "Bridge",
                "C:/workspace/bridge",
                created_at=datetime(2026, 8, 26, 12, 0),
            )


if __name__ == "__main__":
    unittest.main()
