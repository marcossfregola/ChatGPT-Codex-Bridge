[CmdletBinding()]
param()

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
$cloudflared = Join-Path $runtimeRoot "tunnel-client\cloudflared.exe"
$profileFile = Join-Path $runtimeRoot "tunnel-client\profiles\chatgpt-codex-bridge.yaml"
$expectedTunnelId = "tunnel_6a8ef626bf008191a6294996145747e5"

function Redact-SensitiveText {
    param([AllowNull()][string]$Text)

    if ($null -eq $Text) {
        return ""
    }

    $redacted = $Text
    $redacted = [regex]::Replace(
        $redacted,
        '(?im)(Authorization\s*:\s*Bearer\s+)[^\s"\r\n]+',
        '${1}[REDACTED]'
    )
    $redacted = [regex]::Replace(
        $redacted,
        '(?im)(["'']?\b(?:CONTROL_PLANE_API_KEY|OPENAI_API_KEY)\b["'']?\s*[:=]\s*)[^\r\n]+',
        '${1}[REDACTED]'
    )
    $redacted = [regex]::Replace(
        $redacted,
        '(?im)(["'']?\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|secret|password)\b["'']?\s*[:=]\s*)("[^"]*"|''[^'']*''|[^\s,}]+)',
        '${1}[REDACTED]'
    )
    $redacted = [regex]::Replace(
        $redacted,
        '(?im)\b(?:sk|rk|key|tok|pat)-[A-Za-z0-9._-]{12,}\b',
        '[REDACTED]'
    )
    return $redacted
}

foreach ($path in @($secretFile, $tunnelClient, $cloudflared, $profileFile)) {
    if (!(Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Required new runtime file is missing: $path"
    }
}

$profileText = Get-Content -LiteralPath $profileFile -Raw
if ($profileText -notmatch [regex]::Escape($expectedTunnelId)) {
    throw "The profile does not contain the authorized tunnel ID."
}

$cipher = $null
$secure = $null
$bstr = [IntPtr]::Zero
$apiKey = $null
$doctorProcess = $null
$startInfo = $null
$stdoutTask = $null
$stderrTask = $null
$stdout = ""
$stderr = ""
$wrapperError = $null
$exitCode = 1

Write-Output "DOCTOR_START"

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
    $startInfo.FileName = (Resolve-Path -LiteralPath $tunnelClient).Path
    $startInfo.WorkingDirectory = $repoRoot
    $startInfo.UseShellExecute = $false
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    [void]$startInfo.ArgumentList.Add("doctor")
    [void]$startInfo.ArgumentList.Add("--profile-file")
    [void]$startInfo.ArgumentList.Add($profileFile)
    [void]$startInfo.ArgumentList.Add("--cloudflared.path")
    [void]$startInfo.ArgumentList.Add($cloudflared)
    $startInfo.Environment["CONTROL_PLANE_API_KEY"] = $apiKey

    $doctorProcess = [Diagnostics.Process]::new()
    $doctorProcess.StartInfo = $startInfo
    if (!$doctorProcess.Start()) {
        throw "The tunnel-client doctor process could not be started."
    }
    [void]$startInfo.Environment.Remove("CONTROL_PLANE_API_KEY")
    $apiKey = $null

    $stdoutTask = $doctorProcess.StandardOutput.ReadToEndAsync()
    $stderrTask = $doctorProcess.StandardError.ReadToEndAsync()
    $doctorProcess.WaitForExit()
    $stdout = $stdoutTask.GetAwaiter().GetResult()
    $stderr = $stderrTask.GetAwaiter().GetResult()
    $exitCode = $doctorProcess.ExitCode
}
catch {
    $wrapperError = "Doctor wrapper failed: " + $_.Exception.GetType().FullName
    if ($null -ne $doctorProcess) {
        try {
            if (!$doctorProcess.HasExited) {
                $doctorProcess.Kill()
                $doctorProcess.WaitForExit()
            }
        }
        catch {
            # Never broaden process scope while preserving the wrapper error.
        }
    }
    $exitCode = 1
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
    $stdoutTask = $null
    $stderrTask = $null
}

if ($null -ne $wrapperError) {
    Write-Error (Redact-SensitiveText $wrapperError)
}

$safeStdout = Redact-SensitiveText $stdout
$safeStderr = Redact-SensitiveText $stderr
Write-Output "DOCTOR_STDOUT_BEGIN"
if ([string]::IsNullOrEmpty($safeStdout)) {
    Write-Output "[empty]"
}
else {
    Write-Output $safeStdout
}
Write-Output "DOCTOR_STDOUT_END"
Write-Output "DOCTOR_STDERR_BEGIN"
if ([string]::IsNullOrEmpty($safeStderr)) {
    Write-Output "[empty]"
}
else {
    Write-Output $safeStderr
}
Write-Output "DOCTOR_STDERR_END"
Write-Output ("DOCTOR_EXIT_CODE=" + $exitCode)
exit $exitCode
