from __future__ import annotations

import asyncio
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from chatgpt_codex_bridge.core import BridgeCore  # noqa: E402
from chatgpt_codex_bridge.domain.models import ExecutionStatus, TaskMode  # noqa: E402
from chatgpt_codex_bridge.executors.base import ExecutionResult  # noqa: E402
from chatgpt_codex_bridge.persistence.sqlite_store import SQLiteBridgeStore  # noqa: E402
from chatgpt_codex_bridge.policy import PolicyError  # noqa: E402
from chatgpt_codex_bridge.repository_access import (  # noqa: E402
    RepositoryBusyError,
    RepositoryLock,
    WRITER_INSTANCE_CONFIG_KEY,
)


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout.strip()


def make_repo(parent: Path, name: str, *, owner: str | None = "principal") -> Path:
    repo = parent / name
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.name", "Bridge Writer Test")
    git(repo, "config", "user.email", "writer-test@example.invalid")
    if owner is not None:
        git(repo, "config", "--local", WRITER_INSTANCE_CONFIG_KEY, owner)
    (repo / "tracked.txt").write_bytes(b"original\n")
    git(repo, "add", "tracked.txt")
    git(repo, "commit", "-m", "initial")
    return repo


def file_snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


class TestExecutor:
    def __init__(self, *, mutation=None, block: bool = False) -> None:
        self.mutation = mutation
        self.block = block
        self.requests = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def run(self, request, *, on_correlation=None, on_notification=None):
        self.requests.append(request)
        self.started.set()
        if self.block:
            await self.release.wait()
        if self.mutation is not None:
            self.mutation(Path(request.cwd))
        return ExecutionResult(
            thread_id="thread-writer-test",
            turn_id="turn-writer-test",
            status=ExecutionStatus.FINISHED,
            final_response="WRITER_TEST_OK",
        )


class RepositoryWriterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.lock_dir = self.root / "locks"
        self.stores: list[SQLiteBridgeStore] = []

    def tearDown(self) -> None:
        for store in self.stores:
            store.close()
        self.tempdir.cleanup()

    def new_core(
        self,
        name: str,
        instance_id: str | None,
        executor: TestExecutor | None = None,
        *,
        db_name: str | None = None,
    ) -> BridgeCore:
        store = SQLiteBridgeStore(self.root / (db_name or f"{name}.sqlite3"))
        self.stores.append(store)
        return BridgeCore(
            store,
            executor,
            instance_id=instance_id,
            repository_lock_directory=str(self.lock_dir),
        )

    @staticmethod
    def add_task(
        core: BridgeCore,
        repo: Path,
        *,
        project_id: str,
        task_id: str,
        mode: TaskMode = TaskMode.AUTONOMOUS_WRITE,
    ):
        core.create_project("Test project", str(repo), project_id=project_id)
        return core.create_task(
            project_id,
            "perform the disposable writer test",
            task_id=task_id,
            mode=mode,
        )

    async def test_non_owner_is_rejected_without_repository_changes(self) -> None:
        repo = make_repo(self.root, "repo")
        before = file_snapshot(repo)
        executor = TestExecutor(mutation=lambda path: (path / "bad.txt").write_text("bad"))
        core = self.new_core("secondary", "secondary", executor)
        task = self.add_task(
            core, repo, project_id="project-secondary", task_id="task-request"
        )

        with self.assertRaises(PolicyError):
            core.request_execution(task.task_id)
        self.assertEqual(core.store.get_task(task.task_id).execution_status, ExecutionStatus.QUEUED)

        direct_task = self.add_task(
            core,
            repo,
            project_id="project-secondary-direct",
            task_id="task-direct",
        )
        with self.assertRaises(PolicyError):
            await core.run_task(direct_task.task_id)

        self.assertEqual(executor.requests, [])
        self.assertEqual(file_snapshot(repo), before)
        self.assertFalse((repo / ".git" / "index.lock").exists())
        failed = core.store.get_task(direct_task.task_id)
        self.assertEqual(failed.execution_status, ExecutionStatus.FAILED)

    async def test_missing_identity_missing_owner_and_ambiguous_owner_fail_closed(self) -> None:
        repo = make_repo(self.root, "repo")
        cases = [
            ("missing-identity", None, "principal"),
            ("missing-owner", "principal", None),
        ]
        for name, identity, owner in cases:
            case_repo = make_repo(
                self.root,
                name,
                owner=owner,
            )
            core = self.new_core(name, identity)
            task = self.add_task(
                core,
                case_repo,
                project_id=f"project-{name}",
                task_id=f"task-{name}",
            )
            with self.assertRaises(PolicyError):
                core.request_execution(task.task_id)
            self.assertEqual(
                core.store.get_task(task.task_id).execution_status,
                ExecutionStatus.QUEUED,
            )

        git(repo, "config", "--local", "--add", WRITER_INSTANCE_CONFIG_KEY, "secondary")
        ambiguous = self.new_core("ambiguous", "principal")
        task = self.add_task(
            ambiguous,
            repo,
            project_id="project-ambiguous",
            task_id="task-ambiguous",
        )
        with self.assertRaises(PolicyError):
            ambiguous.request_execution(task.task_id)

    async def test_read_only_remains_available_to_non_owner(self) -> None:
        repo = make_repo(self.root, "repo")
        executor = TestExecutor()
        core = self.new_core("secondary", "secondary", executor)
        task = self.add_task(
            core,
            repo,
            project_id="project-read-only",
            task_id="task-read-only",
            mode=TaskMode.READ_ONLY,
        )
        with patch(
            "chatgpt_codex_bridge.core.preflight_read_only_git_index",
            return_value=type("IndexResult", (), {"outcome": "noop", "reason": "ok"})(),
        ):
            result = await core.run_task(task.task_id)
        self.assertEqual(result.execution_status, ExecutionStatus.FINISHED)
        self.assertEqual(executor.requests[0].mode, TaskMode.READ_ONLY)

    async def test_lock_excludes_same_repository_and_does_not_cross_lock_other_repos(self) -> None:
        first_repo = make_repo(self.root, "first")
        other_repo = make_repo(self.root, "other")
        nested_project_path = first_repo / "nested-project"
        nested_project_path.mkdir()
        first = RepositoryLock(first_repo, lock_directory=self.lock_dir).acquire()
        try:
            self.assertNotEqual(first.lock_path, first_repo / ".git" / "index.lock")
            with self.assertRaises(RepositoryBusyError):
                RepositoryLock(first_repo, lock_directory=self.lock_dir).acquire()
            with self.assertRaises(RepositoryBusyError):
                RepositoryLock(
                    nested_project_path,
                    lock_directory=self.lock_dir,
                ).acquire()
            other = RepositoryLock(other_repo, lock_directory=self.lock_dir).acquire()
            other.release()
        finally:
            first.release()
        with RepositoryLock(first_repo, lock_directory=self.lock_dir):
            pass

    async def test_lock_recovers_after_owner_process_dies(self) -> None:
        repo = make_repo(self.root, "repo")
        code = (
            "import sys,time; "
            "from chatgpt_codex_bridge.repository_access import RepositoryLock; "
            "lock=RepositoryLock(sys.argv[1], lock_directory=sys.argv[2]); "
            "lock.acquire(); print('LOCKED', flush=True); time.sleep(60)"
        )
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
        child = subprocess.Popen(
            [sys.executable, "-c", code, str(repo), str(self.lock_dir)],
            cwd=str(ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        try:
            assert child.stdout is not None
            self.assertEqual(child.stdout.readline().strip(), "LOCKED")
            with self.assertRaises(RepositoryBusyError):
                RepositoryLock(repo, lock_directory=self.lock_dir).acquire()
        finally:
            child.terminate()
            child.wait(timeout=10)
            if child.stdout is not None:
                child.stdout.close()

        with RepositoryLock(repo, lock_directory=self.lock_dir):
            pass

    async def test_lock_spans_executor_and_checkpoint_commit(self) -> None:
        repo = make_repo(self.root, "repo")
        executor = TestExecutor(
            block=True,
            mutation=lambda path: (path / "tracked.txt").write_bytes(b"changed\n"),
        )
        core = self.new_core("principal", "principal", executor)
        task = self.add_task(
            core,
            repo,
            project_id="project-owner",
            task_id="task-owner",
        )
        lock_state: dict[str, bool] = {}

        def is_locked() -> bool:
            try:
                with RepositoryLock(repo, lock_directory=self.lock_dir):
                    return False
            except RepositoryBusyError:
                return True

        original_preflight = core._preflight_checkpoint

        def check_preflight_lock(*args, **kwargs):
            lock_state["preflight"] = is_locked()
            return original_preflight(*args, **kwargs)

        original_postflight = core._persist_postflight

        def check_postflight_lock(*args, **kwargs):
            lock_state["postflight"] = is_locked()
            return original_postflight(*args, **kwargs)

        core._preflight_checkpoint = check_preflight_lock
        core._persist_postflight = check_postflight_lock
        running = asyncio.create_task(core.run_task(task.task_id))
        await asyncio.wait_for(executor.started.wait(), timeout=3)

        second_executor = TestExecutor()
        second = self.new_core("second-db", "principal", second_executor)
        second_task = self.add_task(
            second,
            repo,
            project_id="project-second",
            task_id="task-second",
        )
        with self.assertRaises(PolicyError):
            await second.run_task(second_task.task_id)
        self.assertEqual(second_executor.requests, [])

        executor.release.set()
        finished = await asyncio.wait_for(running, timeout=5)
        self.assertEqual(finished.execution_status, ExecutionStatus.FINISHED)
        self.assertEqual(lock_state, {"preflight": True, "postflight": True})
        with RepositoryLock(repo, lock_directory=self.lock_dir):
            pass

        head_before = git(repo, "rev-parse", "HEAD")
        secondary = BridgeCore(
            core.store,
            instance_id="secondary",
            repository_lock_directory=str(self.lock_dir),
        )
        with self.assertRaises(PolicyError):
            secondary.commit_checkpoint(task.task_id, "must not commit")
        self.assertEqual(git(repo, "rev-parse", "HEAD"), head_before)

        held = RepositoryLock(repo, lock_directory=self.lock_dir).acquire()
        try:
            with self.assertRaises(PolicyError):
                core.commit_checkpoint(task.task_id, "blocked while locked")
            self.assertEqual(git(repo, "rev-parse", "HEAD"), head_before)
        finally:
            held.release()
        committed = core.commit_checkpoint(task.task_id, "checkpoint after release")
        self.assertTrue(committed["commit_created"])


if __name__ == "__main__":
    unittest.main()
