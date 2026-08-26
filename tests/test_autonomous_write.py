from __future__ import annotations

import asyncio
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT / "src"))

from chatgpt_codex_bridge.core import BridgeCore  # noqa: E402
from chatgpt_codex_bridge.domain.models import (  # noqa: E402
    ExecutionStatus,
    TaskMode,
    TaskStateError,
)
from chatgpt_codex_bridge.executors.base import ExecutionResult  # noqa: E402
from chatgpt_codex_bridge.executors.codex_app_server import (  # noqa: E402
    CodexAppServerClient,
)
from chatgpt_codex_bridge.mcp_adapter import MCPAdapter  # noqa: E402
from chatgpt_codex_bridge.persistence.sqlite_store import SQLiteBridgeStore  # noqa: E402
from chatgpt_codex_bridge.policy import (  # noqa: E402
    DirtyWorkingTreeError,
    GitPostflightError,
    GitPreflightError,
    PolicyViolationError,
    ProtectedRootError,
)


def git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()


def make_git_repo(parent: Path) -> Path:
    repo = parent / "workspace"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.name", "Bridge Test")
    git(repo, "config", "user.email", "bridge-test@example.invalid")
    git(repo, "branch", "-M", "main")
    (repo / "tracked.txt").write_text("initial\n", encoding="utf-8")
    git(repo, "add", "tracked.txt")
    git(repo, "commit", "-m", "initial")
    return repo


class ControlledExecutor:
    def __init__(self, mutation=None) -> None:
        self.mutation = mutation
        self.requests = []

    async def run(self, request, *, on_correlation=None, on_notification=None):
        self.requests.append(request)
        if self.mutation is not None:
            self.mutation(Path(request.cwd))
        return ExecutionResult(
            thread_id="thread-policy",
            turn_id="turn-policy",
            status=ExecutionStatus.FINISHED,
            final_response="POLICY_TEST_OK",
        )


class AutonomousWriteCoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.store = SQLiteBridgeStore(self.root / "bridge.sqlite3")

    def tearDown(self) -> None:
        self.store.close()
        self.tempdir.cleanup()

    def _task(self, executor, repo: Path, *, mode=TaskMode.AUTONOMOUS_WRITE):
        core = BridgeCore(self.store, executor)
        core.create_project("Project", str(repo), project_id="project-policy")
        return core, core.create_task(
            "project-policy",
            "perform the requested change",
            task_id="task-policy",
            mode=mode,
        )

    def _assert_preflight_failed(self, task) -> None:
        updated = self.store.get_task(task.task_id)
        self.assertIsNotNone(updated)
        assert updated is not None
        self.assertEqual(updated.execution_status, ExecutionStatus.FAILED)
        events = self.store.list_task_events(task.task_id)
        self.assertEqual(
            [event.kind for event in events],
            ["task.created", "policy.violation", "task.failed"],
        )
        self.assertEqual(events[1].payload["phase"], "preflight")
        self.assertEqual(
            [event.kind for event in events if event.kind == "task.failed"],
            ["task.failed"],
        )

    def test_read_only_default_preserves_existing_behavior(self) -> None:
        executor = ControlledExecutor()
        repo = self.root / "not-a-git-repo"
        repo.mkdir()
        core, task = self._task(executor, repo, mode=TaskMode.READ_ONLY)

        result = asyncio.run(core.run_task(task.task_id))

        self.assertEqual(result.execution_status, ExecutionStatus.FINISHED)
        self.assertEqual(executor.requests[0].mode, TaskMode.READ_ONLY)
        self.assertEqual(executor.requests[0].objective, task.objective)
        self.assertFalse(
            any(event.kind.startswith("policy.") for event in self.store.list_task_events(task.task_id))
        )

    async def test_autonomous_mode_uses_contract_and_durable_checkpoint(self) -> None:
        repo = make_git_repo(self.root)
        executor = ControlledExecutor()
        core, task = self._task(executor, repo)

        result = await core.run_task(task.task_id)

        self.assertEqual(result.execution_status, ExecutionStatus.FINISHED)
        request = executor.requests[0]
        self.assertEqual(request.mode, TaskMode.AUTONOMOUS_WRITE)
        self.assertIn("Work only on the requested project.", request.objective)
        self.assertIn("NO commit, NO push, NO tag/release.", request.objective)
        self.assertIn("No destructive operations that were not requested.", request.objective)
        events = self.store.list_task_events(task.task_id)
        checkpoint = next(event for event in events if event.kind == "policy.git_checkpoint")
        postflight = next(event for event in events if event.kind == "policy.postflight")
        self.assertEqual(checkpoint.payload["baseline_branch"], "main")
        self.assertEqual(checkpoint.payload["baseline_head"], git(repo, "rev-parse", "HEAD"))
        self.assertFalse(postflight.payload["policy_violation"])

    async def test_non_git_repo_is_rejected_before_executor(self) -> None:
        repo = self.root / "not-a-git-repo"
        repo.mkdir()
        executor = ControlledExecutor()
        core, task = self._task(executor, repo)

        with self.assertRaises(GitPreflightError) as context:
            await core.run_task(task.task_id)

        self.assertIsInstance(context.exception, GitPreflightError)
        self.assertEqual(executor.requests, [])
        self._assert_preflight_failed(task)

    async def test_dirty_worktree_is_rejected_before_executor(self) -> None:
        repo = make_git_repo(self.root)
        (repo / "tracked.txt").write_text("dirty\n", encoding="utf-8")
        executor = ControlledExecutor()
        core, task = self._task(executor, repo)

        with self.assertRaises(DirtyWorkingTreeError):
            await core.run_task(task.task_id)

        self.assertEqual(executor.requests, [])
        self._assert_preflight_failed(task)

    async def test_protected_bridge_repo_is_rejected_before_executor(self) -> None:
        executor = ControlledExecutor()
        core, task = self._task(executor, ROOT)

        with self.assertRaises(ProtectedRootError):
            await core.run_task(task.task_id)

        self.assertEqual(executor.requests, [])
        self._assert_preflight_failed(task)

    async def test_preflight_failure_is_terminal_and_cannot_be_rerun(self) -> None:
        repo = self.root / "not-a-git-repo"
        repo.mkdir()
        executor = ControlledExecutor()
        core, task = self._task(executor, repo)

        with self.assertRaises(GitPreflightError):
            await core.run_task(task.task_id)
        events_before = self.store.list_task_events(task.task_id)

        with self.assertRaises(TaskStateError):
            await core.run_task(task.task_id)

        self.assertEqual(executor.requests, [])
        self.assertEqual(self.store.list_task_events(task.task_id), events_before)
        self._assert_preflight_failed(task)

    async def test_local_appdata_bridge_roots_and_overlaps_are_rejected(self) -> None:
        local_app_data = self.root / "LocalAppData"
        local_app_data.mkdir()
        for index, name in enumerate(
            (
            "ChatGPTCodexBridge",
            "ChatGPTOpenCodeBridge",
            "VisorVideosDevBridge",
            )
        ):
            protected = local_app_data / name
            protected.mkdir()
            executor = ControlledExecutor()
            project_id = f"project-protected-{index}"
            task_id = f"task-protected-{index}"
            core = BridgeCore(self.store, executor)
            core.create_project("Project", str(protected), project_id=project_id)
            core.create_task(
                project_id, "write", task_id=task_id, mode=TaskMode.AUTONOMOUS_WRITE
            )

            with patch.dict("os.environ", {"LOCALAPPDATA": str(local_app_data)}):
                with self.assertRaises(ProtectedRootError):
                    await core.run_task(task_id)

            self.assertEqual(executor.requests, [])
            self.assertEqual(
                self.store.get_task(task_id).execution_status, ExecutionStatus.FAILED
            )

            descendant = protected / "descendant"
            descendant.mkdir()
            executor = ControlledExecutor()
            descendant_project_id = f"project-descendant-{index}"
            descendant_task_id = f"task-descendant-{index}"
            descendant_core = BridgeCore(self.store, executor)
            descendant_core.create_project(
                "Project", str(descendant), project_id=descendant_project_id
            )
            descendant_core.create_task(
                descendant_project_id,
                "write",
                task_id=descendant_task_id,
                mode=TaskMode.AUTONOMOUS_WRITE,
            )
            with patch.dict("os.environ", {"LOCALAPPDATA": str(local_app_data)}):
                with self.assertRaises(ProtectedRootError):
                    await descendant_core.run_task(descendant_task_id)
            self.assertEqual(executor.requests, [])
            self.assertEqual(
                self.store.get_task(descendant_task_id).execution_status,
                ExecutionStatus.FAILED,
            )

    async def test_clean_repo_checkpoint_records_branch_and_head(self) -> None:
        repo = make_git_repo(self.root)
        baseline_head = git(repo, "rev-parse", "HEAD")
        executor = ControlledExecutor()
        core, task = self._task(executor, repo)

        await core.run_task(task.task_id)

        checkpoint = next(
            event
            for event in self.store.list_task_events(task.task_id)
            if event.kind == "policy.git_checkpoint"
        )
        self.assertEqual(
            {
                checkpoint.payload["baseline_branch"],
                checkpoint.payload["baseline_head"],
            },
            {"main", baseline_head},
        )

    async def test_tracked_change_is_permitted_when_head_and_branch_are_stable(self) -> None:
        repo = make_git_repo(self.root)
        executor = ControlledExecutor(
            mutation=lambda path: (path / "tracked.txt").write_text("changed\n", encoding="utf-8")
        )
        core, task = self._task(executor, repo)

        result = await core.run_task(task.task_id)

        self.assertEqual(result.execution_status, ExecutionStatus.FINISHED)
        postflight = next(
            event
            for event in self.store.list_task_events(task.task_id)
            if event.kind == "policy.postflight"
        )
        self.assertIn("tracked.txt", postflight.payload["changed_files"])
        self.assertFalse(postflight.payload["policy_violation"])

    async def test_untracked_change_is_permitted_when_head_and_branch_are_stable(self) -> None:
        repo = make_git_repo(self.root)
        executor = ControlledExecutor(
            mutation=lambda path: (path / "untracked.txt").write_text("new\n", encoding="utf-8")
        )
        core, task = self._task(executor, repo)

        result = await core.run_task(task.task_id)

        self.assertEqual(result.execution_status, ExecutionStatus.FINISHED)
        postflight = next(
            event
            for event in self.store.list_task_events(task.task_id)
            if event.kind == "policy.postflight"
        )
        self.assertEqual(postflight.payload["untracked_files"], ["untracked.txt"])
        self.assertFalse(postflight.payload["policy_violation"])

    async def test_head_change_is_a_policy_violation_and_is_not_rolled_back(self) -> None:
        repo = make_git_repo(self.root)

        def commit_change(path: Path) -> None:
            (path / "tracked.txt").write_text("committed\n", encoding="utf-8")
            git(path, "add", "tracked.txt")
            git(path, "commit", "-m", "unexpected commit")

        executor = ControlledExecutor(mutation=commit_change)
        core, task = self._task(executor, repo)

        with self.assertRaises(PolicyViolationError):
            await core.run_task(task.task_id)

        self.assertEqual(len(executor.requests), 1)
        self.assertEqual(self.store.get_task(task.task_id).execution_status, ExecutionStatus.FAILED)
        self.assertEqual(git(repo, "log", "-1", "--format=%s"), "unexpected commit")
        self.assertTrue(
            any(event.kind == "policy.violation" for event in self.store.list_task_events(task.task_id))
        )

    async def test_branch_change_is_a_policy_violation(self) -> None:
        repo = make_git_repo(self.root)
        executor = ControlledExecutor(mutation=lambda path: git(path, "checkout", "-b", "unexpected"))
        core, task = self._task(executor, repo)

        with self.assertRaises(PolicyViolationError):
            await core.run_task(task.task_id)

        self.assertEqual(len(executor.requests), 1)
        self.assertEqual(self.store.get_task(task.task_id).execution_status, ExecutionStatus.FAILED)

    async def test_postflight_error_does_not_rerun_executor(self) -> None:
        repo = make_git_repo(self.root)
        executor = ControlledExecutor()
        core, task = self._task(executor, repo)

        with patch("chatgpt_codex_bridge.core.git_postflight", side_effect=RuntimeError("postflight unavailable")):
            with self.assertRaises(GitPostflightError):
                await core.run_task(task.task_id)

        self.assertEqual(len(executor.requests), 1)
        self.assertEqual(self.store.get_task(task.task_id).execution_status, ExecutionStatus.FAILED)

    async def test_result_exposes_durable_autonomous_git_evidence(self) -> None:
        repo = make_git_repo(self.root)
        executor = ControlledExecutor(
            mutation=lambda path: (path / "untracked.txt").write_text("new\n", encoding="utf-8")
        )
        core, task = self._task(executor, repo)
        await core.run_task(task.task_id)

        result = MCPAdapter(core, self.store)._result_for_task(self.store.get_task(task.task_id))

        self.assertEqual(result["mode"], "AUTONOMOUS_WRITE")
        self.assertEqual(result["baseline_branch"], "main")
        self.assertEqual(result["final_branch"], "main")
        self.assertEqual(result["untracked_files"], ["untracked.txt"])
        self.assertFalse(result["policy_violation"])

    def test_orphaned_autonomous_task_attempts_postflight_before_recovery(self) -> None:
        repo = make_git_repo(self.root)
        executor = ControlledExecutor()
        core, task = self._task(executor, repo)
        checkpoint = core.store.append_task_event(
            task.task_id,
            "bridge",
            "policy.git_checkpoint",
            {
                "mode": "AUTONOMOUS_WRITE",
                "repo_path": str(repo.resolve()),
                "baseline_branch": "main",
                "baseline_head": git(repo, "rev-parse", "HEAD"),
                "status_porcelain": "",
                "staged_paths": [],
                "unstaged_paths": [],
                "untracked_paths": [],
            },
        )
        self.assertEqual(checkpoint.kind, "policy.git_checkpoint")
        self.store.transition_task_running(task.task_id, project_id="project-policy")

        recovered = core.recover_orphaned_tasks()

        self.assertEqual(recovered[0].execution_status, ExecutionStatus.FAILED)
        self.assertTrue(
            any(
                event.kind == "policy.postflight"
                for event in self.store.list_task_events(task.task_id)
            )
        )

    async def test_mode_is_persisted_and_adapter_accepts_optional_mode(self) -> None:
        repo = make_git_repo(self.root)
        core = BridgeCore(self.store, ControlledExecutor())
        core.create_project("Project", str(repo), project_id="project-mode")
        adapter = MCPAdapter(core, self.store)

        created = await adapter.call_tool(
            "create_task",
            {
                "project_id": "project-mode",
                "objective": "write",
                "task_id": "task-mode",
                "mode": "AUTONOMOUS_WRITE",
            },
        )

        self.assertEqual(created["mode"], "AUTONOMOUS_WRITE")
        self.assertEqual(self.store.get_task("task-mode").mode, TaskMode.AUTONOMOUS_WRITE)


class AppServerModeTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_only_and_autonomous_write_policies_are_exact(self) -> None:
        client = CodexAppServerClient("codex", ROOT)
        seen: list[tuple[str, dict[str, object]]] = []

        async def fake_request(method: str, params: dict[str, object]):
            seen.append((method, params))
            if method == "thread/start":
                return {"result": {"thread": {"id": "thread-policy"}}}
            if method == "turn/start":
                return {"result": {"turn": {"id": "turn-policy"}}}
            return {"result": {}}

        async def fake_wait(thread_id: str, turn_id: str):
            return {
                "params": {
                    "threadId": thread_id,
                    "turn": {"id": turn_id, "status": "completed"},
                }
            }

        client.request = fake_request  # type: ignore[method-assign]
        client.wait_for_turn_completed = fake_wait  # type: ignore[method-assign]

        await client.thread_start(model="gpt-5.6-luna", cwd=ROOT)
        await client.turn_start(
            thread_id="thread-policy",
            cwd=ROOT,
            model="gpt-5.6-luna",
            prompt="read",
        )
        await client.thread_start(
            model="gpt-5.6-luna",
            cwd=ROOT,
            mode=TaskMode.AUTONOMOUS_WRITE,
        )
        await client.turn_start(
            thread_id="thread-policy",
            cwd=ROOT,
            model="gpt-5.6-luna",
            prompt="write",
            mode=TaskMode.AUTONOMOUS_WRITE,
        )

        readonly_thread = seen[0][1]
        readonly_turn = seen[1][1]
        autonomous_thread = seen[2][1]
        autonomous_turn = seen[3][1]
        self.assertEqual(
            {
                readonly_thread["approvalPolicy"],
                readonly_thread["sandbox"],
                readonly_turn["approvalPolicy"],
                readonly_turn["sandboxPolicy"]["type"],
            },
            {"on-request", "read-only", "readOnly"},
        )
        self.assertFalse(readonly_turn["sandboxPolicy"]["networkAccess"])
        self.assertEqual(autonomous_thread["approvalPolicy"], "never")
        self.assertEqual(autonomous_thread["sandbox"], "danger-full-access")
        self.assertEqual(autonomous_turn["approvalPolicy"], "never")
        self.assertEqual(autonomous_turn["sandboxPolicy"], {"type": "dangerFullAccess"})


if __name__ == "__main__":
    unittest.main()
