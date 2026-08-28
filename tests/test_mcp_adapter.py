from __future__ import annotations

import asyncio
from contextlib import suppress
import inspect
import os
from pathlib import Path
import sys
import tempfile
import time
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
from chatgpt_codex_bridge.execution_worker import ExecutionWorker  # noqa: E402
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
        self.assertIsNone(result["next_cursor"])
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
        self.assertIsNone(result["next_cursor"])
        kinds = {event["kind"] for event in result["events"]}
        self.assertIn("task.created", kinds)
        self.assertIn("task.finished", kinds)
        self.assertLessEqual(len(result["events"]), 100 + 64)

    async def test_get_task_events_legacy_shape_includes_stable_cursor(self) -> None:
        self._create_task()

        result = await self.adapter.call_tool(
            "get_task_events", {"task_id": "task-mcp"}
        )

        self.assertEqual(result["count"], 1)
        self.assertFalse(result["truncated"])
        self.assertEqual(result["next_cursor"], result["events"][-1]["event_id"])

    async def test_get_task_events_since_event_id_is_exclusive_and_ordered(self) -> None:
        self._create_task()
        created = self.store.get_last_task_event("task-mcp")
        self.assertIsNotNone(created)
        appended = [
            self.store.append_task_event(
                "task-mcp", "codex", "incremental", {"index": index}
            )
            for index in range(4)
        ]
        assert created is not None

        result = await self.adapter.call_tool(
            "get_task_events",
            {"task_id": "task-mcp", "since_event_id": created.event_id},
        )

        ids = [event["event_id"] for event in result["events"]]
        self.assertEqual(ids, [event.event_id for event in appended])
        self.assertTrue(all(event_id > created.event_id for event_id in ids))
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(result["count"], len(appended))
        self.assertFalse(result["truncated"])
        self.assertEqual(result["next_cursor"], appended[-1].event_id)

    async def test_get_task_events_empty_page_keeps_cursor(self) -> None:
        self._create_task()
        last = self.store.get_last_task_event("task-mcp")
        self.assertIsNotNone(last)
        assert last is not None

        result = await self.adapter.call_tool(
            "get_task_events",
            {"task_id": "task-mcp", "since_event_id": last.event_id},
        )

        self.assertEqual(result["events"], [])
        self.assertEqual(result["count"], 0)
        self.assertFalse(result["truncated"])
        self.assertEqual(result["next_cursor"], last.event_id)

    async def test_get_task_events_incremental_pages_continue_without_gaps(self) -> None:
        self._create_task()
        initial = [
            self.store.get_last_task_event("task-mcp"),
            *(
                self.store.append_task_event(
                    "task-mcp", "codex", "incremental", {"index": index}
                )
                for index in range(4)
            ),
        ]
        expected_initial = [event.event_id for event in initial if event is not None]
        page_one = await self.adapter.call_tool(
            "get_task_events",
            {"task_id": "task-mcp", "since_event_id": 0, "limit": 2},
        )
        ids_one = [event["event_id"] for event in page_one["events"]]
        self.assertEqual(ids_one, expected_initial[:2])
        self.assertTrue(page_one["truncated"])
        self.assertEqual(page_one["next_cursor"], ids_one[-1])

        appended = [
            self.store.append_task_event(
                "task-mcp", "codex", "incremental", {"index": index}
            )
            for index in range(4, 6)
        ]
        expected_all = [*expected_initial, *(event.event_id for event in appended)]

        page_two = await self.adapter.call_tool(
            "get_task_events",
            {
                "task_id": "task-mcp",
                "since_event_id": page_one["next_cursor"],
                "limit": 3,
            },
        )
        ids_two = [event["event_id"] for event in page_two["events"]]
        self.assertEqual(ids_two, expected_all[2:5])
        self.assertTrue(page_two["truncated"])

        page_three = await self.adapter.call_tool(
            "get_task_events",
            {
                "task_id": "task-mcp",
                "since_event_id": page_two["next_cursor"],
                "limit": 3,
            },
        )
        ids_three = [event["event_id"] for event in page_three["events"]]
        self.assertEqual(ids_three, expected_all[5:])
        self.assertFalse(page_three["truncated"])

        all_ids = [*ids_one, *ids_two, *ids_three]
        self.assertEqual(all_ids, expected_all)
        self.assertEqual(len(all_ids), len(set(all_ids)))

    async def test_get_task_events_rejects_invalid_since_event_id(self) -> None:
        self._create_task()
        for value in (-1, True, "1", 1.5):
            with self.assertRaises(MCPToolError):
                await self.adapter.call_tool(
                    "get_task_events",
                    {"task_id": "task-mcp", "since_event_id": value},
                )

    async def test_official_get_task_events_schema_exposes_since_event_id(self) -> None:
        async def exercise(session):
            await session.initialize()
            tools = await session.list_tools()
            return next(tool for tool in tools.tools if tool.name == "get_task_events")

        tool = await self._with_official_client(exercise)
        cursor_schema = tool.input_schema["properties"]["since_event_id"]
        schema_types = {cursor_schema.get("type")}
        schema_types.update(
            part.get("type")
            for part in cursor_schema.get("anyOf", [])
            if isinstance(part, dict)
        )
        self.assertIn("integer", schema_types)
        self.assertNotIn("since_event_id", tool.input_schema.get("required", []))

    async def test_get_result_uses_targeted_latest_queries(self) -> None:
        self._create_task()
        await self.adapter.call_tool("run_task", {"task_id": "task-mcp"})
        await ExecutionWorker(self.store, self.core).run_once()
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

    async def test_cancelled_core_run_propagates_and_next_worker_task_runs(self) -> None:
        executor = BlockingExecutor()
        self.store.close()
        self.store = SQLiteBridgeStore(self.db_path)
        self.core = BridgeCore(self.store, executor)
        self.adapter = MCPAdapter(self.core, self.store)
        self._create_task("task-cancelled")

        running = asyncio.create_task(self.core.run_task("task-cancelled"))
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
        accepted = await self.adapter.call_tool(
            "run_task", {"task_id": "task-next"}
        )
        self.assertTrue(accepted["accepted"])
        await ExecutionWorker(self.store, self.core).run_once()
        next_result = await self.adapter.call_tool(
            "get_task", {"task_id": "task-next"}
        )
        self.assertEqual(next_result["execution_status"], "FINISHED")

    async def test_official_request_boundary_keeps_server_operational(self) -> None:
        self._create_task("task-request")
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
                    accepted = await session.call_tool(
                        "run_task", {"task_id": "task-request"}
                    )
                    self.assertFalse(accepted.is_error)
                    self.assertTrue(accepted.structured_content["accepted"])
                    self.assertEqual(
                        accepted.structured_content["execution_status"],
                        ExecutionStatus.QUEUED.value,
                    )
                    self.assertEqual(self.executor.requests, [])
                    await ExecutionWorker(self.store, self.core).run_once()
                    self.assertEqual(
                        self.store.get_task("task-request").execution_status,
                        ExecutionStatus.FINISHED,
                    )
                    status = await session.call_tool("get_status", {})
                    self.assertFalse(status.is_error)

                    next_accepted = await session.call_tool(
                        "run_task", {"task_id": "task-request-next"}
                    )
                    self.assertFalse(next_accepted.is_error)
                    await ExecutionWorker(self.store, self.core).run_once()
                    next_result = await session.call_tool(
                        "get_task", {"task_id": "task-request-next"}
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

    async def test_official_server_remains_request_only_during_worker_execution(self) -> None:
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
            try:
                async with ClientSession(client[0], client[1]) as session:
                    await session.initialize()
                    accepted = await session.call_tool(
                        "run_task", {"task_id": "task-global-shutdown"}
                    )
                    self.assertFalse(accepted.is_error)
                    self.assertTrue(accepted.structured_content["accepted"])
                    self.assertEqual(self.executor.requests, [])
                    await ExecutionWorker(self.store, self.core).run_once()
                    self.assertEqual(
                        self.store.get_task("task-global-shutdown").execution_status,
                        ExecutionStatus.FINISHED,
                    )
            finally:
                server_task.cancel()
                with suppress(asyncio.CancelledError):
                    await server_task

    async def test_official_get_result_after_restart(self) -> None:
        self._create_task()
        await self.adapter.call_tool("run_task", {"task_id": "task-mcp"})
        await ExecutionWorker(self.store, self.core).run_once()
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
        self.assertEqual(len(self.executor.requests), 0)
        self.assertTrue(result.structured_content["accepted"])
        self.assertEqual(result.structured_content["execution_status"], "QUEUED")
        self.assertNotIn("final_response", result.structured_content)
        await ExecutionWorker(self.store, self.core).run_once()
        final = await self.adapter.call_tool("get_result", {"task_id": "task-mcp"})
        self.assertTrue(final["available"])
        self.assertEqual(final["final_response"], "MCP_FAKE_OK")

    async def test_run_task_returns_before_a_slow_worker_finishes(self) -> None:
        executor = BlockingExecutor()
        self.store.close()
        self.store = SQLiteBridgeStore(self.db_path)
        self.core = BridgeCore(self.store, executor)
        self.adapter = MCPAdapter(self.core, self.store)
        self._create_task("task-fast-return")

        started_at = time.monotonic()
        accepted = await self.adapter.call_tool(
            "run_task", {"task_id": "task-fast-return"}
        )
        elapsed = time.monotonic() - started_at
        self.assertTrue(accepted["accepted"])
        self.assertEqual(accepted["execution_status"], ExecutionStatus.QUEUED.value)
        self.assertLess(elapsed, 1.0)

        worker_task = asyncio.create_task(
            ExecutionWorker(self.store, self.core).run_once()
        )
        await asyncio.wait_for(executor.started.wait(), timeout=2)
        self.assertFalse(worker_task.done())
        executor.release.set()
        await asyncio.wait_for(worker_task, timeout=5)

    async def test_run_task_does_not_relaunch_non_queued_states(self) -> None:
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

            result = await self.adapter.call_tool("run_task", {"task_id": task_id})
            self.assertEqual(result["execution_status"], status.value)
            self.assertEqual(result["accepted"], status is ExecutionStatus.RUNNING)

        self.assertEqual(self.executor.requests, [])

    async def test_consecutive_requests_are_durable_and_worker_serializes_execution(self) -> None:
        self._create_task("task-one")
        self.core.create_task("project-mcp", "second", task_id="task-two")

        first = await self.adapter.call_tool("run_task", {"task_id": "task-one"})
        second = await self.adapter.call_tool("run_task", {"task_id": "task-two"})
        self.assertTrue(first["accepted"])
        self.assertTrue(second["accepted"])
        await ExecutionWorker(self.store, self.core).run_once()
        await ExecutionWorker(self.store, self.core).run_once()
        self.assertEqual(self.store.get_task("task-one").execution_status, ExecutionStatus.FINISHED)
        self.assertEqual(self.store.get_task("task-two").execution_status, ExecutionStatus.FINISHED)

    async def test_restart_preserves_task_and_events(self) -> None:
        self._create_task()
        await self.adapter.call_tool("run_task", {"task_id": "task-mcp"})
        await ExecutionWorker(self.store, self.core).run_once()
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

    async def test_mcp_restart_does_not_recover_a_legitimate_running_claim(self) -> None:
        self._create_task("task-running-mcp-restart")
        await self.adapter.call_tool(
            "run_task", {"task_id": "task-running-mcp-restart"}
        )
        worker = ExecutionWorker(
            self.store,
            self.core,
            worker_id="mcp-restart-worker",
            pid=61001,
        )
        self.assertIsNotNone(worker.claim_next())
        self.store.close()

        reopened = SQLiteBridgeStore(self.db_path)
        try:
            restarted = MCPAdapter(BridgeCore(reopened), reopened)
            task = await restarted.call_tool(
                "get_task", {"task_id": "task-running-mcp-restart"}
            )
            events = await restarted.call_tool(
                "get_task_events", {"task_id": "task-running-mcp-restart"}
            )
        finally:
            reopened.close()
            self.store = SQLiteBridgeStore(self.db_path)

        self.assertEqual(task["execution_status"], ExecutionStatus.RUNNING.value)
        self.assertNotIn("task.recovered", [event["kind"] for event in events["events"]])

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
