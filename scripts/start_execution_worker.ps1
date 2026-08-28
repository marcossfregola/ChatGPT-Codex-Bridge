[CmdletBinding()]
param(
    [ValidateRange(5, 120)]
    [int]$StartupTimeoutSeconds = 15
)

if ($PSVersionTable.PSVersion.Major -lt 7 -or $PSVersionTable.PSEdition -ne "Core") {
    throw "This script requires PowerShell 7+. Run it with pwsh."
}

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$localAppData = $env:LOCALAPPDATA
if ([string]::IsNullOrWhiteSpace($localAppData)) {
    throw "LOCALAPPDATA is required."
}

$runtimeRoot = Join-Path $localAppData "ChatGPTCodexBridge"
$stateRoot = Join-Path $runtimeRoot "state"
$logsRoot = Join-Path $runtimeRoot "logs"
$dbPath = Join-Path $stateRoot "bridge.sqlite3"
$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
$pidFile = $dbPath + ".execution-worker.pid"
$stateFile = $dbPath + ".execution-worker.state.json"
$stopFile = $dbPath + ".execution-worker.stop"
$lockFile = $dbPath + ".execution-worker.lock"
$stdoutLog = Join-Path $logsRoot "execution-worker.stdout.log"
$stderrLog = Join-Path $logsRoot "execution-worker.stderr.log"
$expectedPython = $null

function Get-VerifiedWorkerProcess {
    param([int]$ProcessId)

    $process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if ($null -eq $process) {
        return $null
    }
    $actualPath = $null
    try {
        $actualPath = $process.Path
    }
    catch {
        return $null
    }
    if ([string]::IsNullOrWhiteSpace($actualPath) -or
        ((Resolve-Path -LiteralPath $actualPath).Path -ine $expectedPython)) {
        return $null
    }
    $record = Get-CimInstance Win32_Process -Filter ("ProcessId = {0}" -f $ProcessId) -ErrorAction SilentlyContinue
    if ($null -eq $record) {
        # Some restricted Windows sessions deny the WMI command-line query.
        # The exact PID plus the exact Bridge .venv executable is the safe
        # fallback; the worker lock remains the authoritative duplicate guard.
        return $process
    }
    $commandLine = [string]$record.CommandLine
    if ($commandLine -notmatch "chatgpt_codex_bridge\.execution_worker") {
        return $null
    }
    if ($commandLine -notmatch [regex]::Escape($dbPath)) {
        return $null
    }
    return $process
}

function Get-ValidPidFromFile {
    param([string]$Path)

    if (!(Test-Path -LiteralPath $Path -PathType Leaf)) {
        return 0
    }
    $value = (Get-Content -LiteralPath $Path -Raw).Trim()
    $parsed = 0
    if ([int]::TryParse($value, [ref]$parsed) -and $parsed -gt 0) {
        return $parsed
    }
    return 0
}

$workerProcess = $null
try {
    if (!(Test-Path -LiteralPath $python -PathType Leaf)) {
        throw "The Bridge .venv Python runtime does not exist: $python"
    }
    $expectedPython = ((& $python -B -c "import sys; print(sys._base_executable)" 2>$null) -join "").Trim()
    if ([string]::IsNullOrWhiteSpace($expectedPython)) {
        $expectedPython = (Resolve-Path -LiteralPath $python).Path
    }

    New-Item -ItemType Directory -Force -Path $stateRoot | Out-Null
    New-Item -ItemType Directory -Force -Path $logsRoot | Out-Null

    $existingPid = Get-ValidPidFromFile $pidFile
    if ($existingPid -gt 0) {
        $existing = Get-VerifiedWorkerProcess $existingPid
        if ($null -ne $existing) {
            Write-Output "WORKER_ALREADY_RUNNING"
            Write-Output ("WORKER_PID=" + $existingPid)
            Write-Output ("DB_PATH=" + $dbPath)
            exit 0
        }
        Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
    }

    if (Test-Path -LiteralPath $stopFile -PathType Leaf) {
        Remove-Item -LiteralPath $stopFile -Force -ErrorAction SilentlyContinue
    }

    $quotedDbPath = '"' + $dbPath.Replace('"', '\"') + '"'
    $workerArguments = @(
        "-B",
        "-m",
        "chatgpt_codex_bridge.execution_worker",
        "--db-path",
        $quotedDbPath
    )
    $workerProcess = Start-Process -FilePath $python -ArgumentList $workerArguments -WorkingDirectory $repoRoot -RedirectStandardOutput $stdoutLog -RedirectStandardError $stderrLog -WindowStyle Hidden -PassThru

    $deadline = (Get-Date).AddSeconds($StartupTimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        $pidFromWorker = Get-ValidPidFromFile $pidFile
        if ($pidFromWorker -gt 0) {
            $verified = Get-VerifiedWorkerProcess $pidFromWorker
            if ($null -ne $verified) {
                Write-Output "WORKER_STARTED"
                Write-Output ("WORKER_PID=" + $pidFromWorker)
                Write-Output ("DB_PATH=" + $dbPath)
                Write-Output ("STATE_FILE=" + $stateFile)
                Write-Output ("LOCK_FILE=" + $lockFile)
                exit 0
            }
        }

        $workerProcess.Refresh()
        if ($workerProcess.HasExited) {
            $exitCode = $workerProcess.ExitCode
            if ($exitCode -eq 2) {
                Write-Output "WORKER_ALREADY_RUNNING_LOCK_HELD"
                Write-Output ("LOCK_FILE=" + $lockFile)
                exit 0
            }
            $tail = if (Test-Path -LiteralPath $stderrLog -PathType Leaf) {
                (Get-Content -LiteralPath $stderrLog -Tail 20 -ErrorAction SilentlyContinue) -join "`n"
            }
            else {
                "[empty]"
            }
            throw "Execution worker exited during startup with code $exitCode. $tail"
        }
        Start-Sleep -Milliseconds 200
    }

    throw "Execution worker did not publish a verified PID before the startup timeout."
}
catch {
    if ($null -ne $workerProcess) {
        try {
            $workerProcess.Refresh()
            if (!$workerProcess.HasExited) {
                $verified = Get-VerifiedWorkerProcess $workerProcess.Id
                if ($null -ne $verified) {
                    [IO.File]::WriteAllText(
                        $stopFile,
                        "{`"requested_by`":`"start_execution_worker`"}" + [Environment]::NewLine,
                        [Text.UTF8Encoding]::new($false)
                    )
                    $stopDeadline = (Get-Date).AddSeconds(10)
                    while ((Get-Date) -lt $stopDeadline) {
                        $workerProcess.Refresh()
                        if ($workerProcess.HasExited) {
                            break
                        }
                        Start-Sleep -Milliseconds 200
                    }
                    if (!$workerProcess.HasExited) {
                        Write-Warning "Worker startup failed and graceful cleanup timed out; no process was killed."
                    }
                }
            }
        }
        catch {
            # Preserve the startup error and never broaden process scope.
        }
    }
    Write-Error $_.Exception.Message
    exit 1
}
