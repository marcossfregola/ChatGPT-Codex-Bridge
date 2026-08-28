from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest


REAL_CODEX = shutil.which("codex")


def _process_alive(pid: int) -> bool | None:
    """Return True/False for a process, or None when Windows denies the query."""

    process_query_limited_information = 0x1000
    synchronize = 0x00100000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    open_process.restype = wintypes.HANDLE
    get_exit_code = kernel32.GetExitCodeProcess
    get_exit_code.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    get_exit_code.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    handle = open_process(
        process_query_limited_information | synchronize,
        False,
        pid,
    )
    if not handle:
        error = ctypes.get_last_error()
        if error in (5, 87):  # access denied / invalid (already gone)
            return False if error == 87 else None
        return None
    try:
        exit_code = wintypes.DWORD()
        if not get_exit_code(handle, ctypes.byref(exit_code)):
            return None
        return exit_code.value == 259  # STILL_ACTIVE
    finally:
        close_handle(handle)


@unittest.skipUnless(
    os.name == "nt" and REAL_CODEX,
    "Windows and the real codex executable are required",
)
class RealCodexAppServerLifecycleTests(unittest.TestCase):
    def test_real_app_server_terminates_after_abrupt_owner_exit(self) -> None:
        assert REAL_CODEX is not None
        owner: subprocess.Popen[bytes] | None = None
        child_pid: int | None = None
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pid_file = root / "codex.pid"
            ready_file = root / "ready"
            owner_code = (
                "from pathlib import Path\n"
                "import os, subprocess, sys, time\n"
                "codex, root, pid_file, ready_file = sys.argv[1:]\n"
                "env = dict(os.environ)\n"
                "codex_home = Path(root) / 'codex-home'\n"
                "codex_home.mkdir(parents=True, exist_ok=True)\n"
                "env['CODEX_HOME'] = str(codex_home)\n"
                "child = subprocess.Popen(\n"
                "    [codex, 'app-server', '--listen', 'stdio://'],\n"
                "    cwd=root, env=env, stdin=subprocess.PIPE,\n"
                "    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,\n"
                ")\n"
                "Path(pid_file).write_text(str(child.pid), encoding='ascii')\n"
                "deadline = time.monotonic() + 8\n"
                "while child.poll() is None and time.monotonic() < deadline:\n"
                "    time.sleep(0.1)\n"
                "Path(ready_file).write_text(\n"
                "    'ready' if child.poll() is None else 'dead',\n"
                "    encoding='ascii',\n"
                ")\n"
                "time.sleep(30)\n"
            )
            environment = dict(os.environ)
            environment["CODEX_HOME"] = str(root / "owner-codex-home")
            owner = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    owner_code,
                    REAL_CODEX,
                    str(root),
                    str(pid_file),
                    str(ready_file),
                ],
                cwd=str(root),
                env=environment,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                deadline = time.monotonic() + 10
                while not ready_file.exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(ready_file.exists(), "real app-server did not start")
                self.assertEqual(ready_file.read_text(encoding="ascii"), "ready")
                child_pid = int(pid_file.read_text(encoding="ascii"))
                self.assertIs(_process_alive(child_pid), True)

                owner.kill()
                owner.wait(timeout=10)

                deadline = time.monotonic() + 5
                classification: str | None = None
                while time.monotonic() < deadline:
                    alive = _process_alive(child_pid)
                    if alive is False:
                        classification = "REAL CHILD TERMINATES RELIABLY"
                        break
                    if alive is None:
                        classification = "REAL CHILD MAY SURVIVE OWNER"
                        break
                    time.sleep(0.1)
                if classification is None:
                    classification = "REAL CHILD MAY SURVIVE OWNER"
                print(f"REAL_ORPHAN_PROBE: {classification}")
                self.assertEqual(classification, "REAL CHILD TERMINATES RELIABLY")
            finally:
                if owner is not None and owner.poll() is None:
                    owner.kill()
                    owner.wait(timeout=10)
                if child_pid is not None and _process_alive(child_pid):
                    subprocess.run(
                        ["taskkill", "/PID", str(child_pid), "/T", "/F"],
                        capture_output=True,
                        check=False,
                    )


if __name__ == "__main__":
    unittest.main()
