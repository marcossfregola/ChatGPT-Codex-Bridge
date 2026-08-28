from __future__ import annotations

import asyncio
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from chatgpt_codex_bridge.domain.models import ExecutionStatus  # noqa: E402
from chatgpt_codex_bridge.executors.base import ExecutionRequest  # noqa: E402
from chatgpt_codex_bridge.executors.codex_app_server import CloseResult  # noqa: E402
from chatgpt_codex_bridge.executors.codex_executor import CodexExecutor  # noqa: E402


class FakeClient:
    def __init__(self, executable, cwd, *, notification_observer=None, **_kwargs):
        self.executable = executable
        self.cwd = str(cwd)
        self.notification_observer = notification_observer
        self.closed = False

    async def start(self):
        return 4123

    async def initialize(self):
        if self.notification_observer:
            self.notification_observer("session/initialized", {})
        return {"result": {}}

    async def account_read(self):
        return {"result": {"account": {"type": "chatgpt"}}}

    async def thread_start(self, *, model, cwd, ephemeral):
        return {"result": {"thread": {"id": "thread-real"}}}

    async def turn_start(self, *, thread_id, cwd, model, prompt, on_turn_started=None):
        if on_turn_started is not None:
            on_turn_started("turn-real")
        if self.notification_observer:
            self.notification_observer("turn/started", {"turnId": "turn-real"})
        await asyncio.sleep(0)
        return (
            {"result": {"turn": {"id": "turn-real"}}},
            {
                "params": {
                    "turn": {
                        "id": "turn-real",
                        "status": "completed",
                        "items": [
                            {
                                "type": "agentMessage",
                                "phase": "final_answer",
                                "text": "BRIDGE_EXECUTOR_OK",
                            }
                        ],
                    }
                }
            },
        )

    async def close(self):
        self.closed = True
        return CloseResult(pid=4123, returncode=0, killed=False)


class InterruptingClient(FakeClient):
    def __init__(self, executable, cwd, *, notification_observer=None, **kwargs):
        super().__init__(
            executable,
            cwd,
            notification_observer=notification_observer,
            **kwargs,
        )
        self.process = SimpleNamespace(pid=4124, returncode=None)
        self.started = asyncio.Event()
        self.interrupts: list[tuple[str, str]] = []

    async def start(self):
        return 4124

    async def thread_start(self, *, model, cwd, ephemeral):
        return {"result": {"thread": {"id": "thread-active"}}}

    async def turn_start(self, *, thread_id, cwd, model, prompt, on_turn_started=None):
        if on_turn_started is not None:
            on_turn_started("turn-active")
        self.started.set()
        await asyncio.Event().wait()

    async def turn_interrupt(self, *, thread_id, turn_id):
        self.interrupts.append((thread_id, turn_id))
        return {"result": {}}

    async def close(self):
        self.closed = True
        self.process = None
        return CloseResult(pid=4124, returncode=0, killed=False)


class ConfirmingClient(InterruptingClient):
    def __init__(self, executable, cwd, *, notification_observer=None, **kwargs):
        super().__init__(
            executable,
            cwd,
            notification_observer=notification_observer,
            **kwargs,
        )
        self.release = asyncio.Event()
        self.cancelled = False

    async def turn_start(self, *, thread_id, cwd, model, prompt, on_turn_started=None):
        if on_turn_started is not None:
            on_turn_started("turn-active")
        self.started.set()
        await self.release.wait()
        return (
            {"result": {"turn": {"id": "turn-active"}}},
            {
                "params": {
                    "threadId": "thread-active",
                    "turn": {
                        "id": "turn-active",
                        "status": "interrupted" if self.cancelled else "completed",
                    },
                }
            },
        )

    async def turn_interrupt(self, *, thread_id, turn_id):
        self.interrupts.append((thread_id, turn_id))
        self.cancelled = True
        self.release.set()
        return {"result": {}}


class CodexExecutorTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_executor_correlates_early_and_closes_owned_client(self):
        with tempfile.TemporaryDirectory() as tempdir:
            executor = CodexExecutor(
                executable=sys.executable,
                client_factory=FakeClient,
            )
            correlations: list[tuple[str | None, str | None]] = []
            notifications: list[tuple[str, dict[str, object]]] = []

            result = await executor.run(
                ExecutionRequest(
                    task_id="task-1",
                    cwd=tempdir,
                    objective="respond",
                    model="gpt-5.6-luna",
                ),
                on_correlation=lambda thread_id, turn_id: correlations.append(
                    (thread_id, turn_id)
                ),
                on_notification=lambda method, params: notifications.append(
                    (method, params)
                ),
            )

        self.assertEqual(correlations, [("thread-real", None), ("thread-real", "turn-real")])
        self.assertEqual(notifications, [("session/initialized", {}), ("turn/started", {"turnId": "turn-real"})])
        self.assertEqual(result.status, ExecutionStatus.FINISHED)
        self.assertEqual(result.thread_id, "thread-real")
        self.assertEqual(result.turn_id, "turn-real")
        self.assertEqual(result.final_response, "BRIDGE_EXECUTOR_OK")
        self.assertEqual(executor.last_pid, 4123)
        self.assertEqual(executor.last_account_type, "chatgpt")
        self.assertIsNotNone(executor.last_close_result)
        assert executor.last_client is not None
        self.assertTrue(executor.last_client.closed)

    async def test_cancellation_interrupts_active_turn_before_close(self):
        client = InterruptingClient(sys.executable, Path.cwd())
        executor = CodexExecutor(
            executable=sys.executable,
            client_factory=lambda *_args, **_kwargs: client,
        )
        running = asyncio.create_task(
            executor.run(
                ExecutionRequest(
                    task_id="task-1",
                    cwd=str(Path.cwd()),
                    objective="wait",
                    model="gpt-5.6-luna",
                )
            )
        )
        await client.started.wait()
        running.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await running

        self.assertEqual(client.interrupts, [("thread-active", "turn-active")])
        self.assertTrue(client.closed)

    async def test_interrupt_confirmation_returns_cancelled_result_and_checks_target(self):
        client = ConfirmingClient(sys.executable, Path.cwd())
        executor = CodexExecutor(
            executable=sys.executable,
            client_factory=lambda *_args, **_kwargs: client,
        )
        running = asyncio.create_task(
            executor.run(
                ExecutionRequest(
                    task_id="task-1",
                    cwd=str(Path.cwd()),
                    objective="wait",
                    model="gpt-5.6-luna",
                )
            )
        )
        await client.started.wait()

        self.assertFalse(
            await executor.cancel_active(
                thread_id="wrong-thread", turn_id="turn-active"
            )
        )
        self.assertEqual(client.interrupts, [])
        self.assertTrue(
            await executor.cancel_active(
                thread_id="thread-active", turn_id="turn-active"
            )
        )
        result = await asyncio.wait_for(running, timeout=2)

        self.assertEqual(result.status, ExecutionStatus.CANCELLED)
        self.assertEqual(result.thread_id, "thread-active")
        self.assertEqual(result.turn_id, "turn-active")
        self.assertEqual(client.interrupts, [("thread-active", "turn-active")])


if __name__ == "__main__":
    unittest.main()
