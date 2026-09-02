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
from chatgpt_codex_bridge.domain.models import ExecutionStatus  # noqa: E402
from chatgpt_codex_bridge.mcp_adapter import MCPAdapter, MCPToolError  # noqa: E402
from chatgpt_codex_bridge.persistence.sqlite_store import SQLiteBridgeStore  # noqa: E402


def git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()


def make_git_repo(root: Path) -> Path:
    repo = root / "workspace"
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
    async def run(self, request, *, on_correlation=None, on_notification=None):
        if on_correlation is not None:
            on_correlation("thread-checkpoint", "turn-checkpoint")
        (Path(request.cwd) / "tracked.txt").write_text(
            "changed\n", encoding="utf-8", newline=""
        )
        if on_notification is not None:
            on_notification(
                "turn/completed",
                {
                    "threadId": "thread-checkpoint",
                    "turnId": "turn-checkpoint",
                    "status": "completed",
                },
            )
        from chatgpt_codex_bridge.executors.base import ExecutionResult

        return ExecutionResult(
            thread_id="thread-checkpoint",
            turn_id="turn-checkpoint",
            status=ExecutionStatus.FINISHED,
            final_response="ok",
        )


class H4RecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = SQLiteBridgeStore(Path(self.tempdir.name) / "bridge.sqlite3")
        self.core = BridgeCore(self.store)
        self.core.create_project("Bridge", "C:/workspace/bridge", project_id="project-1")

    def tearDown(self) -> None:
        self.store.close()
        self.tempdir.cleanup()

    def _running(self, task_id: str = "task-1"):
        task = self.core.create_task("project-1", "run", task_id=task_id)
        self.store.transition_task_running(task_id, project_id="project-1")
        self.store.update_task_runtime(
            task_id, thread_id="thread-1", turn_id="turn-1"
        )
        return self.store.get_task(task_id)

    def test_completed_without_task_finished_requires_reconciliation(self) -> None:
        task = self._running()
        assert task is not None
        self.store.append_task_event(
            task.task_id,
            "codex",
            "turn/completed",
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "status": "completed",
                "final_response": "done",
            },
        )
        recovered = self.core.recover_orphaned_tasks()
        self.assertEqual(recovered[0].execution_status, ExecutionStatus.RUNNING)
        state = self.store.get_reconciliation_state(task.task_id)
        self.assertIsNotNone(state)
        assert state is not None
        self.assertFalse(state["resolved"])
        self.assertEqual(
            [event.kind for event in self.store.list_task_events(task.task_id)].count(
                "task.reconciliation_required"
            ),
            1,
        )
        required = self.store.get_reconciliation_state(task.task_id)
        self.assertIsNotNone(required)
        assert required is not None
        self.assertEqual(
            required["required_event"].payload["executor_dispatch"]["status"],
            "unknown",
        )
        self.assertEqual(self.core.recover_orphaned_tasks()[0].execution_status, ExecutionStatus.RUNNING)
        self.assertEqual(
            [event.kind for event in self.store.list_task_events(task.task_id)].count(
                "task.reconciliation_required"
            ),
            1,
        )

    def test_legacy_claim_without_dispatch_marker_remains_unknown(self) -> None:
        task = self._running("task-legacy-claim")
        assert task is not None
        self.store.append_task_event(
            task.task_id,
            "bridge",
            "task.execution_claimed",
            {"owner_kind": "persistent_worker", "owner_id": "old", "pid": 1},
        )

        self.core.recover_orphaned_tasks()

        state = self.store.get_reconciliation_state(task.task_id)
        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(
            state["required_event"].payload["executor_dispatch"]["status"],
            "unknown",
        )

    def test_interrupt_with_h3_request_becomes_cancelled(self) -> None:
        task = self._running()
        assert task is not None
        self.store.request_task_cancellation(
            task.task_id,
            {
                "contract": "D3-H3",
                "request_id": "cancel-1",
                "requested_via": "cancel_task",
            },
        )
        self.store.append_task_event(
            task.task_id,
            "codex",
            "turn/interrupted",
            {"threadId": "thread-1", "turnId": "turn-1", "status": "interrupted"},
        )
        recovered = self.core.recover_orphaned_tasks()
        self.assertEqual(recovered[0].execution_status, ExecutionStatus.CANCELLED)
        self.assertEqual(
            sum(event.kind == "task.cancelled" for event in self.store.list_task_events(task.task_id)),
            1,
        )

    def test_nested_interrupted_completion_without_h3_is_failed(self) -> None:
        task = self._running()
        assert task is not None
        self.store.append_task_event(
            task.task_id,
            "codex",
            "turn/completed",
            {
                "threadId": "thread-1",
                "turn": {"id": "turn-1", "status": "interrupted"},
            },
        )
        recovered = self.core.recover_orphaned_tasks()
        self.assertEqual(recovered[0].execution_status, ExecutionStatus.FAILED)
        failed = [
            event
            for event in self.store.list_task_events(task.task_id)
            if event.kind == "task.failed"
        ]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0].payload["reason"], "unexpected execution interruption")

    def test_correlated_failed_turn_is_failed_once(self) -> None:
        task = self._running()
        assert task is not None
        self.store.append_task_event(
            task.task_id,
            "codex",
            "turn/failed",
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "status": "failed",
                "error": {"code": "E_FAIL"},
            },
        )
        recovered = self.core.recover_orphaned_tasks()
        self.assertEqual(recovered[0].execution_status, ExecutionStatus.FAILED)
        self.assertEqual(self.core.recover_orphaned_tasks(), [])
        self.assertEqual(
            sum(
                event.kind == "task.failed"
                for event in self.store.list_task_events(task.task_id)
            ),
            1,
        )

    def test_mismatched_cancel_request_does_not_produce_cancelled(self) -> None:
        task = self._running()
        assert task is not None
        self.store.request_task_cancellation(
            task.task_id,
            {
                "contract": "D3-H3",
                "request_id": "cancel-wrong",
                "thread_id": "other-thread",
                "turn_id": "other-turn",
            },
        )
        self.store.append_task_event(
            task.task_id,
            "codex",
            "turn/interrupted",
            {"threadId": "thread-1", "turnId": "turn-1", "status": "interrupted"},
        )
        self.assertEqual(
            self.core.recover_orphaned_tasks()[0].execution_status,
            ExecutionStatus.FAILED,
        )

    def test_resolver_failed_is_narrow_and_idempotent(self) -> None:
        task = self._running()
        assert task is not None
        self.core.recover_orphaned_tasks()
        state = self.store.get_reconciliation_state(task.task_id)
        assert state is not None
        reconciliation_id = state["reconciliation_id"]
        adapter = MCPAdapter(self.core, self.store)
        first = asyncio.run(
            adapter.call_tool(
                "resolve_task_reconciliation",
                {
                    "task_id": task.task_id,
                    "reconciliation_id": reconciliation_id,
                    "resolution": "FAILED",
                },
            )
        )
        second = asyncio.run(
            adapter.call_tool(
                "resolve_task_reconciliation",
                {
                    "task_id": task.task_id,
                    "reconciliation_id": reconciliation_id,
                    "resolution": "FAILED",
                },
            )
        )
        self.assertFalse(first["already_resolved"])
        self.assertTrue(second["already_resolved"])
        self.assertEqual(self.store.get_task(task.task_id).execution_status, ExecutionStatus.FAILED)
        self.assertEqual(
            sum(event.kind == "task.failed" for event in self.store.list_task_events(task.task_id)),
            1,
        )

    def test_resolver_rejects_unsupported_resolution(self) -> None:
        task = self._running()
        assert task is not None
        self.core.recover_orphaned_tasks()
        state = self.store.get_reconciliation_state(task.task_id)
        assert state is not None
        with self.assertRaises(MCPToolError):
            asyncio.run(
                MCPAdapter(self.core, self.store).call_tool(
                    "resolve_task_reconciliation",
                    {
                        "task_id": task.task_id,
                        "reconciliation_id": state["reconciliation_id"],
                        "resolution": "FINISHED",
                    },
                )
            )

    def test_cancel_request_without_interrupt_remains_unknown_and_resolves_failed(self) -> None:
        task = self._running()
        assert task is not None
        self.store.request_task_cancellation(
            task.task_id,
            {
                "contract": "D3-H3",
                "request_id": "cancel-pending",
                "thread_id": "thread-1",
                "turn_id": "turn-1",
            },
        )
        self.core.recover_orphaned_tasks()
        state = self.store.get_reconciliation_state(task.task_id)
        assert state is not None
        result = self.core.resolve_task_reconciliation(
            task.task_id,
            state["reconciliation_id"],
            "FAILED",
        )
        self.assertEqual(result["resolution"], "FAILED")
        resolved = [
            event
            for event in self.store.list_task_events(task.task_id)
            if event.kind == "task.reconciliation_resolved"
        ][0]
        self.assertTrue(resolved.payload["cancel_requested"])

    def test_new_claim_after_marker_rejects_resolver(self) -> None:
        task = self._running()
        assert task is not None
        self.core.recover_orphaned_tasks()
        state = self.store.get_reconciliation_state(task.task_id)
        assert state is not None
        self.store.append_task_event(
            task.task_id,
            "bridge",
            "task.execution_claimed",
            {"owner_kind": "persistent_worker", "owner_id": "new", "pid": 1},
        )
        with self.assertRaises(MCPToolError):
            asyncio.run(
                MCPAdapter(self.core, self.store).call_tool(
                    "resolve_task_reconciliation",
                    {
                        "task_id": task.task_id,
                        "reconciliation_id": state["reconciliation_id"],
                        "resolution": "FAILED",
                    },
                )
            )

    def test_new_correlated_terminal_evidence_rejects_manual_resolver(self) -> None:
        task = self._running()
        assert task is not None
        self.core.recover_orphaned_tasks()
        state = self.store.get_reconciliation_state(task.task_id)
        assert state is not None
        self.store.append_task_event(
            task.task_id,
            "codex",
            "turn/failed",
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "status": "failed",
                "error": "late evidence",
            },
        )
        with self.assertRaises(Exception):
            self.core.resolve_task_reconciliation(
                task.task_id,
                state["reconciliation_id"],
                "FAILED",
            )

    def test_checkpoint_persists_phases_and_cleans_repo(self) -> None:
        repo = make_git_repo(Path(self.tempdir.name))
        core = BridgeCore(self.store, FinishedExecutor())
        core.create_project("Repo", str(repo), project_id="project-git")
        task = core.create_task(
            "project-git", "change", task_id="task-git", mode="AUTONOMOUS_WRITE"
        )
        asyncio.run(core.run_task(task.task_id))
        result = core.commit_checkpoint(task.task_id, "checkpoint")
        self.assertTrue(result["commit_created"])
        self.assertTrue(result["clean"])
        self.assertEqual(result["finalization_status"], "CLEAN")
        self.assertEqual(git(repo, "show", "-s", "--format=%s", "HEAD"), "checkpoint")
        kinds = [event.kind for event in self.store.list_task_events(task.task_id)]
        self.assertIn("checkpoint.commit.started", kinds)
        self.assertIn("checkpoint.commit.ref_updated", kinds)
        self.assertIn("checkpoint.commit.created", kinds)

    def test_checkpoint_created_event_retry_returns_same_commit(self) -> None:
        repo = make_git_repo(Path(self.tempdir.name))
        core = BridgeCore(self.store, FinishedExecutor())
        core.create_project("Repo", str(repo), project_id="project-git")
        task = core.create_task(
            "project-git", "change", task_id="task-git", mode="AUTONOMOUS_WRITE"
        )
        asyncio.run(core.run_task(task.task_id))
        original = self.store.append_task_event
        failed_once = {"value": False}

        def fail_created(task_id, source, kind, payload, **kwargs):
            if kind == "checkpoint.commit.created" and not failed_once["value"]:
                failed_once["value"] = True
                raise RuntimeError("simulated SQLite failure")
            return original(task_id, source, kind, payload, **kwargs)

        with patch.object(self.store, "append_task_event", side_effect=fail_created):
            with self.assertRaises(Exception):
                core.commit_checkpoint(task.task_id, "checkpoint")
        head = git(repo, "rev-parse", "HEAD")
        repaired = core.commit_checkpoint(task.task_id, "checkpoint")
        self.assertEqual(repaired["commit_head"], head)
        self.assertTrue(repaired["repaired"])
        self.assertEqual(
            sum(
                event.kind == "checkpoint.commit.created"
                for event in self.store.list_task_events(task.task_id)
            ),
            1,
        )

    def test_post_cas_worktree_change_is_external_conflict_without_rollback(self) -> None:
        repo = make_git_repo(Path(self.tempdir.name))
        core = BridgeCore(self.store, FinishedExecutor())
        core.create_project("Repo", str(repo), project_id="project-git")
        task = core.create_task(
            "project-git", "change", task_id="task-git", mode="AUTONOMOUS_WRITE"
        )
        asyncio.run(core.run_task(task.task_id))
        import chatgpt_codex_bridge.core as core_module

        original_finalize = core_module.git_checkpoint_finalize

        def mutate_then_finalize(prepared):
            (repo / "tracked.txt").write_text("external\n", encoding="utf-8", newline="")
            return original_finalize(prepared)

        with patch.object(core_module, "git_checkpoint_finalize", side_effect=mutate_then_finalize):
            result = core.commit_checkpoint(task.task_id, "checkpoint")
        self.assertEqual(result["finalization_status"], "EXTERNAL_STATE_CONFLICT")
        self.assertEqual(git(repo, "rev-parse", "HEAD"), result["commit_head"])
        self.assertTrue(result["commit_created"])

    def test_foreign_index_lock_is_never_removed_or_overwritten(self) -> None:
        repo = make_git_repo(Path(self.tempdir.name))
        core = BridgeCore(self.store, FinishedExecutor())
        core.create_project("Repo", str(repo), project_id="project-git")
        task = core.create_task(
            "project-git", "change", task_id="task-git", mode="AUTONOMOUS_WRITE"
        )
        asyncio.run(core.run_task(task.task_id))
        index_path = Path(git(repo, "rev-parse", "--git-path", "index"))
        if not index_path.is_absolute():
            index_path = repo / index_path
        lock_path = Path(f"{index_path}.lock")
        lock_bytes = b"foreign-lock"
        lock_path.write_bytes(lock_bytes)
        result = core.commit_checkpoint(task.task_id, "checkpoint")
        self.assertEqual(result["finalization_status"], "EXTERNAL_STATE_CONFLICT")
        self.assertEqual(lock_path.read_bytes(), lock_bytes)
        self.assertEqual(git(repo, "rev-parse", "HEAD"), result["commit_head"])

    def test_external_descendant_after_cas_is_forward_only(self) -> None:
        repo = make_git_repo(Path(self.tempdir.name))
        core = BridgeCore(self.store, FinishedExecutor())
        core.create_project("Repo", str(repo), project_id="project-git")
        task = core.create_task(
            "project-git", "change", task_id="task-git", mode="AUTONOMOUS_WRITE"
        )
        asyncio.run(core.run_task(task.task_id))
        import chatgpt_codex_bridge.core as core_module

        original_cas = core_module.git_checkpoint_cas

        def cas_then_external_advance(prepared):
            original_cas(prepared)
            external = git(
                repo,
                "commit-tree",
                prepared.candidate_tree,
                "-p",
                prepared.candidate_commit,
                "-m",
                "external descendant",
            )
            git(
                repo,
                "update-ref",
                prepared.branch_ref,
                external,
                prepared.candidate_commit,
            )

        with patch.object(core_module, "git_checkpoint_cas", side_effect=cas_then_external_advance):
            result = core.commit_checkpoint(task.task_id, "checkpoint")
        self.assertEqual(result["finalization_status"], "REF_ADVANCED_AFTER_CHECKPOINT")
        current_head = git(repo, "rev-parse", "HEAD")
        self.assertNotEqual(current_head, result["commit_head"])
        subprocess.run(
            ["git", "-C", str(repo), "merge-base", "--is-ancestor", result["commit_head"], current_head],
            check=True,
            capture_output=True,
        )
        self.assertTrue(result["commit_created"])


if __name__ == "__main__":
    unittest.main()
