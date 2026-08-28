from __future__ import annotations

import asyncio
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT / "src"))

from chatgpt_codex_bridge.core import BridgeCore  # noqa: E402
from chatgpt_codex_bridge.execution_worker import (  # noqa: E402
    ExecutionWorker,
    _write_text_atomically,
    worker_runtime_paths,
)
from chatgpt_codex_bridge.persistence.sqlite_store import SQLiteBridgeStore  # noqa: E402


class AtomicSidecarPublicationTests(unittest.TestCase):
    def test_publishes_and_replaces_existing_destination(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "worker.state.json"
            destination.write_text("old\n", encoding="utf-8")

            _write_text_atomically(destination, "new\n")

            self.assertEqual(destination.read_text(encoding="utf-8"), "new\n")
            self.assertEqual(list(destination.parent.glob(".worker.state.json.*.tmp")), [])

    def test_retries_bounded_permission_error_then_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "worker.state.json"
            calls = 0
            real_replace = os.replace

            def flaky_replace(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise PermissionError(5, "Access is denied")
                real_replace(source, target)

            with patch(
                "chatgpt_codex_bridge.execution_worker.os.replace",
                side_effect=flaky_replace,
            ):
                _write_text_atomically(destination, "new\n")

            self.assertEqual(calls, 2)
            self.assertEqual(destination.read_text(encoding="utf-8"), "new\n")

    def test_persistent_failure_cleans_only_this_operation_temp(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "worker.state.json"
            foreign_temp = destination.with_name(".worker.state.json.foreign.tmp")
            foreign_temp.write_text("foreign\n", encoding="utf-8")

            with patch(
                "chatgpt_codex_bridge.execution_worker.os.replace",
                side_effect=PermissionError(5, "Access is denied"),
            ):
                with self.assertRaises(PermissionError):
                    _write_text_atomically(destination, "new\n")

            self.assertFalse(destination.exists())
            self.assertEqual(foreign_temp.read_text(encoding="utf-8"), "foreign\n")
            self.assertEqual(
                list(destination.parent.glob(f".worker.state.json.{os.getpid()}.tmp")),
                [],
            )


class WorkerObservabilityFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_state_publish_failure_degrades_observability_without_stopping_worker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "bridge.sqlite3"
            store = SQLiteBridgeStore(db_path)
            try:
                core = BridgeCore(store)
                core.create_project("Sidecar", "C:/sidecar", project_id="sidecar-project")
                worker = ExecutionWorker(store, core)
                worker.runtime_paths = worker_runtime_paths(db_path)
                stop_event = asyncio.Event()

                async def stop_soon() -> None:
                    await asyncio.sleep(0.03)
                    stop_event.set()

                with patch(
                    "chatgpt_codex_bridge.execution_worker._write_text_atomically",
                    side_effect=PermissionError(5, "Access is denied"),
                ):
                    await asyncio.wait_for(
                        asyncio.gather(worker.run_forever(stop_event), stop_soon()),
                        timeout=2,
                    )

                self.assertIsNotNone(worker._sidecar_error)  # noqa: SLF001
                self.assertIsNotNone(store.get_project("sidecar-project"))
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
