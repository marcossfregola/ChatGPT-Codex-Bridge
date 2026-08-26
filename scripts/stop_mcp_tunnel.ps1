[CmdletBinding()]
param()

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
$mcpDbMarker = "ChatGPTCodexBridge"

if (!(Test-Path -LiteralPath $pidFile -PathType Leaf)) {
    Write-Output "No ChatGPT-Codex tunnel PID file exists."
    exit 0
}

$pidText = (Get-Content -LiteralPath $pidFile -Raw).Trim()
$tunnelPid = 0
if (![int]::TryParse($pidText, [ref]$tunnelPid) -or $tunnelPid -le 0) {
    throw "The tunnel PID file is invalid; refusing to stop any process."
}

$tunnelProcess = Get-Process -Id $tunnelPid -ErrorAction SilentlyContinue
if ($null -eq $tunnelProcess) {
    Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath $healthFile -PathType Leaf) {
        Remove-Item -LiteralPath $healthFile -Force -ErrorAction SilentlyContinue
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

$mcpChildren = @(
    Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe'" |
        Where-Object {
            $_.ParentProcessId -eq $tunnelPid -and
            $_.ProcessId -ne $tunnelPid -and
            $_.CommandLine -and
            $_.CommandLine -match "chatgpt_codex_bridge\.mcp_server" -and
            $_.CommandLine -match [regex]::Escape($mcpDbMarker)
        }
)

foreach ($child in $mcpChildren) {
    $childProcess = Get-Process -Id ([int]$child.ProcessId) -ErrorAction SilentlyContinue
    if ($null -ne $childProcess) {
        Stop-Process -Id $childProcess.Id -Force
        Write-Output ("Stopped Bridge MCP child PID: " + $childProcess.Id)
    }
}

Stop-Process -Id $tunnelProcess.Id -Force
Wait-Process -Id $tunnelProcess.Id -Timeout 10 -ErrorAction SilentlyContinue

if (Get-Process -Id $tunnelProcess.Id -ErrorAction SilentlyContinue) {
    throw "The new ChatGPT-Codex tunnel process did not stop."
}

Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
if (Test-Path -LiteralPath $healthFile -PathType Leaf) {
    Remove-Item -LiteralPath $healthFile -Force -ErrorAction SilentlyContinue
}
Write-Output ("Stopped ChatGPT-Codex tunnel PID: " + $tunnelPid)
