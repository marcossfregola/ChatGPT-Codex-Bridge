from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
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
