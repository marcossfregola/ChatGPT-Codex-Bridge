from __future__ import annotations

import asyncio
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from chatgpt_codex_bridge.core import BridgeCore  # noqa: E402
from chatgpt_codex_bridge.domain.models import ExecutionStatus, TaskMode  # noqa: E402
from chatgpt_codex_bridge.executors.base import ExecutionResult  # noqa: E402
from chatgpt_codex_bridge.execution_worker import (  # noqa: E402
    ExecutionWorker,
    read_worker_state,
    worker_runtime_paths,
)
from chatgpt_codex_bridge.mcp_adapter import MCPAdapter  # noqa: E402
from chatgpt_codex_bridge.persistence.sqlite_store import SQLiteBridgeStore  # noqa: E402


class FakeExecutor:
    def __init__(self, mutation=None) -> None:
        self.mutation = mutation
        self.requests = []

    async def run(self, request, *, on_correlation=None, on_notification=None):
        self.requests.append(request)
        if on_correlation is not None:
            on_correlation("worker-thread", "worker-turn")
        if on_notification is not None:
            on_notification("turn/completed", {"status": "completed"})
        if self.mutation is not None:
            self.mutation(Path(request.cwd))
        return ExecutionResult(
            thread_id="worker-thread",
            turn_id="worker-turn",
            status=ExecutionStatus.FINISHED,
            final_response="WORKER_OK",
        )


class BlockingCancelableExecutor(FakeExecutor):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.cancel_called = False
        self.child_closed = asyncio.Event()

    async def run(self, request, *, on_correlation=None, on_notification=None):
        self.requests.append(request)
        self.started.set()
        await asyncio.Event().wait()

    async def cancel_active(self) -> bool:
        self.cancel_called = True
        self.child_closed.set()
        return True


class ExecutionWorkerDispatchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.db_path = self.root / "bridge.sqlite3"
        self.store = SQLiteBridgeStore(self.db_path)
        self.executor = FakeExecutor()
        self.core = BridgeCore(self.store, self.executor)
        self.core.create_project("Bridge", "C:/workspace/bridge", project_id="project-worker")
        self.adapter = MCPAdapter(self.core, self.store)

    def tearDown(self) -> None:
        self.store.close()
        self.tempdir.cleanup()

    def create_task(self, task_id: str = "task-worker", *, mode=TaskMode.READ_ONLY):
        return self.core.create_task(
            "project-worker", "execute the worker test", task_id=task_id, mode=mode
        )

    async def test_two_requests_are_one_durable_execution_request(self) -> None:
        self.create_task()

        first = await self.adapter.call_tool("run_task", {"task_id": "task-worker"})
        second = await self.adapter.call_tool("run_task", {"task_id": "task-worker"})

        self.assertTrue(first["accepted"])
        self.assertTrue(second["accepted"])
        self.assertTrue(second["already_requested"])
        self.assertEqual(first["request_id"], second["request_id"])
        events = self.store.list_task_events("task-worker")
        self.assertEqual(
            [event.kind for event in events],
            ["task.created", "task.execution_requested"],
        )
        self.assertEqual(self.executor.requests, [])

    async def test_worker_claim_is_atomic_and_started_once(self) -> None:
        self.create_task()
        await self.adapter.call_tool("run_task", {"task_id": "task-worker"})

        worker = ExecutionWorker(
            self.store, self.core, worker_id="worker-test", pid=12345
        )
        claimed = worker.claim_next()

        self.assertIsNotNone(claimed)
        assert claimed is not None
        self.assertEqual(claimed.execution_status, ExecutionStatus.RUNNING)
        events = self.store.list_task_events("task-worker")
        self.assertEqual(
            [event.kind for event in events],
            [
                "task.created",
                "task.execution_requested",
                "task.execution_claimed",
                "task.started",
            ],
        )
        claim = events[2].payload
        self.assertEqual(claim["owner_kind"], "persistent_worker")
        self.assertEqual(claim["owner_id"], "worker-test")
        self.assertEqual(claim["pid"], 12345)
        self.assertIsInstance(claim["claimed_at"], str)
        self.assertIsNone(worker.claim_next())

    async def test_run_task_during_running_does_not_execute_again(self) -> None:
        self.create_task()
        await self.adapter.call_tool("run_task", {"task_id": "task-worker"})
        worker = ExecutionWorker(self.store, self.core)
        self.assertIsNotNone(worker.claim_next())

        response = await self.adapter.call_tool("run_task", {"task_id": "task-worker"})

        self.assertTrue(response["accepted"])
        self.assertEqual(response["execution_status"], ExecutionStatus.RUNNING.value)
        self.assertEqual(self.executor.requests, [])
        await self.core.execute_claimed_task("task-worker")
        self.assertEqual(len(self.executor.requests), 1)

    async def test_restart_mcp_request_is_idempotent(self) -> None:
        self.create_task()
        first = await self.adapter.call_tool("run_task", {"task_id": "task-worker"})
        self.store.close()

        reopened = SQLiteBridgeStore(self.db_path)
        try:
            restarted_core = BridgeCore(reopened)
            restarted = MCPAdapter(restarted_core, reopened)
            second = await restarted.call_tool(
                "run_task", {"task_id": "task-worker"}
            )
            self.assertEqual(first["request_id"], second["request_id"])
            self.assertTrue(second["already_requested"])
            self.assertEqual(
                len(
                    [
                        event
                        for event in reopened.list_task_events("task-worker")
                        if event.kind == "task.execution_requested"
                    ]
                ),
                1,
            )
        finally:
            reopened.close()
            self.store = SQLiteBridgeStore(self.db_path)

    async def test_historical_queued_zombie_without_request_is_never_claimed(self) -> None:
        self.create_task("task-1f-d3-chain-a")
        worker = ExecutionWorker(self.store, self.core)

        self.assertIsNone(self.store.get_execution_request("task-1f-d3-chain-a"))
        self.assertIsNone(worker.claim_next())
        self.assertEqual(
            self.store.get_task("task-1f-d3-chain-a").execution_status,
            ExecutionStatus.QUEUED,
        )
        self.assertEqual(self.executor.requests, [])

    async def test_status_prioritizes_running_then_requested_queue_over_history(self) -> None:
        self.create_task("task-zombie")
        queued = self.create_task("task-requested")
        await self.adapter.call_tool("run_task", {"task_id": queued.task_id})

        status = await self.adapter.call_tool("get_status", {})
        self.assertEqual(status["task_id"], queued.task_id)
        self.assertEqual(status["active_task_source"], "queued_request")
        self.assertIsNone(status["owner"])

        worker = ExecutionWorker(
            self.store, self.core, worker_id="status-worker", pid=54321
        )
        self.assertIsNotNone(worker.claim_next())
        status = await self.adapter.call_tool("get_status", {})
        self.assertEqual(status["task_id"], queued.task_id)
        self.assertEqual(status["execution_status"], ExecutionStatus.RUNNING.value)
        self.assertEqual(
            status["owner"],
            {
                "owner_kind": "persistent_worker",
                "owner_id": "status-worker",
                "pid": 54321,
                "claimed_at": status["owner"]["claimed_at"],
            },
        )

    def test_worker_recovery_fails_running_task_closed_by_previous_owner(self) -> None:
        task = self.create_task("task-recovery")
        self.store.transition_task_running(task.task_id, project_id=task.project_id)
        recovered = self.core.recover_orphaned_tasks()

        self.assertEqual([item.task_id for item in recovered], [task.task_id])
        self.assertEqual(
            self.store.get_task(task.task_id).execution_status,
            ExecutionStatus.FAILED,
        )
        self.assertEqual(
            [event.kind for event in self.store.list_task_events(task.task_id)],
            ["task.created", "task.started", "task.recovered", "task.failed"],
        )

    async def test_worker_runs_autonomous_write_contract_and_persists_postflight(self) -> None:
        repo = self.root / "workspace"
        repo.mkdir()
        self._git(repo, "init")
        self._git(repo, "config", "user.name", "Worker Test")
        self._git(repo, "config", "user.email", "worker-test@example.invalid")
        (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
        self._git(repo, "add", "tracked.txt")
        self._git(repo, "commit", "-m", "initial")

        self.store.close()
        self.store = SQLiteBridgeStore(self.db_path)
        self.executor = FakeExecutor(
            mutation=lambda path: (path / "worker.txt").write_text("created\n", encoding="utf-8")
        )
        self.core = BridgeCore(self.store, self.executor)
        self.core.create_project("Bridge", str(repo), project_id="project-auto")
        self.adapter = MCPAdapter(self.core, self.store)
        self.core.create_task(
            "project-auto",
            "execute the worker test",
            task_id="task-autonomous",
            mode=TaskMode.AUTONOMOUS_WRITE,
        )

        accepted = await self.adapter.call_tool(
            "run_task", {"task_id": "task-autonomous"}
        )
        result = await ExecutionWorker(self.store, self.core).run_once()

        self.assertTrue(accepted["accepted"])
        self.assertEqual(result.execution_status, ExecutionStatus.FINISHED)
        self.assertEqual(len(self.executor.requests), 1)
        self.assertEqual(
            [event.kind for event in self.store.list_task_events("task-autonomous")],
            [
                "task.created",
                "task.execution_requested",
                "task.execution_claimed",
                "task.started",
                "policy.git_checkpoint",
                "turn/completed",
                "policy.postflight",
                "task.finished",
            ],
        )
        postflight = next(
            event
            for event in self.store.list_task_events("task-autonomous")
            if event.kind == "policy.postflight"
        )
        self.assertIn("worker.txt", postflight.payload["untracked_files"])
        self.assertFalse(postflight.payload["policy_violation"])

    async def test_worker_preserves_autonomous_continuation_baseline(self) -> None:
        repo = self.root / "continuation"
        repo.mkdir()
        self._git(repo, "init")
        self._git(repo, "config", "user.name", "Worker Test")
        self._git(repo, "config", "user.email", "worker-test@example.invalid")
        (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
        self._git(repo, "add", "tracked.txt")
        self._git(repo, "commit", "-m", "initial")

        self.store.close()
        self.store = SQLiteBridgeStore(self.db_path)
        self.executor = FakeExecutor(
            mutation=lambda path: (path / "first.txt").write_text("one\n", encoding="utf-8")
        )
        self.core = BridgeCore(self.store, self.executor)
        self.core.create_project("Bridge", str(repo), project_id="project-continuation")
        self.adapter = MCPAdapter(self.core, self.store)
        self.core.create_task(
            "project-continuation",
            "first continuation step",
            task_id="task-continuation-1",
            mode=TaskMode.AUTONOMOUS_WRITE,
        )
        self.core.create_task(
            "project-continuation",
            "second continuation step",
            task_id="task-continuation-2",
            mode=TaskMode.AUTONOMOUS_WRITE,
        )
        await self.adapter.call_tool("run_task", {"task_id": "task-continuation-1"})
        await self.adapter.call_tool("run_task", {"task_id": "task-continuation-2"})

        await ExecutionWorker(self.store, self.core).run_once()
        await ExecutionWorker(self.store, self.core).run_once()

        self.assertEqual(len(self.executor.requests), 2)
        self.assertEqual(
            self.store.get_task("task-continuation-2").execution_status,
            ExecutionStatus.FINISHED,
        )
        checkpoint = next(
            event
            for event in self.store.list_task_events("task-continuation-2")
            if event.kind == "policy.git_checkpoint"
        )
        self.assertEqual(checkpoint.payload["baseline_kind"], "continuation")
        self.assertEqual(checkpoint.payload["previous_task_id"], "task-continuation-1")

    async def test_controlled_stop_cancels_child_and_persists_terminal_task(self) -> None:
        executor = BlockingCancelableExecutor()
        self.store.close()
        self.store = SQLiteBridgeStore(self.db_path)
        self.core = BridgeCore(self.store, executor)
        self.core.create_project("Bridge", "C:/workspace/bridge", project_id="project-stop")
        self.adapter = MCPAdapter(self.core, self.store)
        self.core.create_task("project-stop", "stop the worker", task_id="task-stop")
        await self.adapter.call_tool("run_task", {"task_id": "task-stop"})

        paths = worker_runtime_paths(self.db_path)
        worker = ExecutionWorker(
            self.store,
            self.core,
            worker_id="stop-worker",
            pid=54321,
            poll_interval=0.01,
            cancel_timeout=0.5,
            runtime_paths=paths,
        )
        running = asyncio.create_task(
            worker.run_forever(stop_path=paths.stop_path)
        )
        try:
            await asyncio.wait_for(executor.started.wait(), timeout=2)
            paths.stop_path.write_text("stop\n", encoding="utf-8")
            await asyncio.wait_for(running, timeout=5)
        finally:
            if not running.done():
                running.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await running
            worker.close_runtime_state()

        self.assertTrue(executor.cancel_called)
        self.assertTrue(executor.child_closed.is_set())
        task = self.store.get_task("task-stop")
        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(task.execution_status, ExecutionStatus.CANCELLED)
        self.assertEqual(
            [event.kind for event in self.store.list_task_events("task-stop")][-1],
            "task.cancelled",
        )
        state = read_worker_state(self.db_path)
        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(state["status"], "stopped")

    @staticmethod
    def _git(repo: Path, *args: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        return completed.stdout.strip()


@unittest.skipUnless(os.name == "nt", "Windows child lifecycle probe")
class WindowsChildOrphanProbeTests(unittest.TestCase):
    def test_abrupt_owner_exit_classifies_child_lifecycle_without_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pid_file = root / "child.pid"
            owner_code = (
                "from pathlib import Path; import os, subprocess, sys, time; "
                "child=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], "
                "stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
                "Path(sys.argv[1]).write_text(str(child.pid), encoding='ascii'); os._exit(0)"
            )
            owner = subprocess.Popen(
                [sys.executable, "-c", owner_code, str(pid_file)],
                cwd=str(ROOT),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            owner.wait(timeout=10)
            deadline = time.monotonic() + 5
            while not pid_file.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(pid_file.exists())
            child_pid = int(pid_file.read_text(encoding="ascii"))
            try:
                time.sleep(0.25)
                probe = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {child_pid}"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                alive = str(child_pid) in probe.stdout
                classification = (
                    "CHILD MAY SURVIVE OWNER"
                    if alive
                    else "CHILD TERMINATES RELIABLY"
                )
                print(f"ORPHAN_PROBE: {classification}")
                self.assertIn(
                    classification,
                    {"CHILD MAY SURVIVE OWNER", "CHILD TERMINATES RELIABLY"},
                )
            finally:
                subprocess.run(
                    ["taskkill", "/PID", str(child_pid), "/T", "/F"],
                    capture_output=True,
                    text=True,
                    check=False,
                )


if __name__ == "__main__":
    unittest.main()
