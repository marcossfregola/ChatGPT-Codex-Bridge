[CmdletBinding()]
param(
    [ValidateRange(5, 600)]
    [int]$ReadinessTimeoutSeconds = 60
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$localAppData = $env:LOCALAPPDATA
if ([string]::IsNullOrWhiteSpace($localAppData)) {
    throw "LOCALAPPDATA is required."
}

$runtimeRoot = Join-Path $localAppData "ChatGPTCodexBridge"
$secretFile = Join-Path $runtimeRoot "secrets\control-plane-api-key.dpapi"
$tunnelClient = Join-Path $runtimeRoot "tunnel-client\tunnel-client.exe"
$profileFile = Join-Path $runtimeRoot "tunnel-client\profiles\chatgpt-codex-bridge.yaml"
$pidFile = Join-Path $runtimeRoot "tunnel-state\tunnel.pid"
$healthFile = Join-Path $runtimeRoot "tunnel-state\health.url"
$expectedTunnelId = "tunnel_6a8ef626bf008191a6294996145747e5"

foreach ($directory in @(
        (Join-Path $runtimeRoot "state"),
        (Join-Path $runtimeRoot "logs"),
        (Join-Path $runtimeRoot "secrets"),
        (Join-Path $runtimeRoot "tunnel-client"),
        (Join-Path $runtimeRoot "tunnel-client\profiles"),
        (Join-Path $runtimeRoot "tunnel-state")
    )) {
    New-Item -ItemType Directory -Force -Path $directory | Out-Null
}

if (!(Test-Path -LiteralPath $secretFile -PathType Leaf)) {
    throw "The DPAPI credential file does not exist."
}
if (!(Test-Path -LiteralPath $tunnelClient -PathType Leaf)) {
    throw "The new tunnel-client binary does not exist."
}
if (!(Test-Path -LiteralPath $profileFile -PathType Leaf)) {
    throw "The new tunnel-client profile does not exist."
}

$resolvedTunnelClient = (Resolve-Path -LiteralPath $tunnelClient).Path
$profileText = Get-Content -LiteralPath $profileFile -Raw
if ($profileText -notmatch [regex]::Escape($expectedTunnelId)) {
    throw "The profile does not contain the authorized tunnel ID."
}
if ($profileText -notmatch "chatgpt_codex_bridge\.mcp_server") {
    throw "The profile does not target the Bridge MCP server."
}

$existingPid = 0
if (Test-Path -LiteralPath $pidFile -PathType Leaf) {
    $pidText = (Get-Content -LiteralPath $pidFile -Raw).Trim()
    if ([int]::TryParse($pidText, [ref]$existingPid) -and $existingPid -gt 0) {
        $existingProcess = Get-Process -Id $existingPid -ErrorAction SilentlyContinue
        if ($null -ne $existingProcess) {
            $existingPath = $null
            try {
                $existingPath = $existingProcess.Path
            }
            catch {
                throw "The PID file points to a live process whose executable cannot be verified."
            }
            if ([string]::IsNullOrWhiteSpace($existingPath) -or
                ((Resolve-Path -LiteralPath $existingPath).Path -ine $resolvedTunnelClient)) {
                throw "The PID file points to a live unrelated process; refusing to stop it."
            }
            throw "The ChatGPT-Codex tunnel is already running."
        }
    }
    Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
}

$cipher = $null
$secure = $null
$bstr = [IntPtr]::Zero
$apiKey = $null
$startInfo = $null
$startedProcess = $null
$healthBaseUrl = $null
$ready = $false

try {
    try {
        $cipher = Get-Content -LiteralPath $secretFile -Raw
        $secure = $cipher | ConvertTo-SecureString
        $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
        $apiKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
        if ([string]::IsNullOrEmpty($apiKey)) {
            throw "The decrypted credential is empty."
        }
    }
    catch {
        throw "The DPAPI credential could not be decrypted under the current Windows identity. Run this script as the account that created the credential."
    }
    finally {
        if ($bstr -ne [IntPtr]::Zero) {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
            $bstr = [IntPtr]::Zero
        }
        $cipher = $null
    }

    $startInfo = [Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $resolvedTunnelClient
    $startInfo.WorkingDirectory = $repoRoot
    $startInfo.UseShellExecute = $false
    [void]$startInfo.ArgumentList.Add("run")
    [void]$startInfo.ArgumentList.Add("--profile-file")
    [void]$startInfo.ArgumentList.Add($profileFile)
    [void]$startInfo.ArgumentList.Add("--pid.file")
    [void]$startInfo.ArgumentList.Add($pidFile)
    $startInfo.Environment["CONTROL_PLANE_API_KEY"] = $apiKey

    $startedProcess = [Diagnostics.Process]::new()
    $startedProcess.StartInfo = $startInfo
    if (!$startedProcess.Start()) {
        throw "The new tunnel-client process could not be started."
    }
    [void]$startInfo.Environment.Remove("CONTROL_PLANE_API_KEY")
    $startInfo = $null
    $apiKey = $null

    $deadline = (Get-Date).AddSeconds($ReadinessTimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if ($startedProcess.HasExited) {
            throw "The new tunnel-client exited before readiness (exit code $($startedProcess.ExitCode))."
        }
        if (Test-Path -LiteralPath $healthFile -PathType Leaf) {
            $healthBaseUrl = (Get-Content -LiteralPath $healthFile -Raw).Trim()
            if (![string]::IsNullOrWhiteSpace($healthBaseUrl)) {
                $readyUrl = $healthBaseUrl.TrimEnd("/") + "/readyz"
                try {
                    $response = Invoke-WebRequest -Uri $readyUrl -UseBasicParsing -TimeoutSec 3
                    if ([int]$response.StatusCode -eq 200) {
                        $ready = $true
                        break
                    }
                }
                catch {
                    # The daemon may still be binding its local health listener.
                }
            }
        }
        Start-Sleep -Milliseconds 500
    }

    if (!$ready) {
        throw "The new tunnel-client did not report /readyz HTTP 200 before the readiness timeout."
    }

    Write-Output ("MCP tunnel PID: " + $startedProcess.Id)
    Write-Output ("Health URL: " + $healthBaseUrl.TrimEnd("/"))
}
catch {
    if ($null -ne $startedProcess) {
        try {
            if (!$startedProcess.HasExited) {
                $runningPath = $startedProcess.MainModule.FileName
                if (![string]::IsNullOrWhiteSpace($runningPath) -and
                    ((Resolve-Path -LiteralPath $runningPath).Path -ieq $resolvedTunnelClient)) {
                    Stop-Process -Id $startedProcess.Id -Force -ErrorAction SilentlyContinue
                }
            }
        }
        catch {
            # Preserve the original startup error and never broaden process scope.
        }
    }
    Write-Error $_.Exception.Message
    exit 1
}
finally {
    if ($null -ne $secure) {
        $secure.Dispose()
        $secure = $null
    }
    if ($bstr -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
        $bstr = [IntPtr]::Zero
    }
    $apiKey = $null
    $cipher = $null
    $startInfo = $null
}
