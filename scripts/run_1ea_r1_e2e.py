"""Execute the single real 1E-A-R1 regression task in its new lab."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
LAB_ROOT = Path(r"C:\Codex\ChatGPT-Codex-Bridge-Lab\stage-1e-a-r1")
DB_PATH = LAB_ROOT / "bridge.sqlite3"
MODEL = "gpt-5.6-luna"
OBJECTIVE = """Read marker.txt and reply exactly:
BRIDGE_1EA_R1_OK

Do not modify files.
Do not use network access.
"""
EXPECTED_RESPONSE = "BRIDGE_1EA_R1_OK"

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


def _print_events(events) -> None:
    print("event_id\tsource\tkind")
    for event in events:
        print(f"{event.event_id}\t{event.source}\t{event.kind}")


async def run(executable: str | None) -> int:
    marker = LAB_ROOT / "marker.txt"
    if marker.read_text(encoding="utf-8").strip() != "BRIDGE_CORE_E2E_MARKER_1EA_R1":
        raise RuntimeError("marker.txt did not contain the required R1 marker")
    if DB_PATH.exists():
        raise RuntimeError(f"refusing to overwrite existing R1 database: {DB_PATH}")

    store = EvidenceStore(DB_PATH)
    executor = CodexExecutor(executable=executable)
    try:
        core = BridgeCore(store, executor)
        project = core.create_project(
            "1E-A-R1 E2E",
            str(LAB_ROOT),
            project_id="project-1ea-r1-real",
        )
        task = core.create_task(
            project.project_id,
            OBJECTIVE,
            model=MODEL,
            task_id="task-1ea-r1-real",
        )
        finished = await core.run_task(task.task_id)
        events_before_close = store.list_task_events(task.task_id)
        _print_events(events_before_close)
        turn_completed = next(
            event for event in events_before_close if event.kind == "turn/completed"
        )
        task_finished = next(
            event for event in events_before_close if event.kind == "task.finished"
        )
        if turn_completed.event_id >= task_finished.event_id:
            raise RuntimeError("turn/completed did not precede task.finished")
        final_response = task_finished.payload.get("final_response")
        if final_response != EXPECTED_RESPONSE:
            raise RuntimeError(f"unexpected final response: {final_response!r}")
        print(f"executable: {executor.last_client.executable if executor.last_client else None}")
        print(f"pid: {executor.last_pid}")
        print(f"model: {MODEL}")
        print(f"project: {project.project_id}")
        print(f"task: {finished.task_id}")
        print(f"thread: {finished.thread_id}")
        print(f"turn: {finished.turn_id}")
        print(f"final_response: {final_response}")
        print(f"execution_status: {finished.execution_status.value}")
        print(f"audit_status: {finished.audit_status.value}")
        print(f"turn_completed_event_id: {turn_completed.event_id}")
        print(f"task_finished_event_id: {task_finished.event_id}")
        print(f"codex_event_count_during_run: {len(store.persisted_during_run)}")
        print(f"close_result: {executor.last_close_result}")
        print(f"app_server_closed: {executor.last_client is not None and executor.last_client.process is None}")
    finally:
        store.close()

    reopened = SQLiteBridgeStore(DB_PATH)
    try:
        recovered_task = reopened.get_task("task-1ea-r1-real")
        recovered_events = reopened.list_task_events("task-1ea-r1-real")
        if recovered_task is None:
            raise RuntimeError("R1 task was not recovered")
        recovered_completed = next(
            event for event in recovered_events if event.kind == "turn/completed"
        )
        recovered_finished = next(
            event for event in recovered_events if event.kind == "task.finished"
        )
        recovered_response = recovered_finished.payload.get("final_response")
        print(f"reopened_task_status: {recovered_task.execution_status.value}")
        print(f"reopened_audit_status: {recovered_task.audit_status.value}")
        print(f"reopened_thread: {recovered_task.thread_id}")
        print(f"reopened_turn: {recovered_task.turn_id}")
        print(f"reopened_turn_completed_event_id: {recovered_completed.event_id}")
        print(f"reopened_task_finished_event_id: {recovered_finished.event_id}")
        print(f"reopened_final_response: {recovered_response}")
        print(f"reopened_event_count: {len(recovered_events)}")
        if recovered_response != EXPECTED_RESPONSE:
            raise RuntimeError("R1 final response was not durable")
        if recovered_completed.event_id >= recovered_finished.event_id:
            raise RuntimeError("reopened event order is invalid")
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
        print(f"E2E_R1_FAILED: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
