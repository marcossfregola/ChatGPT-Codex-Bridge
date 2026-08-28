from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from chatgpt_codex_bridge.core import BridgeCore  # noqa: E402
from chatgpt_codex_bridge.domain.models import ExecutionStatus, TaskMode  # noqa: E402
from chatgpt_codex_bridge.executors.base import ExecutionRequest, ExecutionResult  # noqa: E402
from chatgpt_codex_bridge.persistence.sqlite_store import SQLiteBridgeStore  # noqa: E402
from chatgpt_codex_bridge import readonly_git_index  # noqa: E402
from chatgpt_codex_bridge.readonly_git_index import (  # noqa: E402
    ReadOnlyGitIndexError,
    ReadOnlyGitIndexResult,
    preflight_read_only_git_index,
)


@dataclass(frozen=True)
class DescriptorState:
    owner: bytes | None
    group: bytes | None
    dacl_control: int
    acl_revision: int
    aces: tuple[tuple[int, int, int, bytes], ...]
    binary: bytes


class ReadOnlyGitIndexUnitTests(unittest.TestCase):
    def test_non_windows_is_a_noop_without_loading_pywin32(self) -> None:
        with (
            mock.patch.object(readonly_git_index.os, "name", "posix"),
            mock.patch.object(readonly_git_index, "_load_win32") as load_win32,
        ):
            result = preflight_read_only_git_index("C:/not-a-real-repo")
        self.assertEqual(result, ReadOnlyGitIndexResult("noop", "non_windows"))
        load_win32.assert_not_called()

    def test_unsupported_ace_fails_closed_before_any_write(self) -> None:
        with self.assertRaises(ReadOnlyGitIndexError) as context:
            readonly_git_index._parse_ace(((2, 0), 1, object()))
        self.assertEqual(context.exception.reason, "unsupported_ace")

    def test_failure_payload_is_bounded_to_safe_fields(self) -> None:
        payload = {
            "outcome": "failed",
            "reason": "conflicting_deny",
            "rollback_status": "rollback_verified",
        }
        self.assertEqual(
            BridgeCore._read_only_index_event_payload(
                ReadOnlyGitIndexError(
                    "post_write_verification_failed",
                    rollback_status="rollback_verified",
                )
            ),
            {
                "outcome": "failed",
                "reason": "post_write_verification_failed",
                "rollback_status": "rollback_verified",
            },
        )
        self.assertNotIn("C:\\", str(payload))


@unittest.skipUnless(os.name == "nt", "Windows ACL integration")
class ReadOnlyGitIndexWindowsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        try:
            import ntsecuritycon
            import win32api
            import win32security
        except ImportError as exc:  # pragma: no cover - Windows CI has pywin32
            self.tempdir.cleanup()
            self.skipTest(f"pywin32 unavailable: {exc}")
        self.ntsecuritycon = ntsecuritycon
        self.win32api = win32api
        self.win32security = win32security
        try:
            sid, domain, account_type = win32security.LookupAccountName(
                None, "CodexSandboxUsers"
            )
            if account_type != win32security.SidTypeAlias:
                raise RuntimeError("not a local alias")
            if domain.casefold() != win32api.GetComputerName().casefold():
                raise RuntimeError("not a local group")
            self.sandbox_sid = bytes(sid)
        except Exception as exc:
            self.tempdir.cleanup()
            self.skipTest(f"CodexSandboxUsers unavailable: {exc}")
        token = win32security.OpenProcessToken(
            win32api.GetCurrentProcess(), win32security.TOKEN_QUERY
        )
        self.current_sid = bytes(
            win32security.GetTokenInformation(token, win32security.TokenUser)[0]
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    @staticmethod
    def _git(repo: Path, *args: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        return completed.stdout.strip()

    def _repo(self, name: str) -> tuple[Path, Path]:
        repo = self.root / name
        repo.mkdir()
        self._git(repo, "init", "-b", "main")
        self._git(repo, "config", "user.name", "READ_ONLY ACL test")
        self._git(repo, "config", "user.email", "readonly-acl@example.invalid")
        (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
        self._git(repo, "add", "tracked.txt")
        self._git(repo, "commit", "-m", "initial")
        index = Path(self._git(repo, "rev-parse", "--git-path", "index"))
        if not index.is_absolute():
            index = repo / index
        return repo, index.resolve()

    def _set_controlled_dacl(self, index: Path, *, deny_mask: int | None = None) -> None:
        ws = self.win32security
        acl = ws.ACL(4096, ws.ACL_REVISION)
        if deny_mask is not None:
            acl.AddAccessDeniedAceEx(
                ws.ACL_REVISION,
                0,
                int(deny_mask),
                ws.SID(self.sandbox_sid),
            )
        acl.AddAccessAllowedAceEx(
            ws.ACL_REVISION,
            0,
            int(self.ntsecuritycon.FILE_ALL_ACCESS),
            ws.SID(self.current_sid),
        )
        ws.SetNamedSecurityInfo(
            str(index),
            ws.SE_FILE_OBJECT,
            ws.DACL_SECURITY_INFORMATION | ws.PROTECTED_DACL_SECURITY_INFORMATION,
            None,
            None,
            acl,
            None,
        )

    def _descriptor(self, index: Path) -> DescriptorState:
        ws = self.win32security
        info = (
            ws.OWNER_SECURITY_INFORMATION
            | ws.GROUP_SECURITY_INFORMATION
            | ws.DACL_SECURITY_INFORMATION
        )
        sd = ws.GetFileSecurity(str(index), info)
        owner = sd.GetSecurityDescriptorOwner()
        group = sd.GetSecurityDescriptorGroup()
        control, _revision = sd.GetSecurityDescriptorControl()
        dacl = sd.GetSecurityDescriptorDacl()
        aces = () if dacl is None else tuple(
            (
                ace[0][0],
                ace[0][1],
                ace[1],
                bytes(ace[2]),
            )
            for ace in (dacl.GetAce(i) for i in range(dacl.GetAceCount()))
        )
        return DescriptorState(
            owner=None if owner is None else bytes(owner),
            group=None if group is None else bytes(group),
            dacl_control=control
            & (
                ws.SE_DACL_PRESENT
                | ws.SE_DACL_DEFAULTED
                | ws.SE_DACL_AUTO_INHERITED
                | ws.SE_DACL_PROTECTED
            ),
            acl_revision=ws.ACL_REVISION if dacl is None else dacl.GetAclRevision(),
            aces=aces,
            binary=bytes(sd),
        )

    @staticmethod
    def _fingerprint(index: Path) -> tuple[str, int]:
        data = index.read_bytes()
        return hashlib.sha256(data).hexdigest(), len(data)

    def test_integration_a_corrected_is_idempotent_and_read_only(self) -> None:
        repo, index = self._repo("corrected")
        self._set_controlled_dacl(index)
        before = self._descriptor(index)
        before_fp = self._fingerprint(index)

        first = preflight_read_only_git_index(repo)

        self.assertEqual(first.outcome, "corrected")
        after = self._descriptor(index)
        self.assertEqual(after.owner, before.owner)
        self.assertEqual(after.group, before.group)
        self.assertEqual(after.dacl_control, before.dacl_control)
        self.assertEqual(after.acl_revision, before.acl_revision)
        new_ace = (0, 0, int(self.ntsecuritycon.FILE_GENERIC_READ), self.sandbox_sid)
        self.assertEqual(after.aces.count(new_ace), 1)
        remaining = list(after.aces)
        remaining.remove(new_ace)
        self.assertEqual(tuple(remaining), before.aces)
        write_bits = (
            self.ntsecuritycon.FILE_WRITE_DATA
            | self.ntsecuritycon.FILE_APPEND_DATA
            | self.ntsecuritycon.FILE_WRITE_EA
            | self.ntsecuritycon.FILE_WRITE_ATTRIBUTES
            | self.ntsecuritycon.DELETE
            | self.ntsecuritycon.WRITE_DAC
            | self.ntsecuritycon.WRITE_OWNER
            | 0x40000000
            | 0x10000000
        )
        self.assertEqual(int(self.ntsecuritycon.FILE_GENERIC_READ) & write_bits, 0)
        self.assertEqual(self._fingerprint(index), before_fp)

        descriptor_after_first = self._descriptor(index)
        second = preflight_read_only_git_index(repo)

        self.assertEqual(second.outcome, "already_readable")
        self.assertEqual(self._descriptor(index), descriptor_after_first)
        self.assertEqual(self._fingerprint(index), before_fp)

    def test_integration_b_conflicting_deny_fails_closed_without_mutation(self) -> None:
        repo, index = self._repo("deny")
        # FILE_READ_ATTRIBUTES is part of the minimum read mask but does not
        # prevent GetFileSecurity or the byte-level fixture fingerprint.
        self._set_controlled_dacl(
            index, deny_mask=int(self.ntsecuritycon.FILE_READ_ATTRIBUTES)
        )
        before = self._descriptor(index)
        before_fp = self._fingerprint(index)

        with self.assertRaises(ReadOnlyGitIndexError) as context:
            preflight_read_only_git_index(repo)

        self.assertEqual(context.exception.reason, "conflicting_deny")
        self.assertIsNone(context.exception.rollback_status)
        self.assertEqual(self._descriptor(index), before)
        self.assertEqual(self._fingerprint(index), before_fp)

    def test_integration_c_real_write_then_induced_verification_failure_rolls_back(self) -> None:
        repo, index = self._repo("rollback")
        self._set_controlled_dacl(index)
        before = self._descriptor(index)
        before_fp = self._fingerprint(index)
        real_set_dacl = readonly_git_index._set_dacl
        calls: list[object] = []

        def record_set_dacl(*args: object, **kwargs: object) -> None:
            calls.append(args[2] if len(args) > 2 else kwargs)
            real_set_dacl(*args, **kwargs)

        with (
            mock.patch.object(
                readonly_git_index,
                "_set_dacl",
                side_effect=record_set_dacl,
            ),
            mock.patch.object(
                readonly_git_index,
                "_capture_and_verify_after_write",
                side_effect=RuntimeError("induced verification failure"),
            ),
        ):
            with self.assertRaises(ReadOnlyGitIndexError) as context:
                preflight_read_only_git_index(repo)

        self.assertGreaterEqual(len(calls), 2)
        self.assertEqual(context.exception.reason, "post_write_verification_failed")
        self.assertEqual(context.exception.rollback_status, "rollback_verified")
        self.assertEqual(self._descriptor(index), before)
        self.assertEqual(self._descriptor(index).binary, before.binary)
        self.assertEqual(self._fingerprint(index), before_fp)


class RecordingExecutor:
    def __init__(self) -> None:
        self.requests: list[ExecutionRequest] = []

    async def run(self, request: ExecutionRequest, **_kwargs: object) -> ExecutionResult:
        self.requests.append(request)
        return ExecutionResult(
            thread_id="thread-readonly-acl",
            turn_id="turn-readonly-acl",
            status=ExecutionStatus.FINISHED,
            final_response="READ_ONLY_ACL_OK",
        )


@unittest.skipUnless(os.name == "nt", "Windows ACL integration")
class ReadOnlyGitIndexCoreTests(ReadOnlyGitIndexWindowsTests):
    def test_core_records_corrected_event_before_executor(self) -> None:
        repo, index = self._repo("core-corrected")
        self._set_controlled_dacl(index)
        store = SQLiteBridgeStore(self.root / "bridge.sqlite3")
        executor = RecordingExecutor()
        try:
            core = BridgeCore(store, executor)
            core.create_project("Project", str(repo), project_id="project-readonly-acl")
            task = core.create_task(
                "project-readonly-acl",
                "inspect the repository",
                task_id="task-readonly-acl",
                mode=TaskMode.READ_ONLY,
            )
            result = asyncio.run(core.run_task(task.task_id))
            self.assertEqual(result.execution_status, ExecutionStatus.FINISHED)
            self.assertEqual(len(executor.requests), 1)
            events = store.list_task_events(task.task_id)
            access = [
                event
                for event in events
                if event.kind == "policy.read_only_git_index_access"
            ]
            self.assertEqual(len(access), 1)
            self.assertEqual(
                access[0].payload,
                {"outcome": "corrected", "reason": "added_minimum_read"},
            )
            self.assertNotIn(str(repo), str(access[0].payload))
            self.assertNotIn(str(index), str(access[0].payload))
            self.assertLess(
                access[0].event_id,
                next(event.event_id for event in events if event.kind == "task.finished"),
            )
        finally:
            store.close()

    def test_core_fail_closed_records_event_and_never_calls_executor(self) -> None:
        repo, index = self._repo("core-deny")
        self._set_controlled_dacl(
            index, deny_mask=int(self.ntsecuritycon.FILE_READ_ATTRIBUTES)
        )
        store = SQLiteBridgeStore(self.root / "bridge-deny.sqlite3")
        executor = RecordingExecutor()
        try:
            core = BridgeCore(store, executor)
            core.create_project("Project", str(repo), project_id="project-readonly-deny")
            task = core.create_task(
                "project-readonly-deny",
                "inspect the repository",
                task_id="task-readonly-deny",
                mode=TaskMode.READ_ONLY,
            )
            with self.assertRaises(ReadOnlyGitIndexError):
                asyncio.run(core.run_task(task.task_id))
            self.assertEqual(executor.requests, [])
            events = store.list_task_events(task.task_id)
            access = next(
                event
                for event in events
                if event.kind == "policy.read_only_git_index_access"
            )
            self.assertEqual(
                access.payload,
                {"outcome": "failed", "reason": "conflicting_deny"},
            )
            self.assertNotIn(str(repo), str(access.payload))
            self.assertEqual(
                [event.kind for event in events],
                [
                    "task.created",
                    "policy.read_only_git_index_access",
                    "policy.violation",
                    "task.failed",
                ],
            )
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
