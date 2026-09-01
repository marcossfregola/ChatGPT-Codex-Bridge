from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT / "src"))

from chatgpt_codex_bridge.core import BridgeCore  # noqa: E402
from chatgpt_codex_bridge.domain.models import ExecutionStatus, TaskMode  # noqa: E402
from chatgpt_codex_bridge.executors.base import ExecutionResult  # noqa: E402
from chatgpt_codex_bridge.persistence.sqlite_store import SQLiteBridgeStore  # noqa: E402
from chatgpt_codex_bridge.policy import (  # noqa: E402
    ContinuationBaselineError,
    WORKING_TREE_FINGERPRINT_VERSION,
    git_postflight,
    git_preflight,
    postflight_payload,
    validate_continuation_snapshot,
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
    git(repo, "config", "user.name", "Bridge Fingerprint Test")
    git(repo, "config", "user.email", "fingerprint-test@example.invalid")
    git(repo, "branch", "-M", "main")
    (repo / "app.txt").write_text("ORIGINAL\n", encoding="utf-8")
    (repo / "binary.bin").write_bytes(b"BASE\x00LINE\n")
    git(repo, "add", "app.txt", "binary.bin")
    git(repo, "commit", "-m", "initial")
    return repo


class FingerprintExecutor:
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
            final_response="FINGERPRINT_OK",
        )


class WorkingTreeFingerprintTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_same_state_has_same_v1_fingerprint(self) -> None:
        repo = make_repo(self.root)
        checkpoint = git_preflight(repo)
        (repo / "app.txt").write_text("DIRTY\n", encoding="utf-8")

        first = postflight_payload(git_postflight(checkpoint))
        second = postflight_payload(git_postflight(checkpoint))

        self.assertEqual(first["working_tree_fingerprint_version"], WORKING_TREE_FINGERPRINT_VERSION)
        self.assertEqual(first["working_tree_fingerprint"], second["working_tree_fingerprint"])

    def test_regular_content_difference_changes_fingerprint(self) -> None:
        repo = make_repo(self.root)
        checkpoint = git_preflight(repo)
        (repo / "app.txt").write_text("FIRST\n", encoding="utf-8")
        first = postflight_payload(git_postflight(checkpoint))
        (repo / "app.txt").write_text("SECOND\n", encoding="utf-8")
        second = postflight_payload(git_postflight(checkpoint))

        self.assertNotEqual(
            first["working_tree_fingerprint"], second["working_tree_fingerprint"]
        )

    def test_binary_content_difference_changes_fingerprint(self) -> None:
        repo = make_repo(self.root)
        checkpoint = git_preflight(repo)
        (repo / "binary.bin").write_bytes(b"FIRST\x00BINARY")
        first = postflight_payload(git_postflight(checkpoint))
        (repo / "binary.bin").write_bytes(b"SECOND\x00BINARY")
        second = postflight_payload(git_postflight(checkpoint))

        self.assertNotEqual(
            first["working_tree_fingerprint"], second["working_tree_fingerprint"]
        )

    def test_staged_unstaged_and_untracked_states_are_distinct(self) -> None:
        repo = make_repo(self.root)
        checkpoint = git_preflight(repo)

        (repo / "app.txt").write_text("STAGED\n", encoding="utf-8")
        git(repo, "add", "app.txt")
        staged = postflight_payload(git_postflight(checkpoint))

        git(repo, "reset", "--hard", "HEAD")
        (repo / "app.txt").write_text("STAGED\n", encoding="utf-8")
        unstaged = postflight_payload(git_postflight(checkpoint))

        git(repo, "reset", "--hard", "HEAD")
        (repo / "new.txt").write_text("UNTRACKED\n", encoding="utf-8")
        untracked = postflight_payload(git_postflight(checkpoint))

        fingerprints = {
            staged["working_tree_fingerprint"],
            unstaged["working_tree_fingerprint"],
            untracked["working_tree_fingerprint"],
        }
        self.assertEqual(len(fingerprints), 3)

    def test_more_than_256_paths_has_complete_stable_fingerprint(self) -> None:
        repo = make_repo(self.root)
        checkpoint = git_preflight(repo)
        for index in range(300):
            (repo / f"untracked-{index:03d}.txt").write_text(
                f"{index}\n", encoding="utf-8"
            )

        first = postflight_payload(git_postflight(checkpoint))
        second = postflight_payload(git_postflight(checkpoint))

        self.assertEqual(first["working_tree_fingerprint"], second["working_tree_fingerprint"])
        self.assertEqual(len(first["untracked_files"]), 257)
        self.assertEqual(first["untracked_files"][-1], "[TRUNCATED]")
        self.assertEqual(len(first["content_fingerprints"]), 257)
        self.assertEqual(first["content_fingerprints"][-1]["path"], "[TRUNCATED]")

    def test_large_diff_has_complete_stable_fingerprint(self) -> None:
        repo = make_repo(self.root)
        checkpoint = git_preflight(repo)
        (repo / "app.txt").write_text("x" * 20_000, encoding="utf-8")

        first = postflight_payload(git_postflight(checkpoint))
        second = postflight_payload(git_postflight(checkpoint))

        self.assertTrue(first["diff"].endswith("[TRUNCATED]"))
        self.assertEqual(first["working_tree_fingerprint"], second["working_tree_fingerprint"])

    def test_branch_and_head_changes_change_fingerprint(self) -> None:
        repo = make_repo(self.root)
        checkpoint = git_preflight(repo)
        (repo / "app.txt").write_text("DIRTY\n", encoding="utf-8")
        original = postflight_payload(git_postflight(checkpoint))

        git(repo, "checkout", "-b", "other")
        branch_changed = postflight_payload(git_postflight(checkpoint))
        self.assertNotEqual(
            original["working_tree_fingerprint"],
            branch_changed["working_tree_fingerprint"],
        )

        git(repo, "checkout", "main")
        git(repo, "commit", "--allow-empty", "-m", "external head")
        head_changed = postflight_payload(git_postflight(checkpoint))
        self.assertNotEqual(
            original["working_tree_fingerprint"],
            head_changed["working_tree_fingerprint"],
        )

    def test_symlink_target_is_part_of_fingerprint_when_supported(self) -> None:
        repo = make_repo(self.root)
        checkpoint = git_preflight(repo)
        link = repo / "link.txt"
        try:
            os.symlink("target-a", link)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"symlink unavailable: {error}")
        first = postflight_payload(git_postflight(checkpoint))
        link.unlink()
        os.symlink("target-b", link)
        second = postflight_payload(git_postflight(checkpoint))
        self.assertNotEqual(
            first["working_tree_fingerprint"], second["working_tree_fingerprint"]
        )

    def test_deletion_is_included_in_fingerprint(self) -> None:
        repo = make_repo(self.root)
        checkpoint = git_preflight(repo)
        (repo / "app.txt").unlink()
        deleted = postflight_payload(git_postflight(checkpoint))
        (repo / "app.txt").write_text("REPLACED\n", encoding="utf-8")
        modified = postflight_payload(git_postflight(checkpoint))

        self.assertNotEqual(
            deleted["working_tree_fingerprint"], modified["working_tree_fingerprint"]
        )

    def test_unknown_fingerprint_version_is_rejected(self) -> None:
        repo = make_repo(self.root)
        checkpoint = git_preflight(repo)
        (repo / "app.txt").write_text("DIRTY\n", encoding="utf-8")
        payload = postflight_payload(git_postflight(checkpoint))
        payload["working_tree_fingerprint_version"] = 99

        with self.assertRaises(ContinuationBaselineError):
            validate_continuation_snapshot(
                payload,
                expected_repo=repo,
                expected_branch="main",
                expected_head=git(repo, "rev-parse", "HEAD"),
            )


class FingerprintedContinuationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.store = SQLiteBridgeStore(self.root / "bridge.sqlite3")

    def tearDown(self) -> None:
        self.store.close()
        self.tempdir.cleanup()

    def create_core(self, repo: Path, executor) -> BridgeCore:
        core = BridgeCore(self.store, executor)
        core.create_project("Fingerprint", str(repo), project_id="project-fingerprint")
        return core

    def create_task(self, core: BridgeCore, task_id: str):
        return core.create_task(
            "project-fingerprint",
            "continue with the verified working tree",
            task_id=task_id,
            mode=TaskMode.AUTONOMOUS_WRITE,
        )

    async def test_truncated_previews_with_matching_fingerprint_allow_continuation(
        self,
    ) -> None:
        def create_many(path: Path) -> None:
            for index in range(300):
                (path / f"untracked-{index:03d}.txt").write_text(
                    f"{index}\n", encoding="utf-8"
                )

        repo = make_repo(self.root)
        executor = FingerprintExecutor(create_many)
        core = self.create_core(repo, executor)
        previous = self.create_task(core, "task-previous")
        await core.run_task(previous.task_id)

        postflight = next(
            event
            for event in self.store.list_task_events(previous.task_id)
            if event.kind == "policy.postflight"
        )
        self.assertEqual(
            postflight.payload["working_tree_fingerprint_version"],
            WORKING_TREE_FINGERPRINT_VERSION,
        )
        self.assertTrue(postflight.payload["untracked_files"][-1] == "[TRUNCATED]")
        fingerprint = postflight.payload["working_tree_fingerprint"]

        current = self.create_task(core, "task-current")
        result = await core.run_task(current.task_id)

        self.assertEqual(result.execution_status, ExecutionStatus.FINISHED)
        self.assertEqual(len(executor.requests), 2)
        self.assertEqual(
            postflight.payload["working_tree_fingerprint"], fingerprint
        )
        checkpoint = next(
            event
            for event in self.store.list_task_events(current.task_id)
            if event.kind == "policy.git_checkpoint"
        )
        self.assertEqual(checkpoint.payload["baseline_kind"], "continuation")
        self.assertEqual(checkpoint.payload["previous_task_id"], previous.task_id)

    async def test_truncated_diff_with_matching_fingerprint_allows_continuation(
        self,
    ) -> None:
        def create_large_change(path: Path) -> None:
            (path / "app.txt").write_text("x" * 20_000, encoding="utf-8")

        repo = make_repo(self.root)
        executor = FingerprintExecutor(create_large_change)
        core = self.create_core(repo, executor)
        previous = self.create_task(core, "task-previous")
        await core.run_task(previous.task_id)
        postflight = next(
            event
            for event in self.store.list_task_events(previous.task_id)
            if event.kind == "policy.postflight"
        )
        self.assertTrue(postflight.payload["diff"].endswith("[TRUNCATED]"))

        current = self.create_task(core, "task-current")
        result = await core.run_task(current.task_id)

        self.assertEqual(result.execution_status, ExecutionStatus.FINISHED)
        self.assertEqual(len(executor.requests), 2)

    async def test_different_fingerprint_blocks_even_with_same_diagnostics(self) -> None:
        def mutate(path: Path) -> None:
            (path / "app.txt").write_text("DIRTY\n", encoding="utf-8")

        repo = make_repo(self.root)
        executor = FingerprintExecutor(mutate)
        core = self.create_core(repo, executor)
        previous = self.create_task(core, "task-previous")
        await core.run_task(previous.task_id)
        postflight = next(
            event
            for event in self.store.list_task_events(previous.task_id)
            if event.kind == "policy.postflight"
        )
        payload = dict(postflight.payload)
        payload["working_tree_fingerprint"] = "0" * 64
        self.store.connection.execute(
            "UPDATE task_events SET payload_json = ? WHERE event_id = ?",
            (json.dumps(payload), postflight.event_id),
        )
        self.store.connection.commit()

        current = self.create_task(core, "task-current")
        with self.assertRaises(ContinuationBaselineError):
            await core.run_task(current.task_id)
        self.assertEqual(len(executor.requests), 1)

    async def test_legacy_postflight_without_fingerprint_keeps_existing_path(self) -> None:
        def mutate(path: Path) -> None:
            (path / "app.txt").write_text("DIRTY\n", encoding="utf-8")

        repo = make_repo(self.root)
        executor = FingerprintExecutor(mutate)
        core = self.create_core(repo, executor)
        previous = self.create_task(core, "task-previous")
        await core.run_task(previous.task_id)
        postflight = next(
            event
            for event in self.store.list_task_events(previous.task_id)
            if event.kind == "policy.postflight"
        )
        payload = dict(postflight.payload)
        payload.pop("working_tree_fingerprint_version")
        payload.pop("working_tree_fingerprint")
        self.store.connection.execute(
            "UPDATE task_events SET payload_json = ? WHERE event_id = ?",
            (json.dumps(payload), postflight.event_id),
        )
        self.store.connection.commit()

        current = self.create_task(core, "task-current")
        result = await core.run_task(current.task_id)
        self.assertEqual(result.execution_status, ExecutionStatus.FINISHED)
        self.assertEqual(len(executor.requests), 2)

    async def test_legacy_truncated_postflight_remains_fail_closed(self) -> None:
        def mutate(path: Path) -> None:
            for index in range(300):
                (path / f"untracked-{index:03d}.txt").write_text(
                    f"{index}\n", encoding="utf-8"
                )

        repo = make_repo(self.root)
        executor = FingerprintExecutor(mutate)
        core = self.create_core(repo, executor)
        previous = self.create_task(core, "task-previous")
        await core.run_task(previous.task_id)
        postflight = next(
            event
            for event in self.store.list_task_events(previous.task_id)
            if event.kind == "policy.postflight"
        )
        payload = dict(postflight.payload)
        payload.pop("working_tree_fingerprint_version")
        payload.pop("working_tree_fingerprint")
        self.store.connection.execute(
            "UPDATE task_events SET payload_json = ? WHERE event_id = ?",
            (json.dumps(payload), postflight.event_id),
        )
        self.store.connection.commit()

        current = self.create_task(core, "task-current")
        with self.assertRaises(ContinuationBaselineError):
            await core.run_task(current.task_id)
        self.assertEqual(len(executor.requests), 1)


if __name__ == "__main__":
    unittest.main()
