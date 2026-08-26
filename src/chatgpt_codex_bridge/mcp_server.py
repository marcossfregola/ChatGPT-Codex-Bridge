"""Official MCP Python SDK v2 server for the Bridge application boundary."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import __version__
from .core import BridgeCore
from .executors.codex_executor import CodexExecutor
from .mcp_adapter import MCPAdapter, MCPToolError
from .persistence.sqlite_store import SQLiteBridgeStore
from .single_instance import (
    MCPInstanceAlreadyRunningError,
    MCPInstanceLock,
    MCPInstanceLockError,
    canonical_db_path,
    lock_path_for_db,
)


SERVER_NAME = "chatgpt-codex-bridge"
DEFAULT_APP_DIRECTORY = "ChatGPTCodexBridge"


def default_db_path(local_app_data: str | os.PathLike[str] | None = None) -> Path:
    """Return the stable Bridge-owned default database path.

    The optional argument keeps path resolution deterministic in tests without
    changing the production contract: LOCALAPPDATA is independent of cwd.
    """

    root = local_app_data
    if root is None:
        root = os.environ.get("LOCALAPPDATA")
    if root is None:
        root = Path.home() / "AppData" / "Local"
    return Path(root) / DEFAULT_APP_DIRECTORY / "state" / "bridge.sqlite3"


async def _call_adapter(
    adapter: MCPAdapter, name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    """Translate Bridge tool failures into official SDK tool errors."""

    try:
        return await adapter.call_tool(name, arguments)
    except MCPToolError as exc:
        raise ToolError(str(exc)) from exc
    except Exception as exc:
        # Keep unexpected implementation details server-side.  The official
        # SDK turns ToolError into a CallToolResult with isError=true.
        raise ToolError(f"tool {name!r} failed") from exc


def build_server(adapter: MCPAdapter) -> MCPServer:
    """Build one official SDK server whose tools delegate to MCPAdapter."""

    server = MCPServer(SERVER_NAME, version=__version__)

    @server.tool(
        name="get_status",
        description="Return the current durable Bridge status.",
        structured_output=True,
    )
    async def get_status() -> dict[str, Any]:
        return await _call_adapter(adapter, "get_status", {})

    @server.tool(
        name="create_project",
        description="Create a Bridge project.",
        structured_output=True,
    )
    async def create_project(
        name: str,
        repo_path: str,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        return await _call_adapter(
            adapter,
            "create_project",
            {"name": name, "repo_path": repo_path, "project_id": project_id},
        )

    @server.tool(
        name="create_task",
        description="Create a queued Bridge task.",
        structured_output=True,
    )
    async def create_task(
        project_id: str,
        objective: str,
        model: str | None = None,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        return await _call_adapter(
            adapter,
            "create_task",
            {
                "project_id": project_id,
                "objective": objective,
                "model": model,
                "task_id": task_id,
            },
        )

    @server.tool(
        name="run_task",
        description="Run one queued Bridge task.",
        structured_output=True,
    )
    async def run_task(task_id: str) -> dict[str, Any]:
        return await _call_adapter(adapter, "run_task", {"task_id": task_id})

    @server.tool(
        name="get_task",
        description="Read one durable Bridge task.",
        structured_output=True,
    )
    async def get_task(task_id: str) -> dict[str, Any]:
        return await _call_adapter(adapter, "get_task", {"task_id": task_id})

    @server.tool(
        name="get_task_events",
        description="Read a task journal in event_id order.",
        structured_output=True,
    )
    async def get_task_events(
        task_id: str,
        limit: int | None = None,
    ) -> dict[str, Any]:
        return await _call_adapter(
            adapter,
            "get_task_events",
            {"task_id": task_id, "limit": limit},
        )

    @server.tool(
        name="get_result",
        description="Recover a durable task result from SQLite.",
        structured_output=True,
    )
    async def get_result(task_id: str) -> dict[str, Any]:
        return await _call_adapter(adapter, "get_result", {"task_id": task_id})

    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", type=Path, default=default_db_path())
    parser.add_argument("--executable")
    args = parser.parse_args(argv)
    db_path = canonical_db_path(args.db_path)
    try:
        with MCPInstanceLock(db_path):
            store = SQLiteBridgeStore(db_path)
            try:
                print(
                    f"{SERVER_NAME} MCP server pid={os.getpid()}",
                    file=sys.stderr,
                    flush=True,
                )
                executor = CodexExecutor(executable=args.executable)
                core = BridgeCore(store, executor)
                core.recover_orphaned_tasks()
                adapter = MCPAdapter(core, store)
                build_server(adapter).run(transport="stdio")
                return 0
            finally:
                store.close()
    except MCPInstanceAlreadyRunningError as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        return 2
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DEFAULT_APP_DIRECTORY",
    "MCPInstanceAlreadyRunningError",
    "MCPInstanceLock",
    "MCPInstanceLockError",
    "MCPServer",
    "build_server",
    "canonical_db_path",
    "default_db_path",
    "lock_path_for_db",
    "main",
]
