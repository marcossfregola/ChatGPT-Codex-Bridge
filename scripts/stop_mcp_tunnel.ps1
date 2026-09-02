[CmdletBinding()]
param()

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
$tunnelClient = Join-Path $runtimeRoot "tunnel-client\tunnel-client.exe"
$pidFile = Join-Path $runtimeRoot "tunnel-state\tunnel.pid"
$healthFile = Join-Path $runtimeRoot "tunnel-state\health.url"

function Remove-DirectTunnelStaleMetadata {
    param(
        [Parameter(Mandatory)][string]$RecordedPidText
    )

    if (Test-Path -LiteralPath $pidFile) {
        if (!(Test-Path -LiteralPath $pidFile -PathType Leaf)) {
            throw "The tunnel PID path is not a regular file; refusing metadata cleanup."
        }
        $currentPidText = (Get-Content -LiteralPath $pidFile -Raw).Trim()
        if ($currentPidText -cne $RecordedPidText) {
            throw "The tunnel PID file changed during stop; refusing metadata cleanup."
        }
        Remove-Item -LiteralPath $pidFile -Force -ErrorAction Stop
    }

    if (Test-Path -LiteralPath $healthFile -PathType Leaf) {
        Remove-Item -LiteralPath $healthFile -Force -ErrorAction Stop
    }
}

if (Test-Path -LiteralPath $pidFile) {
    if (!(Test-Path -LiteralPath $pidFile -PathType Leaf)) {
        throw "The tunnel PID path is not a regular file; refusing to stop any process."
    }
}
else {
    Write-Output "No ChatGPT-Codex tunnel PID file exists."
    exit 0
}

$pidText = (Get-Content -LiteralPath $pidFile -Raw).Trim()
$tunnelPid = 0
if ($pidText -notmatch "^[0-9]+$" -or
    ![int]::TryParse($pidText, [ref]$tunnelPid) -or
    $tunnelPid -le 0) {
    throw "The tunnel PID file is invalid; refusing to stop any process."
}

$tunnelProcess = Get-Process -Id $tunnelPid -ErrorAction SilentlyContinue
if ($null -eq $tunnelProcess) {
    Remove-DirectTunnelStaleMetadata -RecordedPidText $pidText
    Write-Output "The recorded ChatGPT-Codex tunnel process is already stopped."
    exit 0
}

try {
    $expectedTunnelClient = [System.IO.Path]::GetFullPath(
        (Resolve-Path -LiteralPath $tunnelClient -ErrorAction Stop).Path
    )
}
catch {
    throw "The expected ChatGPT-Codex tunnel-client.exe cannot be verified; refusing to stop the recorded PID."
}

$actualTunnelClient = $null
try {
    $actualTunnelClient = $tunnelProcess.Path
}
catch {
    throw "The recorded PID executable cannot be verified; refusing to stop it."
}
if ([string]::IsNullOrWhiteSpace($actualTunnelClient)) {
    throw "The recorded PID executable cannot be verified; refusing to stop it."
}

try {
    $actualTunnelClient = [System.IO.Path]::GetFullPath($actualTunnelClient)
}
catch {
    throw "The recorded PID executable path is invalid; refusing to stop it."
}
if (![string]::Equals(
        $actualTunnelClient,
        $expectedTunnelClient,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
    throw "The recorded PID is not the expected ChatGPT-Codex tunnel-client.exe; refusing to stop it."
}

# Re-read the process identity immediately before the PID-scoped termination.
# This narrows the PID-reuse window without ever widening the target scope.
$verifiedTunnelProcess = Get-Process -Id $tunnelPid -ErrorAction SilentlyContinue
if ($null -eq $verifiedTunnelProcess) {
    Remove-DirectTunnelStaleMetadata -RecordedPidText $pidText
    Write-Output "The recorded ChatGPT-Codex tunnel process is already stopped."
    exit 0
}
$verifiedTunnelClient = $null
try {
    $verifiedTunnelClient = $verifiedTunnelProcess.Path
}
catch {
    throw "The recorded PID executable cannot be verified immediately before stop; refusing to stop it."
}
if ([string]::IsNullOrWhiteSpace($verifiedTunnelClient)) {
    throw "The recorded PID executable cannot be verified immediately before stop; refusing to stop it."
}
try {
    $verifiedTunnelClient = [System.IO.Path]::GetFullPath($verifiedTunnelClient)
}
catch {
    throw "The verified PID executable path is invalid; refusing to stop it."
}
if (![string]::Equals(
        $verifiedTunnelClient,
        $expectedTunnelClient,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
    throw "The recorded PID changed identity before stop; refusing to stop it."
}

$taskkill = (Get-Command taskkill.exe -ErrorAction Stop).Source
$taskkillOutput = & $taskkill /PID ([string]$tunnelPid) /T /F 2>&1
$taskkillExit = $LASTEXITCODE
foreach ($line in $taskkillOutput) {
    Write-Output ("TASKKILL: " + $line)
}

$deadline = (Get-Date).AddSeconds(10)
while ((Get-Date) -lt $deadline) {
    $remainingTunnelProcess = Get-Process -Id $tunnelPid -ErrorAction SilentlyContinue
    if ($null -eq $remainingTunnelProcess) {
        Remove-DirectTunnelStaleMetadata -RecordedPidText $pidText
        if ($taskkillExit -ne 0) {
            Write-Output ("taskkill reported exit " + $taskkillExit + ", but the verified tunnel PID is no longer alive.")
        }
        Write-Output ("Stopped ChatGPT-Codex direct tunnel PID: " + $tunnelPid)
        exit 0
    }

    $remainingTunnelClient = $null
    try {
        $remainingTunnelClient = $remainingTunnelProcess.Path
    }
    catch {
        throw "The verified tunnel PID is still alive but its executable cannot be verified; refusing to report success."
    }
    if ([string]::IsNullOrWhiteSpace($remainingTunnelClient)) {
        throw "The verified tunnel PID is still alive but its executable cannot be verified; refusing to report success."
    }
    try {
        $remainingTunnelClient = [System.IO.Path]::GetFullPath($remainingTunnelClient)
    }
    catch {
        throw "The remaining PID executable path is invalid; refusing to report success."
    }
    if (![string]::Equals(
            $remainingTunnelClient,
            $expectedTunnelClient,
            [System.StringComparison]::OrdinalIgnoreCase
        )) {
        throw "The verified tunnel PID is now owned by an unrelated executable; refusing to report success."
    }
    Start-Sleep -Milliseconds 100
}

throw "The verified ChatGPT-Codex tunnel PID did not exit after taskkill; stop failed."
