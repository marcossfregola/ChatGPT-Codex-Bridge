[CmdletBinding()]
param(
    [ValidateRange(1, 120)]
    [int]$GracePeriodSeconds = 20
)

if ($PSVersionTable.PSVersion.Major -lt 7 -or $PSVersionTable.PSEdition -ne "Core") {
    throw "This script requires PowerShell 7+. Run it with pwsh."
}

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$localAppData = $env:LOCALAPPDATA
if ([string]::IsNullOrWhiteSpace($localAppData)) {
    throw "LOCALAPPDATA is required."
}

$runtimeRoot = Join-Path $localAppData "ChatGPTCodexBridge"
$stateRoot = Join-Path $runtimeRoot "state"
$dbPath = Join-Path $stateRoot "bridge.sqlite3"
$pidFile = $dbPath + ".execution-worker.pid"
$stateFile = $dbPath + ".execution-worker.state.json"
$stopFile = $dbPath + ".execution-worker.stop"
$python = Join-Path (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)) ".venv\Scripts\python.exe"
$expectedPython = $null

function Get-PidFromFile {
    param([string]$Path)

    if (!(Test-Path -LiteralPath $Path -PathType Leaf)) {
        return 0
    }
    $value = (Get-Content -LiteralPath $Path -Raw).Trim()
    $parsed = 0
    if (![int]::TryParse($value, [ref]$parsed) -or $parsed -le 0) {
        throw "The execution-worker PID file is invalid; refusing to stop any process."
    }
    return $parsed
}

function Get-PidFromState {
    if (!(Test-Path -LiteralPath $stateFile -PathType Leaf)) {
        return 0
    }
    try {
        $state = Get-Content -LiteralPath $stateFile -Raw | ConvertFrom-Json
        $parsed = 0
        if ([int]::TryParse([string]$state.pid, [ref]$parsed) -and $parsed -gt 0) {
            return $parsed
        }
    }
    catch {
        return 0
    }
    return 0
}

function Get-WorkerProcess {
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
        throw "The recorded PID executable cannot be verified; refusing to stop it."
    }
    if ([string]::IsNullOrWhiteSpace($actualPath) -or
        ((Resolve-Path -LiteralPath $actualPath).Path -ine $expectedPython)) {
        throw "The recorded PID is not the Bridge .venv Python worker; refusing to stop it."
    }
    $records = @(Get-CimInstance Win32_Process -Filter ("ProcessId = {0}" -f $ProcessId) -OperationTimeoutSec 3 -ErrorAction Stop)
    if ($records.Count -ne 1) {
        throw "CIM command line is unavailable for worker PID $ProcessId; refusing partial identity."
    }
    $record = $records[0]
    $commandLine = [string]$record.CommandLine
    if ([string]::IsNullOrWhiteSpace($commandLine) -or
        $commandLine -notmatch "chatgpt_codex_bridge\.execution_worker" -or
        $commandLine -notmatch [regex]::Escape($dbPath)) {
        throw "The recorded PID is not the D3 execution worker; refusing to stop it."
    }
    $process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if ($null -eq $process) {
        return $null
    }
    return $process
}

try {
    if (!(Test-Path -LiteralPath $python -PathType Leaf)) {
        throw "The Bridge .venv Python runtime does not exist: $python"
    }
    $expectedPython = ((& $python -B -c "import sys; print(sys._base_executable)" 2>$null) -join "").Trim()
    if ([string]::IsNullOrWhiteSpace($expectedPython)) {
        $expectedPython = (Resolve-Path -LiteralPath $python).Path
    }
    $workerPid = Get-PidFromFile $pidFile
    if ($workerPid -le 0) {
        $workerPid = Get-PidFromState
    }
    if ($workerPid -le 0) {
        Remove-Item -LiteralPath $stopFile -Force -ErrorAction SilentlyContinue
        Write-Output "WORKER_NOT_RUNNING"
        exit 0
    }

    $workerProcess = Get-WorkerProcess $workerPid
    if ($null -eq $workerProcess) {
        if (Test-Path -LiteralPath $pidFile -PathType Leaf) {
            Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
        }
        Remove-Item -LiteralPath $stopFile -Force -ErrorAction SilentlyContinue
        Write-Output "WORKER_NOT_RUNNING_STALE_STATE_CLEANED"
        Write-Output ("WORKER_PID=" + $workerPid)
        exit 0
    }

    [IO.File]::WriteAllText(
        $stopFile,
        "{`"requested_by`":`"stop_execution_worker`"}" + [Environment]::NewLine,
        [Text.UTF8Encoding]::new($false)
    )
    Write-Output ("WORKER_STOP_REQUESTED_PID=" + $workerPid)

    $deadline = (Get-Date).AddSeconds($GracePeriodSeconds)
    while ((Get-Date) -lt $deadline) {
        $workerProcess.Refresh()
        if ($workerProcess.HasExited) {
            break
        }
        Start-Sleep -Milliseconds 200
    }

    $workerProcess.Refresh()
    if (!$workerProcess.HasExited) {
        throw "Execution worker did not stop within the grace period; no process was killed."
    }
    $exitCode = "unknown"
    try {
        $candidateExitCode = [string]$workerProcess.ExitCode
        if (![string]::IsNullOrWhiteSpace($candidateExitCode)) {
            $exitCode = $candidateExitCode
        }
    }
    catch {
        # Process objects do not expose an exit code consistently on Windows.
    }

    if (Test-Path -LiteralPath $pidFile -PathType Leaf) {
        $recordedPid = (Get-Content -LiteralPath $pidFile -Raw).Trim()
        if ($recordedPid -eq [string]$workerPid) {
            Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
        }
    }
    Remove-Item -LiteralPath $stopFile -Force -ErrorAction SilentlyContinue
    Write-Output "WORKER_STOPPED"
    Write-Output ("WORKER_PID=" + $workerPid)
    Write-Output ("WORKER_EXIT_CODE=" + $exitCode)
    if ($exitCode -ne "0" -and $exitCode -ne "unknown") {
        throw "Execution worker stopped with exit code $exitCode."
    }
}
catch {
    Write-Error $_.Exception.Message
    exit 1
}
