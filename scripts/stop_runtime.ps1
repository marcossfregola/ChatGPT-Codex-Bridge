[CmdletBinding()]
param(
    [ValidateRange(1, 120)]
    [int]$WorkerGracePeriodSeconds = 20
)

if ($PSVersionTable.PSVersion.Major -lt 7 -or $PSVersionTable.PSEdition -ne "Core") {
    throw "This script requires PowerShell 7+. Run it with pwsh."
}

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$scriptsRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$tunnelStop = Join-Path $scriptsRoot "stop_mcp_tunnel.ps1"
$workerStop = Join-Path $scriptsRoot "stop_execution_worker.ps1"
$pwsh = (Get-Command pwsh -ErrorAction Stop).Source
$exitCode = 0

Write-Output "RUNTIME_STOP_ORDER=tunnel_then_worker"

try {
    $tunnelOutput = & $pwsh -NoProfile -File $tunnelStop 2>&1
    $tunnelExit = $LASTEXITCODE
    foreach ($line in $tunnelOutput) {
        Write-Output ("TUNNEL: " + $line)
    }
    if ($tunnelExit -ne 0) {
        $exitCode = 1
        Write-Output ("TUNNEL_STOP=FAILED(" + $tunnelExit + ")")
    }
    else {
        Write-Output "TUNNEL_STOP=OK"
    }
}
catch {
    $exitCode = 1
    Write-Output ("TUNNEL_STOP=FAILED: " + $_.Exception.Message)
}

try {
    $workerOutput = & $pwsh -NoProfile -File $workerStop -GracePeriodSeconds $WorkerGracePeriodSeconds 2>&1
    $workerExit = $LASTEXITCODE
    foreach ($line in $workerOutput) {
        Write-Output ("WORKER: " + $line)
    }
    if ($workerExit -ne 0) {
        $exitCode = 1
        Write-Output ("WORKER_STOP=FAILED(" + $workerExit + ")")
    }
    else {
        Write-Output "WORKER_STOP=OK"
    }
}
catch {
    $exitCode = 1
    Write-Output ("WORKER_STOP=FAILED: " + $_.Exception.Message)
}

if ($exitCode -eq 0) {
    Write-Output "RUNTIME_STOP=OK"
}
else {
    Write-Output "RUNTIME_STOP=PARTIAL_OR_FAILED"
}
exit $exitCode
