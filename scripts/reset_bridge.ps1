[CmdletBinding()]
param(
    [ValidateRange(1, 120)]
    [int]$WorkerGracePeriodSeconds = 20,
    [ValidateRange(1, 120)]
    [int]$TunnelStopTimeoutSeconds = 30,
    [ValidateRange(5, 600)]
    [int]$ReadinessTimeoutSeconds = 60
)

# This is deliberately an orchestrator, not a recovery tool.  It never reads
# the contents of an archived database and it never enumerates project
# repositories.  All destructive operations below are restricted to the
# fixed ChatGPTCodexBridge runtime root derived from LOCALAPPDATA.

$script:Results = @{}
$script:CurrentPhase = "PREFLIGHT"
$script:Paths = $null
$script:ExpectedPythonExecutable = $null
$script:PythonPath = $null
$script:StableFingerprints = @{}
$ErrorActionPreference = "Stop"

function Write-MachineResult {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Value
    )

    $script:Results[$Name] = $Value
    Write-Output ("{0}={1}" -f $Name, $Value)
}

function ConvertTo-SafeErrorText {
    param([AllowNull()][object]$ErrorRecord)

    if ($null -eq $ErrorRecord) {
        return "unknown error"
    }
    $text = ""
    if ($ErrorRecord.PSObject.Properties.Name -contains "Exception" -and $null -ne $ErrorRecord.Exception) {
        $text = [string]$ErrorRecord.Exception.Message
    }
    if ([string]::IsNullOrWhiteSpace($text)) {
        $text = [string]$ErrorRecord
    }
    # Keep diagnostics on one machine-readable line and avoid accidentally
    # printing a command line or credential value supplied by a child tool.
    $text = $text -replace "[\r\n]+", " "
    $text = $text -replace "(?i)(CONTROL_PLANE_API_KEY|api[_-]?key|token|secret|password)\s*[:=]\s*[^\s]+", '$1=[REDACTED]'
    if ($text.Length -gt 500) {
        $text = $text.Substring(0, 500)
    }
    return $text
}

function Get-FullPathWithoutProvider {
    param([Parameter(Mandatory = $true)][string]$Path)

    if ([string]::IsNullOrWhiteSpace($Path)) {
        throw "A required path is empty."
    }
    return [IO.Path]::GetFullPath($Path)
}

function Get-CanonicalPath {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [switch]$RequireExisting
    )

    if (Test-Path -LiteralPath $Path) {
        try {
            return (Resolve-Path -LiteralPath $Path -ErrorAction Stop).Path
        }
        catch {
            throw "Cannot resolve path '$Path': $($_.Exception.Message)"
        }
    }
    if ($RequireExisting) {
        throw "Required path does not exist: $Path"
    }
    return Get-FullPathWithoutProvider $Path
}

function Test-PathUnderRoot {
    param(
        [Parameter(Mandatory = $true)][string]$Candidate,
        [Parameter(Mandatory = $true)][string]$Root
    )

    $candidateFull = Get-FullPathWithoutProvider $Candidate
    $rootFull = (Get-FullPathWithoutProvider $Root).TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar)
    if ($candidateFull.Equals($rootFull, [StringComparison]::OrdinalIgnoreCase)) {
        return $true
    }
    $prefix = $rootFull + [IO.Path]::DirectorySeparatorChar
    return $candidateFull.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)
}

function Assert-ContainedPath {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label,
        [string]$RootPath = "",
        [switch]$RequireExisting,
        [switch]$RequireLeaf,
        [switch]$RequireDirectory
    )

    $canonical = Get-CanonicalPath -Path $Path -RequireExisting:$RequireExisting
    $containmentRoot = if ([string]::IsNullOrWhiteSpace($RootPath)) { $script:Paths.Root } else { $RootPath }
    if (!(Test-PathUnderRoot -Candidate $canonical -Root $containmentRoot)) {
        throw "$Label escapes its authorized root: $Path"
    }
    if ($RequireLeaf -and !(Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Label is not a file: $Path"
    }
    if ($RequireDirectory -and !(Test-Path -LiteralPath $Path -PathType Container)) {
        throw "$Label is not a directory: $Path"
    }
    return $canonical
}

function Assert-ExistingChildrenContained {
    param(
        [Parameter(Mandatory = $true)][string]$Directory,
        [Parameter(Mandatory = $true)][string]$Label
    )

    if (!(Test-Path -LiteralPath $Directory -PathType Container)) {
        return
    }
    foreach ($entry in @(Get-ChildItem -LiteralPath $Directory -Force -ErrorAction Stop)) {
        $resolved = Get-CanonicalPath -Path $entry.FullName -RequireExisting
        if (!(Test-PathUnderRoot -Candidate $resolved -Root $script:Paths.Root)) {
            throw "$Label contains a path outside the runtime root: $($entry.FullName)"
        }
    }
}

function Get-FileSha256 {
    param([Parameter(Mandatory = $true)][string]$Path)

    try {
        return (Get-FileHash -Algorithm SHA256 -LiteralPath $Path -ErrorAction Stop).Hash
    }
    catch {
        throw "Cannot fingerprint stable runtime file '$Path': $($_.Exception.Message)"
    }
}

function Invoke-Preflight {
    if ($PSVersionTable.PSVersion.Major -lt 7 -or $PSVersionTable.PSEdition -ne "Core") {
        throw "This script requires PowerShell 7+. Run it with pwsh."
    }

    Set-StrictMode -Version Latest
    $ErrorActionPreference = "Stop"

    $repoRoot = Split-Path -Parent $PSScriptRoot
    $repoRoot = (Resolve-Path -LiteralPath $repoRoot -ErrorAction Stop).Path
    if (!(Test-Path -LiteralPath (Join-Path $repoRoot ".git")) -or
        !(Test-Path -LiteralPath (Join-Path $repoRoot "pyproject.toml") -PathType Leaf)) {
        throw "The script is not running from the expected Bridge checkout: $repoRoot"
    }

    $localAppData = $env:LOCALAPPDATA
    if ([string]::IsNullOrWhiteSpace($localAppData)) {
        throw "LOCALAPPDATA is required."
    }
    $localAppData = Get-FullPathWithoutProvider $localAppData
    if (!(Test-Path -LiteralPath $localAppData -PathType Container)) {
        throw "LOCALAPPDATA is not an existing directory: $localAppData"
    }

    $runtimeCandidate = Join-Path $localAppData "ChatGPTCodexBridge"
    if (!(Test-Path -LiteralPath $runtimeCandidate -PathType Container)) {
        throw "The ChatGPTCodexBridge runtime root does not exist: $runtimeCandidate"
    }
    $localCanonical = Get-CanonicalPath $localAppData -RequireExisting
    $runtimeCanonical = Get-CanonicalPath $runtimeCandidate -RequireExisting
    if ((Split-Path -Parent $runtimeCanonical).TrimEnd('\') -ine $localCanonical.TrimEnd('\') -or
        (Split-Path -Leaf $runtimeCanonical) -ine "ChatGPTCodexBridge") {
        throw "The runtime root is not the canonical LOCALAPPDATA\ChatGPTCodexBridge directory."
    }

    $script:Paths = [ordered]@{
        RepoRoot = $repoRoot
        LocalAppData = $localCanonical
        Root = $runtimeCanonical
        State = Join-Path $runtimeCanonical "state"
        Logs = Join-Path $runtimeCanonical "logs"
        TunnelState = Join-Path $runtimeCanonical "tunnel-state"
        Secrets = Join-Path $runtimeCanonical "secrets"
        TunnelClientRoot = Join-Path $runtimeCanonical "tunnel-client"
        TunnelProfiles = Join-Path $runtimeCanonical "tunnel-client\profiles"
        StateArchive = Join-Path $runtimeCanonical "state.archive"
        Db = Join-Path $runtimeCanonical "state\bridge.sqlite3"
        WorkerPid = Join-Path $runtimeCanonical "state\bridge.sqlite3.execution-worker.pid"
        WorkerState = Join-Path $runtimeCanonical "state\bridge.sqlite3.execution-worker.state.json"
        WorkerStop = Join-Path $runtimeCanonical "state\bridge.sqlite3.execution-worker.stop"
        WorkerLock = Join-Path $runtimeCanonical "state\bridge.sqlite3.execution-worker.lock"
        McpLock = Join-Path $runtimeCanonical "state\bridge.sqlite3.mcp.lock"
        TunnelPid = Join-Path $runtimeCanonical "tunnel-state\tunnel.pid"
        Health = Join-Path $runtimeCanonical "tunnel-state\health.url"
        Secret = Join-Path $runtimeCanonical "secrets\control-plane-api-key.dpapi"
        TunnelClient = Join-Path $runtimeCanonical "tunnel-client\tunnel-client.exe"
        Cloudflared = Join-Path $runtimeCanonical "tunnel-client\cloudflared.exe"
        Profile = Join-Path $runtimeCanonical "tunnel-client\profiles\chatgpt-codex-bridge.yaml"
        ManagedProfiles = Join-Path $runtimeCanonical "tunnel-client\profiles\managed"
    }

    foreach ($directory in @("State", "Logs", "TunnelState", "Secrets", "TunnelClientRoot", "TunnelProfiles", "StateArchive", "ManagedProfiles")) {
        [void](Assert-ContainedPath -Path $script:Paths[$directory] -Label $directory)
    }
    Assert-ExistingChildrenContained -Directory $script:Paths.State -Label "state"
    Assert-ExistingChildrenContained -Directory $script:Paths.TunnelState -Label "tunnel-state"
    Assert-ExistingChildrenContained -Directory $script:Paths.StateArchive -Label "state.archive"

    foreach ($fileKey in @("Secret", "TunnelClient", "Cloudflared", "Profile")) {
        [void](Assert-ContainedPath -Path $script:Paths[$fileKey] -Label $fileKey -RequireExisting -RequireLeaf)
    }
    $profileText = Get-Content -LiteralPath $script:Paths.Profile -Raw -ErrorAction Stop
    $expectedTunnelId = "tunnel_6a8ef626bf008191a6294996145747e5"
    if ($profileText -notmatch [regex]::Escape($expectedTunnelId)) {
        throw "The tunnel profile does not contain the authorized tunnel ID."
    }
    if ($profileText -notmatch "chatgpt_codex_bridge\.mcp_server") {
        throw "The tunnel profile does not target the Bridge MCP server."
    }

    $script:PythonPath = Join-Path $repoRoot ".venv\Scripts\python.exe"
    [void](Assert-ContainedPath -Path $script:PythonPath -Label "Bridge .venv Python" -RootPath $repoRoot -RequireExisting -RequireLeaf)
    $pythonProbe = (& $script:PythonPath -B -c "import sys; print(sys.version_info[0]); print(sys.version_info[1]); print(sys._base_executable)" 2>$null) -join "`n"
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($pythonProbe)) {
        throw "The Bridge .venv Python runtime could not be executed."
    }
    $pythonLines = @($pythonProbe -split "`n")
    $major = 0
    [void][int]::TryParse($pythonLines[0].Trim(), [ref]$major)
    if ($major -lt 3) {
        throw "The Bridge .venv Python runtime is invalid."
    }
    $minor = 0
    if ($pythonLines.Count -gt 1) {
        [void][int]::TryParse($pythonLines[1].Trim(), [ref]$minor)
    }
    if ($major -ne 3 -or $minor -lt 13) {
        throw "The Bridge .venv Python runtime must be Python 3.13 or newer."
    }
    $basePython = if ($pythonLines.Count -gt 2) { $pythonLines[2].Trim() } else { "" }
    if ([string]::IsNullOrWhiteSpace($basePython) -or !(Test-Path -LiteralPath $basePython -PathType Leaf)) {
        $basePython = (Resolve-Path -LiteralPath $script:PythonPath -ErrorAction Stop).Path
    }
    $script:ExpectedPythonExecutable = (Resolve-Path -LiteralPath $basePython -ErrorAction Stop).Path

    $script:StableFingerprints = @{}
    foreach ($fileKey in @("Secret", "TunnelClient", "Cloudflared", "Profile")) {
        $script:StableFingerprints[$fileKey] = Get-FileSha256 $script:Paths[$fileKey]
    }

    $pwsh = Get-Command pwsh -ErrorAction Stop
    if ([string]::IsNullOrWhiteSpace($pwsh.Source)) {
        throw "PowerShell 7 executable could not be resolved."
    }
}

function Read-PidSidecar {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (!(Test-Path -LiteralPath $Path -PathType Leaf)) {
        return [pscustomobject]@{ Present = $false; Valid = $false; Value = 0 }
    }
    $text = (Get-Content -LiteralPath $Path -Raw -ErrorAction Stop).Trim()
    $value = 0
    $valid = [int]::TryParse($text, [ref]$value) -and $value -gt 0
    return [pscustomobject]@{ Present = $true; Valid = $valid; Value = if ($valid) { $value } else { 0 } }
}

function Read-WorkerStatePid {
    if (!(Test-Path -LiteralPath $script:Paths.WorkerState -PathType Leaf)) {
        return 0
    }
    try {
        $state = Get-Content -LiteralPath $script:Paths.WorkerState -Raw -ErrorAction Stop | ConvertFrom-Json
        $value = 0
        if ($null -ne $state.pid -and [int]::TryParse([string]$state.pid, [ref]$value) -and $value -gt 0) {
            return $value
        }
    }
    catch {
        return 0
    }
    return 0
}

function Get-ProcessCommandRecord {
    param([Parameter(Mandatory = $true)][int]$ProcessId)

    try {
        $records = @(Get-CimInstance Win32_Process -Filter ("ProcessId = {0}" -f $ProcessId) -OperationTimeoutSec 3 -ErrorAction Stop)
        if ($records.Count -ne 1) {
            throw "CIM returned no unique process record."
        }
        return $records[0]
    }
    catch {
        throw "CIM command line is unavailable for PID ${ProcessId}: $($_.Exception.Message)"
    }
}

function Test-CommandLinePathArgument {
    param(
        [Parameter(Mandatory = $true)][string]$CommandLine,
        [Parameter(Mandatory = $true)][string]$ExpectedPath,
        [Parameter(Mandatory = $true)][string[]]$Switches
    )

    foreach ($switchName in $Switches) {
        $escapedSwitch = [regex]::Escape($switchName)
        $pattern = '(?i)' + $escapedSwitch + '\s+(?:"([^"]+)"|(\S+))'
        $match = [regex]::Match($CommandLine, $pattern)
        if (!$match.Success) {
            continue
        }
        $argument = if ($match.Groups[1].Success) { $match.Groups[1].Value } else { $match.Groups[2].Value }
        try {
            $argumentCanonical = (Resolve-Path -LiteralPath $argument -ErrorAction Stop).Path
            if ($argumentCanonical -ieq (Resolve-Path -LiteralPath $ExpectedPath -ErrorAction Stop).Path) {
                return $true
            }
        }
        catch {
            # A malformed/nonexistent argument cannot establish identity.
        }
    }
    return $false
}

function Get-VerifiedProcess {
    param(
        [Parameter(Mandatory = $true)][int]$ProcessId,
        [Parameter(Mandatory = $true)][ValidateSet("worker", "tunnel")][string]$Kind
    )

    $process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if ($null -eq $process) {
        return [pscustomobject]@{ Status = "ABSENT"; Process = $null; Record = $null; Reason = "not running" }
    }

    $actualPath = $null
    try {
        $actualPath = $process.Path
    }
    catch {
        return [pscustomobject]@{ Status = "AMBIGUOUS"; Process = $process; Record = $null; Reason = "executable path unavailable" }
    }
    if ([string]::IsNullOrWhiteSpace($actualPath)) {
        return [pscustomobject]@{ Status = "AMBIGUOUS"; Process = $process; Record = $null; Reason = "executable path empty" }
    }
    try {
        $actualCanonical = (Resolve-Path -LiteralPath $actualPath -ErrorAction Stop).Path
    }
    catch {
        return [pscustomobject]@{ Status = "AMBIGUOUS"; Process = $process; Record = $null; Reason = "executable path cannot be resolved" }
    }

    $expected = if ($Kind -eq "worker") { $script:ExpectedPythonExecutable } else { (Resolve-Path -LiteralPath $script:Paths.TunnelClient -ErrorAction Stop).Path }
    if ($actualCanonical -ine $expected) {
        return [pscustomobject]@{ Status = "MISMATCH"; Process = $process; Record = $null; Reason = "executable path mismatch" }
    }

    $record = $null
    try {
        $record = Get-ProcessCommandRecord -ProcessId $ProcessId
    }
    catch {
        # A hard reset may terminate a process only with complete command-line
        # evidence.  PID plus executable path is deliberately insufficient.
        return [pscustomobject]@{ Status = "AMBIGUOUS"; Process = $process; Record = $null; Reason = (ConvertTo-SafeErrorText $_) }
    }
    $commandLine = [string]$record.CommandLine
    if ([string]::IsNullOrWhiteSpace($commandLine)) {
        return [pscustomobject]@{ Status = "AMBIGUOUS"; Process = $process; Record = $record; Reason = "command line unavailable" }
    }
    $normalizedCommand = $commandLine.Replace('/', '\')
    if ($Kind -eq "worker") {
        if ($normalizedCommand -notmatch "chatgpt_codex_bridge\.execution_worker" -or
            !(Test-CommandLinePathArgument -CommandLine $commandLine -ExpectedPath $script:Paths.Db -Switches @("--db-path"))) {
            return [pscustomobject]@{ Status = "MISMATCH"; Process = $process; Record = $record; Reason = "not the D3 execution worker for this DB" }
        }
    }
    else {
        $profileMatch = Test-CommandLinePathArgument -CommandLine $commandLine -ExpectedPath $script:Paths.Profile -Switches @("--profile-file")
        $managedMatch = $false
        $managedNorm = ($script:Paths.ManagedProfiles + '\').Replace('/', '\')
        if ($normalizedCommand.IndexOf($managedNorm, [StringComparison]::OrdinalIgnoreCase) -ge 0) {
            $managedMatch = $true
        }
        if (!$profileMatch -and !$managedMatch) {
            return [pscustomobject]@{ Status = "MISMATCH"; Process = $process; Record = $record; Reason = "not a Bridge tunnel profile" }
        }
    }
    return [pscustomobject]@{ Status = "VERIFIED"; Process = $process; Record = $record; Reason = "identity verified" }
}

function Invoke-WorkerStop {
    $pidSidecar = Read-PidSidecar $script:Paths.WorkerPid
    $statePid = Read-WorkerStatePid
    if ($pidSidecar.Valid -and $statePid -gt 0 -and $pidSidecar.Value -ne $statePid) {
        throw "Worker PID and worker state PID disagree; refusing termination."
    }
    $candidatePid = if ($pidSidecar.Valid) { $pidSidecar.Value } else { $statePid }
    if ($candidatePid -le 0) {
        Write-Output "WORKER_PID=NONE"
        Write-MachineResult "WORKER_STOP" "PASS"
        return
    }

    $identity = Get-VerifiedProcess -ProcessId $candidatePid -Kind worker
    if ($identity.Status -eq "ABSENT") {
        Write-Output ("WORKER_PID=" + $candidatePid)
        Write-MachineResult "WORKER_STOP" "PASS"
        return
    }
    if ($identity.Status -ne "VERIFIED") {
        throw "The recorded worker PID $candidatePid is live but cannot be identified unambiguously: $($identity.Reason)"
    }

    [IO.File]::WriteAllText(
        $script:Paths.WorkerStop,
        "{`"requested_by`":`"reset_bridge`"}" + [Environment]::NewLine,
        [Text.UTF8Encoding]::new($false)
    )
    $deadline = (Get-Date).AddSeconds($WorkerGracePeriodSeconds)
    while ((Get-Date) -lt $deadline) {
        if ($null -eq (Get-Process -Id $candidatePid -ErrorAction SilentlyContinue)) {
            break
        }
        Start-Sleep -Milliseconds 200
    }

    $stillAlive = Get-Process -Id $candidatePid -ErrorAction SilentlyContinue
    if ($null -ne $stillAlive) {
        # The identity was checked immediately before this authorized hard
        # reset.  Force termination is limited to that exact PID only.
        $recheck = Get-VerifiedProcess -ProcessId $candidatePid -Kind worker
        if ($recheck.Status -ne "VERIFIED") {
            throw "The worker identity changed during shutdown; refusing forced termination."
        }
        Stop-Process -Id $candidatePid -Force -Confirm:$false -ErrorAction Stop
        $forceDeadline = (Get-Date).AddSeconds(10)
        while ((Get-Date) -lt $forceDeadline -and $null -ne (Get-Process -Id $candidatePid -ErrorAction SilentlyContinue)) {
            Start-Sleep -Milliseconds 200
        }
        if ($null -ne (Get-Process -Id $candidatePid -ErrorAction SilentlyContinue)) {
            throw "The identified execution worker could not be stopped."
        }
    }
    Write-Output ("WORKER_PID=" + $candidatePid)
    Write-MachineResult "WORKER_STOP" "PASS"
}

function Get-ManagedTunnelAlias {
    param([AllowNull()]$Record)

    $candidates = [System.Collections.Generic.List[string]]::new()
    if (![string]::IsNullOrWhiteSpace($env:BRIDGE_TUNNEL_RUNTIME_ALIAS)) {
        $candidates.Add($env:BRIDGE_TUNNEL_RUNTIME_ALIAS)
    }
    if ($null -ne $Record) {
        $commandLine = [string]$Record.CommandLine
        $match = [regex]::Match($commandLine, '(?i)(?:--runtime[-_.]?alias|runtime[-_.]?alias)\s+"?([^\s"]+)')
        if ($match.Success) {
            $candidates.Add($match.Groups[1].Value)
        }
    }
    if (Test-Path -LiteralPath $script:Paths.ManagedProfiles -PathType Container) {
        foreach ($profile in @(Get-ChildItem -LiteralPath $script:Paths.ManagedProfiles -Filter '*.yaml' -File -ErrorAction SilentlyContinue)) {
            $candidates.Add($profile.BaseName)
        }
    }
    # A managed installation may use an alias that is not encoded in the
    # profile filename.  `runtimes list` is read-only; only aliases whose
    # returned record contains this installation's authorized tunnel ID are
    # retained for the subsequent status/stop call.
    try {
        $listOutput = & $script:Paths.TunnelClient runtimes list --json 2>&1
        if ($LASTEXITCODE -eq 0) {
            $listText = ($listOutput -join "`n")
            foreach ($candidate in ([regex]::Matches($listText, '(?i)(?:alias|name|runtime_alias)\s*:\s*"([^"]+)"'))) {
                if ($candidate.Success) {
                    $candidates.Add($candidate.Groups[1].Value)
                }
            }
        }
    }
    catch {
        # Direct tunnel installations do not implement the runtimes command.
    }
    return @($candidates | Where-Object { -not [string]::IsNullOrWhiteSpace($_) } | Select-Object -Unique)
}

function Try-StopManagedTunnel {
    param(
        [Parameter(Mandatory = $true)][int]$TunnelPid,
        [AllowNull()]$Record
    )

    foreach ($alias in @(Get-ManagedTunnelAlias -Record $Record)) {
        $statusOutput = & $script:Paths.TunnelClient runtimes status $alias --json 2>&1
        $statusExit = $LASTEXITCODE
        $statusText = ($statusOutput -join "`n")
        if ($statusExit -ne 0 -or $statusText -notmatch [regex]::Escape("tunnel_6a8ef626bf008191a6294996145747e5")) {
            continue
        }
        $stopOutput = & $script:Paths.TunnelClient runtimes stop $alias --json 2>&1
        $stopExit = $LASTEXITCODE
        if ($stopExit -ne 0) {
            continue
        }
        $deadline = (Get-Date).AddSeconds($TunnelStopTimeoutSeconds)
        while ((Get-Date) -lt $deadline) {
            if ($null -eq (Get-Process -Id $TunnelPid -ErrorAction SilentlyContinue)) {
                return $true
            }
            Start-Sleep -Milliseconds 250
        }
    }
    return $false
}

function Test-ManagedTunnelHint {
    param([AllowNull()]$Record)

    if (![string]::IsNullOrWhiteSpace($env:BRIDGE_TUNNEL_RUNTIME_ALIAS)) {
        return $true
    }
    if ($null -eq $Record) {
        return $false
    }
    $commandLine = ([string]$Record.CommandLine).Replace('/', '\')
    $managedNorm = ($script:Paths.ManagedProfiles + '\').Replace('/', '\')
    return $commandLine.IndexOf($managedNorm, [StringComparison]::OrdinalIgnoreCase) -ge 0 -or
        $commandLine -match '(?i)\bruntimes\b|--runtime[-_.]?alias'
}

function Find-VerifiedTunnelProcess {
    # A missing/invalid tunnel.pid is not proof that no D3 tunnel is alive.
    # Enumerate processes, select only the exact installed executable, and
    # require complete command-line/profile evidence for every candidate.
    try {
        $processes = @(Get-Process -ErrorAction Stop)
    }
    catch {
        throw "Unable to enumerate processes while discovering the Bridge tunnel: $($_.Exception.Message)"
    }

    $expectedTunnel = (Resolve-Path -LiteralPath $script:Paths.TunnelClient -ErrorAction Stop).Path
    $expectedProcessName = [IO.Path]::GetFileNameWithoutExtension($expectedTunnel)
    $candidates = [System.Collections.Generic.List[object]]::new()
    foreach ($processSnapshot in $processes) {
        # A process can exit between the enumeration and property access.  A
        # fresh lookup avoids treating that normal race as an ambiguous live
        # candidate.
        $process = Get-Process -Id ([int]$processSnapshot.Id) -ErrorAction SilentlyContinue
        if ($null -eq $process) {
            continue
        }
        $processName = [string]$process.ProcessName
        $actualPath = $null
        try {
            $actualPath = $process.Path
        }
        catch {
            # A protected non-tunnel process is outside the candidate scope;
            # a process whose image name is the installed tunnel-client is
            # ambiguous and therefore fails closed.
            if (!$processName.Equals($expectedProcessName, [StringComparison]::OrdinalIgnoreCase)) {
                continue
            }
            throw "Unable to inspect executable path for PID $($process.Id) during tunnel discovery."
        }
        if ([string]::IsNullOrWhiteSpace($actualPath)) {
            if (!$processName.Equals($expectedProcessName, [StringComparison]::OrdinalIgnoreCase)) {
                continue
            }
            throw "Executable path is unavailable for PID $($process.Id) during tunnel discovery."
        }
        try {
            $actualCanonical = (Resolve-Path -LiteralPath $actualPath -ErrorAction Stop).Path
        }
        catch {
            if (!$processName.Equals($expectedProcessName, [StringComparison]::OrdinalIgnoreCase)) {
                continue
            }
            throw "Executable path for PID $($process.Id) cannot be resolved during tunnel discovery."
        }
        if ($actualCanonical -ine $expectedTunnel) {
            continue
        }

        $identity = Get-VerifiedProcess -ProcessId ([int]$process.Id) -Kind tunnel
        if ($identity.Status -eq "ABSENT") {
            continue
        }
        if ($identity.Status -ne "VERIFIED") {
            throw "A live process uses this installation's tunnel-client but cannot be identified unambiguously (PID $($process.Id)): $($identity.Reason)"
        }
        $candidates.Add($identity)
    }

    if ($candidates.Count -gt 1) {
        throw "Multiple live processes match the authorized Bridge tunnel executable/profile; refusing ambiguous termination."
    }
    if ($candidates.Count -eq 1) {
        return $candidates[0]
    }
    return $null
}

function Test-HealthEndpointLive {
    if (!(Test-Path -LiteralPath $script:Paths.Health -PathType Leaf)) {
        return $false
    }
    $healthValue = (Get-Content -LiteralPath $script:Paths.Health -Raw -ErrorAction Stop).Trim()
    if ([string]::IsNullOrWhiteSpace($healthValue)) {
        return $false
    }
    try {
        $healthProbe = Invoke-WebRequest -Uri ($healthValue.TrimEnd('/') + '/readyz') -UseBasicParsing -TimeoutSec 2 -ErrorAction Stop
        return [int]$healthProbe.StatusCode -eq 200
    }
    catch {
        return $false
    }
}

function Clear-TunnelMarkers {
    if (Test-Path -LiteralPath $script:Paths.TunnelPid -PathType Leaf) {
        Remove-Item -LiteralPath $script:Paths.TunnelPid -Force -Confirm:$false -ErrorAction Stop
    }
    if (Test-Path -LiteralPath $script:Paths.Health -PathType Leaf) {
        Remove-Item -LiteralPath $script:Paths.Health -Force -Confirm:$false -ErrorAction Stop
    }
}

function Stop-VerifiedTunnelProcess {
    param(
        [Parameter(Mandatory = $true)][int]$TunnelPid,
        [Parameter(Mandatory = $true)]$Identity
    )

    if ($Identity.Status -ne "VERIFIED") {
        throw "The tunnel PID $TunnelPid is not fully identified; refusing termination."
    }

    $managedStopped = $false
    if (Test-ManagedTunnelHint -Record $Identity.Record) {
        $managedStopped = Try-StopManagedTunnel -TunnelPid $TunnelPid -Record $Identity.Record
    }
    if (!$managedStopped -and $null -ne (Get-Process -Id $TunnelPid -ErrorAction SilentlyContinue)) {
        # Direct tunnel-client runs have no reliable local graceful API.  The
        # complete PID, executable, command-line, and profile identity above
        # authorize termination of this one D3 tunnel only.
        $recheck = Get-VerifiedProcess -ProcessId $TunnelPid -Kind tunnel
        if ($recheck.Status -ne "VERIFIED") {
            throw "The tunnel identity changed during shutdown; refusing forced termination."
        }
        Stop-Process -Id $TunnelPid -Force -Confirm:$false -ErrorAction Stop
        $deadline = (Get-Date).AddSeconds($TunnelStopTimeoutSeconds)
        while ((Get-Date) -lt $deadline -and $null -ne (Get-Process -Id $TunnelPid -ErrorAction SilentlyContinue)) {
            Start-Sleep -Milliseconds 250
        }
        if ($null -ne (Get-Process -Id $TunnelPid -ErrorAction SilentlyContinue)) {
            throw "The identified tunnel-client could not be stopped."
        }
    }
}

function Invoke-TunnelStop {
    $pidSidecar = Read-PidSidecar $script:Paths.TunnelPid
    $candidatePid = 0
    $identity = $null

    if ($pidSidecar.Valid) {
        $candidatePid = $pidSidecar.Value
        $identity = Get-VerifiedProcess -ProcessId $candidatePid -Kind tunnel
        if ($identity.Status -eq "ABSENT") {
            # The sidecar is stale; discover a replacement D3 process before
            # assuming that the tunnel is gone.
            $identity = Find-VerifiedTunnelProcess
            if ($null -ne $identity) {
                $candidatePid = [int]$identity.Process.Id
            }
        }
        elseif ($identity.Status -ne "VERIFIED") {
            throw "The recorded tunnel PID $candidatePid is live but cannot be identified unambiguously: $($identity.Reason)"
        }
    }
    else {
        # Missing/invalid tunnel.pid requires the same safe process discovery;
        # a health file alone is never used as a termination authorization.
        $identity = Find-VerifiedTunnelProcess
        if ($null -ne $identity) {
            $candidatePid = [int]$identity.Process.Id
        }
    }

    if ($null -eq $identity) {
        if (Test-HealthEndpointLive) {
            throw "health.url reports a live Bridge tunnel but no uniquely identified D3 tunnel process was found; refusing ambiguous termination."
        }
        Clear-TunnelMarkers
        Write-Output "TUNNEL_PID=NONE"
        Write-MachineResult "TUNNEL_STOP" "PASS"
        return
    }

    Stop-VerifiedTunnelProcess -TunnelPid $candidatePid -Identity $identity
    Clear-TunnelMarkers
    Write-Output ("TUNNEL_PID=" + $candidatePid)
    Write-MachineResult "TUNNEL_STOP" "PASS"
}

function Invoke-ProcessScanForBridge {
    # CIM is best-effort because managed Windows hosts may deny process
    # enumeration.  The archive operation itself remains the final guard: an
    # open MCP handle makes the atomic directory move fail closed.
    try {
        $records = @(Get-CimInstance Win32_Process -OperationTimeoutSec 3 -ErrorAction Stop)
    }
    catch {
        Write-MachineResult "PROCESS_SCAN" "UNAVAILABLE"
        return
    }
    $dbNorm = $script:Paths.Db.Replace('/', '\')
    foreach ($record in $records) {
        $commandLine = [string]$record.CommandLine
        if ([string]::IsNullOrWhiteSpace($commandLine)) {
            continue
        }
        $normalized = $commandLine.Replace('/', '\')
        if ($normalized.IndexOf($dbNorm, [StringComparison]::OrdinalIgnoreCase) -ge 0 -and
            ($normalized -match 'chatgpt_codex_bridge\.mcp_server' -or
             $normalized -match 'chatgpt_codex_bridge\.execution_worker')) {
            throw "A Bridge worker or MCP process still references the database (PID $($record.ProcessId))."
        }
    }
    Write-MachineResult "PROCESS_SCAN" "PASS"
}

function Invoke-BridgeLockProbe {
    # Process enumeration can be denied by Windows policy.  The Bridge owners'
    # own OS-backed locks are authoritative in that case: acquiring and
    # releasing both proves that no MCP or worker owner still has the database
    # open before the state directory is moved.
    if (!(Test-Path -LiteralPath $script:Paths.State -PathType Container)) {
        # There is no database or lock anchor from which an owner could be
        # active.  Avoid creating a synthetic state directory solely for the
        # probe so a genuinely absent state reports STATE_ARCHIVE=NONE.
        Write-MachineResult "RUNTIME_LOCK_PROBE" "SKIPPED_EMPTY_STATE"
        return
    }
    $probe = @'
import sys
from chatgpt_codex_bridge.single_instance import (
    ExecutionWorkerLock,
    MCPInstanceAlreadyRunningError,
    MCPInstanceLock,
)

path = sys.argv[1]
for lock_type in (MCPInstanceLock, ExecutionWorkerLock):
    lock = lock_type(path)
    try:
        lock.acquire()
    except MCPInstanceAlreadyRunningError:
        print("ACTIVE")
        raise SystemExit(2)
    else:
        lock.release()
print("AVAILABLE")
'@
    $output = & $script:PythonPath -B -c $probe $script:Paths.Db 2>&1
    if ($LASTEXITCODE -ne 0 -or ($output -join "`n") -notmatch "AVAILABLE") {
        throw "A Bridge MCP/worker lock is still held or could not be verified; refusing to archive state."
    }
    Write-MachineResult "RUNTIME_LOCK_PROBE" "PASS"
}

function Invoke-StateArchive {
    Assert-ExistingChildrenContained -Directory $script:Paths.State -Label "state"
    if (!(Test-Path -LiteralPath $script:Paths.State -PathType Container)) {
        Write-MachineResult "STATE_ARCHIVE" "NONE"
        return
    }
    if (!(Test-Path -LiteralPath $script:Paths.StateArchive -PathType Container)) {
        New-Item -ItemType Directory -Path $script:Paths.StateArchive -Force -ErrorAction Stop | Out-Null
    }
    [void](Assert-ContainedPath -Path $script:Paths.StateArchive -Label "state.archive" -RequireDirectory)
    $archiveName = ([DateTime]::UtcNow.ToString("yyyyMMddTHHmmssfffZ")) + "-" + ([Guid]::NewGuid().ToString("N"))
    $archivePath = Join-Path $script:Paths.StateArchive $archiveName
    [void](Assert-ContainedPath -Path $archivePath -Label "state archive destination")
    if (Test-Path -LiteralPath $archivePath) {
        throw "The generated state archive destination already exists; refusing overwrite."
    }
    try {
        # Same-volume directory move: the old state is either still present or
        # wholly visible at the unique archive path; no file-by-file mixing.
        Move-Item -LiteralPath $script:Paths.State -Destination $archivePath -Force:$false -ErrorAction Stop
    }
    catch {
        throw "State archive move failed; the original state was not intentionally deleted: $($_.Exception.Message)"
    }
    if (!(Test-Path -LiteralPath $archivePath -PathType Container) -or
        (Test-Path -LiteralPath $script:Paths.State)) {
        throw "State archive move did not complete atomically."
    }
    # Deliberately do not inspect the archive contents.
    Write-MachineResult "STATE_ARCHIVE" ((Get-CanonicalPath $archivePath -RequireExisting))
}

function Invoke-StateRecreate {
    foreach ($directory in @($script:Paths.State, $script:Paths.Logs, $script:Paths.TunnelState)) {
        if (!(Test-Path -LiteralPath $directory -PathType Container)) {
            New-Item -ItemType Directory -Path $directory -Force -ErrorAction Stop | Out-Null
        }
        [void](Assert-ContainedPath -Path $directory -Label "recreated runtime directory" -RequireDirectory)
    }
    foreach ($marker in @($script:Paths.WorkerPid, $script:Paths.WorkerState, $script:Paths.WorkerStop, $script:Paths.WorkerLock, $script:Paths.McpLock)) {
        if (Test-Path -LiteralPath $marker) {
            Remove-Item -LiteralPath $marker -Force -Confirm:$false -ErrorAction Stop
        }
    }
    foreach ($marker in @($script:Paths.TunnelPid, $script:Paths.Health)) {
        if (Test-Path -LiteralPath $marker) {
            Remove-Item -LiteralPath $marker -Force -Confirm:$false -ErrorAction Stop
        }
    }
    Write-MachineResult "STATE_RECREATED" "PASS"
}

function Invoke-BridgeScript {
    param(
        [Parameter(Mandatory = $true)][string]$ScriptName,
        [string[]]$Arguments = @(),
        [Parameter(Mandatory = $true)][string]$OutputPrefix
    )

    $scriptPath = Join-Path $script:Paths.RepoRoot ("scripts\" + $ScriptName)
    if (!(Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
        throw "Required lifecycle script is missing: $scriptPath"
    }
    $pwsh = (Get-Command pwsh -ErrorAction Stop).Source
    $runId = [Guid]::NewGuid().ToString("N")
    $stdoutPath = Join-Path $script:Paths.Logs ("reset-{0}.stdout.log" -f $runId)
    $stderrPath = Join-Path $script:Paths.Logs ("reset-{0}.stderr.log" -f $runId)
    $childArguments = @("-NoProfile", "-File", $scriptPath) + $Arguments
    # Redirect to files instead of pipes.  start_mcp_tunnel intentionally
    # leaves its tunnel-client descendant alive; a pipe would remain open in
    # that descendant and make this reset wait forever for EOF.
    $child = Start-Process -FilePath $pwsh -ArgumentList $childArguments -WorkingDirectory $script:Paths.RepoRoot -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath -WindowStyle Hidden -PassThru -ErrorAction Stop
    $child.WaitForExit()
    $childOutput = @()
    if (Test-Path -LiteralPath $stdoutPath -PathType Leaf) {
        $childOutput += Get-Content -LiteralPath $stdoutPath -ErrorAction SilentlyContinue
    }
    if (Test-Path -LiteralPath $stderrPath -PathType Leaf) {
        $childOutput += Get-Content -LiteralPath $stderrPath -ErrorAction SilentlyContinue
    }
    $exitCode = $child.ExitCode
    foreach ($line in @($childOutput)) {
        $safeLine = ConvertTo-SafeErrorText $line
        if (![string]::IsNullOrWhiteSpace($safeLine)) {
            Write-Output ("{0}{1}" -f $OutputPrefix, $safeLine)
        }
    }
    return [pscustomobject]@{ ExitCode = $exitCode; Output = @($childOutput) }
}

function Get-DatabaseCounts {
    [void](Assert-ContainedPath -Path $script:Paths.Db -Label "Bridge database" -RequireExisting -RequireLeaf)
    $query = @'
import json
import sqlite3
import sys
from pathlib import Path

path = Path(sys.argv[1]).resolve()
uri = path.as_uri() + "?mode=ro"
connection = sqlite3.connect(uri, uri=True)
try:
    connection.execute("PRAGMA query_only = ON")
    counts = {}
    for table in ("projects", "tasks", "task_events"):
        counts[table] = int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    counts["queued_or_running"] = int(connection.execute(
        "SELECT COUNT(*) FROM tasks WHERE execution_status IN ('QUEUED', 'RUNNING')"
    ).fetchone()[0])
    counts["schema_version"] = int(connection.execute("PRAGMA user_version").fetchone()[0])
    print(json.dumps(counts, separators=(",", ":")))
finally:
    connection.close()
'@
    $output = & $script:PythonPath -B -c $query $script:Paths.Db 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Read-only validation of the Bridge database failed: $(ConvertTo-SafeErrorText (($output | Select-Object -Last 1)))"
    }
    try {
        return (($output -join "`n") | ConvertFrom-Json)
    }
    catch {
        throw "Read-only validation returned invalid database counts."
    }
}

function Assert-EmptyDatabase {
    $counts = Get-DatabaseCounts
    if ([int]$counts.projects -ne 0 -or [int]$counts.tasks -ne 0 -or
        [int]$counts.task_events -ne 0 -or [int]$counts.queued_or_running -ne 0) {
        throw "The new database is not empty (projects=$($counts.projects), tasks=$($counts.tasks), events=$($counts.task_events))."
    }
    if ([int]$counts.schema_version -ne 3) {
        throw "The new database schema version is unexpected: $($counts.schema_version)"
    }
    return $counts
}

function Invoke-DatabaseBootstrap {
    # Instantiate the normal Bridge store before launching the worker.  This
    # leaves a valid empty database even when strict worker identity startup is
    # refused because CIM is unavailable on the host.
    $bootstrap = @'
import sys
from chatgpt_codex_bridge.persistence.sqlite_store import SQLiteBridgeStore

with SQLiteBridgeStore(sys.argv[1]):
    pass
'@
    $output = & $script:PythonPath -B -c $bootstrap $script:Paths.Db 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Normal Bridge database initialization failed: $(ConvertTo-SafeErrorText (($output | Select-Object -Last 1)))"
    }
    Write-MachineResult "DB_BOOTSTRAP" "PASS"
}

function Assert-WorkerActive {
    $pidSidecar = Read-PidSidecar $script:Paths.WorkerPid
    if (!$pidSidecar.Valid) {
        throw "The new worker did not publish a valid PID sidecar."
    }
    $identity = Get-VerifiedProcess -ProcessId $pidSidecar.Value -Kind worker
    if ($identity.Status -ne "VERIFIED") {
        throw "The new execution worker is not verifiably active: $($identity.Reason)"
    }
    return $pidSidecar.Value
}

function Read-ReadyUrl {
    if (!(Test-Path -LiteralPath $script:Paths.Health -PathType Leaf)) {
        throw "The new tunnel did not publish health.url."
    }
    $value = (Get-Content -LiteralPath $script:Paths.Health -Raw -ErrorAction Stop).Trim()
    $uri = [Uri]$null
    if ([string]::IsNullOrWhiteSpace($value) -or
        ![Uri]::TryCreate($value, [UriKind]::Absolute, [ref]$uri) -or
        $uri.Scheme -ne "http" -or $uri.Host -notin @("127.0.0.1", "localhost", "::1")) {
        throw "The new health.url is not a local HTTP URL."
    }
    return $value.TrimEnd('/')
}

function Wait-Ready {
    $baseUrl = Read-ReadyUrl
    $readyUrl = $baseUrl + "/readyz"
    $deadline = (Get-Date).AddSeconds($ReadinessTimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $response = Invoke-WebRequest -Uri $readyUrl -UseBasicParsing -TimeoutSec 3 -ErrorAction Stop
            if ([int]$response.StatusCode -eq 200) {
                return [pscustomobject]@{ BaseUrl = $baseUrl; ReadyUrl = $readyUrl }
            }
        }
        catch {
            # The local listener may still be binding; retry only until the
            # bounded readiness deadline.
        }
        Start-Sleep -Milliseconds 500
    }
    throw "The new tunnel did not report /readyz HTTP 200 before the readiness timeout."
}

function Assert-StableFilesUnchanged {
    foreach ($fileKey in @("Secret", "TunnelClient", "Cloudflared", "Profile")) {
        if (!(Test-Path -LiteralPath $script:Paths[$fileKey] -PathType Leaf)) {
            throw "A stable runtime file disappeared: $($script:Paths[$fileKey])"
        }
        $current = Get-FileSha256 $script:Paths[$fileKey]
        if ($current -ine $script:StableFingerprints[$fileKey]) {
            throw "A stable runtime file changed during reset: $($script:Paths[$fileKey])"
        }
    }
}

try {
    Invoke-Preflight
    Write-MachineResult "PREFLIGHT" "PASS"
    Write-MachineResult "EXTERNAL_REPOS_TOUCHED" "0"

    $script:CurrentPhase = "WORKER_STOP"
    Invoke-WorkerStop

    $script:CurrentPhase = "TUNNEL_STOP"
    Invoke-TunnelStop

    $script:CurrentPhase = "PROCESS_SCAN"
    Invoke-ProcessScanForBridge

    $script:CurrentPhase = "RUNTIME_LOCK_PROBE"
    Invoke-BridgeLockProbe

    $script:CurrentPhase = "STATE_ARCHIVE"
    Invoke-StateArchive

    $script:CurrentPhase = "STATE_RECREATED"
    Invoke-StateRecreate

    $script:CurrentPhase = "DB_BOOTSTRAP"
    Invoke-DatabaseBootstrap

    $script:CurrentPhase = "WORKER_START"
    $workerStart = Invoke-BridgeScript -ScriptName "start_execution_worker.ps1" -Arguments @(
        "-StartupTimeoutSeconds", [string][Math]::Max(5, [Math]::Min(120, $WorkerGracePeriodSeconds))
    ) -OutputPrefix "WORKER_START_OUTPUT="
    if ($workerStart.ExitCode -ne 0) {
        throw "Execution worker start failed with exit code $($workerStart.ExitCode)."
    }
    [void](Assert-WorkerActive)
    Write-MachineResult "WORKER_START" "PASS"

    $script:CurrentPhase = "DB_INITIALIZED"
    [void](Assert-EmptyDatabase)
    Write-MachineResult "DB_INITIALIZED" "PASS"

    $script:CurrentPhase = "TUNNEL_START"
    $tunnelStart = Invoke-BridgeScript -ScriptName "start_mcp_tunnel.ps1" -Arguments @(
        "-ReadinessTimeoutSeconds", [string][Math]::Max(5, [Math]::Min(600, $ReadinessTimeoutSeconds))
    ) -OutputPrefix "TUNNEL_START_OUTPUT="
    if ($tunnelStart.ExitCode -ne 0) {
        $tunnelStartText = ($tunnelStart.Output -join "`n")
        if ($tunnelStartText -match '(?i)readiness|readyz') {
            Write-MachineResult "READINESS" "FAIL"
        }
        throw "MCP tunnel start failed with exit code $($tunnelStart.ExitCode)."
    }
    $tunnelPidSidecar = Read-PidSidecar $script:Paths.TunnelPid
    if (!$tunnelPidSidecar.Valid) {
        throw "The new tunnel did not publish a valid PID sidecar."
    }
    $tunnelIdentity = Get-VerifiedProcess -ProcessId $tunnelPidSidecar.Value -Kind tunnel
    if ($tunnelIdentity.Status -ne "VERIFIED") {
        throw "The new tunnel process is not verifiably active: $($tunnelIdentity.Reason)"
    }
    Write-MachineResult "TUNNEL_START" "PASS"

    $script:CurrentPhase = "READINESS"
    [void](Wait-Ready)
    Write-MachineResult "READINESS" "PASS"

    $script:CurrentPhase = "DOCTOR_WORKER"
    $workerDoctor = Invoke-BridgeScript -ScriptName "doctor_execution_worker.ps1" -OutputPrefix "DOCTOR_WORKER_OUTPUT="
    if ($workerDoctor.ExitCode -ne 0) {
        throw "Execution worker doctor failed with exit code $($workerDoctor.ExitCode)."
    }
    Write-MachineResult "DOCTOR_WORKER" "PASS"

    $script:CurrentPhase = "DOCTOR_TUNNEL"
    $tunnelDoctor = Invoke-BridgeScript -ScriptName "doctor_mcp_tunnel.ps1" -OutputPrefix "DOCTOR_TUNNEL_OUTPUT="
    if ($tunnelDoctor.ExitCode -ne 0) {
        throw "MCP tunnel doctor failed with exit code $($tunnelDoctor.ExitCode)."
    }
    Write-MachineResult "DOCTOR_TUNNEL" "PASS"

    $script:CurrentPhase = "FINAL_READINESS"
    [void](Assert-EmptyDatabase)
    [void](Assert-WorkerActive)
    $finalTunnelPid = Read-PidSidecar $script:Paths.TunnelPid
    if (!$finalTunnelPid.Valid -or
        (Get-VerifiedProcess -ProcessId $finalTunnelPid.Value -Kind tunnel).Status -ne "VERIFIED") {
        throw "The tunnel is not active and verifiable at final readiness."
    }
    [void](Wait-Ready)
    Assert-StableFilesUnchanged
    Write-MachineResult "FINAL_READINESS" "PASS"

    Write-Output "BRIDGE_RESET=PASS"
    Write-Output "READY_FOR_CHATGPT=YES"
    exit 0
}
catch {
    $failedPhase = $script:CurrentPhase
    if (![string]::IsNullOrWhiteSpace($failedPhase) -and !$script:Results.ContainsKey($failedPhase)) {
        Write-MachineResult $failedPhase "FAIL"
    }
    Write-Output ("RESET_ERROR=" + (ConvertTo-SafeErrorText $_))
    Write-Output "BRIDGE_RESET=FAIL"
    Write-Output "READY_FOR_CHATGPT=NO"
    exit 1
}
