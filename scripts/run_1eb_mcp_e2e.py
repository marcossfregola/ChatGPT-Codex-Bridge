"""Run the official MCP SDK fake flow and one real Luna flow for 1E-B-R1."""

from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress
import re
from pathlib import Path
import sys
import tempfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.memory import create_client_server_memory_streams


REPO_ROOT = Path(__file__).resolve().parents[1]
LAB_ROOT = Path(r"C:\Codex\ChatGPT-Codex-Bridge-Lab\stage-1e-b-r1")
DB_PATH = LAB_ROOT / "bridge.sqlite3"
MARKER = "BRIDGE_MCP_OFFICIAL_E2E_MARKER_1EB_R1"
EXPECTED_RESPONSE = "BRIDGE_1EB_R1_OK"
FAKE_RESPONSE = "BRIDGE_FAKE_1EB_R1_OK"
MODEL = "gpt-5.6-luna"
TASK_ID = "task-1eb-r1-real"
PROJECT_ID = "project-1eb-r1-real"
OBJECTIVE = """Read marker.txt and reply exactly:

BRIDGE_1EB_R1_OK

Do not modify files.
Do not use network access."""
EXPECTED_TOOLS = [
    "get_status",
    "create_project",
    "create_task",
    "run_task",
    "get_task",
    "get_task_events",
    "get_result",
]
VENV_PYTHON = REPO_ROOT / ".venv" / "Scripts" / "python.exe"

sys.path.insert(0, str(REPO_ROOT / "src"))

from chatgpt_codex_bridge.core import BridgeCore  # noqa: E402
from chatgpt_codex_bridge.domain.models import ExecutionStatus  # noqa: E402
from chatgpt_codex_bridge.executors.base import ExecutionResult  # noqa: E402
from chatgpt_codex_bridge.mcp_adapter import MCPAdapter  # noqa: E402
from chatgpt_codex_bridge.mcp_server import build_server  # noqa: E402
from chatgpt_codex_bridge.persistence.sqlite_store import SQLiteBridgeStore  # noqa: E402


class FakeExecutor:
    async def run(self, request, *, on_correlation=None, on_notification=None):
        if on_correlation is not None:
            on_correlation("thread-fake-1eb-r1", "turn-fake-1eb-r1")
        if on_notification is not None:
            on_notification(
                "thread/started",
                {"threadId": "thread-fake-1eb-r1"},
            )
            on_notification(
                "turn/completed",
                {
                    "turn": {
                        "id": "turn-fake-1eb-r1",
                        "status": "completed",
                    }
                },
            )
        return ExecutionResult(
            thread_id="thread-fake-1eb-r1",
            turn_id="turn-fake-1eb-r1",
            status=ExecutionStatus.FINISHED,
            final_response=FAKE_RESPONSE,
        )


async def _run_memory_server(server, callback):
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


async def run_fake_local() -> None:
    """Exercise official client → official server → adapter → Core → fake executor."""

    with tempfile.TemporaryDirectory() as tempdir:
        store = SQLiteBridgeStore(Path(tempdir) / "bridge.sqlite3")
        try:
            adapter = MCPAdapter(BridgeCore(store, FakeExecutor()), store)
            server = build_server(adapter)

            async def exercise(session):
                initialized = await session.initialize()
                tools = await session.list_tools()
                project = await session.call_tool(
                    "create_project",
                    {
                        "project_id": "project-1eb-r1-fake",
                        "name": "1E-B-R1 fake",
                        "repo_path": tempdir,
                    },
                )
                task = await session.call_tool(
                    "create_task",
                    {
                        "project_id": project.structured_content["project_id"],
                        "task_id": "task-1eb-r1-fake",
                        "objective": "fake official MCP task",
                    },
                )
                finished = await session.call_tool(
                    "run_task",
                    {"task_id": task.structured_content["task_id"]},
                )
                result = await session.call_tool(
                    "get_result",
                    {"task_id": task.structured_content["task_id"]},
                )
                return initialized, tools, project, task, finished, result

            initialized, tools, project, task, finished, result = await _run_memory_server(
                server, exercise
            )
        finally:
            store.close()

    if [tool.name for tool in tools.tools] != EXPECTED_TOOLS:
        raise RuntimeError(
            "official fake tools/list mismatch: "
            f"{[tool.name for tool in tools.tools]!r}"
        )
    if any(response.is_error for response in (project, task, finished, result)):
        raise RuntimeError("official fake tools/call returned an error")
    fake_result = result.structured_content
    if fake_result.get("final_response") != FAKE_RESPONSE:
        raise RuntimeError(f"unexpected fake result: {fake_result!r}")

    print(f"fake_mcp_protocol_version: {initialized.protocol_version}")
    print("fake_mcp_tools: " + ",".join(EXPECTED_TOOLS))
    print("fake_mcp_calls: initialize, tools/list, create_project, create_task, run_task, get_result")
    print(f"fake_mcp_response: {fake_result['final_response']}")
    print("fake_mcp_shutdown: ok")


async def _call_tool(session, name: str, arguments: dict) -> dict:
    response = await session.call_tool(name, arguments)
    if response.is_error:
        message = response.content[0].text if response.content else "unknown MCP tool error"
        raise RuntimeError(f"MCP tool {name} failed: {message}")
    if response.structured_content is None:
        raise RuntimeError(f"MCP tool {name} did not return structured content")
    return response.structured_content


def _event_rows(events: list[dict]) -> list[tuple[int, str, str]]:
    rows = []
    for event in events:
        event_id = event.get("event_id")
        if not isinstance(event_id, int):
            raise RuntimeError(f"event_id was not an integer: {event!r}")
        rows.append((event_id, str(event.get("source")), str(event.get("kind"))))
    return rows


async def run_real(executable: str | None) -> None:
    marker_path = LAB_ROOT / "marker.txt"
    expected_marker = MARKER + "\n"
    if marker_path.read_text(encoding="utf-8") != expected_marker:
        raise RuntimeError("stage-1e-b-r1 marker.txt did not contain the exact marker")
    if DB_PATH.exists():
        raise RuntimeError(f"refusing to overwrite existing E2E database: {DB_PATH}")
    if not VENV_PYTHON.exists():
        raise RuntimeError(f"project venv Python does not exist: {VENV_PYTHON}")

    args = [
        "-m",
        "chatgpt_codex_bridge.mcp_server",
        "--db-path",
        str(DB_PATH),
    ]
    if executable:
        args.extend(["--executable", executable])
    parameters = StdioServerParameters(
        command=str(VENV_PYTHON),
        args=args,
        cwd=str(REPO_ROOT),
    )

    with tempfile.TemporaryDirectory() as tempdir:
        stderr_path = Path(tempdir) / "mcp-server.stderr.log"
        with stderr_path.open("w+", encoding="utf-8") as stderr:
            try:
                async with stdio_client(parameters, errlog=stderr) as (read, write):
                    async with ClientSession(read, write) as session:
                        initialized = await session.initialize()
                        tools = await session.list_tools()
                        if [tool.name for tool in tools.tools] != EXPECTED_TOOLS:
                            raise RuntimeError(
                                "official real tools/list mismatch: "
                                f"{[tool.name for tool in tools.tools]!r}"
                            )
                        project = await _call_tool(
                            session,
                            "create_project",
                            {
                                "project_id": PROJECT_ID,
                                "name": "1E-B-R1 real",
                                "repo_path": str(LAB_ROOT),
                            },
                        )
                        task = await _call_tool(
                            session,
                            "create_task",
                            {
                                "project_id": PROJECT_ID,
                                "task_id": TASK_ID,
                                "objective": OBJECTIVE,
                                "model": MODEL,
                            },
                        )
                        finished = await _call_tool(
                            session,
                            "run_task",
                            {"task_id": TASK_ID},
                        )
                        durable_task = await _call_tool(
                            session,
                            "get_task",
                            {"task_id": TASK_ID},
                        )
                        result = await _call_tool(
                            session,
                            "get_result",
                            {"task_id": TASK_ID},
                        )
                        events_result = await _call_tool(
                            session,
                            "get_task_events",
                            {"task_id": TASK_ID},
                        )
            except BaseException as exc:
                stderr.seek(0)
                diagnostic = stderr.read().strip()
                raise RuntimeError(
                    "official MCP server process failed"
                    + (f": {diagnostic}" if diagnostic else " without stderr")
                ) from exc

            stderr.seek(0)
            stderr_text = stderr.read()

    pid_match = re.search(r"pid=(\d+)", stderr_text)
    if pid_match is None:
        raise RuntimeError(f"MCP server PID was not recorded: {stderr_text!r}")
    pid = pid_match.group(1)

    if finished["final_response"] != EXPECTED_RESPONSE:
        raise RuntimeError(f"unexpected run_task response: {finished!r}")
    if not result["available"] or result["final_response"] != EXPECTED_RESPONSE:
        raise RuntimeError(f"unexpected durable MCP result: {result!r}")
    if durable_task["execution_status"] != "FINISHED":
        raise RuntimeError(f"unexpected task status: {durable_task!r}")
    if durable_task["audit_status"] != "PENDING":
        raise RuntimeError(f"unexpected audit status: {durable_task!r}")

    events = events_result["events"]
    rows = _event_rows(events)
    if rows != sorted(rows):
        raise RuntimeError(f"event journal is not ordered: {rows!r}")
    kinds = [kind for _event_id, _source, kind in rows]
    required = ["task.created", "task.started", "turn/completed", "task.finished"]
    missing = [kind for kind in required if kind not in kinds]
    if missing:
        raise RuntimeError(f"missing required journal events: {missing!r}; got {kinds!r}")
    completed_id = next(event_id for event_id, _source, kind in rows if kind == "turn/completed")
    finished_id = next(event_id for event_id, _source, kind in rows if kind == "task.finished")
    if completed_id >= finished_id:
        raise RuntimeError(
            f"turn/completed must precede task.finished: {completed_id} >= {finished_id}"
        )

    print(f"real_mcp_protocol_version: {initialized.protocol_version}")
    print(f"real_mcp_pid: {pid}")
    print(f"real_mcp_tools: {','.join(EXPECTED_TOOLS)}")
    print(f"real_mcp_model: {MODEL}")
    print(f"real_mcp_task: {durable_task['task_id']}")
    print(f"real_mcp_thread: {durable_task['thread_id']}")
    print(f"real_mcp_turn: {durable_task['turn_id']}")
    print(f"real_mcp_execution_status: {durable_task['execution_status']}")
    print(f"real_mcp_audit_status: {durable_task['audit_status']}")
    print(f"real_mcp_final_response: {result['final_response']}")
    print("real_mcp_events:")
    for event_id, source, kind in rows:
        print(f"{event_id}\t{source}\t{kind}")
    if stderr_text.strip():
        print("real_mcp_stderr:")
        print(stderr_text.strip())


def reopen_and_report() -> None:
    reopened = SQLiteBridgeStore(DB_PATH)
    try:
        task = reopened.get_task(TASK_ID)
        events = reopened.list_task_events(TASK_ID)
        if task is None:
            raise RuntimeError("real MCP task was not durable after process shutdown")
        finished_events = [event for event in events if event.kind == "task.finished"]
        if not finished_events:
            raise RuntimeError("task.finished was not durable after process shutdown")
        final_response = finished_events[-1].payload.get("final_response")
        if final_response != EXPECTED_RESPONSE:
            raise RuntimeError(f"unexpected reopened final response: {final_response!r}")
        print("reopened_db: ok")
        print(f"reopened_task: {task.task_id}")
        print(f"reopened_thread: {task.thread_id}")
        print(f"reopened_turn: {task.turn_id}")
        print(f"reopened_execution_status: {task.execution_status.value}")
        print(f"reopened_audit_status: {task.audit_status.value}")
        print(f"reopened_event_count: {len(events)}")
        print(f"reopened_final_response: {final_response}")
    finally:
        reopened.close()


async def run(executable: str | None) -> int:
    await run_fake_local()
    await run_real(executable)
    reopen_and_report()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable")
    args = parser.parse_args()
    try:
        return asyncio.run(run(args.executable))
    except Exception as exc:
        print(f"E2E_1EB_R1_FAILED: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
