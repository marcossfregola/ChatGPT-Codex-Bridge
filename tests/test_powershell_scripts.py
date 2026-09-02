from __future__ import annotations

import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = (
    ROOT / "scripts" / "start_mcp_tunnel.ps1",
    ROOT / "scripts" / "stop_mcp_tunnel.ps1",
    ROOT / "scripts" / "doctor_mcp_tunnel.ps1",
    ROOT / "scripts" / "start_execution_worker.ps1",
    ROOT / "scripts" / "stop_execution_worker.ps1",
    ROOT / "scripts" / "doctor_execution_worker.ps1",
    ROOT / "scripts" / "start_runtime.ps1",
    ROOT / "scripts" / "stop_runtime.ps1",
    ROOT / "scripts" / "reset_bridge.ps1",
)
GUARD_MESSAGE = "This script requires PowerShell 7+. Run it with pwsh."


class PowerShellScriptTests(unittest.TestCase):
    def test_start_and_doctor_guard_before_runtime_side_effects(self) -> None:
        for script in SCRIPTS:
            text = script.read_text(encoding="utf-8")
            guard = text.index("$PSVersionTable.PSVersion.Major")
            self.assertIn(GUARD_MESSAGE, text)
            self.assertLess(guard, text.index("Set-StrictMode"))
            first_side_effect = "New-Item" if "New-Item" in text else "Write-Output"
            self.assertLess(guard, text.index(first_side_effect))
            if "ProcessStartInfo" in text:
                self.assertLess(guard, text.index("ProcessStartInfo"))
            if "ArgumentList.Add" in text:
                self.assertLess(guard, text.index("ArgumentList.Add"))

    def test_process_identity_helpers_have_no_partial_fallback(self) -> None:
        for script_name in ("start_execution_worker.ps1", "stop_execution_worker.ps1", "reset_bridge.ps1"):
            text = (ROOT / "scripts" / script_name).read_text(encoding="utf-8").lower()
            self.assertNotIn("verified_" + "fallback", text)
            self.assertNotIn("safe " + "fallback", text)

    def test_tunnel_doctor_uses_ephemeral_health_probe_port(self) -> None:
        text = (ROOT / "scripts" / "doctor_mcp_tunnel.ps1").read_text(encoding="utf-8")
        self.assertIn('[void]$startInfo.ArgumentList.Add("--health.listen-addr")', text)
        self.assertIn('[void]$startInfo.ArgumentList.Add("127.0.0.1:0")', text)

    def test_runtime_start_uses_supported_tunnel_health_readiness(self) -> None:
        start_runtime = (ROOT / "scripts" / "start_runtime.ps1").read_text(encoding="utf-8")
        start_tunnel = (ROOT / "scripts" / "start_mcp_tunnel.ps1").read_text(encoding="utf-8")

        self.assertNotIn("$healthFile", start_tunnel)
        for text in (start_runtime, start_tunnel):
            self.assertIn('"health"', text)
            self.assertIn('"--port"', text)
            self.assertIn('"--pid-file"', text)
            self.assertIn('"--require-control-plane-poll"', text)
            self.assertIn('"--json"', text)
        self.assertIn(
            "Test-TunnelClientReadiness `\n            -TunnelClientPath $expected",
            start_runtime,
        )
        self.assertIn(
            "Test-TunnelClientReadiness `\n                -TunnelClientPath $resolvedTunnelClient",
            start_tunnel,
        )
        self.assertIn("supported health readiness", start_tunnel)

    def _isolated_tunnel_probe_context(
        self, directory: str
    ) -> tuple[str, Path, Path, Path, Path]:
        pwsh = shutil.which("pwsh")
        if pwsh is None:
            self.skipTest("PowerShell 7 unavailable")

        runtime_root = Path(directory) / "ChatGPTCodexBridge"
        tunnel_client_root = runtime_root / "tunnel-client"
        tunnel_state = runtime_root / "tunnel-state"
        tunnel_client_root.mkdir(parents=True)
        tunnel_state.mkdir(parents=True)
        tunnel_client = tunnel_client_root / "tunnel-client.cmd"
        tunnel_client.write_text(
            "@echo off\r\n"
            "if /I not \"%~1\"==\"health\" exit /b 1\r\n"
            "if /I not \"%~2\"==\"--port\" exit /b 1\r\n"
            "if \"%~3\"==\"8877\" exit /b 0\r\n"
            "exit /b 1\r\n",
            encoding="ascii",
        )
        pid_file = tunnel_state / "tunnel.pid"
        pid_file.write_text("12345\n", encoding="ascii")
        health_file = tunnel_state / "health.url"
        return pwsh, tunnel_client, pid_file, health_file, ROOT / "scripts" / "start_mcp_tunnel.ps1"

    def _run_tunnel_readiness_probe(
        self,
        *,
        pwsh: str,
        tunnel_client: Path,
        pid_file: Path,
        health_port: int,
        expected_ready: bool,
        readiness_function: str,
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory(prefix="bridge readiness ") as temp_dir:
            harness = Path(temp_dir) / "readiness harness.ps1"
            harness.write_text(
                """param(
    [Parameter(Mandatory)][string]$TunnelClientPath,
    [Parameter(Mandatory)][int]$HealthPort,
    [Parameter(Mandatory)][string]$TunnelPidFile,
    [Parameter(Mandatory)][string]$ExpectedReady
)
$ErrorActionPreference = "Stop"
"""
                + readiness_function
                + """
$expected = [bool]::Parse($ExpectedReady)
$ready = Test-TunnelClientReadiness -TunnelClientPath $TunnelClientPath -HealthPort $HealthPort -TunnelPidFile $TunnelPidFile
Write-Output ("READINESS=" + $ready)
if ($ready -ne $expected) {
    exit 2
}
if ($ready) {
    exit 0
}
exit 1
""",
                encoding="utf-8",
            )
            return subprocess.run(
                [
                    pwsh,
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(harness),
                    "-TunnelClientPath",
                    str(tunnel_client),
                    "-HealthPort",
                    str(health_port),
                    "-TunnelPidFile",
                    str(pid_file),
                    "-ExpectedReady",
                    str(expected_ready).lower(),
                ],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )

    def _readiness_function_source(self, script_path: Path) -> str:
        text = script_path.read_text(encoding="utf-8")
        function_start = text.index("function Test-TunnelClientReadiness")
        function_end = text.index("$cipher = $null", function_start)
        return text[function_start:function_end]

    def _run_powershell_script(
        self,
        script_path: Path,
        local_app_data: str,
        *arguments: str,
    ) -> subprocess.CompletedProcess[str]:
        pwsh = shutil.which("pwsh")
        if pwsh is None:
            self.skipTest("PowerShell 7 unavailable")
        environment = dict(os.environ)
        environment["LOCALAPPDATA"] = local_app_data
        return subprocess.run(
            [
                pwsh,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script_path),
                *arguments,
            ],
            cwd=str(ROOT),
            env=environment,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=15,
            check=False,
        )

    @unittest.skipUnless(shutil.which("pwsh"), "PowerShell 7 unavailable")
    def test_stop_is_idempotent_without_pid_file(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bridge no tunnel pid ") as directory:
            completed = self._run_powershell_script(
                ROOT / "scripts" / "stop_mcp_tunnel.ps1",
                directory,
            )

            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertIn("No ChatGPT-Codex tunnel PID file exists", completed.stdout)

    @unittest.skipUnless(shutil.which("pwsh"), "PowerShell 7 unavailable")
    def test_stop_treats_dead_pid_as_safe_idempotent_state(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bridge stale tunnel ") as directory:
            tunnel_state = Path(directory) / "ChatGPTCodexBridge" / "tunnel-state"
            tunnel_state.mkdir(parents=True)
            pid_file = tunnel_state / "tunnel.pid"
            health_file = tunnel_state / "health.url"
            pid_file.write_text("2147483647\n", encoding="ascii")
            health_file.write_text("http://127.0.0.1:8877\n", encoding="ascii")

            completed = self._run_powershell_script(
                ROOT / "scripts" / "stop_mcp_tunnel.ps1",
                directory,
            )

            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertIn("already stopped", completed.stdout)
            self.assertFalse(pid_file.exists())
            self.assertFalse(health_file.exists())

    @unittest.skipUnless(shutil.which("pwsh"), "PowerShell 7 unavailable")
    def test_stop_fails_closed_for_invalid_pid(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bridge invalid tunnel pid ") as directory:
            tunnel_state = Path(directory) / "ChatGPTCodexBridge" / "tunnel-state"
            tunnel_state.mkdir(parents=True)
            pid_file = tunnel_state / "tunnel.pid"
            health_file = tunnel_state / "health.url"
            pid_file.write_text("not-a-pid\n", encoding="ascii")
            health_file.write_text("http://127.0.0.1:8877\n", encoding="ascii")

            completed = self._run_powershell_script(
                ROOT / "scripts" / "stop_mcp_tunnel.ps1",
                directory,
            )

            output = completed.stdout + completed.stderr
            self.assertNotEqual(completed.returncode, 0, output)
            self.assertIn("PID file is invalid", output)
            self.assertTrue(pid_file.exists())
            self.assertTrue(health_file.exists())
            self.assertNotIn("TASKKILL:", output)

    @unittest.skipUnless(shutil.which("pwsh"), "PowerShell 7 unavailable")
    def test_stop_fails_closed_for_live_unrelated_pid(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bridge unrelated tunnel ") as directory:
            tunnel_root = Path(directory) / "ChatGPTCodexBridge"
            tunnel_state = tunnel_root / "tunnel-state"
            tunnel_client_root = tunnel_root / "tunnel-client"
            tunnel_state.mkdir(parents=True)
            tunnel_client_root.mkdir(parents=True)
            (tunnel_client_root / "tunnel-client.exe").write_text("test placeholder", encoding="ascii")
            (tunnel_state / "tunnel.pid").write_text(str(os.getpid()) + "\n", encoding="ascii")

            completed = self._run_powershell_script(
                ROOT / "scripts" / "stop_mcp_tunnel.ps1",
                directory,
            )

            output = completed.stdout + completed.stderr
            self.assertNotEqual(completed.returncode, 0, output)
            self.assertIn("not the expected ChatGPT-Codex tunnel-client.exe", output)
            self.assertNotIn("TASKKILL:", output)
            self.assertTrue((tunnel_state / "tunnel.pid").exists())

    @unittest.skipUnless(shutil.which("pwsh"), "PowerShell 7 unavailable")
    def test_stop_terminates_only_verified_direct_tunnel_tree(self) -> None:
        pwsh = shutil.which("pwsh")
        comspec = os.environ.get("COMSPEC")
        if pwsh is None or not comspec:
            self.skipTest("PowerShell and COMSPEC are required")

        with tempfile.TemporaryDirectory(prefix="bridge verified tunnel ") as directory:
            temp_root = Path(directory)
            tunnel_root = temp_root / "ChatGPTCodexBridge"
            tunnel_state = tunnel_root / "tunnel-state"
            tunnel_client_root = tunnel_root / "tunnel-client"
            tunnel_state.mkdir(parents=True)
            tunnel_client_root.mkdir(parents=True)
            tunnel_client = tunnel_client_root / "tunnel-client.exe"
            shutil.copy2(comspec, tunnel_client)

            child_script = temp_root / "direct tunnel child.ps1"
            child_pid_file = temp_root / "direct tunnel child.pid"
            child_script.write_text(
                """param([Parameter(Mandatory)][string]$PidPath)
$ErrorActionPreference = "Stop"
[System.IO.File]::WriteAllText($PidPath, [string]$PID, [System.Text.UTF8Encoding]::new($false))
Start-Sleep -Seconds 30
""",
                encoding="utf-8",
            )
            child_command = subprocess.list2cmdline(
                [
                    pwsh,
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(child_script),
                    str(child_pid_file),
                ]
            )
            tunnel_command = (
                f"{subprocess.list2cmdline([str(tunnel_client)])} "
                f"/c \"{child_command}\""
            )
            tunnel_process = subprocess.Popen(
                tunnel_command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            child_pid: int | None = None
            try:
                for _ in range(100):
                    if child_pid_file.exists():
                        break
                    if tunnel_process.poll() is not None:
                        self.fail("verified tunnel test process exited before publishing its child PID")
                    time.sleep(0.05)
                self.assertTrue(child_pid_file.exists())
                child_pid = int(child_pid_file.read_text(encoding="ascii").strip())

                (tunnel_state / "tunnel.pid").write_text(str(tunnel_process.pid) + "\n", encoding="ascii")
                (tunnel_state / "health.url").write_text("http://127.0.0.1:8877\n", encoding="ascii")
                completed = self._run_powershell_script(
                    ROOT / "scripts" / "stop_mcp_tunnel.ps1",
                    directory,
                )

                output = completed.stdout + completed.stderr
                if completed.returncode != 0 and (
                    "acceso denegado" in output.lower()
                    or "access is denied" in output.lower()
                ):
                    self.skipTest(
                        "taskkill.exe cannot terminate test processes in this environment"
                    )
                self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
                self.assertIn("Stopped ChatGPT-Codex direct tunnel PID", completed.stdout)
                self.assertIsNotNone(tunnel_process.poll())
                child_completed = subprocess.run(
                    [
                        pwsh,
                        "-NoLogo",
                        "-NoProfile",
                        "-NonInteractive",
                        "-Command",
                        f"if (Get-Process -Id {child_pid} -ErrorAction SilentlyContinue) {{ exit 1 }} else {{ exit 0 }}",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                self.assertEqual(child_completed.returncode, 0, child_completed.stdout + child_completed.stderr)
                self.assertFalse((tunnel_state / "tunnel.pid").exists())
                self.assertFalse((tunnel_state / "health.url").exists())
            finally:
                if child_pid is not None:
                    try:
                        os.kill(child_pid, signal.SIGTERM)
                    except (PermissionError, ProcessLookupError):
                        pass
                if tunnel_process.poll() is None:
                    tunnel_process.terminate()
                    try:
                        tunnel_process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        tunnel_process.kill()
                        tunnel_process.wait(timeout=5)

    def test_stop_does_not_widen_process_termination_scope(self) -> None:
        stop_tunnel = (ROOT / "scripts" / "stop_mcp_tunnel.ps1").read_text(encoding="utf-8")
        stop_runtime = (ROOT / "scripts" / "stop_runtime.ps1").read_text(encoding="utf-8")
        stop_worker = (ROOT / "scripts" / "stop_execution_worker.ps1").read_text(encoding="utf-8")

        self.assertNotIn("Stop-Process", stop_tunnel)
        self.assertNotIn("Get-Process -Name", stop_tunnel)
        self.assertIn("Get-Process -Id", stop_tunnel)
        self.assertIn("taskkill.exe", stop_tunnel)
        self.assertIn("/PID", stop_tunnel)
        self.assertIn("/T", stop_tunnel)
        self.assertIn("/F", stop_tunnel)
        self.assertNotIn("RuntimeAlias", stop_tunnel)
        self.assertNotIn("runtimes", stop_tunnel.lower())
        self.assertNotIn("Stop-Process", stop_runtime)
        self.assertNotIn("TunnelRuntimeAlias", stop_runtime)
        self.assertNotIn("runtimes", stop_runtime.lower())
        self.assertIn("worker_then_direct_tunnel", stop_runtime)
        self.assertLess(
            stop_runtime.index("$workerOutput = &"),
            stop_runtime.index("$tunnelOutput = &"),
        )
        self.assertNotIn("Stop-Process", stop_worker)

    @unittest.skipUnless(shutil.which("pwsh"), "PowerShell 7 unavailable")
    def test_startup_approves_ready_runtime_without_health_file(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bridge readiness fixture ") as directory:
            pwsh, tunnel_client, pid_file, health_file, tunnel_script = (
                self._isolated_tunnel_probe_context(directory)
            )
            self.assertFalse(health_file.exists())
            completed = self._run_tunnel_readiness_probe(
                pwsh=pwsh,
                tunnel_client=tunnel_client,
                pid_file=pid_file,
                health_port=8877,
                expected_ready=True,
                readiness_function=self._readiness_function_source(tunnel_script),
            )

            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertIn("READINESS=True", completed.stdout)
            self.assertFalse(health_file.exists())

    @unittest.skipUnless(shutil.which("pwsh"), "PowerShell 7 unavailable")
    def test_startup_rejects_unready_runtime_without_health_file(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bridge readiness fixture ") as directory:
            pwsh, tunnel_client, pid_file, health_file, tunnel_script = (
                self._isolated_tunnel_probe_context(directory)
            )
            self.assertFalse(health_file.exists())
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe_socket:
                probe_socket.bind(("127.0.0.1", 0))
                unavailable_port = probe_socket.getsockname()[1]
            completed = self._run_tunnel_readiness_probe(
                pwsh=pwsh,
                tunnel_client=tunnel_client,
                pid_file=pid_file,
                health_port=unavailable_port,
                expected_ready=False,
                readiness_function=self._readiness_function_source(tunnel_script),
            )

            self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
            self.assertIn("READINESS=False", completed.stdout)
            self.assertFalse(health_file.exists())

    @unittest.skipUnless(shutil.which("pwsh"), "PowerShell 7 unavailable")
    def test_lifecycle_transport_and_wrapper_exit_with_persistent_descendant(self) -> None:
        start_runtime = (ROOT / "scripts" / "start_runtime.ps1").read_text(encoding="utf-8")
        function_start = start_runtime.index("function Invoke-LifecycleScript")
        function_end = start_runtime.index("function Test-ExistingTunnel", function_start)
        lifecycle_function = start_runtime[function_start:function_end]

        with tempfile.TemporaryDirectory(prefix="bridge argument transport ") as temp_dir:
            temp_root = Path(temp_dir)
            scripts_dir = temp_root / "scripts with spaces"
            scripts_dir.mkdir()
            child_script = scripts_dir / "start_execution_worker.ps1"
            descendant_script = scripts_dir / "persistent descendant.ps1"
            descendant_pid_path = temp_root / "descendant pid with spaces.txt"
            stop_path = temp_root / "stop signal with spaces.txt"
            ready_path = temp_root / "descendant ready with spaces.txt"
            captured_output = temp_root / "captured output with spaces.txt"
            logs_root = temp_root / "logs with spaces"
            logs_root.mkdir()
            expected_value = "value with spaces & punctuation"

            descendant_script.write_text(
                """param(
    [Parameter(Mandatory)][string]$StopPath,
    [Parameter(Mandatory)][string]$ReadyPath
)
$ErrorActionPreference = "Stop"
[System.IO.File]::WriteAllText($ReadyPath, "DESCENDANT_STARTED", [System.Text.UTF8Encoding]::new($false))
while (-not (Test-Path -LiteralPath $StopPath -PathType Leaf)) {
    Start-Sleep -Milliseconds 25
}
[System.IO.File]::WriteAllText($ReadyPath, "DESCENDANT_STOPPED", [System.Text.UTF8Encoding]::new($false))
exit 0
""",
                encoding="utf-8",
            )

            child_script.write_text(
                """param(
    [Parameter(Mandatory)][string]$DescendantPath,
    [Parameter(Mandatory)][string]$DescendantPidPath,
    [Parameter(Mandatory)][string]$StopPath,
    [Parameter(Mandatory)][string]$ReadyPath,
    [Parameter(Mandatory)][string]$OutputPath,
    [Parameter(Mandatory)][string]$Value
)
$ErrorActionPreference = "Stop"
$pwsh = (Get-Command pwsh -ErrorAction Stop).Source
$startInfo = [System.Diagnostics.ProcessStartInfo]::new()
$startInfo.FileName = $pwsh
$startInfo.WorkingDirectory = Split-Path -Parent $DescendantPath
$startInfo.UseShellExecute = $false
$startInfo.CreateNoWindow = $true
foreach ($argument in @(
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        $DescendantPath,
        "-StopPath",
        $StopPath,
        "-ReadyPath",
        $ReadyPath
    )) {
    [void]$startInfo.ArgumentList.Add([string]$argument)
}
$descendant = [System.Diagnostics.Process]::new()
try {
    $descendant.StartInfo = $startInfo
    if (-not $descendant.Start()) {
        throw "The persistent descendant could not be started."
    }
    [System.IO.File]::WriteAllText($DescendantPidPath, [string]$descendant.Id, [System.Text.UTF8Encoding]::new($false))
    [System.IO.File]::WriteAllText($OutputPath, $Value, [System.Text.UTF8Encoding]::new($false))
    Write-Output "WRAPPER_DONE"
    exit 17
}
finally {
    $descendant.Dispose()
}
""",
                encoding="utf-8",
            )

            harness = temp_root / "lifecycle harness with spaces.ps1"
            harness.write_text(
                """param(
    [Parameter(Mandatory)][string]$ChildPath,
    [Parameter(Mandatory)][string]$DescendantPath,
    [Parameter(Mandatory)][string]$DescendantPidPath,
    [Parameter(Mandatory)][string]$StopPath,
    [Parameter(Mandatory)][string]$ReadyPath,
    [Parameter(Mandatory)][string]$OutputPath,
    [Parameter(Mandatory)][string]$Value,
    [Parameter(Mandatory)][string]$LogRoot
)
$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $ChildPath
$logsRoot = $LogRoot
$pwsh = (Get-Command pwsh -ErrorAction Stop).Source
"""
                + lifecycle_function
                + """
 $descendantProcess = $null
 $stopIssued = $false
 try {
    $result = Invoke-LifecycleScript -ScriptPath $ChildPath -ArgumentList @(
    "-DescendantPath",
    $DescendantPath,
    "-DescendantPidPath",
    $DescendantPidPath,
    "-StopPath",
    $StopPath,
    "-ReadyPath",
    $ReadyPath,
    "-OutputPath",
    $OutputPath,
    "-Value",
    $Value
) -LogPrefix "argument-transport"
    $outputText = $result.Output -join [Environment]::NewLine
    $readyDeadline = (Get-Date).AddSeconds(5)
    while (
        (
            (-not (Test-Path -LiteralPath $ReadyPath -PathType Leaf)) -or
            (-not (Test-Path -LiteralPath $DescendantPidPath -PathType Leaf))
        ) -and
        ((Get-Date) -lt $readyDeadline)
    ) {
        Start-Sleep -Milliseconds 25
    }
    if (-not (Test-Path -LiteralPath $DescendantPidPath -PathType Leaf)) {
        throw "The lifecycle wrapper did not publish the persistent descendant PID."
    }
    $descendantPid = [int](Get-Content -LiteralPath $DescendantPidPath -Raw).Trim()
    $descendantProcess = Get-Process -Id $descendantPid -ErrorAction Stop
    $descendantAliveBeforeStop = -not $descendantProcess.HasExited
    Write-Output ("WRAPPER_EXIT_CODE=" + $result.ExitCode)
    Write-Output ("WRAPPER_OUTPUT_HAS_DONE=" + ($outputText -match "WRAPPER_DONE"))
    Write-Output ("DESCENDANT_ALIVE_WHEN_LAUNCHER_RETURNED=" + $descendantAliveBeforeStop)

    [System.IO.File]::WriteAllText($StopPath, "STOP", [System.Text.UTF8Encoding]::new($false))
    $stopIssued = $true
    [void]$descendantProcess.WaitForExit(5000)
    $descendantAliveAfterStop = -not $descendantProcess.HasExited
    Write-Output ("DESCENDANT_ALIVE_AFTER_STOP=" + $descendantAliveAfterStop)

    $success = (
        $result.ExitCode -eq 17 -and
        ($outputText -match "WRAPPER_DONE") -and
        $descendantAliveBeforeStop -and
        (-not $descendantAliveAfterStop)
    )
    if (-not $success) {
        exit 1
    }
    exit 0
 }
 finally {
    if (-not $stopIssued) {
        [System.IO.File]::WriteAllText($StopPath, "STOP", [System.Text.UTF8Encoding]::new($false))
    }
    if ($null -ne $descendantProcess -and -not $descendantProcess.HasExited) {
        [void]$descendantProcess.WaitForExit(5000)
    }
 }
""",
                encoding="utf-8",
            )

            completed = subprocess.run(
                [
                    shutil.which("pwsh"),
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(harness),
                    str(child_script),
                    str(descendant_script),
                    str(descendant_pid_path),
                    str(stop_path),
                    str(ready_path),
                    str(captured_output),
                    expected_value,
                    str(logs_root),
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertIn("WRAPPER_EXIT_CODE=17", completed.stdout)
            self.assertIn("WRAPPER_OUTPUT_HAS_DONE=True", completed.stdout)
            self.assertIn("DESCENDANT_ALIVE_WHEN_LAUNCHER_RETURNED=True", completed.stdout)
            self.assertIn("DESCENDANT_ALIVE_AFTER_STOP=False", completed.stdout)
            self.assertEqual(captured_output.read_text(encoding="utf-8"), expected_value)
            self.assertIn("WRAPPER_DONE", (logs_root / "argument-transport.stdout.log").read_text(encoding="utf-8"))
            self.assertEqual((logs_root / "argument-transport.stderr.log").read_text(encoding="utf-8"), "")

    @unittest.skipUnless(shutil.which("powershell.exe"), "Windows PowerShell 5.1 unavailable")
    def test_windows_powershell_51_fails_immediately_without_runtime_start(self) -> None:
        for script in SCRIPTS:
            completed = subprocess.run(
                [
                    "powershell.exe",
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(script),
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn(GUARD_MESSAGE, completed.stdout + completed.stderr)


if __name__ == "__main__":
    unittest.main()
