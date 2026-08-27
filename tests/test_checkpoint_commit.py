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
    AuditStatus,
    ExecutionStatus,
    TaskMode,
    TaskStateError,
)
from chatgpt_codex_bridge.executors.base import ExecutionResult  # noqa: E402
from chatgpt_codex_bridge.mcp_adapter import MCPAdapter, MCPToolError  # noqa: E402
from chatgpt_codex_bridge.persistence.sqlite_store import SQLiteBridgeStore  # noqa: E402
from chatgpt_codex_bridge.policy import (  # noqa: E402
    CheckpointCommitError,
    PolicyError,
)


def git_raw(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )
    return completed.stdout


def git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    return git_raw(repo, *args, env=env).strip()


def make_git_repo(parent: Path, name: str = "workspace") -> Path:
    repo = parent / name
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.name", "Bridge Test")
    git(repo, "config", "user.email", "bridge-test@example.invalid")
    git(repo, "branch", "-M", "main")
    (repo / "tracked.txt").write_text("initial\n", encoding="utf-8", newline="")
    git(repo, "add", "tracked.txt")
    git(repo, "commit", "-m", "initial")
    return repo


class FinishedExecutor:
    def __init__(self, mutation) -> None:
        self.mutation = mutation
        self.requests = []

    async def run(self, request, *, on_correlation=None, on_notification=None):
        self.requests.append(request)
        self.mutation(Path(request.cwd))
        return ExecutionResult(
            thread_id="thread-checkpoint",
            turn_id="turn-checkpoint",
            status=ExecutionStatus.FINISHED,
            final_response="CHECKPOINT_OK",
        )


class CheckpointCommitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.store = SQLiteBridgeStore(self.root / "bridge.sqlite3")

    def tearDown(self) -> None:
        self.store.close()
        self.tempdir.cleanup()

    def _prepare(
        self,
        mutation=lambda path: (path / "tracked.txt").write_text(
            "changed\n", encoding="utf-8", newline=""
        ),
        *,
        mode: TaskMode = TaskMode.AUTONOMOUS_WRITE,
        task_id: str = "task-checkpoint",
        project_id: str = "project-checkpoint",
        repo_name: str = "workspace",
    ):
        repo = make_git_repo(self.root, repo_name)
        executor = FinishedExecutor(mutation)
        core = BridgeCore(self.store, executor)
        core.create_project("Project", str(repo), project_id=project_id)
        task = core.create_task(
            project_id,
            "make the requested change",
            task_id=task_id,
            mode=mode,
        )
        return repo, core, task, executor

    def _finished(self, **kwargs):
        repo, core, task, executor = self._prepare(**kwargs)
        asyncio.run(core.run_task(task.task_id))
        return repo, core, task, executor

    def _commit(self, **kwargs):
        repo, core, task, executor = self._finished(**kwargs)
        result = core.commit_checkpoint(task.task_id, "checkpoint D3")
        return repo, core, task, executor, result

    def _postflight_payload(self, task):
        events = self.store.list_task_events(task.task_id)
        return dict(next(event for event in reversed(events) if event.kind == "policy.postflight").payload)

    def test_01_finished_valid_task(self) -> None:
        _, _, task, _ = self._finished()
        self.assertEqual(task.execution_status, ExecutionStatus.QUEUED)
        self.assertEqual(self.store.get_task(task.task_id).execution_status, ExecutionStatus.FINISHED)

    def test_02_commit_created(self) -> None:
        repo, _, task, _, result = self._commit()
        self.assertEqual(result["task_id"], task.task_id)
        self.assertNotEqual(result["previous_head"], result["commit_head"])
        self.assertEqual(git(repo, "log", "-1", "--format=%s"), "checkpoint D3")

    def test_03_repo_is_clean(self) -> None:
        repo, _, _, _, result = self._commit()
        self.assertTrue(result["clean"])
        self.assertEqual(git_raw(repo, "status", "--porcelain"), "")
        self.assertEqual(git(repo, "diff"), "")
        self.assertEqual(git(repo, "diff", "--cached"), "")

    def test_04_head_advances_once(self) -> None:
        repo, _, _, _, result = self._commit()
        self.assertEqual(git(repo, "rev-list", "--count", f"{result['previous_head']}..HEAD"), "1")

    def test_05_parent_is_previous_head(self) -> None:
        repo, _, _, _, result = self._commit()
        self.assertEqual(git(repo, "rev-parse", "HEAD^"), result["previous_head"])

    def test_06_commit_paths_are_exact(self) -> None:
        repo, _, _, _, result = self._commit()
        self.assertEqual(result["paths"], ["tracked.txt"])
        self.assertEqual(git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD^", "HEAD"), "tracked.txt")

    def test_07_author_is_exact(self) -> None:
        repo, _, _, _, _ = self._commit()
        self.assertEqual(git(repo, "show", "-s", "--format=%an <%ae>", "HEAD"), "Marcos Sfregola <marcos.sfregola@gmail.com>")

    def test_08_committer_is_exact(self) -> None:
        repo, _, _, _, _ = self._commit()
        self.assertEqual(git(repo, "show", "-s", "--format=%cn <%ce>", "HEAD"), "Marcos Sfregola <marcos.sfregola@gmail.com>")

    def test_09_message_is_exact(self) -> None:
        repo, _, _, _, _ = self._commit()
        self.assertEqual(git(repo, "show", "-s", "--format=%s", "HEAD"), "checkpoint D3")

    def test_10_duplicate_call_rejects_stably(self) -> None:
        repo, core, task, _, _ = self._commit()
        before = self.store.count_task_events(task.task_id)
        with self.assertRaises(CheckpointCommitError):
            core.commit_checkpoint(task.task_id, "checkpoint D3")
        self.assertEqual(self.store.count_task_events(task.task_id), before)
        self.assertEqual(git(repo, "rev-list", "--count", "HEAD~1..HEAD"), "1")

    def test_11_queued_rejects(self) -> None:
        repo, core, task, _ = self._prepare()
        with self.assertRaises(TaskStateError):
            core.commit_checkpoint(task.task_id, "checkpoint D3")
        self.assertEqual(git_raw(repo, "status", "--porcelain"), "")

    def test_12_running_rejects(self) -> None:
        repo, core, task, _ = self._prepare()
        self.store.update_task_runtime(task.task_id, execution_status=ExecutionStatus.RUNNING)
        with self.assertRaises(TaskStateError):
            core.commit_checkpoint(task.task_id, "checkpoint D3")
        self.assertEqual(git_raw(repo, "status", "--porcelain"), "")

    def test_13_failed_rejects(self) -> None:
        repo, core, task, _ = self._prepare()
        self.store.update_task_runtime(task.task_id, execution_status=ExecutionStatus.FAILED)
        with self.assertRaises(TaskStateError):
            core.commit_checkpoint(task.task_id, "checkpoint D3")
        self.assertEqual(git_raw(repo, "status", "--porcelain"), "")

    def test_14_read_only_rejects(self) -> None:
        repo, core, task, _ = self._finished(mode=TaskMode.READ_ONLY)
        with self.assertRaises(PolicyError):
            core.commit_checkpoint(task.task_id, "checkpoint D3")
        self.assertEqual(git_raw(repo, "status", "--porcelain"), " M tracked.txt\n")

    def test_15_policy_violation_rejects(self) -> None:
        repo, core, task, _ = self._finished()
        self.store.append_task_event(task.task_id, "bridge", "policy.violation", {"phase": "test"})
        with self.assertRaises(PolicyError):
            core.commit_checkpoint(task.task_id, "checkpoint D3")
        self.assertEqual(git_raw(repo, "status", "--porcelain"), " M tracked.txt\n")

    def test_16_head_divergence_rejects(self) -> None:
        repo, core, task, _ = self._finished()
        git(repo, "add", "tracked.txt")
        git(repo, "commit", "-m", "external")
        with self.assertRaises(CheckpointCommitError):
            core.commit_checkpoint(task.task_id, "checkpoint D3")

    def test_17_branch_divergence_rejects(self) -> None:
        repo, core, task, _ = self._finished()
        git(repo, "checkout", "-b", "other")
        with self.assertRaises(CheckpointCommitError):
            core.commit_checkpoint(task.task_id, "checkpoint D3")

    def test_18_fingerprint_divergence_rejects(self) -> None:
        repo, core, task, _ = self._finished()
        (repo / "tracked.txt").write_text("different\n", encoding="utf-8", newline="")
        with self.assertRaises(CheckpointCommitError):
            core.commit_checkpoint(task.task_id, "checkpoint D3")

    def test_19_truncated_postflight_rejects(self) -> None:
        repo, core, task, _ = self._finished()
        payload = self._postflight_payload(task)
        payload["diff"] = "[TRUNCATED]"
        self.store.append_task_event(task.task_id, "bridge", "policy.postflight", payload)
        with self.assertRaises(CheckpointCommitError):
            core.commit_checkpoint(task.task_id, "checkpoint D3")
        self.assertEqual(git_raw(repo, "status", "--porcelain"), " M tracked.txt\n")

    def test_20_project_repo_mismatch_rejects(self) -> None:
        repo, core, task, _ = self._finished()
        payload = self._postflight_payload(task)
        payload["repo_path"] = str(self.root / "not-the-project")
        self.store.append_task_event(task.task_id, "bridge", "policy.postflight", payload)
        with self.assertRaises(CheckpointCommitError):
            core.commit_checkpoint(task.task_id, "checkpoint D3")
        self.assertEqual(git_raw(repo, "status", "--porcelain"), " M tracked.txt\n")

    def test_21_tracked_modified_is_supported(self) -> None:
        repo, _, _, _, result = self._commit()
        self.assertEqual(result["paths"], ["tracked.txt"])
        self.assertEqual(git(repo, "show", "HEAD:tracked.txt"), "changed")

    def test_22_untracked_is_supported(self) -> None:
        repo, _, _, _, result = self._commit(
            mutation=lambda path: (path / "new.txt").write_text("new\n", encoding="utf-8", newline="")
        )
        self.assertEqual(result["paths"], ["new.txt"])
        self.assertEqual(git(repo, "show", "HEAD:new.txt"), "new")

    def test_23_delete_is_supported(self) -> None:
        repo, _, _, _, result = self._commit(
            mutation=lambda path: (path / "tracked.txt").unlink()
        )
        self.assertEqual(result["paths"], ["tracked.txt"])
        self.assertFalse((repo / "tracked.txt").exists())

    def test_24_staged_prior_is_supported(self) -> None:
        def mutation(path: Path) -> None:
            (path / "tracked.txt").write_text("staged\n", encoding="utf-8", newline="")
            git(path, "add", "tracked.txt")

        repo, _, _, _, result = self._commit(mutation=mutation)
        self.assertEqual(result["paths"], ["tracked.txt"])
        self.assertEqual(git(repo, "show", "HEAD:tracked.txt"), "staged")

    def test_25_invalid_path_rejects(self) -> None:
        repo, core, task, _ = self._finished()
        payload = self._postflight_payload(task)
        payload["changed_files"] = ["../outside.txt"]
        self.store.append_task_event(task.task_id, "bridge", "policy.postflight", payload)
        with self.assertRaises(CheckpointCommitError):
            core.commit_checkpoint(task.task_id, "checkpoint D3")
        self.assertEqual(git_raw(repo, "status", "--porcelain"), " M tracked.txt\n")

    def test_absolute_path_rejects_before_git_mutation(self) -> None:
        repo, core, task, _ = self._finished()
        head_before = git(repo, "rev-parse", "HEAD")
        index_before = (repo / ".git" / "index").read_bytes()
        worktree_before = (repo / "tracked.txt").read_bytes()
        payload = self._postflight_payload(task)
        payload["changed_files"] = [str(repo / "outside.txt")]
        self.store.append_task_event(task.task_id, "bridge", "policy.postflight", payload)

        with self.assertRaises(CheckpointCommitError):
            core.commit_checkpoint(task.task_id, "checkpoint D3")

        self.assertEqual(git(repo, "rev-parse", "HEAD"), head_before)
        self.assertEqual((repo / ".git" / "index").read_bytes(), index_before)
        self.assertEqual((repo / "tracked.txt").read_bytes(), worktree_before)
        self.assertFalse(
            any(
                event.kind == "checkpoint.commit.created"
                for event in self.store.list_task_events(task.task_id)
            )
        )

    def test_nul_path_rejects_before_git_mutation(self) -> None:
        repo, core, task, _ = self._finished()
        head_before = git(repo, "rev-parse", "HEAD")
        index_before = (repo / ".git" / "index").read_bytes()
        worktree_before = (repo / "tracked.txt").read_bytes()
        payload = self._postflight_payload(task)
        payload["changed_files"] = ["unsafe\x00name.txt"]
        self.store.append_task_event(task.task_id, "bridge", "policy.postflight", payload)

        with self.assertRaises(CheckpointCommitError):
            core.commit_checkpoint(task.task_id, "checkpoint D3")

        self.assertEqual(git(repo, "rev-parse", "HEAD"), head_before)
        self.assertEqual((repo / ".git" / "index").read_bytes(), index_before)
        self.assertEqual((repo / "tracked.txt").read_bytes(), worktree_before)
        self.assertFalse(
            any(
                event.kind == "checkpoint.commit.created"
                for event in self.store.list_task_events(task.task_id)
            )
        )

    def test_later_queued_task_rejects_earlier_checkpoint(self) -> None:
        repo, core, task, _ = self._finished()
        head_before = git(repo, "rev-parse", "HEAD")
        index_before = (repo / ".git" / "index").read_bytes()
        worktree_before = (repo / "tracked.txt").read_bytes()
        later = core.create_task(
            "project-checkpoint",
            "later queued task",
            task_id="task-later-queued",
            mode=TaskMode.AUTONOMOUS_WRITE,
        )
        self.assertEqual(later.execution_status, ExecutionStatus.QUEUED)

        with self.assertRaises(PolicyError):
            core.commit_checkpoint(task.task_id, "checkpoint D3")

        self.assertEqual(git(repo, "rev-parse", "HEAD"), head_before)
        self.assertEqual((repo / ".git" / "index").read_bytes(), index_before)
        self.assertEqual((repo / "tracked.txt").read_bytes(), worktree_before)
        self.assertEqual(git_raw(repo, "status", "--porcelain"), " M tracked.txt\n")
        self.assertFalse(
            any(
                event.kind == "checkpoint.commit.created"
                for event in self.store.list_task_events(task.task_id)
            )
        )

    def test_26_later_task_rejects_earlier_checkpoint(self) -> None:
        repo, core, task, _ = self._finished()
        core.create_task("project-checkpoint", "later", task_id="task-later", mode=TaskMode.READ_ONLY)
        with self.assertRaises(PolicyError):
            core.commit_checkpoint(task.task_id, "checkpoint D3")
        self.assertEqual(git_raw(repo, "status", "--porcelain"), " M tracked.txt\n")

    def test_27_stage_failure_preserves_real_index_and_worktree(self) -> None:
        repo, core, task, _ = self._finished()
        index_before = (repo / ".git" / "index").read_bytes()
        worktree_before = (repo / "tracked.txt").read_bytes()
        import chatgpt_codex_bridge.policy as policy

        original = policy._git_with_env

        def fail_stage(repo_arg, *args, **kwargs):
            if "add" in args:
                raise CheckpointCommitError("stage failure")
            return original(repo_arg, *args, **kwargs)

        with patch.object(policy, "_git_with_env", side_effect=fail_stage):
            with self.assertRaises(CheckpointCommitError):
                core.commit_checkpoint(task.task_id, "checkpoint D3")
        self.assertEqual((repo / ".git" / "index").read_bytes(), index_before)
        self.assertEqual((repo / "tracked.txt").read_bytes(), worktree_before)

    def test_28_post_stage_failure_preserves_real_index_and_worktree(self) -> None:
        repo, core, task, _ = self._finished()
        index_before = (repo / ".git" / "index").read_bytes()
        with patch("chatgpt_codex_bridge.policy._checkpoint_stage_matches_worktree", side_effect=CheckpointCommitError("post-stage failure")):
            with self.assertRaises(CheckpointCommitError):
                core.commit_checkpoint(task.task_id, "checkpoint D3")
        self.assertEqual((repo / ".git" / "index").read_bytes(), index_before)
        self.assertEqual(git_raw(repo, "status", "--porcelain"), " M tracked.txt\n")

    def test_29_commit_failure_preserves_index_when_head_unchanged(self) -> None:
        repo, core, task, _ = self._finished()
        index_before = (repo / ".git" / "index").read_bytes()
        import chatgpt_codex_bridge.policy as policy

        original = policy._git_with_env

        def fail_commit(repo_arg, *args, **kwargs):
            if "commit" in args:
                raise CheckpointCommitError("commit failure")
            return original(repo_arg, *args, **kwargs)

        previous_head = git(repo, "rev-parse", "HEAD")
        with patch.object(policy, "_git_with_env", side_effect=fail_commit):
            with self.assertRaises(CheckpointCommitError):
                core.commit_checkpoint(task.task_id, "checkpoint D3")
        self.assertEqual(git(repo, "rev-parse", "HEAD"), previous_head)
        self.assertEqual((repo / ".git" / "index").read_bytes(), index_before)
        self.assertEqual(git_raw(repo, "status", "--porcelain"), " M tracked.txt\n")

    def test_30_pre_commit_hook_is_not_executed(self) -> None:
        repo, core, task, _ = self._finished()
        marker = repo / "hook-ran.txt"
        (repo / ".git" / "hooks" / "pre-commit").write_text(
            f"#!/bin/sh\ntouch '{marker}'\nexit 1\n", encoding="utf-8", newline="\n"
        )
        core.commit_checkpoint(task.task_id, "checkpoint D3")
        self.assertFalse(marker.exists())

    def test_31_gpg_signing_is_neutralized(self) -> None:
        repo, core, task, _ = self._finished()
        git(repo, "config", "commit.gpgSign", "true")
        result = core.commit_checkpoint(task.task_id, "checkpoint D3")
        self.assertTrue(result["clean"])

    def test_32_event_persistence_failure_does_not_duplicate(self) -> None:
        repo, core, task, _ = self._finished()
        original = self.store.append_task_event

        def fail_event(task_id, source, kind, payload, **kwargs):
            if kind == "checkpoint.commit.created":
                raise RuntimeError("event persistence failure")
            return original(task_id, source, kind, payload, **kwargs)

        with patch.object(self.store, "append_task_event", side_effect=fail_event):
            with self.assertRaises(RuntimeError):
                core.commit_checkpoint(task.task_id, "checkpoint D3")
        head = git(repo, "rev-parse", "HEAD")
        with self.assertRaises(CheckpointCommitError):
            core.commit_checkpoint(task.task_id, "checkpoint D3")
        self.assertEqual(git(repo, "rev-parse", "HEAD"), head)

    def test_33_no_remote_operations(self) -> None:
        repo, core, task, _ = self._finished()
        import chatgpt_codex_bridge.policy as policy

        original = policy._git_with_env
        commands = []

        def observe(repo_arg, *args, **kwargs):
            commands.append(tuple(args))
            return original(repo_arg, *args, **kwargs)

        with patch.object(policy, "_git_with_env", side_effect=observe):
            core.commit_checkpoint(task.task_id, "checkpoint D3")
        self.assertEqual(git(repo, "remote"), "")
        self.assertFalse(any(any(op in command for op in ("push", "fetch", "pull")) for command in commands))

    def test_34_d2_cont_correction_then_checkpoint(self) -> None:
        repo = make_git_repo(self.root)
        core = BridgeCore(self.store, FinishedExecutor(lambda path: (path / "tracked.txt").write_text("A\n", encoding="utf-8", newline="")))
        core.create_project("Project", str(repo), project_id="project-checkpoint")
        first = core.create_task("project-checkpoint", "A", task_id="task-a", mode=TaskMode.AUTONOMOUS_WRITE)
        asyncio.run(core.run_task(first.task_id))
        correction_executor = FinishedExecutor(lambda path: (path / "tracked.txt").write_text("A-R1\n", encoding="utf-8", newline=""))
        core.executor = correction_executor
        correction = core.create_task("project-checkpoint", "correct A", task_id="task-a-r1", mode=TaskMode.AUTONOMOUS_WRITE)
        asyncio.run(core.run_task(correction.task_id))
        result = core.commit_checkpoint(correction.task_id, "checkpoint A-R1")
        self.assertTrue(result["clean"])
        self.assertEqual(git(repo, "show", "HEAD:tracked.txt"), "A-R1")

    def test_35_a_b_c_checkpoint_chain(self) -> None:
        repo = make_git_repo(self.root)
        core = BridgeCore(self.store, FinishedExecutor(lambda path: (path / "tracked.txt").write_text("A\n", encoding="utf-8", newline="")))
        core.create_project("Project", str(repo), project_id="project-checkpoint")
        heads = []
        for name, content in (("a", "A\n"), ("b", "B\n"), ("c", "C\n")):
            core.executor = FinishedExecutor(lambda path, value=content: (path / "tracked.txt").write_text(value, encoding="utf-8", newline=""))
            task = core.create_task("project-checkpoint", name, task_id=f"task-{name}", mode=TaskMode.AUTONOMOUS_WRITE)
            asyncio.run(core.run_task(task.task_id))
            result = core.commit_checkpoint(task.task_id, f"checkpoint {name.upper()}")
            heads.append(result["commit_head"])
            self.assertEqual(git_raw(repo, "status", "--porcelain"), "")
        self.assertEqual(len(set(heads)), 3)
        self.assertEqual(git(repo, "show", "HEAD:tracked.txt"), "C")

    def test_36_index_install_failure_after_head_move_is_conservative(self) -> None:
        repo, core, task, _ = self._finished()
        index_before = (repo / ".git" / "index").read_bytes()
        previous_head = git(repo, "rev-parse", "HEAD")
        with patch("chatgpt_codex_bridge.policy.os.replace", side_effect=OSError("index install failure")):
            with self.assertRaises(CheckpointCommitError):
                core.commit_checkpoint(task.task_id, "checkpoint D3")
        new_head = git(repo, "rev-parse", "HEAD")
        self.assertNotEqual(new_head, previous_head)
        self.assertEqual((repo / ".git" / "index").read_bytes(), index_before)
        with self.assertRaises(CheckpointCommitError):
            core.commit_checkpoint(task.task_id, "checkpoint D3")
        self.assertEqual(git(repo, "rev-parse", "HEAD"), new_head)

    def test_adapter_commit_tool_delegates(self) -> None:
        repo, core, task, _ = self._finished()
        result = asyncio.run(
            MCPAdapter(core, self.store).call_tool(
                "commit_checkpoint",
                {"task_id": task.task_id, "message": "checkpoint D3"},
            )
        )
        self.assertEqual(result["project_id"], "project-checkpoint")
        self.assertEqual(git(repo, "log", "-1", "--format=%s"), "checkpoint D3")

    def test_adapter_commit_tool_error_is_safe(self) -> None:
        _, core, task, _ = self._finished()
        with self.assertRaises(MCPToolError) as context:
            asyncio.run(
                MCPAdapter(core, self.store).call_tool(
                    "commit_checkpoint", {"task_id": task.task_id, "message": ""}
                )
            )
        self.assertNotIn("Traceback", str(context.exception))


if __name__ == "__main__":
    unittest.main()
