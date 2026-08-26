from __future__ import annotations

import asyncio
from pathlib import Path
import sys
import tempfile
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


if __name__ == "__main__":
    unittest.main()
