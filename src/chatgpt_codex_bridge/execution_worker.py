"""Persistent, single-owner execution worker for the Bridge database."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import json
import os
import sys
import uuid
from dataclasses import dataclass
from typing import Any

from .core import BridgeCore
from .executors.codex_executor import CodexExecutor
from .domain.models import (
    ExecutionStatus,
    Task,
    TaskStateError,
    timestamp_to_text,
    utc_now,
)
from .persistence.sqlite_store import SQLiteBridgeStore
from .single_instance import (
    ExecutionWorkerAlreadyRunningError,
    ExecutionWorkerLock,
    canonical_db_path,
)


DEFAULT_POLL_INTERVAL = 0.25
DEFAULT_CANCEL_TIMEOUT = 5.0
WORKER_PID_SUFFIX = ".execution-worker.pid"
WORKER_STATE_SUFFIX = ".execution-worker.state.json"
WORKER_STOP_SUFFIX = ".execution-worker.stop"


@dataclass(frozen=True)
class WorkerRuntimePaths:
    """Runtime sidecars owned by one execution worker and one database."""

    db_path: Path
    pid_path: Path
    state_path: Path
    stop_path: Path


def worker_runtime_paths(db_path: str | os.PathLike[str]) -> WorkerRuntimePaths:
    """Return deterministic, database-scoped worker control paths."""

    canonical = canonical_db_path(db_path)
    return WorkerRuntimePaths(
        db_path=canonical,
        pid_path=Path(f"{canonical}{WORKER_PID_SUFFIX}"),
        state_path=Path(f"{canonical}{WORKER_STATE_SUFFIX}"),
        stop_path=Path(f"{canonical}{WORKER_STOP_SUFFIX}"),
    )


def _write_text_atomically(path: Path, value: str) -> None:
    """Write one bounded control/state file without exposing a partial value."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(value, encoding="utf-8")
    os.replace(temporary, path)


def read_worker_state(db_path: str | os.PathLike[str]) -> dict[str, Any] | None:
    """Read bounded worker state without opening or mutating the database."""

    state_path = worker_runtime_paths(db_path).state_path
    try:
        raw = state_path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return None
    if len(raw) > 16_384:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(value, dict):
        return None
    return value


class ExecutionWorker:
    """Own and execute one durable D3-R2 request at a time."""

    def __init__(
        self,
        store: SQLiteBridgeStore,
        core: BridgeCore,
        *,
        worker_id: str | None = None,
        pid: int | None = None,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        cancel_timeout: float = DEFAULT_CANCEL_TIMEOUT,
        runtime_paths: WorkerRuntimePaths | None = None,
    ) -> None:
        if not isinstance(poll_interval, (int, float)) or poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        if not isinstance(cancel_timeout, (int, float)) or cancel_timeout <= 0:
            raise ValueError("cancel_timeout must be positive")
        self.store = store
        self.core = core
        self.worker_id = worker_id or str(uuid.uuid4())
        self.pid = os.getpid() if pid is None else pid
        self.poll_interval = float(poll_interval)
        self.cancel_timeout = float(cancel_timeout)
        self.runtime_paths = runtime_paths
        self._active_task_id: str | None = None
        self._stop_requested = False
        self._last_error: str | None = None
        if runtime_paths is not None:
            _write_text_atomically(runtime_paths.pid_path, f"{self.pid}\n")
            self._write_state("starting")

    @property
    def owner_payload(self) -> dict[str, object]:
        return {
            "owner_kind": "persistent_worker",
            "owner_id": self.worker_id,
            "pid": self.pid,
            "claimed_at": timestamp_to_text(utc_now()),
        }

    @property
    def active_task_id(self) -> str | None:
        """Return the task currently owned by this worker, if any."""

        return self._active_task_id

    def _write_state(
        self,
        status: str,
        *,
        active_task_id: str | None = None,
        error: BaseException | str | None = None,
    ) -> None:
        paths = self.runtime_paths
        if paths is None:
            return
        self._last_error = None if error is None else str(error)[:500]
        payload: dict[str, Any] = {
            "pid": self.pid,
            "db_path": str(paths.db_path),
            "worker_id": self.worker_id,
            "owner_kind": "persistent_worker",
            "status": status,
            "active_task_id": active_task_id,
            "last_error": self._last_error,
            "updated_at": timestamp_to_text(utc_now()),
        }
        _write_text_atomically(
            paths.state_path,
            json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
        )

    def _stop_file_exists(self) -> bool:
        return self.runtime_paths is not None and self.runtime_paths.stop_path.exists()

    def _stop_requested_now(self, stop_event: asyncio.Event) -> bool:
        return stop_event.is_set() or self._stop_file_exists()

    def _ensure_cancelled(self, task_id: str, *, reason: str) -> None:
        """Persist cancellation if cancellation happened before Core could do so."""

        task = self.store.get_task(task_id)
        if task is None or task.execution_status is not ExecutionStatus.RUNNING:
            return
        try:
            self.store.transition_task_terminal(
                task_id,
                execution_status=ExecutionStatus.CANCELLED,
                event_kind="task.cancelled",
                payload={"reason": reason},
            )
        except TaskStateError:
            # Core may have persisted the terminal transition concurrently.
            pass

    def claim_next(self) -> Task | None:
        """Atomically claim the oldest explicit request, if one is ready."""

        task = self.store.find_next_requested_task()
        if task is None:
            return None
        self._write_state("claiming", active_task_id=task.task_id)
        owner = self.owner_payload
        try:
            claimed, _ = self.store.claim_task_execution(task.task_id, owner)
        except TaskStateError:
            # Another process cannot normally race while this worker lock is
            # held, but a stale/externally repaired database must not stop the
            # polling loop.
            self._write_state("idle")
            return None
        self._active_task_id = claimed.task_id
        self._write_state("running", active_task_id=claimed.task_id)
        return claimed

    async def run_once(self) -> Task | None:
        """Claim and execute at most one task; return its durable final state."""

        claimed = self.claim_next()
        if claimed is None:
            self._write_state("idle")
            return None
        try:
            await self.core.execute_claimed_task(claimed.task_id)
            return self.store.get_task(claimed.task_id)
        except asyncio.CancelledError:
            self._ensure_cancelled(
                claimed.task_id,
                reason="execution worker stop requested",
            )
            raise
        except Exception as error:
            self._write_state("error", active_task_id=claimed.task_id, error=error)
            raise
        finally:
            self._active_task_id = None
            if not self._stop_requested:
                self._write_state("idle")

    async def _cancel_execution_task(self, execution_task: asyncio.Task[Any]) -> None:
        """Interrupt the owned executor, then cancel and close the run task."""

        task_id = self._active_task_id
        self._stop_requested = True
        self._write_state("stopping", active_task_id=task_id)
        try:
            await asyncio.wait_for(
                self.core._cancel_active_execution(),  # noqa: SLF001
                timeout=self.cancel_timeout,
            )
        except BaseException:
            # Cancellation remains bounded even if the app-server interrupt is
            # unavailable or does not answer.
            pass
        execution_task.cancel()
        try:
            await execution_task
        except asyncio.CancelledError:
            pass
        except Exception as error:
            self._write_state("error", active_task_id=task_id, error=error)
        if task_id is not None:
            self._ensure_cancelled(
                task_id,
                reason="execution worker stop requested",
            )

    async def run_forever(
        self,
        stop_event: asyncio.Event | None = None,
        *,
        stop_path: str | os.PathLike[str] | None = None,
    ) -> None:
        """Poll until stopped, keeping the worker alive across task failures."""

        event = stop_event or asyncio.Event()
        if stop_path is not None:
            if self.runtime_paths is None:
                self.runtime_paths = worker_runtime_paths(self.store.db_path)
            self.runtime_paths = WorkerRuntimePaths(
                db_path=self.runtime_paths.db_path,
                pid_path=self.runtime_paths.pid_path,
                state_path=self.runtime_paths.state_path,
                stop_path=Path(stop_path),
            )
        self._stop_requested = False
        self._write_state("idle")
        execution_task: asyncio.Task[Any] | None = None
        try:
            while not self._stop_requested_now(event):
                execution_task = asyncio.create_task(self.run_once())
                while not execution_task.done():
                    if self._stop_requested_now(event):
                        await self._cancel_execution_task(execution_task)
                        break
                    await asyncio.sleep(min(self.poll_interval, 0.1))
                if not execution_task.done():
                    continue
                try:
                    await execution_task
                except asyncio.CancelledError:
                    if not self._stop_requested:
                        raise
                except Exception as error:
                    # Core has already persisted a terminal failure for
                    # execution errors. The owner remains available for the
                    # next request.
                    self._write_state("error", error=error)
                execution_task = None
                if self._stop_requested_now(event):
                    break
                try:
                    await asyncio.wait_for(event.wait(), timeout=self.poll_interval)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            self._stop_requested = True
            if execution_task is not None and not execution_task.done():
                await self._cancel_execution_task(execution_task)
            raise
        finally:
            self._write_state("stopped")

    def close_runtime_state(self, status: str = "stopped", error: BaseException | str | None = None) -> None:
        """Publish final state and remove only this worker's PID sidecar."""

        paths = self.runtime_paths
        if paths is None:
            return
        self._write_state(status, error=error)
        try:
            if paths.pid_path.read_text(encoding="utf-8").strip() == str(self.pid):
                paths.pid_path.unlink(missing_ok=True)
        except (FileNotFoundError, OSError):
            pass
        try:
            paths.stop_path.unlink(missing_ok=True)
        except OSError:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", type=Path, required=True)
    parser.add_argument("--executable")
    args = parser.parse_args(argv)
    db_path = canonical_db_path(args.db_path)
    paths = worker_runtime_paths(db_path)
    worker: ExecutionWorker | None = None
    store: SQLiteBridgeStore | None = None
    try:
        with ExecutionWorkerLock(db_path):
            paths.stop_path.unlink(missing_ok=True)
            store = SQLiteBridgeStore(db_path)
            try:
                executor = CodexExecutor(executable=args.executable)
                core = BridgeCore(store, executor)
                worker = ExecutionWorker(store, core, runtime_paths=paths)
                recovered = core.recover_orphaned_tasks()
                print(
                    f"chatgpt-codex-bridge execution worker pid={os.getpid()} "
                    f"recovered={len(recovered)}",
                    file=sys.stderr,
                    flush=True,
                )
                asyncio.run(worker.run_forever(stop_path=paths.stop_path))
                return 0
            except KeyboardInterrupt:
                if worker is not None:
                    worker.close_runtime_state()
                return 0
            except Exception as error:
                if worker is not None:
                    worker.close_runtime_state("failed", error)
                raise
            finally:
                if store is not None:
                    store.close()
    except ExecutionWorkerAlreadyRunningError as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        return 2
    except KeyboardInterrupt:
        if worker is not None:
            worker.close_runtime_state()
        return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DEFAULT_CANCEL_TIMEOUT",
    "DEFAULT_POLL_INTERVAL",
    "ExecutionWorker",
    "WorkerRuntimePaths",
    "main",
    "read_worker_state",
    "worker_runtime_paths",
]
