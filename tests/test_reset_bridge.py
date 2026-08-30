from __future__ import annotations

from pathlib import Path
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

from chatgpt_codex_bridge.domain.models import ExecutionStatus, Project, Task
from chatgpt_codex_bridge.persistence.sqlite_store import SQLiteBridgeStore


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "reset_bridge.ps1"
PWSH = shutil.which("pwsh")
_LIVE_TEST_LAUNCHERS: list[subprocess.Popen[bytes]] = []


def _isolated_environment(local_app_data: Path, temp_root: Path) -> dict[str, str]:
    """Build a child environment with an explicitly writable isolated temp."""

    isolated_temp = temp_root / "tmp"
    isolated_temp.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ)
    environment["LOCALAPPDATA"] = str(local_app_data)
    environment["TEMP"] = str(isolated_temp)
    environment["TMP"] = str(isolated_temp)
    environment["TMPDIR"] = str(isolated_temp)
    source_root = str(ROOT / "src")
    existing_python_path = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        source_root
        if not existing_python_path
        else source_root + os.pathsep + existing_python_path
    )
    return environment


def _make_runtime(temp_root: Path) -> tuple[Path, Path]:
    """Create only the isolated D3 runtime layout used by reset tests."""

    local_app_data = temp_root / "LocalAppData"
    runtime = local_app_data / "ChatGPTCodexBridge"
    state = runtime / "state"
    tunnel_state = runtime / "tunnel-state"
    secrets = runtime / "secrets"
    profiles = runtime / "tunnel-client" / "profiles"
    for path in (state, tunnel_state, secrets, profiles):
        path.mkdir(parents=True, exist_ok=True)
    (secrets / "control-plane-api-key.dpapi").write_text(
        "not-used\n", encoding="ascii"
    )
    # A copied Python host is sufficient for identity-only tunnel processes;
    # the normal start script will fail closed before any network operation.
    shutil.copy2(
        Path(sys._base_executable),
        runtime / "tunnel-client" / "tunnel-client.exe",
    )
    (runtime / "tunnel-client" / "cloudflared.exe").write_bytes(
        b"not an executable"
    )
    (profiles / "chatgpt-codex-bridge.yaml").write_text(
        "control_plane:\n"
        "  tunnel_id: tunnel_6a8ef626bf008191a6294996145747e5\n"
        "mcp:\n"
        "  command: chatgpt_codex_bridge.mcp_server\n",
        encoding="utf-8",
    )
    return local_app_data, runtime


def _run_reset(
    environment: dict[str, str],
    *,
    timeout: int = 45,
    extra_args: list[str] | None = None,
    command_override: str | None = None,
) -> subprocess.CompletedProcess[str]:
    args = [
        str(PWSH),
        "-NoProfile",
        "-NonInteractive",
    ]
    if command_override is None:
        args.extend(["-File", str(SCRIPT)])
    else:
        args.extend(["-Command", command_override])
    if extra_args:
        args.extend(extra_args)
    output_root = Path(environment["TMPDIR"])
    output_root.mkdir(parents=True, exist_ok=True)
    run_id = uuid.uuid4().hex
    stdout_path = output_root / f"reset-{run_id}.stdout.log"
    stderr_path = output_root / f"reset-{run_id}.stderr.log"
    with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open(
        "w", encoding="utf-8"
    ) as stderr:
        completed = subprocess.run(
            args,
            cwd=str(ROOT),
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            timeout=timeout,
            check=False,
        )
    return subprocess.CompletedProcess(
        completed.args,
        completed.returncode,
        stdout_path.read_text(encoding="utf-8"),
        stderr_path.read_text(encoding="utf-8"),
    )


def _reset_output(completed: subprocess.CompletedProcess[str]) -> list[str]:
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


def _pid_is_running(pid: int) -> bool:
    probe = subprocess.run(
        [
            str(PWSH),
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            f"if (Get-Process -Id {pid} -ErrorAction SilentlyContinue) {{ exit 0 }} else {{ exit 1 }}",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    return probe.returncode == 0


def _cim_available() -> bool:
    probe = subprocess.run(
        [
            str(PWSH),
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "try { Get-CimInstance Win32_Process -ErrorAction Stop | Select-Object -First 1 | Out-Null; exit 0 } catch { exit 1 }",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    return probe.returncode == 0


def _stop_temp_worker(environment: dict[str, str]) -> None:
    completed = subprocess.run(
        [
            str(PWSH),
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(ROOT / "scripts" / "stop_execution_worker.ps1"),
            "-GracePeriodSeconds",
            "5",
        ],
        cwd=str(ROOT),
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    if completed.returncode != 0:
        # The production stop script deliberately refuses a CIM-less
        # identity. Test cleanup may terminate only the worker sidecar created
        # under this test's temporary LOCALAPPDATA, after rechecking its exact
        # Python image.
        local_app_data = Path(environment["LOCALAPPDATA"])
        pid_file = local_app_data / "ChatGPTCodexBridge" / "state" / "bridge.sqlite3.execution-worker.pid"
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text(encoding="ascii").strip())
            except ValueError:
                pid = 0
            if pid > 0:
                expected = str(Path(sys._base_executable).resolve()).replace("'", "''")
                subprocess.run(
                    [
                        str(PWSH),
                        "-NoProfile",
                        "-NonInteractive",
                        "-Command",
                        (
                            f"$p=Get-Process -Id {pid} -ErrorAction SilentlyContinue; "
                            f"if($null -ne $p -and $p.Path -ieq '{expected}') "
                            f"{{ Stop-Process -Id {pid} -Force -Confirm:$false }}"
                        ),
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                    check=False,
                )
    for launcher in list(_LIVE_TEST_LAUNCHERS):
        try:
            launcher.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        if launcher.poll() is not None:
            _LIVE_TEST_LAUNCHERS.remove(launcher)


def _start_temp_worker(local_app_data: Path, temp_root: Path) -> int:
    """Start the worker entrypoint directly under isolated environment vars."""

    environment = _isolated_environment(local_app_data, temp_root)
    launcher = subprocess.Popen(
        [
            str(ROOT / ".venv" / "Scripts" / "python.exe"),
            "-B",
            "-m",
            "chatgpt_codex_bridge.execution_worker",
            "--db-path",
            str(local_app_data / "ChatGPTCodexBridge" / "state" / "bridge.sqlite3"),
        ],
        cwd=str(ROOT),
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    pid_file = local_app_data / "ChatGPTCodexBridge" / "state" / "bridge.sqlite3.execution-worker.pid"
    for _ in range(50):
        if pid_file.exists():
            try:
                value = int(pid_file.read_text(encoding="utf-8").strip())
            except ValueError:
                value = 0
            if value > 0 and _pid_is_running(value):
                try:
                    launcher.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    _LIVE_TEST_LAUNCHERS.append(launcher)
                return value
        if launcher.poll() is not None and not pid_file.exists():
            raise AssertionError("isolated execution worker failed to start")
        time.sleep(0.1)
    if launcher.poll() is None:
        launcher.terminate()
        launcher.wait(timeout=10)
    raise AssertionError("isolated execution worker did not publish its PID")


def _stop_known_process(process: subprocess.Popen[bytes]) -> None:
    """Stop only a process created by the current test."""

    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)


def _initialize_database(runtime: Path) -> Path:
    db_path = runtime / "state" / "bridge.sqlite3"
    with SQLiteBridgeStore(db_path):
        pass
    return db_path


def _cim_failure_command() -> str:
    """Invoke reset while making process-commandline access fail closed."""

    return (
        "function global:Get-CimInstance { throw 'CIM_UNAVAILABLE_TEST' }; "
        "function global:Stop-Process { "
        "[IO.File]::WriteAllText($env:STOP_MARKER, 'CALLED'); "
        "throw 'STOP_PROCESS_CALLED' "
        "}; "
        "& $env:RESET_BRIDGE_SCRIPT -WorkerGracePeriodSeconds 2 "
        "-TunnelStopTimeoutSeconds 2 -ReadinessTimeoutSeconds 5; "
        "exit $LASTEXITCODE"
    )


def _start_fake_tunnel(
    runtime: Path, *, profile: Path | None = None
) -> subprocess.Popen[bytes]:
    executable = runtime / "tunnel-client" / "tunnel-client.exe"
    arguments = [
        str(executable),
        "-B",
        "-c",
        "import time; time.sleep(60)",
    ]
    if profile is not None:
        arguments.extend(["--profile-file", str(profile)])
    return subprocess.Popen(
        arguments,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


class ResetBridgeContractTests(unittest.TestCase):
    """Isolated contract checks for the emergency reset orchestrator.

    The real runtime is intentionally never used here.  The executable test
    only exercises the fail-closed preflight with a temporary LOCALAPPDATA;
    the remaining checks assert that every destructive branch is bounded by
    the documented identity/path guards.
    """

    def test_script_has_fail_closed_machine_contract_and_no_other_bridge_scope(self) -> None:
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("BRIDGE_RESET=PASS", text)
        self.assertIn("READY_FOR_CHATGPT=YES", text)
        self.assertIn("BRIDGE_RESET=FAIL", text)
        self.assertIn("READY_FOR_CHATGPT=NO", text)
        self.assertIn("EXTERNAL_REPOS_TOUCHED", text)
        self.assertIn("PREFLIGHT", text)
        self.assertIn("STATE_ARCHIVE", text)
        self.assertIn("STATE_RECREATED", text)
        self.assertIn("DB_INITIALIZED", text)
        self.assertIn("DOCTOR_WORKER", text)
        self.assertIn("DOCTOR_TUNNEL", text)
        self.assertNotIn("ChatGPTOpenCodeBridge", text)
        self.assertNotIn("VisorVideosDevBridge", text)
        self.assertLess(text.index("BRIDGE_RESET=PASS"), text.index("READY_FOR_CHATGPT=YES"))
        self.assertLess(text.index("BRIDGE_RESET=FAIL"), text.index("READY_FOR_CHATGPT=NO"))

    def test_identity_and_archive_guards_cover_worker_direct_and_managed_tunnel(self) -> None:
        text = SCRIPT.read_text(encoding="utf-8")
        for marker in (
            "Get-VerifiedProcess",
            "chatgpt_codex_bridge\\.execution_worker",
            "tunnel-client.exe",
            "Find-VerifiedTunnelProcess",
            "CIM command line is unavailable",
            "runtimes status",
            "runtimes stop",
            "Stop-Process -Id $candidatePid -Force",
            "Move-Item -LiteralPath $script:Paths.State",
            "Assert-ExistingChildrenContained",
            "RUNTIME_LOCK_PROBE",
            "Get-DatabaseCounts",
            "mode=ro",
            "query_only",
        ):
            self.assertIn(marker, text)
        self.assertNotIn("VERIFIED_" + "FALLBACK", text)

    def test_preflight_failure_in_temp_localappdata_is_non_destructive_and_final_lines_are_stable(self) -> None:
        if os.name != "nt" or not PWSH:
            self.skipTest("PowerShell 7 is required")
        with tempfile.TemporaryDirectory(prefix="bridge-reset-test-") as directory:
            directory_path = Path(directory)
            environment = _isolated_environment(directory_path, directory_path)
            completed = _run_reset(environment, timeout=20)
            self.assertNotEqual(completed.returncode, 0)
            lines = _reset_output(completed)
            self.assertGreaterEqual(len(lines), 3)
            self.assertEqual(lines[-2:], ["BRIDGE_RESET=FAIL", "READY_FOR_CHATGPT=NO"])
            self.assertIn("PREFLIGHT=FAIL", lines)
            self.assertFalse((Path(directory) / "ChatGPTCodexBridge").exists())

    @unittest.skipUnless(os.name == "nt" and PWSH, "PowerShell 7 is required")
    def test_isolated_archive_and_empty_database_before_tunnel_failure(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bridge-reset-runtime-") as directory:
            directory_path = Path(directory)
            local_app_data, runtime = _make_runtime(directory_path)
            state = runtime / "state"
            tunnel_state = runtime / "tunnel-state"
            # A stale PID/health pair is safe to remove after process discovery
            # finds no exact tunnel-client candidate.
            (tunnel_state / "tunnel.pid").write_text("999999\n", encoding="ascii")
            (tunnel_state / "health.url").write_text(
                "http://127.0.0.1:9\n", encoding="ascii"
            )

            old_db = state / "bridge.sqlite3"
            with SQLiteBridgeStore(old_db) as store:
                external_repo = directory_path / "external-repo"
                external_repo.mkdir()
                external_marker = external_repo / "dirty.txt"
                external_marker.write_text("untouched\n", encoding="utf-8")
                store.create_project(Project("p-old", "old", str(external_repo)))
                store.create_task(
                    Task(
                        "t-old",
                        "p-old",
                        "old running task",
                        execution_status=ExecutionStatus.RUNNING,
                    )
                )
                store.append_task_event("t-old", "test", "task.started", {"old": True})

            environment = _isolated_environment(local_app_data, directory_path)
            try:
                completed = _run_reset(
                    environment,
                    timeout=45,
                    extra_args=[
                        "-WorkerGracePeriodSeconds",
                        "5",
                        "-TunnelStopTimeoutSeconds",
                        "2",
                        "-ReadinessTimeoutSeconds",
                        "5",
                    ],
                )
                reset_output = completed.stdout
                self.assertNotEqual(completed.returncode, 0)
                self.assertIn("TUNNEL_STOP=PASS", reset_output)
                self.assertIn("STATE_ARCHIVE=", reset_output)
                if _cim_available():
                    self.assertIn("TUNNEL_START=FAIL", reset_output)
                else:
                    # On hosts where CIM is ACL-blocked, the new worker is
                    # deliberately not treated as identified; its freshly
                    # initialized DB is still inspected below.
                    self.assertIn("WORKER_START=FAIL", reset_output)
                self.assertEqual(
                    _reset_output(completed)[-2:],
                    ["BRIDGE_RESET=FAIL", "READY_FOR_CHATGPT=NO"],
                )

                archive_dirs = list((runtime / "state.archive").iterdir())
                self.assertEqual(len(archive_dirs), 1)
                archived_db = archive_dirs[0] / "bridge.sqlite3"
                self.assertTrue(archived_db.is_file())
                self.assertTrue(old_db.is_file())
                with SQLiteBridgeStore(old_db) as fresh_store:
                    self.assertEqual(fresh_store.list_projects(), [])
                    self.assertEqual(fresh_store.list_tasks("p-old"), [])
                    self.assertEqual(fresh_store.count_task_events("t-old"), 0)
                self.assertEqual(external_marker.read_text(encoding="utf-8"), "untouched\n")
            finally:
                _stop_temp_worker(environment)

    @unittest.skipUnless(os.name == "nt" and PWSH, "PowerShell 7 is required")
    def test_active_bridge_lock_fails_before_archive(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bridge-reset-lock-") as directory:
            directory_path = Path(directory)
            local_app_data, runtime = _make_runtime(directory_path)
            state = runtime / "state"
            db_path = state / "bridge.sqlite3"
            with SQLiteBridgeStore(db_path):
                pass

            holder = subprocess.Popen(
                [
                    str(ROOT / ".venv" / "Scripts" / "python.exe"),
                    "-B",
                    "-c",
                    (
                        "import sys, time; "
                        "from chatgpt_codex_bridge.single_instance import MCPInstanceLock; "
                        "lock=MCPInstanceLock(sys.argv[1]); lock.acquire(); "
                        "print('LOCKED', flush=True); time.sleep(60)"
                    ),
                    str(db_path),
                ],
                cwd=str(ROOT),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                lock_path = Path(f"{db_path}.mcp.lock")
                for _ in range(50):
                    if lock_path.exists():
                        break
                    if holder.poll() is not None:
                        self.fail("the lock-holder process exited before acquiring its lock")
                    time.sleep(0.1)
                self.assertTrue(lock_path.exists())

                environment = _isolated_environment(local_app_data, directory_path)
                output = Path(directory) / "reset.stdout.log"
                with output.open("w", encoding="utf-8") as stream:
                    completed = subprocess.run(
                        [
                            str(PWSH),
                            "-NoProfile",
                            "-NonInteractive",
                            "-File",
                            str(SCRIPT),
                            "-TunnelStopTimeoutSeconds",
                            "2",
                        ],
                        cwd=str(ROOT),
                        env=environment,
                        stdin=subprocess.DEVNULL,
                        stdout=stream,
                        stderr=subprocess.DEVNULL,
                        timeout=20,
                        check=False,
                    )
                reset_output = output.read_text(encoding="utf-8")
                self.assertNotEqual(completed.returncode, 0)
                self.assertIn("BRIDGE_RESET=FAIL", reset_output)
                self.assertIn("READY_FOR_CHATGPT=NO", reset_output)
                self.assertNotIn("STATE_ARCHIVE=", reset_output)
                self.assertTrue(db_path.is_file())
                self.assertFalse((runtime / "state.archive").exists())
            finally:
                if holder.poll() is None:
                    holder.terminate()
                    try:
                        holder.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        holder.kill()
                        holder.wait(timeout=10)

    @unittest.skipUnless(os.name == "nt" and PWSH, "PowerShell 7 is required")
    def test_worker_cim_inaccessible_refuses_kill(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bridge-reset-worker-cim-") as directory:
            directory_path = Path(directory)
            local_app_data, runtime = _make_runtime(directory_path)
            _initialize_database(runtime)
            environment = _isolated_environment(local_app_data, directory_path)
            marker = directory_path / "stop-process-called.txt"
            environment["RESET_BRIDGE_SCRIPT"] = str(SCRIPT)
            environment["STOP_MARKER"] = str(marker)
            worker_pid = _start_temp_worker(local_app_data, directory_path)
            try:
                completed = _run_reset(
                    environment,
                    timeout=20,
                    command_override=_cim_failure_command(),
                )
                output = completed.stdout
                self.assertNotEqual(completed.returncode, 0)
                self.assertIn("WORKER_STOP=FAIL", output)
                self.assertEqual(_reset_output(completed)[-2:], ["BRIDGE_RESET=FAIL", "READY_FOR_CHATGPT=NO"])
                self.assertNotIn("STATE_ARCHIVE=", output)
                self.assertTrue(_pid_is_running(worker_pid), "CIM failure must not kill the worker")
                self.assertFalse(marker.exists(), "Stop-Process must not be called on CIM failure")
            finally:
                _stop_temp_worker(environment)

    @unittest.skipUnless(os.name == "nt" and PWSH, "PowerShell 7 is required")
    def test_worker_incorrect_commandline_refuses_kill(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bridge-reset-worker-cmd-") as directory:
            directory_path = Path(directory)
            local_app_data, runtime = _make_runtime(directory_path)
            db_path = _initialize_database(runtime)
            worker = subprocess.Popen(
                [
                    str(ROOT / ".venv" / "Scripts" / "python.exe"),
                    "-B",
                    "-c",
                    "import time; time.sleep(60)",
                ],
                cwd=str(ROOT),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            pid_file = runtime / "state" / "bridge.sqlite3.execution-worker.pid"
            pid_file.write_text(f"{worker.pid}\n", encoding="ascii")
            environment = _isolated_environment(local_app_data, directory_path)
            try:
                completed = _run_reset(
                    environment,
                    timeout=20,
                    extra_args=[
                        "-WorkerGracePeriodSeconds",
                        "2",
                        "-TunnelStopTimeoutSeconds",
                        "2",
                        "-ReadinessTimeoutSeconds",
                        "5",
                    ],
                )
                output = completed.stdout
                self.assertNotEqual(completed.returncode, 0)
                self.assertIn("WORKER_STOP=FAIL", output)
                self.assertEqual(_reset_output(completed)[-2:], ["BRIDGE_RESET=FAIL", "READY_FOR_CHATGPT=NO"])
                self.assertNotIn("STATE_ARCHIVE=", output)
                self.assertTrue(_pid_is_running(worker.pid), "incorrect command line must not kill the process")
            finally:
                _stop_known_process(worker)

    @unittest.skipUnless(os.name == "nt" and PWSH, "PowerShell 7 is required")
    def test_direct_tunnel_cim_inaccessible_refuses_kill(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bridge-reset-tunnel-cim-") as directory:
            directory_path = Path(directory)
            local_app_data, runtime = _make_runtime(directory_path)
            profile = runtime / "tunnel-client" / "profiles" / "chatgpt-codex-bridge.yaml"
            tunnel = _start_fake_tunnel(runtime, profile=profile)
            tunnel_pid_file = runtime / "tunnel-state" / "tunnel.pid"
            tunnel_pid_file.write_text(f"{tunnel.pid}\n", encoding="ascii")
            environment = _isolated_environment(local_app_data, directory_path)
            marker = directory_path / "stop-process-called.txt"
            environment["RESET_BRIDGE_SCRIPT"] = str(SCRIPT)
            environment["STOP_MARKER"] = str(marker)
            try:
                completed = _run_reset(
                    environment,
                    timeout=20,
                    command_override=_cim_failure_command(),
                )
                output = completed.stdout
                self.assertNotEqual(completed.returncode, 0)
                self.assertIn("TUNNEL_STOP=FAIL", output)
                self.assertEqual(_reset_output(completed)[-2:], ["BRIDGE_RESET=FAIL", "READY_FOR_CHATGPT=NO"])
                self.assertNotIn("STATE_ARCHIVE=", output)
                self.assertTrue(_pid_is_running(tunnel.pid), "CIM failure must not kill the tunnel")
                self.assertFalse(marker.exists(), "Stop-Process must not be called on CIM failure")
            finally:
                _stop_known_process(tunnel)

    @unittest.skipUnless(os.name == "nt" and PWSH, "PowerShell 7 is required")
    def test_foreign_tunnel_pid_refuses_kill(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bridge-reset-foreign-pid-") as directory:
            directory_path = Path(directory)
            local_app_data, runtime = _make_runtime(directory_path)
            foreign = subprocess.Popen(
                [str(PWSH), "-NoProfile", "-NonInteractive", "-Command", "Start-Sleep -Seconds 60"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            (runtime / "tunnel-state" / "tunnel.pid").write_text(
                f"{foreign.pid}\n", encoding="ascii"
            )
            environment = _isolated_environment(local_app_data, directory_path)
            try:
                completed = _run_reset(environment, timeout=20)
                output = completed.stdout
                self.assertNotEqual(completed.returncode, 0)
                self.assertIn("TUNNEL_STOP=FAIL", output)
                self.assertEqual(_reset_output(completed)[-2:], ["BRIDGE_RESET=FAIL", "READY_FOR_CHATGPT=NO"])
                self.assertNotIn("STATE_ARCHIVE=", output)
                self.assertTrue(_pid_is_running(foreign.pid), "a foreign PID must not be killed")
            finally:
                _stop_known_process(foreign)

    @unittest.skipUnless(os.name == "nt" and PWSH, "PowerShell 7 is required")
    def test_direct_tunnel_wrong_profile_refuses_kill(self) -> None:
        if not _cim_available():
            self.skipTest("strict profile evidence requires accessible CIM")
        with tempfile.TemporaryDirectory(prefix="bridge-reset-tunnel-profile-") as directory:
            directory_path = Path(directory)
            local_app_data, runtime = _make_runtime(directory_path)
            wrong_profile = runtime / "tunnel-client" / "profiles" / "wrong.yaml"
            wrong_profile.write_text("not the authorized profile\n", encoding="utf-8")
            tunnel = _start_fake_tunnel(runtime, profile=wrong_profile)
            (runtime / "tunnel-state" / "tunnel.pid").write_text(
                f"{tunnel.pid}\n", encoding="ascii"
            )
            environment = _isolated_environment(local_app_data, directory_path)
            try:
                completed = _run_reset(environment, timeout=20)
                output = completed.stdout
                self.assertNotEqual(completed.returncode, 0)
                self.assertIn("TUNNEL_STOP=FAIL", output)
                self.assertEqual(_reset_output(completed)[-2:], ["BRIDGE_RESET=FAIL", "READY_FOR_CHATGPT=NO"])
                self.assertTrue(_pid_is_running(tunnel.pid))
            finally:
                _stop_known_process(tunnel)

    @unittest.skipUnless(os.name == "nt" and PWSH, "PowerShell 7 is required")
    def test_worker_complete_identity_can_be_stopped(self) -> None:
        if not _cim_available():
            self.skipTest("complete worker identity requires accessible CIM")
        with tempfile.TemporaryDirectory(prefix="bridge-reset-worker-complete-") as directory:
            directory_path = Path(directory)
            local_app_data, runtime = _make_runtime(directory_path)
            _initialize_database(runtime)
            environment = _isolated_environment(local_app_data, directory_path)
            worker_pid = _start_temp_worker(local_app_data, directory_path)
            try:
                completed = _run_reset(
                    environment,
                    timeout=30,
                    extra_args=[
                        "-WorkerGracePeriodSeconds",
                        "5",
                        "-TunnelStopTimeoutSeconds",
                        "2",
                        "-ReadinessTimeoutSeconds",
                        "5",
                    ],
                )
                output = completed.stdout
                self.assertIn("WORKER_STOP=PASS", output)
                self.assertFalse(_pid_is_running(worker_pid))
                self.assertIn("STATE_ARCHIVE=", output)
                self.assertNotIn("WORKER_STOP=FAIL", output)
            finally:
                _stop_temp_worker(environment)

    @unittest.skipUnless(os.name == "nt" and PWSH, "PowerShell 7 is required")
    def test_direct_tunnel_complete_identity_can_be_stopped(self) -> None:
        if not _cim_available():
            self.skipTest("complete tunnel identity requires accessible CIM")
        with tempfile.TemporaryDirectory(prefix="bridge-reset-tunnel-complete-") as directory:
            directory_path = Path(directory)
            local_app_data, runtime = _make_runtime(directory_path)
            profile = runtime / "tunnel-client" / "profiles" / "chatgpt-codex-bridge.yaml"
            tunnel = _start_fake_tunnel(runtime, profile=profile)
            (runtime / "tunnel-state" / "tunnel.pid").write_text(
                f"{tunnel.pid}\n", encoding="ascii"
            )
            environment = _isolated_environment(local_app_data, directory_path)
            try:
                completed = _run_reset(
                    environment,
                    timeout=30,
                    extra_args=[
                        "-WorkerGracePeriodSeconds",
                        "5",
                        "-TunnelStopTimeoutSeconds",
                        "5",
                        "-ReadinessTimeoutSeconds",
                        "5",
                    ],
                )
                output = completed.stdout
                self.assertIn("TUNNEL_STOP=PASS", output)
                self.assertFalse(_pid_is_running(tunnel.pid))
                self.assertIn("STATE_ARCHIVE=", output)
            finally:
                _stop_known_process(tunnel)

    @unittest.skipUnless(os.name == "nt" and PWSH, "PowerShell 7 is required")
    def test_missing_tunnel_pid_discovers_no_candidate_and_continues(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bridge-reset-no-tunnel-") as directory:
            directory_path = Path(directory)
            local_app_data, runtime = _make_runtime(directory_path)
            _initialize_database(runtime)
            environment = _isolated_environment(local_app_data, directory_path)
            completed = _run_reset(
                environment,
                timeout=30,
                extra_args=[
                    "-WorkerGracePeriodSeconds",
                    "2",
                    "-TunnelStopTimeoutSeconds",
                    "2",
                    "-ReadinessTimeoutSeconds",
                    "5",
                ],
            )
            output = completed.stdout
            self.assertIn("TUNNEL_STOP=PASS", output)
            self.assertIn("STATE_ARCHIVE=", output)
            self.assertEqual(_reset_output(completed)[-2:], ["BRIDGE_RESET=FAIL", "READY_FOR_CHATGPT=NO"])
            _stop_temp_worker(environment)

    @unittest.skipUnless(os.name == "nt" and PWSH, "PowerShell 7 is required")
    def test_missing_tunnel_pid_identified_candidate_is_stopped(self) -> None:
        if not _cim_available():
            self.skipTest("complete tunnel identity requires accessible CIM")
        with tempfile.TemporaryDirectory(prefix="bridge-reset-discover-tunnel-") as directory:
            directory_path = Path(directory)
            local_app_data, runtime = _make_runtime(directory_path)
            profile = runtime / "tunnel-client" / "profiles" / "chatgpt-codex-bridge.yaml"
            tunnel = _start_fake_tunnel(runtime, profile=profile)
            environment = _isolated_environment(local_app_data, directory_path)
            try:
                completed = _run_reset(
                    environment,
                    timeout=30,
                    extra_args=[
                        "-WorkerGracePeriodSeconds",
                        "2",
                        "-TunnelStopTimeoutSeconds",
                        "5",
                        "-ReadinessTimeoutSeconds",
                        "5",
                    ],
                )
                output = completed.stdout
                self.assertIn("TUNNEL_STOP=PASS", output)
                self.assertFalse(_pid_is_running(tunnel.pid))
                self.assertIn("STATE_ARCHIVE=", output)
            finally:
                _stop_known_process(tunnel)

    @unittest.skipUnless(os.name == "nt" and PWSH, "PowerShell 7 is required")
    def test_missing_tunnel_pid_ambiguous_candidate_refuses_kill(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bridge-reset-ambiguous-tunnel-") as directory:
            directory_path = Path(directory)
            local_app_data, runtime = _make_runtime(directory_path)
            tunnel = _start_fake_tunnel(runtime)
            environment = _isolated_environment(local_app_data, directory_path)
            try:
                completed = _run_reset(environment, timeout=20)
                output = completed.stdout
                self.assertNotEqual(completed.returncode, 0)
                self.assertIn("TUNNEL_STOP=FAIL", output)
                self.assertEqual(_reset_output(completed)[-2:], ["BRIDGE_RESET=FAIL", "READY_FOR_CHATGPT=NO"])
                self.assertNotIn("STATE_ARCHIVE=", output)
                self.assertTrue(_pid_is_running(tunnel.pid))
            finally:
                _stop_known_process(tunnel)

    @unittest.skipUnless(os.name == "nt" and PWSH, "PowerShell 7 is required")
    def test_double_reset_archives_each_isolated_empty_state(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bridge-reset-double-") as directory:
            directory_path = Path(directory)
            local_app_data, runtime = _make_runtime(directory_path)
            _initialize_database(runtime)
            environment = _isolated_environment(local_app_data, directory_path)
            try:
                for expected_archives in (1, 2):
                    completed = _run_reset(
                        environment,
                        timeout=30,
                        extra_args=[
                            "-WorkerGracePeriodSeconds",
                            "2",
                            "-TunnelStopTimeoutSeconds",
                            "2",
                            "-ReadinessTimeoutSeconds",
                            "5",
                        ],
                    )
                    self.assertNotEqual(completed.returncode, 0)
                    self.assertIn("TUNNEL_STOP=PASS", completed.stdout)
                    archives = list((runtime / "state.archive").iterdir())
                    self.assertEqual(len(archives), expected_archives)
                    with SQLiteBridgeStore(runtime / "state" / "bridge.sqlite3") as store:
                        self.assertEqual(store.list_projects(), [])
                        self.assertEqual(store.list_tasks("missing"), [])
                    _stop_temp_worker(environment)
            finally:
                _stop_temp_worker(environment)

    @unittest.skipUnless(os.name == "nt" and PWSH, "PowerShell 7 is required")
    def test_recovery_after_archive_cut_recreates_empty_state(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bridge-reset-after-archive-") as directory:
            directory_path = Path(directory)
            local_app_data, runtime = _make_runtime(directory_path)
            shutil.rmtree(runtime / "state")
            old_archive = runtime / "state.archive" / "interrupted-archive"
            old_archive.mkdir(parents=True)
            (old_archive / "archive.marker").write_text("preserve\n", encoding="ascii")
            environment = _isolated_environment(local_app_data, directory_path)
            try:
                completed = _run_reset(
                    environment,
                    timeout=30,
                    extra_args=[
                        "-WorkerGracePeriodSeconds",
                        "2",
                        "-TunnelStopTimeoutSeconds",
                        "2",
                        "-ReadinessTimeoutSeconds",
                        "5",
                    ],
                )
                output = completed.stdout
                self.assertNotEqual(completed.returncode, 0)
                self.assertIn("STATE_ARCHIVE=NONE", output)
                self.assertIn("STATE_RECREATED=PASS", output)
                self.assertTrue((runtime / "state" / "bridge.sqlite3").exists())
                self.assertEqual((old_archive / "archive.marker").read_text(encoding="ascii"), "preserve\n")
            finally:
                _stop_temp_worker(environment)

    @unittest.skipUnless(os.name == "nt" and PWSH, "PowerShell 7 is required")
    def test_archive_failure_preserves_original_state(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bridge-reset-archive-failure-") as directory:
            directory_path = Path(directory)
            local_app_data, runtime = _make_runtime(directory_path)
            db_path = _initialize_database(runtime)
            archive_file = runtime / "state.archive"
            archive_file.write_bytes(b"not-a-directory")
            environment = _isolated_environment(local_app_data, directory_path)
            completed = _run_reset(environment, timeout=20)
            output = completed.stdout
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("STATE_ARCHIVE=FAIL", output)
            self.assertEqual(_reset_output(completed)[-2:], ["BRIDGE_RESET=FAIL", "READY_FOR_CHATGPT=NO"])
            self.assertTrue(db_path.exists())
            self.assertEqual(archive_file.read_bytes(), b"not-a-directory")

    @unittest.skipUnless(os.name == "nt" and PWSH, "PowerShell 7 is required")
    def test_recovery_after_new_database_cut_archives_and_recreates(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bridge-reset-after-db-") as directory:
            directory_path = Path(directory)
            local_app_data, runtime = _make_runtime(directory_path)
            _initialize_database(runtime)
            environment = _isolated_environment(local_app_data, directory_path)
            try:
                completed = _run_reset(
                    environment,
                    timeout=30,
                    extra_args=[
                        "-WorkerGracePeriodSeconds",
                        "2",
                        "-TunnelStopTimeoutSeconds",
                        "2",
                        "-ReadinessTimeoutSeconds",
                        "5",
                    ],
                )
                output = completed.stdout
                self.assertNotEqual(completed.returncode, 0)
                self.assertIn("STATE_ARCHIVE=", output)
                self.assertIn("STATE_RECREATED=PASS", output)
                archives = list((runtime / "state.archive").iterdir())
                self.assertEqual(len(archives), 1)
                with SQLiteBridgeStore(runtime / "state" / "bridge.sqlite3") as store:
                    self.assertEqual(store.list_projects(), [])
                    self.assertEqual(store.list_tasks("missing"), [])
            finally:
                _stop_temp_worker(environment)


if __name__ == "__main__":
    unittest.main()
