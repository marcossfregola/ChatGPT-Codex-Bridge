from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT / "src"))

from chatgpt_codex_bridge.core import (  # noqa: E402
    BridgeCore,
    _MAX_NOTIFICATION_TEXT,
    _bounded_error_message,
    _bounded_notification_value,
)
from chatgpt_codex_bridge.domain.models import (  # noqa: E402
    AuditStatus,
    ExecutionStatus,
    TaskStateError,
)
from chatgpt_codex_bridge.executors.base import (  # noqa: E402
    ExecutionRequest,
    ExecutionResult,
)
from chatgpt_codex_bridge.persistence.sqlite_store import SQLiteBridgeStore  # noqa: E402


class FakeExecutor:
    def __init__(self, *, fail: Exception | None = None, store=None) -> None:
        self.fail = fail
        self.store = store
        self.requests: list[ExecutionRequest] = []

    async def run(self, request, *, on_correlation=None, on_notification=None):
        self.requests.append(request)
        if on_correlation is not None:
            on_correlation("thread-fake", None)
        if on_notification is not None:
            on_notification("thread/started", {"threadId": "thread-fake"})
        if self.store is not None:
            self.asserted_event_count_during_run = self.store.count_task_events(
                request.task_id
            )
        if self.fail is not None:
            raise self.fail
        if on_correlation is not None:
            on_correlation("thread-fake", "turn-fake")
        if on_notification is not None:
            on_notification("turn/started", {"turnId": "turn-fake"})
            on_notification(
                "turn/completed",
                {"turn": {"id": "turn-fake", "status": "completed"}},
            )
        await asyncio.sleep(0)
        return ExecutionResult(
            thread_id="thread-fake",
            turn_id="turn-fake",
            status=ExecutionStatus.FINISHED,
            final_response="BRIDGE_FAKE_OK",
        )


class FailingNotificationStore(SQLiteBridgeStore):
    def append_task_event(self, task_id, source, kind, payload, **kwargs):
        if source == "codex":
            raise RuntimeError("observer persistence failed")
        return super().append_task_event(task_id, source, kind, payload, **kwargs)


class CancellableExecutor:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancel_called = False
        self.requests: list[ExecutionRequest] = []

    async def run(self, request, *, on_correlation=None, on_notification=None):
        self.requests.append(request)
        if on_correlation is not None:
            on_correlation("thread-cancel", "turn-cancel")
        self.started.set()
        await asyncio.Event().wait()

    async def cancel_active(self) -> None:
        self.cancel_called = True


class BridgeCoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = SQLiteBridgeStore(Path(self.tempdir.name) / "bridge.sqlite3")

    def tearDown(self) -> None:
        self.store.close()
        self.tempdir.cleanup()

    def _core(self, executor=None) -> BridgeCore:
        return BridgeCore(self.store, executor or FakeExecutor(store=self.store))

    def test_notification_text_truncation_is_explicit_and_bounded(self) -> None:
        short = "short notification"
        long = "x" * (_MAX_NOTIFICATION_TEXT + 100)

        self.assertEqual(_bounded_notification_value(short), short)
        bounded = _bounded_notification_value(long)
        self.assertIsInstance(bounded, str)
        assert isinstance(bounded, str)
        self.assertEqual(len(bounded), _MAX_NOTIFICATION_TEXT)
        self.assertTrue(bounded.endswith("[TRUNCATED]"))
        self.assertTrue(_bounded_error_message(ValueError(long)).endswith("[TRUNCATED]"))

    def test_core_creates_project(self) -> None:
        project = self._core().create_project(
            "Bridge", "C:/workspace/bridge", project_id="project-1"
        )
        self.assertEqual(project.project_id, "project-1")
        self.assertEqual(self.store.get_project("project-1"), project)

    def test_core_creates_queued_pending_task_and_created_event(self) -> None:
        core = self._core()
        core.create_project("Bridge", "C:/workspace/bridge", project_id="project-1")
        task = core.create_task(
            "project-1", "do the task", task_id="task-1", model="gpt-5.6-luna"
        )
        self.assertEqual(task.execution_status, ExecutionStatus.QUEUED)
        self.assertEqual(task.audit_status, AuditStatus.PENDING)
        events = self.store.list_task_events("task-1")
        self.assertEqual([(event.source, event.kind) for event in events], [("bridge", "task.created")])

    async def test_run_task_uses_async_executor_and_finishes(self) -> None:
        executor = FakeExecutor(store=self.store)
        core = self._core(executor)
        core.create_project("Bridge", "C:/workspace/bridge", project_id="project-1")
        core.create_task("project-1", "do the task", task_id="task-1")

        result = await core.run_task("task-1")

        self.assertTrue(inspect.iscoroutinefunction(executor.run))
        self.assertEqual(result.execution_status, ExecutionStatus.FINISHED)
        self.assertEqual(result.audit_status, AuditStatus.PENDING)
        self.assertEqual(result.thread_id, "thread-fake")
        self.assertEqual(result.turn_id, "turn-fake")
        self.assertEqual(executor.requests[0].cwd, "C:/workspace/bridge")
        self.assertEqual(executor.asserted_event_count_during_run, 3)

    async def test_only_queued_tasks_can_run(self) -> None:
        for status in (
            ExecutionStatus.RUNNING,
            ExecutionStatus.FINISHED,
            ExecutionStatus.FAILED,
            ExecutionStatus.CANCELLED,
        ):
            executor = FakeExecutor(store=self.store)
            core = self._core(executor)
            project_id = f"project-{status.value.lower()}"
            task_id = f"task-{status.value.lower()}"
            core.create_project("Bridge", "C:/workspace/bridge", project_id=project_id)
            core.create_task(project_id, "do the task", task_id=task_id)
            self.store.update_task_runtime(task_id, execution_status=status)

            with self.assertRaisesRegex(TaskStateError, f"state {status.value}"):
                await core.run_task(task_id)
            self.assertEqual(executor.requests, [])

    async def test_cancellation_marks_cancelled_once_and_propagates(self) -> None:
        executor = CancellableExecutor()
        core = self._core(executor)
        core.create_project("Bridge", "C:/workspace/bridge", project_id="project-1")
        core.create_task("project-1", "do the task", task_id="task-1")

        running = asyncio.create_task(core.run_task("task-1"))
        await executor.started.wait()
        running.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await running

        task = self.store.get_task("task-1")
        assert task is not None
        self.assertEqual(task.execution_status, ExecutionStatus.CANCELLED)
        self.assertTrue(executor.cancel_called)
        kinds = [event.kind for event in self.store.list_task_events("task-1")]
        self.assertEqual(kinds.count("task.cancelled"), 1)
        self.assertNotIn("task.failed", kinds)
        self.assertNotIn("task.finished", kinds)

    def test_recover_orphaned_running_task_is_failed_once(self) -> None:
        core = self._core()
        core.create_project("Bridge", "C:/workspace/bridge", project_id="project-1")
        core.create_task("project-1", "do the task", task_id="task-1")
        self.store.transition_task_running("task-1", project_id="project-1")

        self.store.close()
        self.store = SQLiteBridgeStore(Path(self.tempdir.name) / "bridge.sqlite3")
        core = self._core()

        recovered = core.recover_orphaned_tasks()

        self.assertEqual([task.task_id for task in recovered], ["task-1"])
        task = self.store.get_task("task-1")
        assert task is not None
        self.assertEqual(task.execution_status, ExecutionStatus.FAILED)
        events = self.store.list_task_events("task-1")
        self.assertEqual(
            [event.kind for event in events],
            ["task.created", "task.started", "task.recovered", "task.failed"],
        )
        self.assertEqual(events[-1].payload["recovered_from"], "RUNNING")

    async def test_success_has_one_terminal_event_after_turn_completed(self) -> None:
        core = self._core()
        core.create_project("Bridge", "C:/workspace/bridge", project_id="project-1")
        core.create_task("project-1", "do the task", task_id="task-1")

        await core.run_task("task-1")

        events = self.store.list_task_events("task-1")
        terminal = [
            event
            for event in events
            if event.kind in {"task.finished", "task.failed", "task.cancelled"}
        ]
        self.assertEqual([event.kind for event in terminal], ["task.finished"])
        completed_id = next(event.event_id for event in events if event.kind == "turn/completed")
        finished_id = terminal[0].event_id
        assert completed_id is not None and finished_id is not None
        self.assertLess(completed_id, finished_id)

    async def test_notifications_are_durable_and_journal_order_is_append_order(self) -> None:
        core = self._core()
        core.create_project("Bridge", "C:/workspace/bridge", project_id="project-1")
        core.create_task("project-1", "do the task", task_id="task-1")

        await core.run_task("task-1")

        events = self.store.list_task_events("task-1")
        self.assertEqual(
            [event.kind for event in events],
            [
                "task.created",
                "task.started",
                "thread/started",
                "turn/started",
                "turn/completed",
                "task.finished",
            ],
        )
        self.assertEqual([event.event_id for event in events], sorted(event.event_id for event in events))
        self.assertEqual(events[2].source, "codex")
        self.assertEqual(events[2].payload, {"threadId": "thread-fake"})

    async def test_executor_failure_marks_failed_pending_and_records_safe_error(self) -> None:
        executor = FakeExecutor(fail=ValueError("controlled failure"), store=self.store)
        core = self._core(executor)
        core.create_project("Bridge", "C:/workspace/bridge", project_id="project-1")
        core.create_task("project-1", "do the task", task_id="task-1")

        with self.assertRaises(ValueError):
            await core.run_task("task-1")

        task = self.store.get_task("task-1")
        assert task is not None
        self.assertEqual(task.execution_status, ExecutionStatus.FAILED)
        self.assertEqual(task.audit_status, AuditStatus.PENDING)
        failed = self.store.get_last_task_event("task-1")
        assert failed is not None
        self.assertEqual(failed.kind, "task.failed")
        self.assertEqual(failed.payload, {"error_type": "ValueError", "message": "controlled failure"})
        self.assertNotIn("Traceback", str(failed.payload))
        terminal_kinds = [
            event.kind
            for event in self.store.list_task_events("task-1")
            if event.kind in {"task.finished", "task.failed", "task.cancelled"}
        ]
        self.assertEqual(terminal_kinds, ["task.failed"])

    async def test_observer_persistence_failure_is_not_hidden(self) -> None:
        self.store.close()
        self.store = FailingNotificationStore(Path(self.tempdir.name) / "failing.sqlite3")
        executor = FakeExecutor(store=self.store)
        core = self._core(executor)
        core.create_project("Bridge", "C:/workspace/bridge", project_id="project-1")
        core.create_task("project-1", "do the task", task_id="task-1")

        with self.assertRaisesRegex(RuntimeError, "observer persistence failed"):
            await core.run_task("task-1")

        task = self.store.get_task("task-1")
        assert task is not None
        self.assertEqual(task.execution_status, ExecutionStatus.FAILED)
        self.assertEqual(task.audit_status, AuditStatus.PENDING)

    def test_core_source_has_no_codex_wire_protocol_dependency(self) -> None:
        source = inspect.getsource(BridgeCore)
        self.assertNotIn("CodexAppServerClient", source)
        self.assertNotIn("jsonrpc", source)


if __name__ == "__main__":
    unittest.main()
