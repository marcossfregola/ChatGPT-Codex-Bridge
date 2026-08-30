from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
PWSH = shutil.which("pwsh")


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


@unittest.skipUnless(os.name == "nt" and PWSH, "PowerShell 7 is required")
class WorkerScriptLifecycleTests(unittest.TestCase):
    def _run(
        self,
        script: str,
        local_app_data: str,
        *arguments: str,
        capture: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        environment = dict(os.environ)
        environment["LOCALAPPDATA"] = local_app_data
        output_options: dict[str, object]
        if capture:
            output_options = {
                "capture_output": True,
                "text": True,
                "stdin": subprocess.DEVNULL,
            }
        else:
            # A persistent worker can outlive this PowerShell process.  Do not
            # give it the test runner's capture pipes, or the pipes remain open
            # until the worker is stopped and subprocess.run cannot return.
            output_options = {
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
                "stdin": subprocess.DEVNULL,
            }
        return subprocess.run(
            [
                str(PWSH),
                "-NoProfile",
                "-File",
                str(SCRIPTS / script),
                *arguments,
            ],
            cwd=str(ROOT),
            env=environment,
            timeout=40,
            check=False,
            **output_options,
        )

    def test_start_double_start_doctor_and_controlled_stop(self) -> None:
        if not _cim_available():
            self.skipTest("strict worker lifecycle identity requires accessible CIM")
        with tempfile.TemporaryDirectory() as directory:
            first = self._run("start_execution_worker.ps1", directory, capture=False)
            self.assertEqual(first.returncode, 0)
            runtime = Path(directory) / "ChatGPTCodexBridge" / "state"
            pid_path = runtime / "bridge.sqlite3.execution-worker.pid"
            state_path = runtime / "bridge.sqlite3.execution-worker.state.json"
            self.assertTrue(pid_path.exists())
            self.assertTrue(state_path.exists())
            try:
                first_pid = pid_path.read_text(encoding="utf-8").strip()
                second = self._run("start_execution_worker.ps1", directory, capture=False)
                self.assertEqual(second.returncode, 0)
                self.assertEqual(first_pid, pid_path.read_text(encoding="utf-8").strip())

                doctor = self._run("doctor_execution_worker.ps1", directory)
                self.assertEqual(doctor.returncode, 0, doctor.stdout + doctor.stderr)
                self.assertIn("WORKER_ACTIVE=true", doctor.stdout)
                self.assertIn("STATE_PID_CONSISTENT=true", doctor.stdout)
                self.assertIn("DB_READ_STATUS=ok", doctor.stdout)
            finally:
                stopped = self._run(
                    "stop_execution_worker.ps1",
                    directory,
                    "-GracePeriodSeconds",
                    "10",
                    capture=False,
                )
                self.assertEqual(stopped.returncode, 0)

            after = self._run("doctor_execution_worker.ps1", directory)
            self.assertEqual(after.returncode, 0, after.stdout + after.stderr)
            self.assertIn("WORKER_ACTIVE=false", after.stdout)

    def test_runtime_wrappers_report_partial_start_and_stop_in_order(self) -> None:
        if not _cim_available():
            self.skipTest("strict worker lifecycle identity requires accessible CIM")
        with tempfile.TemporaryDirectory() as directory:
            started = self._run("start_runtime.ps1", directory, capture=False)
            self.assertNotEqual(started.returncode, 0)
            worker_pid = (
                Path(directory)
                / "ChatGPTCodexBridge"
                / "state"
                / "bridge.sqlite3.execution-worker.pid"
            )
            self.assertTrue(worker_pid.exists())
            first_pid = worker_pid.read_text(encoding="utf-8").strip()
            restarted = self._run("start_runtime.ps1", directory, capture=False)
            self.assertNotEqual(restarted.returncode, 0)
            self.assertEqual(first_pid, worker_pid.read_text(encoding="utf-8").strip())
            stopped = self._run(
                "stop_runtime.ps1",
                directory,
                "-WorkerGracePeriodSeconds",
                "10",
                capture=False,
            )
            self.assertEqual(stopped.returncode, 0)
            self.assertFalse(worker_pid.exists())

    def test_wrappers_and_doctor_are_scoped_to_d3_runtime(self) -> None:
        start_runtime = (SCRIPTS / "start_runtime.ps1").read_text(encoding="utf-8")
        stop_runtime = (SCRIPTS / "stop_runtime.ps1").read_text(encoding="utf-8")
        doctor = (SCRIPTS / "doctor_execution_worker.ps1").read_text(encoding="utf-8")

        self.assertLess(
            start_runtime.index("start_execution_worker.ps1"),
            start_runtime.index("start_mcp_tunnel.ps1"),
        )
        self.assertLess(
            stop_runtime.index("stop_execution_worker.ps1"),
            stop_runtime.index("stop_mcp_tunnel.ps1"),
        )
        self.assertIn("ChatGPTCodexBridge", start_runtime)
        self.assertIn("bridge.sqlite3", start_runtime)
        self.assertNotIn("ChatGPTOpenCodeBridge", start_runtime + stop_runtime + doctor)
        self.assertNotIn("VisorVideosDevBridge", start_runtime + stop_runtime + doctor)
        self.assertNotIn("Start-Process", doctor)
        self.assertNotIn("Stop-Process", doctor)
        self.assertNotIn("Remove-Item", doctor)
        self.assertIn("mode=ro", doctor)
        self.assertIn("query_only", doctor)
        self.assertNotIn("SQLiteBridgeStore", doctor)

    def test_tunnel_stop_is_fail_closed_without_managed_runtime(self) -> None:
        tunnel_stop = (SCRIPTS / "stop_mcp_tunnel.ps1").read_text(encoding="utf-8")
        tunnel_start = (SCRIPTS / "start_mcp_tunnel.ps1").read_text(encoding="utf-8")
        worker_start = (SCRIPTS / "start_execution_worker.ps1").read_text(encoding="utf-8")

        self.assertIn("RuntimeAlias", tunnel_stop)
        self.assertIn("runtimes stop", tunnel_stop)
        self.assertIn("supported local graceful shutdown", tunnel_stop)
        self.assertNotIn("Stop-Process", tunnel_stop)
        self.assertNotIn("Stop-Process -Force", tunnel_start)
        self.assertNotIn("Stop-Process -Force", worker_start)

    def test_runtime_stop_requires_explicit_managed_alias_and_stops_worker_first(self) -> None:
        stop_runtime = (SCRIPTS / "stop_runtime.ps1").read_text(encoding="utf-8")

        self.assertIn("TunnelRuntimeAlias", stop_runtime)
        self.assertIn("worker_then_managed_tunnel", stop_runtime)
        self.assertLess(
            stop_runtime.index("$workerStop"),
            stop_runtime.index("$tunnelStop"),
        )

    def test_direct_tunnel_stop_refuses_before_process_action(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / "ChatGPTCodexBridge" / "tunnel-state"
            runtime.mkdir(parents=True)
            (runtime / "tunnel.pid").write_text("12345\n", encoding="ascii")

            stopped = self._run("stop_mcp_tunnel.ps1", directory)

            self.assertNotEqual(stopped.returncode, 0)
            self.assertIn("no supported local graceful shutdown", stopped.stdout + stopped.stderr)


if __name__ == "__main__":
    unittest.main()
