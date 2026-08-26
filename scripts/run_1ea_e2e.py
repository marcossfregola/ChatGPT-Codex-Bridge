"""Execute the real 1E-A Bridge flow in the protected laboratory."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
LAB_ROOT = Path(r"C:\Codex\ChatGPT-Codex-Bridge-Lab\stage-1e-a")
DB_PATH = LAB_ROOT / "bridge.sqlite3"
MODEL = "gpt-5.6-luna"
OBJECTIVE = """Read marker.txt from the current workspace and reply exactly:
BRIDGE_1EA_OK

Do not modify files.
Do not use network access.
"""

sys.path.insert(0, str(SRC_ROOT))

from chatgpt_codex_bridge.core import BridgeCore  # noqa: E402
from chatgpt_codex_bridge.executors import CodexExecutor  # noqa: E402
from chatgpt_codex_bridge.persistence import SQLiteBridgeStore  # noqa: E402


class EvidenceStore(SQLiteBridgeStore):
    def __init__(self, db_path: Path) -> None:
        self.persisted_during_run: list[tuple[int | None, str, str]] = []
        super().__init__(db_path)

    def append_task_event(self, task_id, source, kind, payload, **kwargs):
        event = super().append_task_event(task_id, source, kind, payload, **kwargs)
        if source == "codex":
            self.persisted_during_run.append((event.event_id, source, kind))
        return event


async def run(executable: str | None) -> int:
    marker = LAB_ROOT / "marker.txt"
    if marker.read_text(encoding="utf-8").strip() != "BRIDGE_CORE_E2E_MARKER_1EA":
        raise RuntimeError("marker.txt did not contain the required marker")
    if DB_PATH.exists():
        raise RuntimeError(f"refusing to overwrite existing E2E database: {DB_PATH}")

    store = EvidenceStore(DB_PATH)
    executor = CodexExecutor(executable=executable)
    try:
        core = BridgeCore(store, executor)
        project = core.create_project(
            "1E-A E2E",
            str(LAB_ROOT),
            project_id="project-1ea-real",
        )
        task = core.create_task(
            project.project_id,
            OBJECTIVE,
            model=MODEL,
            task_id="task-1ea-real",
        )
        finished = await core.run_task(task.task_id)
        events_before_close = store.list_task_events(task.task_id)
        codex_event_ids = [
            event.event_id for event in events_before_close if event.source == "codex"
        ]
        finished_event = next(
            event for event in events_before_close if event.kind == "task.finished"
        )
        final_response = finished_event.payload.get("final_response")
        close_result = executor.last_close_result
        client = executor.last_client
        print(f"executable: {client.executable if client is not None else None}")
        print(f"pid: {executor.last_pid}")
        print(f"account: {executor.last_account_type}")
        print(f"model: {MODEL}")
        print(f"project: {project.project_id}")
        print(f"task: {finished.task_id}")
        print(f"thread: {finished.thread_id}")
        print(f"turn: {finished.turn_id}")
        print(f"status: {finished.execution_status.value}")
        print(f"audit_status: {finished.audit_status.value}")
        print(f"final_response: {final_response}")
        print(f"journal_events: {len(events_before_close)}")
        print("journal_kinds: " + ", ".join(event.kind for event in events_before_close))
        print(f"codex_event_count: {len(codex_event_ids)}")
        print(f"codex_event_ids: {codex_event_ids}")
        print(f"codex_before_task_finished: {all(event_id < finished_event.event_id for event_id in codex_event_ids)}")
        print(f"persisted_during_run_count: {len(store.persisted_during_run)}")
        print(f"close_result: {close_result}")
        print(f"app_server_closed: {client is not None and client.process is None}")
    finally:
        store.close()

    reopened = SQLiteBridgeStore(DB_PATH)
    try:
        recovered_task = reopened.get_task("task-1ea-real")
        recovered_events = reopened.list_task_events("task-1ea-real")
        recovered_finished = next(
            event for event in recovered_events if event.kind == "task.finished"
        )
        print(f"reopened_task_status: {recovered_task.execution_status.value if recovered_task else None}")
        print(f"reopened_audit_status: {recovered_task.audit_status.value if recovered_task else None}")
        print(f"reopened_thread: {recovered_task.thread_id if recovered_task else None}")
        print(f"reopened_turn: {recovered_task.turn_id if recovered_task else None}")
        print(f"reopened_event_count: {len(recovered_events)}")
        print(f"reopened_final_response: {recovered_finished.payload.get('final_response')}")
    finally:
        reopened.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--executable")
    args = parser.parse_args()
    try:
        return asyncio.run(run(args.executable))
    except Exception as exc:
        print(f"E2E_FAILED: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
