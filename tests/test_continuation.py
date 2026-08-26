from __future__ import annotations

import asyncio
import json
from pathlib import Path
import subprocess
import tempfile
import unittest


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
from chatgpt_codex_bridge.persistence.sqlite_store import SQLiteBridgeStore  # noqa: E402
from chatgpt_codex_bridge.policy import (  # noqa: E402
    ContinuationBaselineError,
    DirtyWorkingTreeError,
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


def make_repo(parent: Path, name: str = "workspace") -> Path:
    repo = parent / name
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.name", "Bridge Continuation Test")
    git(repo, "config", "user.email", "continuation-test@example.invalid")
    git(repo, "branch", "-M", "main")
    (repo / "app.txt").write_text("ORIGINAL\n", encoding="utf-8")
    (repo / "keep.txt").write_text("KEEP\n", encoding="utf-8")
    (repo / "binary.bin").write_bytes(b"A\x00BASELINE\n")
    git(repo, "add", "app.txt", "keep.txt", "binary.bin")
    git(repo, "commit", "-m", "initial")
    return repo


class SequenceExecutor:
    def __init__(self, *mutations) -> None:
        self.mutations = list(mutations)
        self.requests = []

    async def run(self, request, *, on_correlation=None, on_notification=None):
        self.requests.append(request)
        index = len(self.requests) - 1
        if index < len(self.mutations) and self.mutations[index] is not None:
            self.mutations[index](Path(request.cwd))
        return ExecutionResult(
            thread_id=f"thread-{index + 1}",
            turn_id=f"turn-{index + 1}",
            status=ExecutionStatus.FINISHED,
            final_response=f"CONTINUATION_{index + 1}_OK",
        )


class ContinuationBaselineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.store = SQLiteBridgeStore(self.root / "bridge.sqlite3")

    def tearDown(self) -> None:
        self.store.close()
        self.tempdir.cleanup()

    def new_core(self, repo: Path, executor) -> BridgeCore:
        core = BridgeCore(self.store, executor)
        core.create_project("Continuation", str(repo), project_id="project-cont")
        return core

    def create_task(self, core: BridgeCore, task_id: str, mode=TaskMode.AUTONOMOUS_WRITE):
        return core.create_task(
            "project-cont",
            "perform the requested continuation",
            task_id=task_id,
            mode=mode,
        )

    async def seed_finished_auto(self, mutation, *, name: str = "workspace"):
        repo = make_repo(self.root, name)
        executor = SequenceExecutor(mutation)
        core = self.new_core(repo, executor)
        previous = self.create_task(core, "task-previous")
        result = await core.run_task(previous.task_id)
        self.assertEqual(result.execution_status, ExecutionStatus.FINISHED)
        current = self.create_task(core, "task-current")
        return repo, executor, core, previous, current

    def assert_preflight_terminal(self, task_id: str) -> None:
        task = self.store.get_task(task_id)
        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(task.execution_status, ExecutionStatus.FAILED)
        events = self.store.list_task_events(task_id)
        self.assertEqual(
            [event.kind for event in events],
            ["task.created", "policy.violation", "task.failed"],
        )
        self.assertEqual(events[1].payload["phase"], "preflight")

    async def assert_dirty_rejected(
        self, repo: Path, core: BridgeCore, executor, *, expected_requests: int = 0
    ):
        current = self.create_task(core, "task-current")
        with self.assertRaises(DirtyWorkingTreeError):
            await core.run_task(current.task_id)
        self.assertEqual(len(executor.requests), expected_requests)
        self.assert_preflight_terminal(current.task_id)
        return current

    async def test_clean_baseline_remains_allowed(self) -> None:
        repo = make_repo(self.root)
        executor = SequenceExecutor()
        core = self.new_core(repo, executor)
        task = self.create_task(core, "task-clean")

        result = await core.run_task(task.task_id)

        self.assertEqual(result.execution_status, ExecutionStatus.FINISHED)
        checkpoint = next(
            event
            for event in self.store.list_task_events(task.task_id)
            if event.kind == "policy.git_checkpoint"
        )
        self.assertEqual(checkpoint.payload["baseline_kind"], "clean")
        self.assertIsNone(checkpoint.payload["previous_task_id"])

    async def test_three_task_chain_accepts_exact_accumulated_state(self) -> None:
        def first(path: Path) -> None:
            (path / "app.txt").write_text("UPDATED_1\n", encoding="utf-8")
            (path / "first.untracked").write_text("ONE\n", encoding="utf-8")

        def second(path: Path) -> None:
            (path / "keep.txt").write_text("UPDATED_2\n", encoding="utf-8")

        def third(path: Path) -> None:
            (path / "third.untracked").write_text("THREE\n", encoding="utf-8")

        repo = make_repo(self.root)
        executor = SequenceExecutor(first, second, third)
        core = self.new_core(repo, executor)
        tasks = [self.create_task(core, f"task-{index}") for index in range(1, 4)]

        for task in tasks:
            result = await core.run_task(task.task_id)
            self.assertEqual(result.execution_status, ExecutionStatus.FINISHED)

        self.assertEqual(len(executor.requests), 3)
        for index, task in enumerate(tasks):
            checkpoint = next(
                event
                for event in self.store.list_task_events(task.task_id)
                if event.kind == "policy.git_checkpoint"
            )
            if index == 0:
                self.assertEqual(checkpoint.payload["baseline_kind"], "clean")
                self.assertIsNone(checkpoint.payload["previous_task_id"])
            else:
                self.assertEqual(checkpoint.payload["baseline_kind"], "continuation")
                self.assertEqual(
                    checkpoint.payload["previous_task_id"], tasks[index - 1].task_id
                )
            postflight = next(
                event
                for event in self.store.list_task_events(task.task_id)
                if event.kind == "policy.postflight"
            )
            self.assertFalse(postflight.payload["policy_violation"])
        self.assertEqual(
            git(repo, "status", "--porcelain").splitlines(),
            ["M app.txt", " M keep.txt", "?? first.untracked", "?? third.untracked"],
        )

    async def test_clean_baseline_wins_over_prior_dirty_task_after_external_restore(
        self,
    ) -> None:
        def mutate(path: Path) -> None:
            (path / "app.txt").write_text("DIRTY_PRIOR\n", encoding="utf-8")
            (path / "prior.untracked").write_text("DIRTY_PRIOR\n", encoding="utf-8")

        repo = make_repo(self.root)
        previous_executor = SequenceExecutor(mutate)
        core = self.new_core(repo, previous_executor)
        previous = self.create_task(core, "task-previous")
        previous_result = await core.run_task(previous.task_id)
        self.assertEqual(previous_result.execution_status, ExecutionStatus.FINISHED)
        self.assertNotEqual(git(repo, "status", "--porcelain"), "")

        git(repo, "reset", "--hard", "HEAD")
        git(repo, "clean", "-fd")
        self.assertEqual(git(repo, "status", "--porcelain"), "")

        current_executor = SequenceExecutor()
        current_core = BridgeCore(self.store, current_executor)
        current = self.create_task(current_core, "task-current")
        result = await current_core.run_task(current.task_id)

        self.assertEqual(result.execution_status, ExecutionStatus.FINISHED)
        self.assertEqual(len(current_executor.requests), 1)
        checkpoint = next(
            event
            for event in self.store.list_task_events(current.task_id)
            if event.kind == "policy.git_checkpoint"
        )
        self.assertEqual(checkpoint.payload["baseline_kind"], "clean")
        self.assertIsNone(checkpoint.payload["previous_task_id"])

    async def test_dirty_without_prior_task_is_rejected(self) -> None:
        repo = make_repo(self.root)
        (repo / "app.txt").write_text("EXTERNAL\n", encoding="utf-8")
        executor = SequenceExecutor()
        core = self.new_core(repo, executor)

        await self.assert_dirty_rejected(repo, core, executor)

    async def test_preflight_failure_is_terminal_and_not_retryable(self) -> None:
        repo = make_repo(self.root)
        (repo / "app.txt").write_text("EXTERNAL\n", encoding="utf-8")
        executor = SequenceExecutor()
        core = self.new_core(repo, executor)
        current = self.create_task(core, "task-current")

        with self.assertRaises(DirtyWorkingTreeError):
            await core.run_task(current.task_id)
        events_before = self.store.list_task_events(current.task_id)

        with self.assertRaises(TaskStateError):
            await core.run_task(current.task_id)

        self.assertEqual(executor.requests, [])
        self.assertEqual(self.store.list_task_events(current.task_id), events_before)
        self.assert_preflight_terminal(current.task_id)

    async def test_failed_prior_task_is_not_a_continuation_baseline(self) -> None:
        repo = make_repo(self.root)
        executor = SequenceExecutor()
        core = self.new_core(repo, executor)
        previous = self.create_task(core, "task-failed")
        self.store.transition_task_running(previous.task_id, project_id="project-cont")
        self.store.transition_task_terminal(
            previous.task_id,
            execution_status=ExecutionStatus.FAILED,
            event_kind="task.failed",
            payload={"error_type": "TestFailure", "message": "expected"},
        )
        (repo / "app.txt").write_text("EXTERNAL\n", encoding="utf-8")

        await self.assert_dirty_rejected(repo, core, executor)

    async def test_newer_failed_task_supersedes_eligible_baseline(self) -> None:
        def mutate(path: Path) -> None:
            (path / "untracked.txt").write_text("UNCHANGED\n", encoding="utf-8")

        repo = make_repo(self.root)
        executor = SequenceExecutor(mutate)
        core = self.new_core(repo, executor)
        previous = self.create_task(core, "task-previous")
        await core.run_task(previous.task_id)
        failed = self.create_task(core, "task-failed")
        self.store.transition_task_running(failed.task_id, project_id="project-cont")
        self.store.transition_task_terminal(
            failed.task_id,
            execution_status=ExecutionStatus.FAILED,
            event_kind="task.failed",
            payload={"error_type": "TestFailure", "message": "expected"},
        )

        await self.assert_dirty_rejected(repo, core, executor, expected_requests=1)

    async def test_cancelled_prior_task_is_not_a_continuation_baseline(self) -> None:
        repo = make_repo(self.root)
        executor = SequenceExecutor()
        core = self.new_core(repo, executor)
        previous = self.create_task(core, "task-cancelled")
        self.store.transition_task_running(previous.task_id, project_id="project-cont")
        self.store.transition_task_terminal(
            previous.task_id,
            execution_status=ExecutionStatus.CANCELLED,
            event_kind="task.cancelled",
            payload={"reason": "test"},
        )
        (repo / "app.txt").write_text("EXTERNAL\n", encoding="utf-8")

        await self.assert_dirty_rejected(repo, core, executor)

    async def test_newer_cancelled_task_supersedes_eligible_baseline(self) -> None:
        def mutate(path: Path) -> None:
            (path / "untracked.txt").write_text("UNCHANGED\n", encoding="utf-8")

        repo = make_repo(self.root)
        executor = SequenceExecutor(mutate)
        core = self.new_core(repo, executor)
        previous = self.create_task(core, "task-previous")
        await core.run_task(previous.task_id)
        cancelled = self.create_task(core, "task-cancelled")
        self.store.transition_task_running(cancelled.task_id, project_id="project-cont")
        self.store.transition_task_terminal(
            cancelled.task_id,
            execution_status=ExecutionStatus.CANCELLED,
            event_kind="task.cancelled",
            payload={"reason": "test"},
        )

        await self.assert_dirty_rejected(repo, core, executor, expected_requests=1)

    async def test_read_only_finished_prior_task_is_not_a_baseline(self) -> None:
        repo = make_repo(self.root)

        def mutate(path: Path) -> None:
            (path / "app.txt").write_text("READ_ONLY_CHANGE\n", encoding="utf-8")

        executor = SequenceExecutor(mutate)
        core = self.new_core(repo, executor)
        previous = self.create_task(core, "task-read-only", mode=TaskMode.READ_ONLY)
        await core.run_task(previous.task_id)

        await self.assert_dirty_rejected(repo, core, executor, expected_requests=1)

    async def test_newer_read_only_task_supersedes_eligible_baseline(self) -> None:
        def mutate(path: Path) -> None:
            (path / "untracked.txt").write_text("UNCHANGED\n", encoding="utf-8")

        repo = make_repo(self.root)
        executor = SequenceExecutor(mutate, None)
        core = self.new_core(repo, executor)
        previous = self.create_task(core, "task-previous")
        await core.run_task(previous.task_id)
        readonly = self.create_task(core, "task-read-only", mode=TaskMode.READ_ONLY)
        await core.run_task(readonly.task_id)

        await self.assert_dirty_rejected(repo, core, executor, expected_requests=2)

    async def test_branch_change_rejects_continuation(self) -> None:
        def mutate(path: Path) -> None:
            (path / "app.txt").write_text("UPDATED\n", encoding="utf-8")

        repo, executor, core, _, current = await self.seed_finished_auto(mutate)
        git(repo, "checkout", "-b", "external-branch")

        with self.assertRaises(ContinuationBaselineError):
            await core.run_task(current.task_id)
        self.assertEqual(len(executor.requests), 1)
        self.assert_preflight_terminal(current.task_id)

    async def test_head_change_rejects_continuation(self) -> None:
        def mutate(path: Path) -> None:
            (path / "untracked.txt").write_text("UNCHANGED\n", encoding="utf-8")

        repo, executor, core, _, current = await self.seed_finished_auto(mutate)
        git(repo, "commit", "--allow-empty", "-m", "external head")

        with self.assertRaises(ContinuationBaselineError):
            await core.run_task(current.task_id)
        self.assertEqual(len(executor.requests), 1)
        self.assert_preflight_terminal(current.task_id)

    async def test_tracked_content_change_rejects_continuation(self) -> None:
        def mutate(path: Path) -> None:
            (path / "app.txt").write_text("UPDATED\n", encoding="utf-8")

        repo, executor, core, _, current = await self.seed_finished_auto(mutate)
        (repo / "app.txt").write_text("EXTERNALLY_CHANGED\n", encoding="utf-8")

        with self.assertRaises(ContinuationBaselineError):
            await core.run_task(current.task_id)
        self.assertEqual(len(executor.requests), 1)
        self.assert_preflight_terminal(current.task_id)

    async def test_tracked_binary_content_change_rejects_with_same_git_diff_summary(
        self,
    ) -> None:
        def mutate(path: Path) -> None:
            (path / "binary.bin").write_bytes(b"B\x00TASK\n")

        repo, executor, core, _, current = await self.seed_finished_auto(mutate)
        postflight = next(
            event
            for event in self.store.list_task_events("task-previous")
            if event.kind == "policy.postflight"
        )
        self.assertIn("Binary files", postflight.payload["diff"])

        (repo / "binary.bin").write_bytes(b"C\x00EXTERNAL\n")

        with self.assertRaises(ContinuationBaselineError):
            await core.run_task(current.task_id)
        self.assertEqual(len(executor.requests), 1)
        self.assert_preflight_terminal(current.task_id)

    async def test_staged_binary_content_change_rejects_with_same_git_diff_summary(
        self,
    ) -> None:
        def mutate(path: Path) -> None:
            (path / "binary.bin").write_bytes(b"B\x00TASK\n")
            git(path, "add", "binary.bin")

        repo, executor, core, _, current = await self.seed_finished_auto(mutate)
        postflight = next(
            event
            for event in self.store.list_task_events("task-previous")
            if event.kind == "policy.postflight"
        )
        self.assertIn("Binary files", postflight.payload["cached_diff"])

        (repo / "binary.bin").write_bytes(b"C\x00EXTERNAL\n")
        git(repo, "add", "binary.bin")

        with self.assertRaises(ContinuationBaselineError):
            await core.run_task(current.task_id)
        self.assertEqual(len(executor.requests), 1)
        self.assert_preflight_terminal(current.task_id)

    async def test_deleted_dirty_paths_are_not_fingerprinted(self) -> None:
        def mutate(path: Path) -> None:
            (path / "app.txt").unlink()

        repo, executor, core, _, current = await self.seed_finished_auto(mutate)
        postflight = next(
            event
            for event in self.store.list_task_events("task-previous")
            if event.kind == "policy.postflight"
        )
        self.assertNotIn(
            "app.txt",
            [item["path"] for item in postflight.payload["content_fingerprints"]],
        )

        result = await core.run_task(current.task_id)

        self.assertEqual(result.execution_status, ExecutionStatus.FINISHED)
        self.assertEqual(len(executor.requests), 2)

    async def test_untracked_content_change_rejects_continuation(self) -> None:
        def mutate(path: Path) -> None:
            (path / "untracked.txt").write_text("UPDATED\n", encoding="utf-8")

        repo, executor, core, _, current = await self.seed_finished_auto(mutate)
        (repo / "untracked.txt").write_text("EXTERNALLY_CHANGED\n", encoding="utf-8")

        with self.assertRaises(ContinuationBaselineError):
            await core.run_task(current.task_id)
        self.assertEqual(len(executor.requests), 1)
        self.assert_preflight_terminal(current.task_id)

    async def test_untracked_directory_files_are_fingerprinted_individually(self) -> None:
        def mutate(path: Path) -> None:
            cache = path / "__pycache__"
            cache.mkdir()
            (cache / "a.pyc").write_bytes(b"A\x00TASK")
            (cache / "b.pyc").write_bytes(b"B\x00TASK")

        repo, executor, core, _, current = await self.seed_finished_auto(mutate)
        postflight = next(
            event
            for event in self.store.list_task_events("task-previous")
            if event.kind == "policy.postflight"
        )
        self.assertEqual(
            postflight.payload["untracked_files"],
            ["__pycache__/a.pyc", "__pycache__/b.pyc"],
        )
        self.assertEqual(
            [item["path"] for item in postflight.payload["content_fingerprints"]],
            ["__pycache__/a.pyc", "__pycache__/b.pyc"],
        )

        (repo / "__pycache__" / "a.pyc").write_bytes(b"A\x00EXTERNAL")

        with self.assertRaises(ContinuationBaselineError):
            await core.run_task(current.task_id)
        self.assertEqual(len(executor.requests), 1)
        self.assert_preflight_terminal(current.task_id)

    async def test_extra_untracked_path_rejects_continuation(self) -> None:
        def mutate(path: Path) -> None:
            (path / "untracked.txt").write_text("UNCHANGED\n", encoding="utf-8")

        repo, executor, core, _, current = await self.seed_finished_auto(mutate)
        (repo / "extra.txt").write_text("EXTRA\n", encoding="utf-8")

        with self.assertRaises(ContinuationBaselineError):
            await core.run_task(current.task_id)
        self.assertEqual(len(executor.requests), 1)
        self.assert_preflight_terminal(current.task_id)

    async def test_missing_untracked_path_allows_new_clean_baseline(
        self,
    ) -> None:
        def mutate(path: Path) -> None:
            (path / "untracked.txt").write_text("UNCHANGED\n", encoding="utf-8")

        repo, executor, core, _, current = await self.seed_finished_auto(mutate)
        (repo / "untracked.txt").unlink()

        result = await core.run_task(current.task_id)

        self.assertEqual(result.execution_status, ExecutionStatus.FINISHED)
        self.assertEqual(len(executor.requests), 2)
        checkpoint = next(
            event
            for event in self.store.list_task_events(current.task_id)
            if event.kind == "policy.git_checkpoint"
        )
        self.assertEqual(checkpoint.payload["baseline_kind"], "clean")
        self.assertIsNone(checkpoint.payload["previous_task_id"])

    async def test_staged_and_unstaged_state_is_compared(self) -> None:
        def mutate(path: Path) -> None:
            (path / "app.txt").write_text("STAGED\n", encoding="utf-8")
            git(path, "add", "app.txt")

        repo, executor, core, _, current = await self.seed_finished_auto(mutate)

        result = await core.run_task(current.task_id)

        self.assertEqual(result.execution_status, ExecutionStatus.FINISHED)
        checkpoint = next(
            event
            for event in self.store.list_task_events(current.task_id)
            if event.kind == "policy.git_checkpoint"
        )
        self.assertEqual(checkpoint.payload["baseline_kind"], "continuation")
        self.assertEqual(checkpoint.payload["staged_paths"], ["app.txt"])
        self.assertEqual(checkpoint.payload["unstaged_paths"], [])

    async def test_old_dirty_postflight_without_content_evidence_is_rejected(self) -> None:
        def mutate(path: Path) -> None:
            (path / "app.txt").write_text("UPDATED\n", encoding="utf-8")

        repo, executor, core, previous, current = await self.seed_finished_auto(mutate)
        event = next(
            event
            for event in self.store.list_task_events(previous.task_id)
            if event.kind == "policy.postflight"
        )
        payload = dict(event.payload)
        payload.pop("content_fingerprints", None)
        self.store.connection.execute(
            "UPDATE task_events SET payload_json = ? WHERE event_id = ?",
            (json.dumps(payload), event.event_id),
        )
        self.store.connection.commit()

        with self.assertRaises(ContinuationBaselineError):
            await core.run_task(current.task_id)
        self.assertEqual(len(executor.requests), 1)
        self.assert_preflight_terminal(current.task_id)

    async def test_old_clean_postflight_without_content_evidence_remains_allowed(self) -> None:
        repo = make_repo(self.root)
        executor = SequenceExecutor()
        core = self.new_core(repo, executor)
        previous = self.create_task(core, "task-previous")
        await core.run_task(previous.task_id)
        event = next(
            event
            for event in self.store.list_task_events(previous.task_id)
            if event.kind == "policy.postflight"
        )
        payload = dict(event.payload)
        payload.pop("content_fingerprints", None)
        self.store.connection.execute(
            "UPDATE task_events SET payload_json = ? WHERE event_id = ?",
            (json.dumps(payload), event.event_id),
        )
        self.store.connection.commit()

        current = self.create_task(core, "task-current")
        result = await core.run_task(current.task_id)

        self.assertEqual(result.execution_status, ExecutionStatus.FINISHED)
        self.assertEqual(len(executor.requests), 2)


if __name__ == "__main__":
    unittest.main()
