[CmdletBinding()]
param()

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
$dbPath = Join-Path $stateRoot "bridge.sqlite3"
$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
$expectedPython = $null
$pidFile = $dbPath + ".execution-worker.pid"
$stateFile = $dbPath + ".execution-worker.state.json"
$stopFile = $dbPath + ".execution-worker.stop"
$lockFile = $dbPath + ".execution-worker.lock"

function Get-PidFromFile {
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

function Get-StateValue {
    param(
        [AllowNull()]$State,
        [string]$Name
    )

    if ($null -ne $State -and $State.PSObject.Properties.Name -contains $Name) {
        return $State.$Name
    }
    return $null
}

$state = $null
if (Test-Path -LiteralPath $stateFile -PathType Leaf) {
    try {
        $state = Get-Content -LiteralPath $stateFile -Raw | ConvertFrom-Json
    }
    catch {
        $state = $null
    }
}

$workerPid = Get-PidFromFile $pidFile
$statePid = 0
$statePidValue = Get-StateValue $state "pid"
if ($null -ne $statePidValue) {
    [void][int]::TryParse([string]$statePidValue, [ref]$statePid)
}
if ($workerPid -le 0 -and $statePid -gt 0) {
    $workerPid = $statePid
}

$processActive = $false
$processIdentity = "NOT_FOUND"
$pythonAvailable = Test-Path -LiteralPath $python -PathType Leaf
$expectedPython = ""
if ($pythonAvailable) {
    $expectedPython = ((& $python -B -c "import sys; print(sys._base_executable)" 2>$null) -join "").Trim()
    if ([string]::IsNullOrWhiteSpace($expectedPython)) {
        $expectedPython = (Resolve-Path -LiteralPath $python).Path
    }
}
if ($workerPid -gt 0) {
    $process = Get-Process -Id $workerPid -ErrorAction SilentlyContinue
    if ($null -ne $process) {
        $actualPath = $null
        try {
            $actualPath = $process.Path
        }
        catch {
            $actualPath = $null
        }
        $pathMatches = $pythonAvailable -and
            -not [string]::IsNullOrWhiteSpace($actualPath) -and
            ((Resolve-Path -LiteralPath $actualPath).Path -ieq $expectedPython)
        $record = Get-CimInstance Win32_Process -Filter ("ProcessId = {0}" -f $workerPid) -OperationTimeoutSec 3 -ErrorAction SilentlyContinue
        $commandMatches = $false
        if ($null -ne $record) {
            $commandLine = [string]$record.CommandLine
            $commandMatches = -not [string]::IsNullOrWhiteSpace($commandLine) -and
                $commandLine -match "chatgpt_codex_bridge\.execution_worker" -and
                $commandLine -match [regex]::Escape($dbPath)
        }
        if ($pathMatches -and $commandMatches) {
            $processActive = $true
            $processIdentity = "VERIFIED"
        }
        else {
            $processIdentity = if ($null -eq $record) { "CIM_UNAVAILABLE" } else { "MISMATCH" }
        }
    }
}

$workerStatusValue = Get-StateValue $state "status"
$workerStatus = if ($null -eq $workerStatusValue) { "[missing]" } else { [string]$workerStatusValue }
$workerIdValue = Get-StateValue $state "worker_id"
$workerId = if ($null -eq $workerIdValue) { "[missing]" } else { [string]$workerIdValue }
$workerActive = $processActive -and $workerStatus -notin @("stopped", "failed")
$stateConsistent = $workerPid -gt 0 -and $statePid -eq $workerPid
$lockPresent = Test-Path -LiteralPath $lockFile -PathType Leaf
$stopRequested = Test-Path -LiteralPath $stopFile -PathType Leaf
$dbExists = Test-Path -LiteralPath $dbPath -PathType Leaf

Write-Output "WORKER_DOCTOR_BEGIN"
Write-Output ("DB_PATH=" + $dbPath)
Write-Output ("DB_EXISTS=" + $dbExists.ToString().ToLowerInvariant())
Write-Output ("PID_FILE=" + $pidFile)
Write-Output ("PID_FILE_VALUE=" + $workerPid)
Write-Output ("STATE_FILE=" + $stateFile)
Write-Output ("STATE_PID=" + $statePid)
Write-Output ("STATE_STATUS=" + $workerStatus)
Write-Output ("STATE_WORKER_ID=" + $workerId)
Write-Output ("PROCESS_IDENTITY=" + $processIdentity)
Write-Output ("WORKER_ACTIVE=" + $workerActive.ToString().ToLowerInvariant())
Write-Output ("STATE_PID_CONSISTENT=" + $stateConsistent.ToString().ToLowerInvariant())
Write-Output ("LOCK_FILE=" + $lockFile)
Write-Output ("LOCK_FILE_PRESENT=" + $lockPresent.ToString().ToLowerInvariant())
Write-Output "LOCK_OWNERSHIP=OS_HANDLE_REQUIRES_ACTIVE_PROCESS"
Write-Output ("STOP_REQUESTED=" + $stopRequested.ToString().ToLowerInvariant())

$exitCode = 0
if (!$dbExists) {
    Write-Output "DB_READ_STATUS=missing"
    $exitCode = 1
}
elseif (!(Test-Path -LiteralPath $python -PathType Leaf)) {
    Write-Output "DB_READ_STATUS=python_missing"
    $exitCode = 1
}
else {
    $dbQuery = @'
import json
from pathlib import Path
import sqlite3
import sys

db_path = sys.argv[1]
db_uri = Path(db_path).resolve().as_uri() + "?mode=ro"
connection = sqlite3.connect(db_uri, uri=True)
connection.row_factory = sqlite3.Row
try:
    connection.execute("PRAGMA query_only = ON")
    rows = connection.execute(
        """
        SELECT
            t.task_id,
            t.execution_status,
            EXISTS(
                SELECT 1 FROM task_events request_event
                WHERE request_event.task_id = t.task_id
                  AND request_event.kind = 'task.execution_requested'
            ) AS requested,
            (
                SELECT claim_event.payload_json
                FROM task_events claim_event
                WHERE claim_event.task_id = t.task_id
                  AND claim_event.kind = 'task.execution_claimed'
                ORDER BY claim_event.event_id DESC
                LIMIT 1
            ) AS claim_payload
        FROM tasks t
        WHERE t.execution_status IN ('RUNNING', 'QUEUED')
        ORDER BY t.task_id
        """
    ).fetchall()
    tasks = []
    for row in rows:
        owner = None
        if row["claim_payload"]:
            try:
                candidate = json.loads(row["claim_payload"])
                owner = candidate if isinstance(candidate, dict) else None
            except (TypeError, ValueError):
                owner = None
        tasks.append({
            "task_id": row["task_id"],
            "status": row["execution_status"],
            "requested": bool(row["requested"]),
            "owner_kind": owner.get("owner_kind") if owner else None,
            "owner_id": owner.get("owner_id") if owner else None,
            "pid": owner.get("pid") if owner else None,
        })
    print(json.dumps({"tasks": tasks}, ensure_ascii=False, separators=(",", ":")))
finally:
    connection.close()
'@
    $dbOutput = & $python -B -c $dbQuery $dbPath 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Output "DB_READ_STATUS=error"
        Write-Output ("DB_READ_ERROR=" + (($dbOutput | Select-Object -Last 3) -join " | "))
        $exitCode = 1
    }
    else {
        try {
            $dbStatus = ($dbOutput -join "`n") | ConvertFrom-Json
            $taskValues = @($dbStatus.tasks)
            $requested = @($taskValues | Where-Object { $_.status -eq "QUEUED" -and $_.requested })
            $running = @($taskValues | Where-Object { $_.status -eq "RUNNING" })
            Write-Output "DB_READ_STATUS=ok"
            Write-Output ("REQUESTED_TASK_COUNT=" + $requested.Count)
            Write-Output ("RUNNING_TASK_COUNT=" + $running.Count)
            foreach ($task in $requested) {
                Write-Output ("REQUESTED_TASK_ID=" + $task.task_id)
            }
            foreach ($task in $running) {
                Write-Output ("RUNNING_TASK_ID=" + $task.task_id)
                Write-Output ("RUNNING_OWNER=" + $task.owner_kind + ":" + $task.owner_id + ":" + $task.pid)
            }
        }
        catch {
            Write-Output "DB_READ_STATUS=invalid_output"
            $exitCode = 1
        }
    }
}

Write-Output "WORKER_DOCTOR_END"
exit $exitCode
