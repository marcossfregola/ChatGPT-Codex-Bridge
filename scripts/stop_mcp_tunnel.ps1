[CmdletBinding()]
param(
    [string]$RuntimeAlias = ""
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
$tunnelClient = Join-Path $runtimeRoot "tunnel-client\tunnel-client.exe"
$pidFile = Join-Path $runtimeRoot "tunnel-state\tunnel.pid"
$healthFile = Join-Path $runtimeRoot "tunnel-state\health.url"
$expectedTunnelId = "tunnel_6a8ef626bf008191a6294996145747e5"

if (!(Test-Path -LiteralPath $pidFile -PathType Leaf)) {
    Write-Output "No ChatGPT-Codex tunnel PID file exists."
    exit 0
}

if ([string]::IsNullOrWhiteSpace($RuntimeAlias)) {
    throw (
        "The direct tunnel-client run has no supported local graceful shutdown. " +
        "Refusing process termination; provide -RuntimeAlias for a managed runtime."
    )
}

$pidText = (Get-Content -LiteralPath $pidFile -Raw).Trim()
$tunnelPid = 0
if (![int]::TryParse($pidText, [ref]$tunnelPid) -or $tunnelPid -le 0) {
    throw "The tunnel PID file is invalid; refusing to stop any process."
}

$tunnelProcess = Get-Process -Id $tunnelPid -ErrorAction SilentlyContinue
if ($null -eq $tunnelProcess) {
    Remove-Item -LiteralPath $pidFile -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath $healthFile -PathType Leaf) {
        Remove-Item -LiteralPath $healthFile -ErrorAction SilentlyContinue
    }
    Write-Output "The recorded ChatGPT-Codex tunnel process is already stopped."
    exit 0
}

$expectedTunnelClient = (Resolve-Path -LiteralPath $tunnelClient).Path
$actualTunnelClient = $null
try {
    $actualTunnelClient = $tunnelProcess.Path
}
catch {
    throw "The recorded PID executable cannot be verified; refusing to stop it."
}
if ([string]::IsNullOrWhiteSpace($actualTunnelClient) -or
    ((Resolve-Path -LiteralPath $actualTunnelClient).Path -ine $expectedTunnelClient)) {
    throw "The recorded PID is not the new ChatGPT-Codex tunnel-client; refusing to stop it."
}

$statusOutput = & $tunnelClient runtimes status $RuntimeAlias --json 2>&1
$statusExit = $LASTEXITCODE
$statusText = ($statusOutput -join "`n")
if ($statusExit -ne 0 -or $statusText -notmatch [regex]::Escape($expectedTunnelId)) {
    throw "Managed runtime '$RuntimeAlias' is not verifiably the authorized ChatGPT-Codex tunnel; refusing to stop it."
}

$stopOutput = & $tunnelClient runtimes stop $RuntimeAlias --json 2>&1
$stopExit = $LASTEXITCODE
foreach ($line in $stopOutput) {
    Write-Output ("RUNTIMES: " + $line)
}
if ($stopExit -ne 0) {
    throw "Managed tunnel runtime '$RuntimeAlias' did not accept graceful stop (exit $stopExit)."
}

$deadline = (Get-Date).AddSeconds(30)
while ((Get-Date) -lt $deadline) {
    if ($null -eq (Get-Process -Id $tunnelProcess.Id -ErrorAction SilentlyContinue)) {
        Write-Output ("Gracefully stopped managed ChatGPT-Codex tunnel PID: " + $tunnelPid)
        exit 0
    }
    Start-Sleep -Milliseconds 250
}

throw "Managed tunnel runtime '$RuntimeAlias' did not exit within the graceful timeout; no process was killed."
