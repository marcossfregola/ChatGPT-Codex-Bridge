from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from collections.abc import Callable
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT / "src"))

from chatgpt_codex_bridge.core import BridgeCore  # noqa: E402
from chatgpt_codex_bridge.domain.models import ExecutionStatus, TaskMode  # noqa: E402
from chatgpt_codex_bridge.executors.base import ExecutionResult  # noqa: E402
from chatgpt_codex_bridge.execution_worker import ExecutionWorker  # noqa: E402
from chatgpt_codex_bridge.mcp_adapter import MCPAdapter  # noqa: E402
from chatgpt_codex_bridge.persistence.sqlite_store import (  # noqa: E402
    RECONCILIATION_BASELINE_ADOPTED_EVENT,
    SQLiteBridgeStore,
)
from chatgpt_codex_bridge.policy import (  # noqa: E402
    ContinuationBaselineError,
    checkpoint_payload,
    git_postflight,
    git_preflight,
    postflight_payload,
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
    git(repo, "config", "user.name", "Bridge Adoption Test")
    git(repo, "config", "user.email", "adoption-test@example.invalid")
    git(repo, "branch", "-M", "main")
    (repo / "app.txt").write_text("ORIGINAL\n", encoding="utf-8")
    git(repo, "add", "app.txt")
    git(repo, "commit", "-m", "initial")
    return repo


class FinishedExecutor:
    def __init__(self) -> None:
        self.requests = []

    async def run(self, request, *, on_correlation=None, on_notification=None):
        self.requests.append(request)
        return ExecutionResult(
            thread_id="thread-continuation",
            turn_id="turn-continuation",
            status=ExecutionStatus.FINISHED,
            final_response="ADOPTED_CONTINUATION_OK",
        )


class ReconciliationAdoptionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.store = SQLiteBridgeStore(self.root / "bridge.sqlite3")
        self.core = BridgeCore(self.store)

    def tearDown(self) -> None:
        self.store.close()
        self.tempdir.cleanup()

    def _create_project(self, repo: Path, project_id: str = "project-adoption") -> None:
        self.core.create_project("Adoption", str(repo), project_id=project_id)

    def _finish_inspection(
        self,
        project_id: str = "project-adoption",
        *,
        task_id: str = "task-inspection",
        mode: TaskMode = TaskMode.READ_ONLY,
        violation: bool = False,
    ):
        inspection = self.core.create_task(
            project_id,
            "inspect the survivor",
            task_id=task_id,
            mode=mode,
        )
        self.store.transition_task_running(
            inspection.task_id, project_id=inspection.project_id
        )
        if violation:
            self.store.append_task_event(
                inspection.task_id,
                "bridge",
                "policy.violation",
                {"phase": "inspection", "policy_violation": True},
            )
        self.store.transition_task_terminal(
            inspection.task_id,
            execution_status=ExecutionStatus.FINISHED,
            event_kind="task.finished",
            payload={"final_response": "INSPECTED"},
        )
        return inspection

    def _seed_source(
        self,
        *,
        repo_name: str = "workspace",
        project_id: str = "project-adoption",
        source_task_id: str = "task-source",
        truncated: bool = True,
        status: ExecutionStatus = ExecutionStatus.FINISHED,
        source_mode: TaskMode = TaskMode.AUTONOMOUS_WRITE,
        inspection_before_postflight: bool = False,
        create_inspection: bool = True,
        source_violation: bool = False,
        large_change: bool = False,
        postflight_mutator: Callable[[dict], None] | None = None,
    ):
        repo = make_repo(self.root, repo_name)
        self._create_project(repo, project_id=project_id)
        source = self.core.create_task(
            project_id,
            "survivor autonomous task",
            task_id=source_task_id,
            mode=source_mode,
        )
        self.store.transition_task_running(source.task_id, project_id=source.project_id)
        checkpoint = git_preflight(repo)
        checkpoint_event = self.store.append_task_event(
            source.task_id,
            "bridge",
            "policy.git_checkpoint",
            {"mode": TaskMode.AUTONOMOUS_WRITE.value, **checkpoint_payload(checkpoint)},
        )
        survivor = "SURVIVOR\n"
        if large_change:
            survivor += "x" * 20_000
        (repo / "app.txt").write_text(survivor, encoding="utf-8")

        inspection = None
        if inspection_before_postflight:
            inspection = self._finish_inspection(project_id)

        postflight = postflight_payload(git_postflight(checkpoint))
        if truncated and not large_change:
            postflight["diff"] = "bounded Git diff evidence [TRUNCATED]"
        if postflight_mutator is not None:
            postflight_mutator(postflight)
        postflight_event = self.store.append_task_event(
            source.task_id, "bridge", "policy.postflight", postflight
        )
        if source_violation:
            self.store.append_task_event(
                source.task_id,
                "bridge",
                "policy.violation",
                {"phase": "postflight", "policy_violation": True},
            )
        terminal_kind = {
            ExecutionStatus.FINISHED: "task.finished",
            ExecutionStatus.FAILED: "task.failed",
            ExecutionStatus.CANCELLED: "task.cancelled",
        }[status]
        self.store.transition_task_terminal(
            source.task_id,
            execution_status=status,
            event_kind=terminal_kind,
            payload={"final_response": "SURVIVOR"},
        )
        if inspection is None and create_inspection:
            inspection = self._finish_inspection(project_id)
        return repo, source, inspection, checkpoint_event, postflight_event

    def _adoption_events(self, task_id: str):
        return [
            event
            for event in self.store.list_task_events(task_id)
            if event.kind == RECONCILIATION_BASELINE_ADOPTED_EVENT
        ]

    def test_direct_adoption_persists_without_inspection_task(self) -> None:
        repo, source, inspection, _, _ = self._seed_source(
            truncated=False, create_inspection=False
        )
        self.assertIsNone(inspection)

        result = self.core.adopt_reconciled_continuation_baseline(
            source.task_id, mode="direct"
        )

        self.assertTrue(result["adopted"])
        self.assertFalse(result["idempotent"])
        self.assertEqual(result["adoption_mode"], "direct")
        self.assertIsNone(result["inspection_task_id"])
        event = self._adoption_events(source.task_id)[0]
        self.assertEqual(event.payload["adoption_mode"], "direct")
        self.assertIsNone(event.payload["inspection_task_id"])
        self.assertIsNone(event.payload["inspection_terminal_event_id"])
        self.assertEqual(event.payload["inspection_high_water_event_id"], 0)
        self.assertTrue(Path(event.payload["snapshot"]["repo_path"]).samefile(repo))
        self.assertEqual(
            event.payload["snapshot"]["working_tree_fingerprint_version"], 1
        )
        self.assertEqual(event.payload["fingerprint"], event.payload["evidence_fingerprint"])

    def test_direct_adoption_round_trips_all_provenance(self) -> None:
        _, source, _, _, _ = self._seed_source(
            truncated=False, create_inspection=False
        )
        result = self.core.adopt_reconciled_continuation_baseline(
            source.task_id, mode="direct"
        )
        before = self._adoption_events(source.task_id)[0]
        before_payload = before.payload
        before_id = before.event_id
        self.store.close()

        reopened = SQLiteBridgeStore(self.store.db_path)
        self.store = reopened
        after = self._adoption_events(source.task_id)[0]

        self.assertEqual(after.event_id, before_id)
        self.assertEqual(after.payload, before_payload)
        self.assertEqual(after.payload["adoption_mode"], "direct")
        self.assertEqual(after.payload["source_task_id"], source.task_id)
        self.assertEqual(
            after.payload["source_postflight_event_id"],
            before_payload["source_postflight_event_id"],
        )
        self.assertEqual(
            after.payload["source_high_water_event_id"],
            before_payload["source_high_water_event_id"],
        )
        self.assertEqual(
            after.payload["inspection_high_water_event_id"], 0
        )
        self.assertEqual(after.payload["fingerprint"], result["fingerprint"])
        self.assertEqual(
            after.payload["fingerprint"], after.payload["evidence_fingerprint"]
        )

    async def test_direct_adopted_baseline_enables_continuation(self) -> None:
        repo, source, inspection, _, _ = self._seed_source(
            truncated=False, create_inspection=False
        )
        self.assertIsNone(inspection)
        self.core.adopt_reconciled_continuation_baseline(
            source.task_id, mode="direct"
        )

        executor = FinishedExecutor()
        continuation_core = BridgeCore(self.store, executor)
        current = continuation_core.create_task(
            "project-adoption",
            "continue after direct adoption",
            task_id="task-current-direct",
            mode=TaskMode.AUTONOMOUS_WRITE,
        )
        finished = await continuation_core.run_task(current.task_id)

        self.assertEqual(finished.execution_status, ExecutionStatus.FINISHED)
        self.assertEqual(len(executor.requests), 1)
        checkpoint = next(
            event
            for event in self.store.list_task_events(current.task_id)
            if event.kind == "policy.git_checkpoint"
        )
        self.assertEqual(checkpoint.payload["baseline_kind"], "continuation")
        self.assertEqual(checkpoint.payload["previous_task_id"], source.task_id)
        self.assertEqual((repo / "app.txt").read_text(encoding="utf-8"), "SURVIVOR\n")

    async def test_legacy_adoption_event_without_mode_remains_valid(self) -> None:
        _, source, inspection, _, _ = self._seed_source()
        self.core.adopt_reconciled_continuation_baseline(
            source.task_id, inspection.task_id
        )
        adoption = self._adoption_events(source.task_id)[0]
        legacy_payload = dict(adoption.payload)
        legacy_payload.pop("adoption_mode")
        self.store.connection.execute(
            "UPDATE task_events SET payload_json = ? WHERE event_id = ?",
            (json.dumps(legacy_payload), adoption.event_id),
        )
        self.store.connection.commit()

        executor = FinishedExecutor()
        continuation_core = BridgeCore(self.store, executor)
        current = continuation_core.create_task(
            "project-adoption",
            "continue from a legacy persisted adoption",
            task_id="task-current-legacy-event",
            mode=TaskMode.AUTONOMOUS_WRITE,
        )
        finished = await continuation_core.run_task(current.task_id)

        self.assertEqual(finished.execution_status, ExecutionStatus.FINISHED)
        self.assertEqual(len(executor.requests), 1)

    def test_direct_adoption_rejects_inspection_hybrid(self) -> None:
        _, source, inspection, _, _ = self._seed_source(truncated=False)

        with self.assertRaises(ContinuationBaselineError):
            self.core.adopt_reconciled_continuation_baseline(
                source.task_id,
                inspection.task_id,
                mode="direct",
            )
        self.assertEqual(self._adoption_events(source.task_id), [])

    def test_legacy_adoption_without_inspection_remains_invalid(self) -> None:
        _, source, inspection, _, _ = self._seed_source(
            truncated=False, create_inspection=False
        )
        self.assertIsNone(inspection)

        with self.assertRaises(ContinuationBaselineError):
            self.core.adopt_reconciled_continuation_baseline(source.task_id)
        self.assertEqual(self._adoption_events(source.task_id), [])

    def test_direct_adoption_rejects_branch_change(self) -> None:
        repo, source, _, _, _ = self._seed_source(
            truncated=False, create_inspection=False
        )
        subprocess.run(
            ["git", "-C", str(repo), "switch", "-c", "feature"],
            check=True,
            capture_output=True,
            text=True,
        )

        with self.assertRaises(ContinuationBaselineError):
            self.core.adopt_reconciled_continuation_baseline(
                source.task_id, mode="direct"
            )
        self.assertEqual(self._adoption_events(source.task_id), [])

    def test_direct_adoption_rejects_head_change(self) -> None:
        repo, source, _, _, _ = self._seed_source(
            truncated=False, create_inspection=False
        )
        (repo / "head-change.txt").write_text("HEAD CHANGE\n", encoding="utf-8")
        git(repo, "add", "app.txt", "head-change.txt")
        git(repo, "commit", "-m", "head change")

        with self.assertRaises(ContinuationBaselineError):
            self.core.adopt_reconciled_continuation_baseline(
                source.task_id, mode="direct"
            )
        self.assertEqual(self._adoption_events(source.task_id), [])

    def test_direct_adoption_rejects_repo_mismatch(self) -> None:
        repo, source, _, _, postflight_event = self._seed_source(
            truncated=False, create_inspection=False
        )
        other_repo = make_repo(self.root, "other-repo")
        payload = dict(postflight_event.payload)
        payload["repo_path"] = str(other_repo)
        self.store.connection.execute(
            "UPDATE task_events SET payload_json = ? WHERE event_id = ?",
            (json.dumps(payload), postflight_event.event_id),
        )
        self.store.connection.commit()

        with self.assertRaises(ContinuationBaselineError):
            self.core.adopt_reconciled_continuation_baseline(
                source.task_id, mode="direct"
            )
        self.assertEqual(self._adoption_events(source.task_id), [])
        self.assertEqual(git(repo, "status", "--porcelain"), "M app.txt")

    def test_direct_adoption_rejects_ineligible_source(self) -> None:
        _, source, _, _, _ = self._seed_source(
            truncated=False,
            create_inspection=False,
            source_mode=TaskMode.READ_ONLY,
        )

        with self.assertRaises(ContinuationBaselineError):
            self.core.adopt_reconciled_continuation_baseline(
                source.task_id, mode="direct"
            )
        self.assertEqual(self._adoption_events(source.task_id), [])

    def test_direct_adoption_rejects_superseded_source(self) -> None:
        _, source, _, _, source_postflight = self._seed_source(
            truncated=False, create_inspection=False
        )
        newer = self.core.create_task(
            "project-adoption",
            "newer autonomous task",
            task_id="task-newer-direct",
            mode=TaskMode.AUTONOMOUS_WRITE,
        )
        self.store.transition_task_running(newer.task_id, project_id=newer.project_id)
        self.store.append_task_event(
            newer.task_id,
            "bridge",
            "policy.postflight",
            dict(source_postflight.payload),
        )
        self.store.transition_task_terminal(
            newer.task_id,
            execution_status=ExecutionStatus.FINISHED,
            event_kind="task.finished",
            payload={"final_response": "NEWER"},
        )

        with self.assertRaises(ContinuationBaselineError):
            self.core.adopt_reconciled_continuation_baseline(
                source.task_id, mode="direct"
            )
        self.assertEqual(self._adoption_events(source.task_id), [])

    def test_direct_adoption_rejects_invalid_or_unknown_fingerprint(self) -> None:
        for index, invalid_value in enumerate(("invalid", 999)):
            with self.subTest(invalid_value=invalid_value):
                _, source, _, _, postflight_event = self._seed_source(
                    repo_name=f"workspace-invalid-{index}",
                    project_id=f"project-invalid-{index}",
                    source_task_id=f"task-invalid-{index}",
                    truncated=False,
                    create_inspection=False,
                )
                payload = dict(postflight_event.payload)
                if isinstance(invalid_value, int):
                    payload["working_tree_fingerprint_version"] = invalid_value
                else:
                    payload["working_tree_fingerprint"] = invalid_value
                self.store.connection.execute(
                    "UPDATE task_events SET payload_json = ? WHERE event_id = ?",
                    (json.dumps(payload), postflight_event.event_id),
                )
                self.store.connection.commit()

                with self.assertRaises(ContinuationBaselineError):
                    self.core.adopt_reconciled_continuation_baseline(
                        source.task_id, mode="direct"
                    )
                self.assertEqual(self._adoption_events(source.task_id), [])

    def test_direct_adoption_rejects_double_capture_change_without_event(self) -> None:
        _, source, _, _, _ = self._seed_source(
            truncated=False, create_inspection=False
        )
        source = self.store.get_task(source.task_id)
        assert source is not None
        context = self.core._validated_baseline_adoption_context(  # noqa: SLF001
            source,
            None,
            adoption_mode="direct",
        )
        snapshot = self.core._capture_reconciled_snapshot(  # noqa: SLF001
            context["checkpoint"], context["project"]
        )
        changed = dict(snapshot)
        changed["diff"] = snapshot["diff"] + " changed"

        with patch.object(
            self.core,
            "_capture_reconciled_snapshot",
            side_effect=[snapshot, changed],
        ):
            with self.assertRaises(ContinuationBaselineError):
                self.core.adopt_reconciled_continuation_baseline(
                    source.task_id, mode="direct"
                )
        self.assertEqual(self._adoption_events(source.task_id), [])

    async def test_explicit_adoption_persists_and_enables_continuation(self) -> None:
        repo, source, inspection, _, _ = self._seed_source()

        result = self.core.adopt_reconciled_continuation_baseline(
            source.task_id, inspection.task_id
        )

        self.assertTrue(result["adopted"])
        self.assertFalse(result["idempotent"])
        self.assertEqual(result["adoption_mode"], "legacy")
        self.assertEqual(result["baseline_kind"], "reconciled_continuation")
        event = self._adoption_events(source.task_id)[0]
        self.assertEqual(event.event_id, result["adoption_event_id"])
        self.assertEqual(event.payload["adoption_mode"], "legacy")
        self.assertEqual(event.payload["source_task_id"], source.task_id)
        self.assertEqual(event.payload["inspection_task_id"], inspection.task_id)
        self.assertIn("content_fingerprints", event.payload["snapshot"])
        self.assertEqual(event.payload["snapshot"]["final_branch"], "main")

        executor = FinishedExecutor()
        continuation_core = BridgeCore(self.store, executor)
        current = continuation_core.create_task(
            "project-adoption",
            "continue after explicit adoption",
            task_id="task-current",
            mode=TaskMode.AUTONOMOUS_WRITE,
        )
        finished = await continuation_core.run_task(current.task_id)

        self.assertEqual(finished.execution_status, ExecutionStatus.FINISHED)
        self.assertEqual(len(executor.requests), 1)
        checkpoint = next(
            event
            for event in self.store.list_task_events(current.task_id)
            if event.kind == "policy.git_checkpoint"
        )
        self.assertEqual(checkpoint.payload["baseline_kind"], "continuation")
        self.assertEqual(checkpoint.payload["previous_task_id"], source.task_id)
        self.assertEqual((repo / "app.txt").read_text(encoding="utf-8"), "SURVIVOR\n")

    async def test_worker_running_task_accepts_adopted_baseline_without_self_invalidation(
        self,
    ) -> None:
        repo, source, inspection, _, _ = self._seed_source()
        self.core.adopt_reconciled_continuation_baseline(
            source.task_id, inspection.task_id
        )

        executor = FinishedExecutor()
        continuation_core = BridgeCore(self.store, executor)
        current = continuation_core.create_task(
            "project-adoption",
            "continue through the persistent worker",
            task_id="task-current-worker",
            mode=TaskMode.AUTONOMOUS_WRITE,
        )
        adapter = MCPAdapter(continuation_core, self.store)
        accepted = await adapter.call_tool("run_task", {"task_id": current.task_id})
        worker = ExecutionWorker(
            self.store,
            continuation_core,
            worker_id="worker-adopted-baseline",
            pid=42001,
        )

        claimed = worker.claim_next()
        self.assertIsNotNone(claimed)
        assert claimed is not None
        self.assertTrue(accepted["accepted"])
        self.assertEqual(claimed.execution_status, ExecutionStatus.RUNNING)
        self.assertEqual(
            [event.kind for event in self.store.list_task_events(current.task_id)],
            [
                "task.created",
                "task.execution_requested",
                "task.execution_claimed",
                "task.started",
            ],
        )
        adoption_event = self._adoption_events(source.task_id)[0]
        current_initial_events = self.store.list_task_events(current.task_id)
        self.assertGreater(
            max(event.event_id or 0 for event in current_initial_events),
            adoption_event.event_id or 0,
        )

        finished = await continuation_core.execute_claimed_task(current.task_id)

        self.assertEqual(finished.execution_status, ExecutionStatus.FINISHED)
        self.assertEqual(len(executor.requests), 1)
        checkpoint = next(
            event
            for event in self.store.list_task_events(current.task_id)
            if event.kind == "policy.git_checkpoint"
        )
        self.assertEqual(checkpoint.payload["baseline_kind"], "continuation")
        self.assertEqual(checkpoint.payload["previous_task_id"], source.task_id)
        self.assertEqual((repo / "app.txt").read_text(encoding="utf-8"), "SURVIVOR\n")

    async def test_adopted_baseline_revalidation_still_rejects_another_later_autonomous_candidate(
        self,
    ) -> None:
        _, source, inspection, _, _ = self._seed_source()
        self.core.adopt_reconciled_continuation_baseline(
            source.task_id, inspection.task_id
        )

        executor = FinishedExecutor()
        continuation_core = BridgeCore(self.store, executor)
        adapter = MCPAdapter(continuation_core, self.store)
        worker = ExecutionWorker(
            self.store,
            continuation_core,
            worker_id="worker-intervening-baseline",
            pid=42002,
        )
        intervening = continuation_core.create_task(
            "project-adoption",
            "intervene after the adopted baseline",
            task_id="task-intervening-worker",
            mode=TaskMode.AUTONOMOUS_WRITE,
        )
        await adapter.call_tool("run_task", {"task_id": intervening.task_id})
        intervening_claimed = worker.claim_next()
        self.assertIsNotNone(intervening_claimed)
        assert intervening_claimed is not None
        self.assertEqual(intervening_claimed.execution_status, ExecutionStatus.RUNNING)

        current = continuation_core.create_task(
            "project-adoption",
            "must reject an intervening autonomous task",
            task_id="task-current-intervening",
            mode=TaskMode.AUTONOMOUS_WRITE,
        )
        await adapter.call_tool("run_task", {"task_id": current.task_id})
        current_claimed = worker.claim_next()
        self.assertIsNotNone(current_claimed)
        assert current_claimed is not None
        self.assertEqual(current_claimed.execution_status, ExecutionStatus.RUNNING)

        adoption_event = self._adoption_events(source.task_id)[0]
        intervening_events = self.store.list_task_events(intervening.task_id)
        self.assertGreater(
            max(event.event_id or 0 for event in intervening_events),
            adoption_event.event_id or 0,
        )
        source_task = self.store.get_task(source.task_id)
        assert source_task is not None
        with self.assertRaises(ContinuationBaselineError):
            continuation_core._validated_baseline_adoption_context(  # noqa: SLF001
                source_task,
                inspection.task_id,
                excluded_task_id=current.task_id,
            )

        self.assertEqual(executor.requests, [])

    async def test_large_git_evidence_uses_complete_adoption_snapshot(self) -> None:
        repo, source, inspection, _, _ = self._seed_source(large_change=True)
        source_postflight = next(
            event
            for event in self.store.list_task_events(source.task_id)
            if event.kind == "policy.postflight"
        )
        self.assertTrue(source_postflight.payload["diff"].endswith("[TRUNCATED]"))

        result = self.core.adopt_reconciled_continuation_baseline(
            source.task_id, inspection.task_id
        )
        event = self._adoption_events(source.task_id)[0]

        self.assertTrue(result["adopted"])
        self.assertGreater(len(event.payload["snapshot"]["diff"]), 16_384)
        self.assertFalse(event.payload["snapshot"]["diff"].endswith("[TRUNCATED]"))

        executor = FinishedExecutor()
        continuation_core = BridgeCore(self.store, executor)
        current = continuation_core.create_task(
            "project-adoption",
            "continue with a large adopted worktree",
            task_id="task-current",
            mode=TaskMode.AUTONOMOUS_WRITE,
        )
        finished = await continuation_core.run_task(current.task_id)
        self.assertEqual(finished.execution_status, ExecutionStatus.FINISHED)
        self.assertEqual(len(executor.requests), 1)
        self.assertEqual((repo / "app.txt").read_text(encoding="utf-8"), "SURVIVOR\n" + "x" * 20_000)

    def test_adoption_is_idempotent_and_does_not_append_a_second_event(self) -> None:
        repo, source, inspection, _, _ = self._seed_source()

        first = self.core.adopt_reconciled_continuation_baseline(
            source.task_id, inspection.task_id
        )
        second = self.core.adopt_reconciled_continuation_baseline(
            source.task_id, inspection.task_id
        )

        self.assertTrue(first["adopted"])
        self.assertTrue(second["idempotent"])
        self.assertEqual(first["adoption_event_id"], second["adoption_event_id"])
        self.assertEqual(len(self._adoption_events(source.task_id)), 1)

        (repo / "app.txt").write_text("CHANGED_AFTER_ADOPTION\n", encoding="utf-8")
        with self.assertRaises(ContinuationBaselineError):
            self.core.adopt_reconciled_continuation_baseline(
                source.task_id, inspection.task_id
            )
        self.assertEqual(len(self._adoption_events(source.task_id)), 1)

    def test_double_snapshot_mismatch_is_fail_closed_without_event(self) -> None:
        _, source, inspection, _, _ = self._seed_source()
        source = self.store.get_task(source.task_id)
        assert source is not None
        context = self.core._validated_baseline_adoption_context(
            source, inspection.task_id
        )
        snapshot = self.core._capture_reconciled_snapshot(
            context["checkpoint"], context["project"]
        )
        changed = dict(snapshot)
        changed["diff"] = snapshot["diff"] + " changed"

        with patch.object(
            self.core,
            "_capture_reconciled_snapshot",
            side_effect=[snapshot, changed],
        ):
            with self.assertRaises(ContinuationBaselineError):
                self.core.adopt_reconciled_continuation_baseline(
                    source.task_id, inspection.task_id
                )
        self.assertEqual(self._adoption_events(source.task_id), [])

    async def test_legacy_truncated_source_without_adoption_remains_a_blocker(self) -> None:
        _, source, _, _, _ = self._seed_source()
        source_postflight = next(
            event
            for event in self.store.list_task_events(source.task_id)
            if event.kind == "policy.postflight"
        )
        legacy_payload = dict(source_postflight.payload)
        legacy_payload.pop("working_tree_fingerprint_version")
        legacy_payload.pop("working_tree_fingerprint")
        self.store.connection.execute(
            "UPDATE task_events SET payload_json = ? WHERE event_id = ?",
            (json.dumps(legacy_payload), source_postflight.event_id),
        )
        self.store.connection.commit()
        executor = FinishedExecutor()
        current_core = BridgeCore(self.store, executor)
        current = current_core.create_task(
            "project-adoption",
            "must not silently fall back",
            task_id="task-current",
            mode=TaskMode.AUTONOMOUS_WRITE,
        )

        with self.assertRaises(ContinuationBaselineError):
            await current_core.run_task(current.task_id)
        self.assertEqual(executor.requests, [])
        self.assertEqual(self._adoption_events(source.task_id), [])
        current_state = self.store.get_task(current.task_id)
        assert current_state is not None
        self.assertEqual(current_state.execution_status, ExecutionStatus.FAILED)

    def test_source_must_be_autonomous(self) -> None:
        _, source, inspection, _, _ = self._seed_source(source_mode=TaskMode.READ_ONLY)
        with self.assertRaises(ContinuationBaselineError):
            self.core.adopt_reconciled_continuation_baseline(
                source.task_id, inspection.task_id
            )

    def test_source_must_be_finished(self) -> None:
        _, failed, inspection, _, _ = self._seed_source(status=ExecutionStatus.FAILED)
        with self.assertRaises(ContinuationBaselineError):
            self.core.adopt_reconciled_continuation_baseline(
                failed.task_id, inspection.task_id
            )

    def test_source_postflight_must_be_incomplete(self) -> None:
        _, complete, inspection, _, _ = self._seed_source(truncated=False)
        with self.assertRaises(ContinuationBaselineError):
            self.core.adopt_reconciled_continuation_baseline(
                complete.task_id, inspection.task_id
            )

    def test_missing_diff_without_sentinel_is_not_recoverable(self) -> None:
        _, source, inspection, _, _ = self._seed_source(
            truncated=False,
            postflight_mutator=lambda payload: payload.pop("diff"),
        )
        with self.assertRaises(ContinuationBaselineError):
            self.core.adopt_reconciled_continuation_baseline(
                source.task_id, inspection.task_id
            )
        self.assertEqual(self._adoption_events(source.task_id), [])

    def test_missing_content_fingerprints_without_sentinel_is_not_recoverable(
        self,
    ) -> None:
        _, source, inspection, _, _ = self._seed_source(
            truncated=False,
            postflight_mutator=lambda payload: payload.pop("content_fingerprints"),
        )
        with self.assertRaises(ContinuationBaselineError):
            self.core.adopt_reconciled_continuation_baseline(
                source.task_id, inspection.task_id
            )
        self.assertEqual(self._adoption_events(source.task_id), [])

    def test_invalid_fingerprint_without_sentinel_is_not_recoverable(self) -> None:
        def invalidate(payload: dict) -> None:
            payload["content_fingerprints"][0]["sha256"] = "invalid"

        _, source, inspection, _, _ = self._seed_source(
            truncated=False,
            postflight_mutator=invalidate,
        )
        with self.assertRaises(ContinuationBaselineError):
            self.core.adopt_reconciled_continuation_baseline(
                source.task_id, inspection.task_id
            )
        self.assertEqual(self._adoption_events(source.task_id), [])

    async def test_invalid_adoption_does_not_fall_back_to_an_older_baseline(self) -> None:
        _, older, inspection, _, older_postflight = self._seed_source(truncated=False)
        newer = self.core.create_task(
            "project-adoption",
            "newer autonomous task with invalid adoption",
            task_id="task-newer-invalid-adoption",
            mode=TaskMode.AUTONOMOUS_WRITE,
        )
        self.store.transition_task_running(newer.task_id, project_id=newer.project_id)
        source_payload = dict(older_postflight.payload)
        self.store.append_task_event(
            newer.task_id, "bridge", "policy.postflight", source_payload
        )
        self.store.transition_task_terminal(
            newer.task_id,
            execution_status=ExecutionStatus.FINISHED,
            event_kind="task.finished",
            payload={"final_response": "NEWER"},
        )
        self.store.append_task_event(
            newer.task_id,
            "bridge",
            RECONCILIATION_BASELINE_ADOPTED_EVENT,
            {"inspection_task_id": inspection.task_id},
        )

        executor = FinishedExecutor()
        current_core = BridgeCore(self.store, executor)
        current = current_core.create_task(
            "project-adoption",
            "must not use older baseline",
            task_id="task-current-invalid-adoption",
            mode=TaskMode.AUTONOMOUS_WRITE,
        )
        with self.assertRaises(ContinuationBaselineError):
            await current_core.run_task(current.task_id)
        self.assertEqual(executor.requests, [])
        self.assertEqual(self._adoption_events(older.task_id), [])

    def test_inspection_must_be_posterior_same_project_read_only_finished_clean(self) -> None:
        repo, source, inspection, _, _ = self._seed_source(
            inspection_before_postflight=True
        )
        with self.assertRaises(ContinuationBaselineError):
            self.core.adopt_reconciled_continuation_baseline(
                source.task_id, inspection.task_id
            )

        other_repo = make_repo(self.root, "other-workspace")
        self._create_project(other_repo, project_id="project-other")
        other = self._finish_inspection("project-other", task_id="task-other")
        with self.assertRaises(ContinuationBaselineError):
            self.core.adopt_reconciled_continuation_baseline(
                source.task_id, other.task_id
            )

        running = self.core.create_task(
            "project-adoption",
            "unfinished inspection",
            task_id="task-running-inspection",
            mode=TaskMode.READ_ONLY,
        )
        self.store.transition_task_running(
            running.task_id, project_id=running.project_id
        )
        with self.assertRaises(ContinuationBaselineError):
            self.core.adopt_reconciled_continuation_baseline(
                source.task_id, running.task_id
            )

        dirty_inspection = self._finish_inspection(
            task_id="task-dirty-inspection", violation=True
        )
        with self.assertRaises(ContinuationBaselineError):
            self.core.adopt_reconciled_continuation_baseline(
                source.task_id, dirty_inspection.task_id
            )
        self.assertEqual(self._adoption_events(source.task_id), [])
        self.assertEqual(git(repo, "status", "--porcelain"), "M app.txt")

    def test_source_policy_violation_cannot_be_adopted(self) -> None:
        _, source, inspection, _, _ = self._seed_source(source_violation=True)
        with self.assertRaises(ContinuationBaselineError):
            self.core.adopt_reconciled_continuation_baseline(
                source.task_id, inspection.task_id
            )
        self.assertEqual(self._adoption_events(source.task_id), [])

    def test_source_must_be_the_latest_autonomous_candidate(self) -> None:
        _, source, inspection, _, source_postflight = self._seed_source()
        newer = self.core.create_task(
            "project-adoption",
            "newer autonomous task",
            task_id="task-newer",
            mode=TaskMode.AUTONOMOUS_WRITE,
        )
        self.store.transition_task_running(newer.task_id, project_id=newer.project_id)
        source_payload = next(
            event.payload
            for event in self.store.list_task_events(source.task_id)
            if event.event_id == source_postflight.event_id
        )
        self.store.append_task_event(
            newer.task_id, "bridge", "policy.postflight", dict(source_payload)
        )
        self.store.transition_task_terminal(
            newer.task_id,
            execution_status=ExecutionStatus.FINISHED,
            event_kind="task.finished",
            payload={"final_response": "NEWER"},
        )

        with self.assertRaises(ContinuationBaselineError):
            self.core.adopt_reconciled_continuation_baseline(
                source.task_id, inspection.task_id
            )
        self.assertEqual(self._adoption_events(source.task_id), [])

    def test_mcp_adapter_marks_the_new_adoption_event_critical(self) -> None:
        adapter = MCPAdapter(self.core, self.store)
        self.assertIn(
            "reconciliation.baseline_adopted", adapter._critical_event_kinds()
        )
        self.assertNotIn(
            "policy.reconciliation_baseline_adopted", adapter._critical_event_kinds()
        )

    async def test_mcp_adapter_dispatches_explicit_adoption(self) -> None:
        _, source, inspection, _, _ = self._seed_source()
        adapter = MCPAdapter(self.core, self.store)

        result = await adapter.call_tool(
            "adopt_reconciled_continuation_baseline",
            {
                "source_task_id": source.task_id,
                "inspection_task_id": inspection.task_id,
            },
        )

        self.assertTrue(result["adopted"])
        self.assertEqual(result["source_task_id"], source.task_id)
        self.assertEqual(result["inspection_task_id"], inspection.task_id)

    async def test_mcp_adapter_dispatches_direct_adoption_without_inspection(self) -> None:
        _, source, inspection, _, _ = self._seed_source(
            truncated=False, create_inspection=False
        )
        self.assertIsNone(inspection)
        adapter = MCPAdapter(self.core, self.store)

        result = await adapter.call_tool(
            "adopt_reconciled_continuation_baseline",
            {"source_task_id": source.task_id, "mode": "direct"},
        )

        self.assertTrue(result["adopted"])
        self.assertEqual(result["adoption_mode"], "direct")
        self.assertIsNone(result["inspection_task_id"])


if __name__ == "__main__":
    unittest.main()
