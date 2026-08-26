from __future__ import annotations

import json
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
from chatgpt_codex_bridge.persistence.sqlite_store import SQLiteBridgeStore  # noqa: E402
from chatgpt_codex_bridge.single_instance import (  # noqa: E402
    MCPInstanceAlreadyRunningError,
    MCPInstanceLock,
    canonical_db_path,
    lock_path_for_db,
)


CHILD_ENV = os.environ.copy()
CHILD_ENV["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + CHILD_ENV.get(
    "PYTHONPATH", ""
)

PROBE_CODE = """
import sys
from chatgpt_codex_bridge.single_instance import MCPInstanceAlreadyRunningError, MCPInstanceLock
try:
    lock = MCPInstanceLock(sys.argv[1])
    lock.acquire()
except MCPInstanceAlreadyRunningError:
    print("REJECTED", flush=True)
else:
    print("ACQUIRED", flush=True)
    lock.release()
"""

ACQUIRE_CODE = """
import sys
from chatgpt_codex_bridge.single_instance import MCPInstanceLock
with MCPInstanceLock(sys.argv[1]):
    print("ACQUIRED", flush=True)
"""


def run_child(code: str, *args: str, timeout: float = 10.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", code, *args],
        cwd=str(ROOT),
        env=CHILD_ENV,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def wait_for_file(path: Path, process: subprocess.Popen[str], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise AssertionError(
                f"holder exited early ({process.returncode}): {stdout!r} {stderr!r}"
            )
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {path}")


def start_holder(db_path: Path, ready_path: Path) -> subprocess.Popen[str]:
    code = (
        "from pathlib import Path; import sys,time; "
        "from chatgpt_codex_bridge.single_instance import MCPInstanceLock; "
        "lock=MCPInstanceLock(sys.argv[1]); lock.acquire(); "
        "Path(sys.argv[2]).write_text('READY', encoding='ascii'); "
        "time.sleep(60)"
    )
    return subprocess.Popen(
        [sys.executable, "-c", code, str(db_path), str(ready_path)],
        cwd=str(ROOT),
        env=CHILD_ENV,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.kill()
    try:
        process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate(timeout=5)


class MCPInstanceLockTests(unittest.TestCase):
    def test_path_is_canonical_and_lock_is_derived_per_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "nested" / "bridge.sqlite3"
            canonical = canonical_db_path(db_path)
            self.assertEqual(canonical, db_path.resolve())
            self.assertEqual(lock_path_for_db(db_path), Path(f"{canonical}.mcp.lock"))

    def test_normal_release_allows_next_process_and_file_can_remain(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "bridge.sqlite3"
            lock = MCPInstanceLock(db_path)
            lock.acquire()
            try:
                self.assertTrue(lock.acquired)
                blocked = run_child(PROBE_CODE, str(db_path))
                self.assertEqual(blocked.stdout.strip(), "REJECTED")
            finally:
                lock.release()
            self.assertTrue(lock.lock_path.exists())
            allowed = run_child(ACQUIRE_CODE, str(db_path))
            self.assertEqual(allowed.stdout.strip(), "ACQUIRED")

    def test_abrupt_process_death_releases_lock_and_stale_file_is_harmless(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db_path = root / "bridge.sqlite3"
            ready = root / "holder.ready"
            holder = start_holder(db_path, ready)
            try:
                wait_for_file(ready, holder)
                blocked = run_child(PROBE_CODE, str(db_path))
                self.assertEqual(blocked.stdout.strip(), "REJECTED")
                holder.kill()
                holder.communicate(timeout=5)
                allowed = run_child(ACQUIRE_CODE, str(db_path))
                self.assertEqual(allowed.stdout.strip(), "ACQUIRED")
            finally:
                stop_process(holder)

            lock_path = lock_path_for_db(db_path)
            lock_path.write_bytes(b"\0")
            with MCPInstanceLock(db_path) as lock:
                self.assertTrue(lock.acquired)

    def test_different_databases_can_be_locked_by_different_processes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db_a = root / "a" / "bridge.sqlite3"
            db_b = root / "b" / "bridge.sqlite3"
            with MCPInstanceLock(db_a):
                result = run_child(ACQUIRE_CODE, str(db_b))
            self.assertEqual(result.stdout.strip(), "ACQUIRED")

    def test_simultaneous_race_has_exactly_one_owner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db_path = root / "bridge.sqlite3"
            gate = root / "go"
            results = [root / "result-a", root / "result-b"]
            code = """
from pathlib import Path
import sys
import time
from chatgpt_codex_bridge.single_instance import MCPInstanceAlreadyRunningError, MCPInstanceLock
gate = Path(sys.argv[2])
result = Path(sys.argv[3])
deadline = time.monotonic() + 10
while not gate.exists() and time.monotonic() < deadline:
    time.sleep(0.005)
try:
    lock = MCPInstanceLock(sys.argv[1])
    lock.acquire()
except MCPInstanceAlreadyRunningError:
    result.write_text("REJECTED", encoding="ascii")
else:
    result.write_text("ACQUIRED", encoding="ascii")
    time.sleep(2)
"""
            processes = [
                subprocess.Popen(
                    [sys.executable, "-c", code, str(db_path), str(gate), str(result)],
                    cwd=str(ROOT),
                    env=CHILD_ENV,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                for result in results
            ]
            try:
                time.sleep(0.1)
                gate.write_text("GO", encoding="ascii")
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and not all(
                    result.exists() for result in results
                ):
                    time.sleep(0.02)
                self.assertTrue(all(result.exists() for result in results))
                values = [result.read_text(encoding="ascii") for result in results]
                self.assertEqual(values.count("ACQUIRED"), 1)
                self.assertEqual(values.count("REJECTED"), 1)
            finally:
                for process in processes:
                    stop_process(process)

    def test_owner_blocks_mcp_start_without_opening_or_recovering_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db_path = root / "bridge.sqlite3"
            store = SQLiteBridgeStore(db_path)
            core = BridgeCore(store, object())
            core.create_project("Bridge", "C:/workspace/recovery", project_id="project-recovery")
            core.create_task("project-recovery", "recover me", task_id="task-recovery")
            store.transition_task_running("task-recovery", project_id="project-recovery")
            store.close()

            ready = root / "holder.ready"
            holder = start_holder(db_path, ready)
            try:
                wait_for_file(ready, holder)
                blocked = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "chatgpt_codex_bridge.mcp_server",
                        "--db-path",
                        str(db_path),
                    ],
                    cwd=str(ROOT),
                    env=CHILD_ENV,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(blocked.returncode, 2)
                self.assertIn("Bridge MCP instance already active for database:", blocked.stderr)
                self.assertNotIn("MCP server pid=", blocked.stderr)

                unchanged = SQLiteBridgeStore(db_path)
                try:
                    task = unchanged.get_task("task-recovery")
                    events = unchanged.list_task_events("task-recovery")
                finally:
                    unchanged.close()
                self.assertIsNotNone(task)
                self.assertEqual(task.execution_status.value, "RUNNING")
                self.assertEqual(
                    [event.kind for event in events],
                    ["task.created", "task.started"],
                )
            finally:
                stop_process(holder)

            recovery_code = """
import json
import sys
from pathlib import Path
from chatgpt_codex_bridge.core import BridgeCore
from chatgpt_codex_bridge.persistence.sqlite_store import SQLiteBridgeStore
from chatgpt_codex_bridge.single_instance import MCPInstanceLock
db = Path(sys.argv[1])
with MCPInstanceLock(db):
    store = SQLiteBridgeStore(db)
    try:
        recovered = BridgeCore(store, object()).recover_orphaned_tasks()
        print(json.dumps({"recovered": [task.task_id for task in recovered]}), flush=True)
    finally:
        store.close()
"""
            recovered = run_child(recovery_code, str(db_path))
            self.assertEqual(recovered.returncode, 0, recovered.stderr)
            self.assertEqual(json.loads(recovered.stdout)["recovered"], ["task-recovery"])

            final = SQLiteBridgeStore(db_path)
            try:
                task = final.get_task("task-recovery")
                events = final.list_task_events("task-recovery")
            finally:
                final.close()
            self.assertIsNotNone(task)
            self.assertEqual(task.execution_status.value, "FAILED")
            self.assertEqual(
                [event.kind for event in events],
                ["task.created", "task.started", "task.recovered", "task.failed"],
            )


if __name__ == "__main__":
    unittest.main()
