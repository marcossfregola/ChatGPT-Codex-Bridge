from __future__ import annotations

import asyncio
from contextlib import suppress
import inspect
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402
from mcp.shared.memory import create_client_server_memory_streams  # noqa: E402
from mcp.shared.exceptions import MCPError  # noqa: E402

from chatgpt_codex_bridge.core import (  # noqa: E402
    BridgeCore,
    MAX_FINAL_RESPONSE_BYTES,
)
from chatgpt_codex_bridge.domain.models import ExecutionStatus  # noqa: E402
from chatgpt_codex_bridge.executors.base import ExecutionResult  # noqa: E402
from chatgpt_codex_bridge.mcp_adapter import (  # noqa: E402
    DEFAULT_EVENT_LIMIT,
    MCPAdapter,
    MCPConcurrencyError,
    MCPToolError,
)
from chatgpt_codex_bridge.mcp_server import (  # noqa: E402
    _call_adapter,
    build_server,
    default_db_path,
)
from chatgpt_codex_bridge.persistence.sqlite_store import SQLiteBridgeStore  # noqa: E402


class ImmediateExecutor:
    def __init__(self) -> None:
        self.requests = []

    async def run(self, request, *, on_correlation=None, on_notification=None):
        self.requests.append(request)
        if on_correlation is not None:
            on_correlation("thread-mcp", None)
        if on_notification is not None:
            on_notification("thread/started", {"threadId": "thread-mcp"})
        if on_correlation is not None:
            on_correlation("thread-mcp", "turn-mcp")
        if on_notification is not None:
            on_notification(
                "turn/completed",
                {
                    "threadId": "thread-mcp",
                    "turn": {"id": "turn-mcp", "status": "completed"},
                },
            )
        return ExecutionResult(
            thread_id="thread-mcp",
            turn_id="turn-mcp",
            status=ExecutionStatus.FINISHED,
            final_response="MCP_FAKE_OK",
        )


class BlockingExecutor(ImmediateExecutor):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.child_closed = asyncio.Event()
        self.cancel_active_called = asyncio.Event()

    def cancel_active(self) -> None:
        self.cancel_active_called.set()

    async def run(self, request, *, on_correlation=None, on_notification=None):
        self.requests.append(request)
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.child_closed.set()
            raise
        return await ImmediateExecutor.run(
            self,
            request,
            on_correlation=on_correlation,
            on_notification=on_notification,
        )


class MCPAdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "bridge.sqlite3"
        self.store = SQLiteBridgeStore(self.db_path)
        self.executor = ImmediateExecutor()
        self.core = BridgeCore(self.store, self.executor)
        self.adapter = MCPAdapter(self.core, self.store)

    def tearDown(self) -> None:
        self.store.close()
        self.tempdir.cleanup()

    def _create_project(self) -> None:
        self.core.create_project(
            "Bridge", "C:/workspace/bridge", project_id="project-mcp"
        )

    def _create_task(self, task_id: str = "task-mcp") -> None:
        self._create_project()
        self.core.create_task("project-mcp", "do the MCP task", task_id=task_id)

    def _bulk_noise_events(self, task_id: str, count: int) -> None:
        self.store.connection.executemany(
            """
            INSERT INTO task_events (task_id, source, kind, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                (task_id, "codex", "noise", f'{{"index":{index}}}', "2026-08-27T00:00:00Z")
                for index in range(count)
            ),
        )
        self.store.connection.commit()

    async def _with_official_client(self, callback, adapter=None):
        server = build_server(adapter or self.adapter)
        async with create_client_server_memory_streams() as (client, server_streams):
            server_task = asyncio.create_task(
                server._lowlevel_server.run(
                    server_streams[0],
                    server_streams[1],
                    server._lowlevel_server.create_initialization_options(),
                )
            )
            try:
                async with ClientSession(client[0], client[1]) as session:
                    return await callback(session)
            finally:
                server_task.cancel()
                with suppress(asyncio.CancelledError):
                    await server_task

    def test_server_uses_official_sdk_and_not_manual_wire(self) -> None:
        import chatgpt_codex_bridge.mcp_server as module

        source = inspect.getsource(module)
        self.assertIn("from mcp.server.mcpserver import MCPServer", source)
        self.assertNotIn("json.loads", source)
        self.assertNotIn("MCP_PROTOCOL_VERSION", source)

    def test_adapter_has_no_sdk_wire_dependency(self) -> None:
        import chatgpt_codex_bridge.mcp_adapter as module

        source = inspect.getsource(module)
        self.assertNotIn("from mcp", source)
        self.assertNotIn("inputSchema", source)
        self.assertNotIn("CodexAppServerClient", source)
        self.assertNotIn("app-server", source)
        self.assertNotIn("CODEX_HOME", source)

    def test_stage_is_1fd(self) -> None:
        self.assertEqual(self.adapter.stage, "1F-D")

    async def test_official_initialize_and_tools_list_expose_exactly_eight_tools(self) -> None:
        async def exercise(session):
            initialized = await session.initialize()
            tools = await session.list_tools()
            return initialized, tools

        initialized, tools = await self._with_official_client(exercise)

        self.assertTrue(initialized.protocol_version)
        self.assertEqual(
            [tool.name for tool in tools.tools],
            [
                "get_status",
                "create_project",
                "create_task",
                "run_task",
                "get_task",
                "get_task_events",
                "get_result",
                "commit_checkpoint",
            ],
        )
        self.assertIn("properties", tools.tools[1].input_schema)

    async def test_official_get_status(self) -> None:
        async def exercise(session):
            await session.initialize()
            return await session.call_tool("get_status", {})

        result = await self._with_official_client(exercise)

        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content["executor"], "codex")
        self.assertIsNone(result.structured_content["active_task"])

    async def test_commit_checkpoint_tool_has_exact_schema(self) -> None:
        async def exercise(session):
            await session.initialize()
            tools = await session.list_tools()
            return next(tool for tool in tools.tools if tool.name == "commit_checkpoint")

        tool = await self._with_official_client(exercise)
        self.assertEqual(
            set(tool.input_schema["required"]), {"task_id", "message"}
        )
        self.assertEqual(
            tool.input_schema["properties"]["task_id"]["type"], "string"
        )
        self.assertEqual(
            tool.input_schema["properties"]["message"]["type"], "string"
        )

    async def test_official_create_project(self) -> None:
        async def exercise(session):
            await session.initialize()
            return await session.call_tool(
                "create_project",
                {"name": "Bridge", "repo_path": "C:/workspace/bridge", "project_id": "p1"},
            )

        result = await self._with_official_client(exercise)

        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content["project_id"], "p1")
        self.assertEqual(self.store.get_project("p1").name, "Bridge")

    async def test_official_create_task(self) -> None:
        self._create_project()

        async def exercise(session):
            await session.initialize()
            return await session.call_tool(
                "create_task",
                {"project_id": "project-mcp", "objective": "do it", "task_id": "t1"},
            )

        result = await self._with_official_client(exercise)

        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content["execution_status"], "QUEUED")
        self.assertEqual(result.structured_content["audit_status"], "PENDING")
        self.assertEqual(result.structured_content["model"], "gpt-5.6-luna")

    async def test_official_get_task(self) -> None:
        self._create_task()

        async def exercise(session):
            await session.initialize()
            return await session.call_tool("get_task", {"task_id": "task-mcp"})

        result = await self._with_official_client(exercise)

        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content["task_id"], "task-mcp")

    async def test_official_get_task_events(self) -> None:
        self._create_task()

        async def exercise(session):
            await session.initialize()
            return await session.call_tool(
                "get_task_events", {"task_id": "task-mcp"}
            )

        result = await self._with_official_client(exercise)

        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content["events"][0]["kind"], "task.created")

    async def test_get_task_events_default_is_bounded_without_full_deserialization(self) -> None:
        self._create_task()
        self._bulk_noise_events("task-mcp", 10_001)

        with patch.object(
            self.store, "_event_from_row", wraps=self.store._event_from_row
        ) as decode:
            result = await self.adapter.call_tool(
                "get_task_events", {"task_id": "task-mcp"}
            )

        self.assertEqual(result["count"], 10_002)
        self.assertTrue(result["truncated"])
        self.assertLessEqual(len(result["events"]), DEFAULT_EVENT_LIMIT)
        self.assertLessEqual(decode.call_count, DEFAULT_EVENT_LIMIT + 64)

    async def test_get_task_events_explicit_limit_preserves_critical_events(self) -> None:
        self._create_task()
        self._bulk_noise_events("task-mcp", 10_001)
        self.store.transition_task_running("task-mcp", project_id="project-mcp")
        self.store.transition_task_terminal(
            "task-mcp",
            execution_status=ExecutionStatus.FINISHED,
            event_kind="task.finished",
            payload={"final_response": "done"},
        )

        result = await self.adapter.call_tool(
            "get_task_events", {"task_id": "task-mcp", "limit": 100}
        )

        self.assertEqual(result["count"], 10_004)
        self.assertTrue(result["truncated"])
        kinds = {event["kind"] for event in result["events"]}
        self.assertIn("task.created", kinds)
        self.assertIn("task.finished", kinds)
        self.assertLessEqual(len(result["events"]), 100 + 64)

    async def test_get_result_uses_targeted_latest_queries(self) -> None:
        self._create_task()
        await self.adapter.call_tool("run_task", {"task_id": "task-mcp"})
        with patch.object(
            self.store,
            "list_task_events",
            side_effect=AssertionError("full journal scan is forbidden"),
        ):
            result = await self.adapter.call_tool(
                "get_result", {"task_id": "task-mcp"}
            )
        self.assertTrue(result["available"])
        self.assertEqual(result["final_response"], "MCP_FAKE_OK")

    async def test_get_result_bounds_historical_large_final_response(self) -> None:
        self._create_task()
        self.store.transition_task_running("task-mcp", project_id="project-mcp")
        self.store.transition_task_terminal(
            "task-mcp",
            execution_status=ExecutionStatus.FINISHED,
            event_kind="task.finished",
            payload={"final_response": "x" * (MAX_FINAL_RESPONSE_BYTES * 2)},
        )

        result = await self.adapter.call_tool(
            "get_result", {"task_id": "task-mcp"}
        )

        self.assertEqual(
            len(result["final_response"].encode("utf-8")),
            MAX_FINAL_RESPONSE_BYTES,
        )
        self.assertTrue(result["final_response"].endswith("[TRUNCATED]"))

    async def test_get_status_bounds_large_last_event_payload(self) -> None:
        self._create_task()
        self.store.append_task_event(
            "task-mcp",
            "codex",
            "item/completed",
            {
                "threadId": "thread-status",
                **{f"field-{index}": "x" * 4096 for index in range(64)},
            },
        )

        result = await self.adapter.call_tool("get_status", {})

        payload = result["last_event"]["payload"]
        self.assertTrue(payload["_truncated"])
        self.assertEqual(payload["threadId"], "thread-status")
        self.assertIn("event_id", result["last_event"])
        self.assertIn("created_at", result["last_event"])

    async def test_cancelled_run_propagates_at_adapter_boundary_and_next_task_runs(self) -> None:
        executor = BlockingExecutor()
        self.store.close()
        self.store = SQLiteBridgeStore(self.db_path)
        self.core = BridgeCore(self.store, executor)
        self.adapter = MCPAdapter(self.core, self.store)
        self._create_task("task-cancelled")

        running = asyncio.create_task(
            _call_adapter(self.adapter, "run_task", {"task_id": "task-cancelled"})
        )
        await executor.started.wait()
        running.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await running

        await asyncio.wait_for(executor.child_closed.wait(), timeout=2)
        self.assertTrue(executor.cancel_active_called.is_set())
        cancelled = await self.adapter.call_tool(
            "get_task", {"task_id": "task-cancelled"}
        )
        self.assertEqual(cancelled["execution_status"], "CANCELLED")
        status = await self.adapter.call_tool("get_status", {})
        self.assertEqual(status["last_event"]["kind"], "task.cancelled")

        self.core.create_task("project-mcp", "next", task_id="task-next")
        executor.release.set()
        next_result = await self.adapter.call_tool(
            "run_task", {"task_id": "task-next"}
        )
        self.assertEqual(next_result["execution_status"], "FINISHED")

    async def test_official_request_cancellation_keeps_server_operational(self) -> None:
        executor = BlockingExecutor()
        self.store.close()
        self.store = SQLiteBridgeStore(self.db_path)
        self.core = BridgeCore(self.store, executor)
        self.adapter = MCPAdapter(self.core, self.store)
        self._create_task("task-request-cancelled")
        self.core.create_task("project-mcp", "next", task_id="task-request-next")

        server = build_server(self.adapter)
        async with create_client_server_memory_streams() as (client, server_streams):
            server_task = asyncio.create_task(
                server._lowlevel_server.run(
                    server_streams[0],
                    server_streams[1],
                    server._lowlevel_server.create_initialization_options(),
                )
            )
            try:
                async with ClientSession(client[0], client[1]) as session:
                    await session.initialize()
                    running = asyncio.create_task(
                        session.call_tool(
                            "run_task", {"task_id": "task-request-cancelled"}
                        )
                    )
                    await executor.started.wait()
                    running.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await running

                    await asyncio.wait_for(executor.child_closed.wait(), timeout=2)
                    self.assertEqual(
                        self.store.get_task("task-request-cancelled").execution_status,
                        ExecutionStatus.CANCELLED,
                    )
                    status = await session.call_tool("get_status", {})
                    self.assertFalse(status.is_error)

                    executor.release.set()
                    next_result = await session.call_tool(
                        "run_task", {"task_id": "task-request-next"}
                    )
                    self.assertFalse(next_result.is_error)
                    self.assertEqual(
                        next_result.structured_content["execution_status"],
                        ExecutionStatus.FINISHED.value,
                    )
            finally:
                server_task.cancel()
                with suppress(asyncio.CancelledError):
                    await server_task

    async def test_official_global_shutdown_propagates_cancellation(self) -> None:
        executor = BlockingExecutor()
        self.store.close()
        self.store = SQLiteBridgeStore(self.db_path)
        self.core = BridgeCore(self.store, executor)
        self.adapter = MCPAdapter(self.core, self.store)
        self._create_task("task-global-shutdown")

        server = build_server(self.adapter)
        async with create_client_server_memory_streams() as (client, server_streams):
            server_task = asyncio.create_task(
                server._lowlevel_server.run(
                    server_streams[0],
                    server_streams[1],
                    server._lowlevel_server.create_initialization_options(),
                )
            )
            async with ClientSession(client[0], client[1]) as session:
                await session.initialize()
                running = asyncio.create_task(
                    session.call_tool(
                        "run_task", {"task_id": "task-global-shutdown"}
                    )
                )
                await executor.started.wait()

                server_task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await server_task

                with self.assertRaises(MCPError) as call_error:
                    await asyncio.wait_for(running, timeout=2)
                self.assertNotIsInstance(call_error.exception, ToolError)
                await asyncio.wait_for(executor.child_closed.wait(), timeout=2)
                self.assertTrue(executor.cancel_active_called.is_set())
                self.assertEqual(
                    self.store.get_task("task-global-shutdown").execution_status,
                    ExecutionStatus.CANCELLED,
                )

    async def test_official_get_result_after_restart(self) -> None:
        self._create_task()
        await self.adapter.call_tool("run_task", {"task_id": "task-mcp"})
        self.store.close()

        reopened = SQLiteBridgeStore(self.db_path)
        try:
            restarted = MCPAdapter(BridgeCore(reopened, ImmediateExecutor()), reopened)

            async def exercise(session):
                await session.initialize()
                return await session.call_tool("get_result", {"task_id": "task-mcp"})

            result = await self._with_official_client(exercise, restarted)
        finally:
            reopened.close()
            self.store = SQLiteBridgeStore(self.db_path)

        self.assertFalse(result.is_error)
        self.assertTrue(result.structured_content["available"])
        self.assertEqual(result.structured_content["final_response"], "MCP_FAKE_OK")

    async def test_official_tool_error_is_safe(self) -> None:
        async def exercise(session):
            await session.initialize()
            return await session.call_tool("get_task", {"task_id": "missing"})

        result = await self._with_official_client(exercise)

        self.assertTrue(result.is_error)
        self.assertIn("task does not exist", result.content[0].text)
        self.assertNotIn("Traceback", result.content[0].text)

    async def test_run_task_delegates_to_core(self) -> None:
        self._create_task()

        async def exercise(session):
            await session.initialize()
            return await session.call_tool("run_task", {"task_id": "task-mcp"})

        result = await self._with_official_client(exercise)

        self.assertFalse(result.is_error)
        self.assertEqual(len(self.executor.requests), 1)
        self.assertEqual(result.structured_content["execution_status"], "FINISHED")
        self.assertEqual(result.structured_content["final_response"], "MCP_FAKE_OK")

    async def test_run_task_rejects_non_queued_states_without_executor(self) -> None:
        for status in (
            ExecutionStatus.RUNNING,
            ExecutionStatus.FINISHED,
            ExecutionStatus.FAILED,
            ExecutionStatus.CANCELLED,
        ):
            project_id = f"project-{status.value.lower()}"
            task_id = f"task-{status.value.lower()}"
            self.core.create_project("Bridge", "C:/workspace/bridge", project_id=project_id)
            self.core.create_task(project_id, "do the task", task_id=task_id)
            self.store.update_task_runtime(task_id, execution_status=status)

            with self.assertRaisesRegex(MCPToolError, f"state {status.value}"):
                await self.adapter.call_tool("run_task", {"task_id": task_id})

        self.assertEqual(self.executor.requests, [])

    async def test_concurrent_run_is_rejected(self) -> None:
        executor = BlockingExecutor()
        self.store.close()
        self.store = SQLiteBridgeStore(self.db_path)
        self.core = BridgeCore(self.store, executor)
        self.adapter = MCPAdapter(self.core, self.store)
        self._create_task("task-one")
        self.core.create_task("project-mcp", "second", task_id="task-two")

        first = asyncio.create_task(
            self.adapter.call_tool("run_task", {"task_id": "task-one"})
        )
        await executor.started.wait()
        with self.assertRaisesRegex(MCPConcurrencyError, "another task"):
            await self.adapter.call_tool("run_task", {"task_id": "task-two"})
        executor.release.set()
        result = await first

        self.assertEqual(result["execution_status"], "FINISHED")

    async def test_restart_preserves_task_and_events(self) -> None:
        self._create_task()
        await self.adapter.call_tool("run_task", {"task_id": "task-mcp"})
        self.store.close()
        reopened = SQLiteBridgeStore(self.db_path)
        try:
            restarted = MCPAdapter(BridgeCore(reopened, ImmediateExecutor()), reopened)
            task = await restarted.call_tool("get_task", {"task_id": "task-mcp"})
            events = await restarted.call_tool(
                "get_task_events", {"task_id": "task-mcp"}
            )
        finally:
            reopened.close()
            self.store = SQLiteBridgeStore(self.db_path)

        self.assertEqual(task["execution_status"], "FINISHED")
        self.assertEqual(events["events"][-1]["kind"], "task.finished")

    async def test_default_db_path_is_cwd_independent(self) -> None:
        local_app_data = Path(self.tempdir.name) / "LocalAppData"
        first_cwd = Path(self.tempdir.name) / "cwd-one"
        second_cwd = Path(self.tempdir.name) / "cwd-two"
        first_cwd.mkdir()
        second_cwd.mkdir()
        previous_cwd = Path.cwd()
        try:
            os.chdir(first_cwd)
            first = default_db_path(local_app_data)
            os.chdir(second_cwd)
            second = default_db_path(local_app_data)
        finally:
            os.chdir(previous_cwd)

        expected = local_app_data / "ChatGPTCodexBridge" / "state" / "bridge.sqlite3"
        self.assertEqual(first, expected)
        self.assertEqual(second, expected)

    async def test_official_stdio_process_honors_db_path_stdout_and_shutdown(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            db_path = Path(tempdir) / "lab" / "bridge.sqlite3"
            stderr_path = Path(tempdir) / "server.stderr.log"
            parameters = StdioServerParameters(
                command=sys.executable,
                args=["-m", "chatgpt_codex_bridge.mcp_server", "--db-path", str(db_path)],
                cwd=str(ROOT),
            )
            with stderr_path.open("w+", encoding="utf-8") as stderr:
                async with stdio_client(parameters, errlog=stderr) as (read, write):
                    async with ClientSession(read, write) as session:
                        initialized = await session.initialize()
                        tools = await session.list_tools()
                stderr.seek(0)
                stderr_text = stderr.read()

            self.assertTrue(initialized.protocol_version)
            self.assertEqual(len(tools.tools), 8)
            self.assertTrue(db_path.exists())
            self.assertIn("pid=", stderr_text)
            reopened = SQLiteBridgeStore(db_path)
            reopened.close()


if __name__ == "__main__":
    unittest.main()
