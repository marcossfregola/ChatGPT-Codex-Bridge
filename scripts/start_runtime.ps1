[CmdletBinding()]
param(
    [ValidateRange(5, 120)]
    [int]$WorkerStartupTimeoutSeconds = 15,
    [ValidateRange(5, 600)]
    [int]$TunnelReadinessTimeoutSeconds = 60
)

if ($PSVersionTable.PSVersion.Major -lt 7 -or $PSVersionTable.PSEdition -ne "Core") {
    throw "This script requires PowerShell 7+. Run it with pwsh."
}

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$scriptsRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent $scriptsRoot
$workerStart = Join-Path $scriptsRoot "start_execution_worker.ps1"
$tunnelStart = Join-Path $scriptsRoot "start_mcp_tunnel.ps1"
$pwsh = (Get-Command pwsh -ErrorAction Stop).Source
$localAppData = $env:LOCALAPPDATA
if ([string]::IsNullOrWhiteSpace($localAppData)) {
    throw "LOCALAPPDATA is required."
}

$runtimeRoot = Join-Path $localAppData "ChatGPTCodexBridge"
$stateRoot = Join-Path $runtimeRoot "state"
$logsRoot = Join-Path $runtimeRoot "logs"
$dbPath = Join-Path $stateRoot "bridge.sqlite3"
$tunnelClient = Join-Path $runtimeRoot "tunnel-client\tunnel-client.exe"
$tunnelPidFile = Join-Path $runtimeRoot "tunnel-state\tunnel.pid"

New-Item -ItemType Directory -Force -Path $logsRoot | Out-Null

function Invoke-LifecycleScript {
    param(
        [string]$ScriptPath,
        [string[]]$ArgumentList,
        [string]$LogPrefix
    )

    $stdoutPath = Join-Path $logsRoot ($LogPrefix + ".stdout.log")
    $stderrPath = Join-Path $logsRoot ($LogPrefix + ".stderr.log")
    Remove-Item -LiteralPath $stdoutPath, $stderrPath -Force -ErrorAction SilentlyContinue
    $childArguments = @(
            "-NoProfile",
            "-File",
            $ScriptPath
        ) + $ArgumentList
    # Do not use Start-Process -Wait here: on Windows PowerShell 7 it can
    # wait for the complete descendant tree, which would include the
    # intentionally persistent execution worker.  Wait only for this wrapper
    # process while the worker continues independently.
    $child = Start-Process -FilePath $pwsh -ArgumentList $childArguments -WorkingDirectory $repoRoot -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath -WindowStyle Hidden -PassThru
    $child.WaitForExit()
    $output = @()
    if (Test-Path -LiteralPath $stdoutPath -PathType Leaf) {
        $output += Get-Content -LiteralPath $stdoutPath -ErrorAction SilentlyContinue
    }
    if (Test-Path -LiteralPath $stderrPath -PathType Leaf) {
        $output += Get-Content -LiteralPath $stderrPath -ErrorAction SilentlyContinue
    }
    return [pscustomobject]@{
        ExitCode = $child.ExitCode
        Output = $output
    }
}

function Test-ExistingTunnel {
    if (!(Test-Path -LiteralPath $tunnelPidFile -PathType Leaf)) {
        return $false
    }
    $pidText = (Get-Content -LiteralPath $tunnelPidFile -Raw).Trim()
    $tunnelPid = 0
    if (![int]::TryParse($pidText, [ref]$tunnelPid) -or $tunnelPid -le 0) {
        Remove-Item -LiteralPath $tunnelPidFile -Force -ErrorAction SilentlyContinue
        return $false
    }
    $process = Get-Process -Id $tunnelPid -ErrorAction SilentlyContinue
    if ($null -eq $process) {
        Remove-Item -LiteralPath $tunnelPidFile -Force -ErrorAction SilentlyContinue
        return $false
    }
    $expected = (Resolve-Path -LiteralPath $tunnelClient).Path
    $actual = $null
    try {
        $actual = $process.Path
    }
    catch {
        throw "The tunnel PID file points to a live process whose executable cannot be verified."
    }
    if ([string]::IsNullOrWhiteSpace($actual) -or
        ((Resolve-Path -LiteralPath $actual).Path -ine $expected)) {
        throw "The tunnel PID file points to a live unrelated process; refusing to stop or replace it."
    }
    return $true
}

$workerOk = $false
$tunnelOk = $false

try {
    $workerResult = Invoke-LifecycleScript $workerStart @(
        "-StartupTimeoutSeconds",
        [string]$WorkerStartupTimeoutSeconds
    ) "runtime-worker-start"
    foreach ($line in $workerResult.Output) {
        Write-Output ("WORKER: " + $line)
    }
    if ($workerResult.ExitCode -ne 0) {
        throw "execution worker start failed with exit code $($workerResult.ExitCode)"
    }
    $workerOk = $true

    if (Test-ExistingTunnel) {
        Write-Output "TUNNEL: TUNNEL_ALREADY_RUNNING"
        $tunnelOk = $true
    }
    else {
        $tunnelResult = Invoke-LifecycleScript $tunnelStart @(
            "-ReadinessTimeoutSeconds",
            [string]$TunnelReadinessTimeoutSeconds
        ) "runtime-tunnel-start"
        foreach ($line in $tunnelResult.Output) {
            Write-Output ("TUNNEL: " + $line)
        }
        if ($tunnelResult.ExitCode -ne 0) {
            throw "MCP tunnel start failed with exit code $($tunnelResult.ExitCode)"
        }
        $tunnelOk = $true
    }

    Write-Output "RUNTIME_START=OK"
    exit 0
}
catch {
    if ($workerOk -and !$tunnelOk) {
        Write-Output "PARTIAL_RUNTIME: worker is live; tunnel failed or was not started."
    }
    elseif (!$workerOk) {
        Write-Output "PARTIAL_RUNTIME: worker failed; tunnel was not attempted."
    }
    Write-Error $_.Exception.Message
    exit 1
}
