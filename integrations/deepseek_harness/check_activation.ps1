# Post-install / post-restart activation check for the Rosetta Harness plugin.
#
# The point of this script is to stop the previous false positive: a 401 from
# the status route, or a recent process start time, only proves that *some*
# current-generation module is answering. It does not prove that the bundle
# installed under %LOCALAPPDATA%\Rosetta\Harness\plugin-current is the bundle
# that is running, nor that the source being reviewed is what was installed.
#
# The check therefore compares three things:
#
#   1. source tree  -> installed bundle      (SHA-256 of index.js, client.js and
#                                             python/rosetta_harness_bridge.py)
#   2. installed bundle -> running process   (the plugin publishes a generation
#                                             digest computed from the same three
#                                             files at load time as the
#                                             x-rosetta-generation response header,
#                                             including on its 401 response)
#   3. running tool list -> expected surface (ros_maintenance must be advertised)
#
# States:
#   NOT INSTALLED / INSTALLED INCOMPLETE
#   INSTALLED BUT STALE        - run setup.ps1
#   SYNCED - RESTART REQUIRED  - files match, but the running process is older
#   ACTIVE                     - the running process loaded the installed bytes
#
# Run:  powershell -NoProfile -ExecutionPolicy Bypass -File check_activation.ps1
param(
    [int]$Port = 0,
    [string]$BaseUrl = '',
    [string]$Workspace = (Get-Location).Path,
    [string]$Bundle = ''
)
$ErrorActionPreference = 'Continue'

function Resolve-HarnessBase {
    param([int]$Explicit, [string]$Url)

    if ($Url) { return $Url.TrimEnd('/') }
    if ($Explicit -gt 0) { return "http://127.0.0.1:$Explicit" }

    $ports = @()
    try {
        $pids = @(Get-Process -ErrorAction SilentlyContinue |
                  Where-Object { $_.ProcessName -like '*DeepSeek*' } |
                  Select-Object -ExpandProperty Id)
        if ($pids.Count -gt 0) {
            $ports = @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
                       Where-Object { $pids -contains $_.OwningProcess -and
                                      $_.LocalAddress -in @('127.0.0.1', '0.0.0.0', '::', '::1') } |
                       Select-Object -ExpandProperty LocalPort -Unique)
        }
    } catch { $ports = @() }

    $notFoundBase = $null
    foreach ($candidate in @($ports) + 19387 | Select-Object -Unique) {
        $base = "http://127.0.0.1:$candidate"
        try {
            $response = Invoke-WebRequest -Uri "$base/rosetta-harness/state" -UseBasicParsing -TimeoutSec 3
            if ($response.StatusCode -eq 200) { return $base }
        } catch {
            if ($_.Exception.Response) {
                $status = [int]$_.Exception.Response.StatusCode
                if ($status -eq 401) { return $base }
                if ($status -eq 404 -and -not $notFoundBase) { $notFoundBase = $base }
            }
        }
    }
    if ($notFoundBase) { return $notFoundBase }
    return 'http://127.0.0.1:19387'
}

function Get-FileDigest {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    try {
        return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
    } catch { return $null }
}

# The exact digest the Host computes at load time. Order and separators must
# match GENERATION_FILES in index.js.
function Get-Generation {
    param([string]$Root)

    $files = [ordered]@{
        'index.js'                         = Join-Path $Root 'index.js'
        'client.js'                        = Join-Path $Root 'client.js'
        'python/rosetta_harness_bridge.py' = Join-Path $Root 'python\rosetta_harness_bridge.py'
    }
    $digests = [ordered]@{}
    $lines = New-Object System.Collections.Generic.List[string]
    foreach ($name in $files.Keys) {
        $digest = Get-FileDigest $files[$name]
        if (-not $digest) {
            return [pscustomobject]@{ Digest = $null; Files = $digests; Missing = $name }
        }
        $digests[$name] = $digest
        $lines.Add("${name}:$digest")
    }
    $payload = ($lines -join "`n") + "`n"
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($payload)
        $digest = ([System.BitConverter]::ToString($sha.ComputeHash($bytes)) -replace '-', '').ToLowerInvariant()
    } finally { $sha.Dispose() }
    return [pscustomobject]@{ Digest = $digest; Files = $digests; Missing = $null }
}

# Read status and response headers even for a 401. Invoke-WebRequest hides the
# headers of an error response on Windows PowerShell 5.1, and the generation
# header is exactly what distinguishes "new module, refused anonymous caller"
# from "old module, refused anonymous caller".
function Invoke-RouteProbe {
    param([string]$Url)

    $request = [System.Net.HttpWebRequest]::Create($Url)
    $request.Method = 'GET'
    $request.Timeout = 15000
    $request.ReadWriteTimeout = 15000
    $request.AllowAutoRedirect = $false
    $request.UserAgent = 'RosettaCheckActivation'
    $response = $null
    $failure = $null
    try {
        $response = $request.GetResponse()
    } catch [System.Net.WebException] {
        $failure = $_.Exception.Message
        $response = $_.Exception.Response
    } catch {
        return [pscustomobject]@{ Status = $null; Headers = @{}; Body = ''; Error = $_.Exception.Message }
    }
    if (-not $response) {
        return [pscustomobject]@{ Status = $null; Headers = @{}; Body = ''; Error = $failure }
    }
    try {
        $headers = @{}
        foreach ($key in $response.Headers.AllKeys) {
            $headers[$key.ToLowerInvariant()] = $response.Headers[$key]
        }
        $reader = New-Object System.IO.StreamReader($response.GetResponseStream())
        $body = $reader.ReadToEnd()
        $reader.Dispose()
        return [pscustomobject]@{
            Status  = [int]$response.StatusCode
            Headers = $headers
            Body    = $body
            Error   = $null
        }
    } catch {
        return [pscustomobject]@{ Status = $null; Headers = @{}; Body = ''; Error = $_.Exception.Message }
    } finally {
        $response.Close()
    }
}

$Base = Resolve-HarnessBase -Explicit $Port -Url $BaseUrl
$Route = "$Base/rosetta-harness/state"
$SourceRoot = $PSScriptRoot
if (-not $Bundle) { $Bundle = Join-Path $env:LOCALAPPDATA 'Rosetta\Harness\plugin-current' }
$failures = 0
$state = 'UNKNOWN'

function Report {
    param([string]$Label, [bool]$Ok, [string]$Detail = '')
    if ($Ok) {
        Write-Host "[PASS] $Label" -ForegroundColor Green
    } else {
        Write-Host "[FAIL] $Label" -ForegroundColor Red
        if ($Detail) { Write-Host "       $Detail" -ForegroundColor DarkGray }
        $script:failures++
    }
}

function Note {
    param([string]$Text, [string]$Colour = 'DarkGray')
    Write-Host "       $Text" -ForegroundColor $Colour
}

Write-Host "Rosetta Harness plugin - activation check"
Write-Host "source : $SourceRoot"
Write-Host "bundle : $Bundle"
Write-Host "route  : $Route"
Write-Host ""

# --- 1. source vs installed bundle -----------------------------------------
$sourceGen = Get-Generation $SourceRoot
$bundleExists = Test-Path -LiteralPath $Bundle -PathType Container
$bundleGen = if ($bundleExists) { Get-Generation $Bundle } else { $null }

Report 'a plugin bundle is installed' $bundleExists @"
No bundle at $Bundle.
Run setup.ps1, install the printed plugin-current path with the Harness plugin
manager on first use, then restart Harness.
"@

$bundleComplete = $bundleExists -and $bundleGen -and $bundleGen.Digest
Report 'the installed bundle contains index.js, client.js and the bridge' ([bool]$bundleComplete) @"
The bundle at $Bundle is missing $($bundleGen.Missing).
"@

$synced = $bundleComplete -and $sourceGen.Digest -and ($bundleGen.Digest -eq $sourceGen.Digest)
if ($bundleComplete) {
    Write-Host "       file hashes (source -> installed):"
    foreach ($name in $sourceGen.Files.Keys) {
        $same = $sourceGen.Files[$name] -eq $bundleGen.Files[$name]
        $mark = if ($same) { 'match' } else { 'DIFFERS' }
        Write-Host ("       {0,-34} {1}  {2}" -f $name, $mark, $bundleGen.Files[$name].Substring(0, 12))
    }
    Note "source generation: $($sourceGen.Digest)"
    Note "bundle generation: $($bundleGen.Digest)"
}
Report 'the installed bundle equals the source being reviewed' $synced @"
The installed bundle was built from different bytes than the source tree.
Run setup.ps1 to sync plugin-current, then restart Harness.
"@

# --- 2. what the running process actually loaded ---------------------------
$probe = Invoke-RouteProbe $Route
$routeReachable = $null -ne $probe.Status
$runningDigest = if ($probe.Headers -and $probe.Headers.ContainsKey('x-rosetta-generation')) {
    [string]$probe.Headers['x-rosetta-generation']
} else { '' }
$runningTools = if ($probe.Headers -and $probe.Headers.ContainsKey('x-rosetta-tools')) {
    [string]$probe.Headers['x-rosetta-tools']
} else { '' }
$runningHasMaintenance = $runningTools -and (($runningTools -split ',') -contains 'ros_maintenance')

Report 'the status route is reachable' $routeReachable @"
No HTTP response at $Route.
HTTP 404 means the plugin's Host half is not active in this process yet.
"@
if ($routeReachable -and $probe.Status -eq 404) {
    Report 'the status route is registered' $false 'HTTP 404 - the Host half is not active in this process. Restart Harness.'
} elseif ($routeReachable) {
    Report 'the status route is registered' $true
}

# A 401 is expected for an anonymous script and is NOT evidence of a new build.
if ($routeReachable -and $probe.Status -ne 404) {
    Note "unauthenticated status: HTTP $($probe.Status) (401 is expected and is not proof of a new build)"
}
Report 'the running module reports its generation digest' ([bool]$runningDigest) @"
No x-rosetta-generation header on the response.
The running Host module predates the generation check (or the plugin is not
active), so this process cannot be proven to run the installed bytes.
Restart DeepSeek Harness.
"@
Report 'the running generation matches the installed bundle' `
    ([bool]$runningDigest -and [bool]$bundleGen -and $runningDigest -eq $bundleGen.Digest) @"
running: $runningDigest
bundle : $($bundleGen.Digest)
A different digest means the process still holds a previous generation: restart Harness.
"@
Report 'the running module advertises ros_maintenance' ([bool]$runningHasMaintenance) @"
x-rosetta-tools: $runningTools
Expected ros_maintenance in the running tool list. Restart after setup; if it is
still absent, the Harness profile is loading a different bundle.
"@

# Process start times are kept as a hint only. Copied files can preserve or
# change mtimes, so this is never used to decide ACTIVE.
Write-Host ""
$procs = @(Get-Process | Where-Object { $_.ProcessName -like '*DeepSeek*' })
if ($procs.Count -gt 0) {
    $oldest = ($procs | Sort-Object StartTime | Select-Object -First 1)
    Note "oldest Harness process started: $($oldest.StartTime)"
    if ($bundleExists) {
        $newestBundleWrite = Get-ChildItem -LiteralPath $Bundle -Recurse -File |
            Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if ($newestBundleWrite -and $newestBundleWrite.LastWriteTime -gt $oldest.StartTime) {
            if ($runningDigest -and $bundleGen -and $runningDigest -eq $bundleGen.Digest) {
                Note "bundle timestamps postdate the app process, but the running generation matches the installed bundle; no restart is indicated" 'DarkGray'
            } else {
                Note "bundle timestamp postdates the app process; this is only a hint. Use the running-generation check above to determine whether a restart is needed" 'Yellow'
            }
        }
    }
} else {
    Note 'no DeepSeek Harness process found (hint only; the route check is authoritative)' 'Yellow'
}

# --- 3. the installed bridge can actually answer ---------------------------
# This exercises the installed bytes, but it proves only that the Python side
# works. It is not evidence about which Host module generation is loaded.
Write-Host ""
$pythonCandidates = @(
    $env:ROSETTA_PYTHON,
    (Join-Path $env:LOCALAPPDATA 'Rosetta\Harness\venv\Scripts\python.exe'),
    (Join-Path $PSScriptRoot '..\..\.verify\Scripts\python.exe'),
    (Get-Command python -ErrorAction SilentlyContinue).Source
)
$python = $pythonCandidates | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
$bridge = if ($bundleExists) {
    Join-Path $Bundle 'python\rosetta_harness_bridge.py'
} else {
    Join-Path $PSScriptRoot 'python\rosetta_harness_bridge.py'
}
$workspace = if ($Workspace -and (Test-Path -LiteralPath $Workspace)) { $Workspace } else { (Get-Location).Path }
if ($python -and (Test-Path -LiteralPath $bridge)) {
    # --activity-mode off keeps a diagnostic probe out of the user's history.
    # An older installed bridge may not know the flag, so inspect it first.
    $bridgeExtra = @()
    if (Select-String -LiteralPath $bridge -Pattern '--activity-mode' -Quiet) {
        $bridgeExtra = @('--activity-mode', 'off')
    }
    $probeCommand = '{"command":"pairs"}'
    $out = $probeCommand | & $python $bridge --allow-root $workspace @bridgeExtra 2>&1
    $lines = @($out | ForEach-Object { [string]$_ } | Where-Object { $_.Trim() })
    $line = if ($lines.Count -gt 0) { $lines[-1] } else { '' }
    $ok = $false
    $count = 0
    try {
        $parsed = $line | ConvertFrom-Json
        $ok = ($parsed.ok -eq $true)
        $count = @($parsed.pairs).Count
    } catch { }
    Report 'the installed bridge answers with the four tool pairs' ($ok -and $count -eq 4) `
        "bridge said: $line"
} else {
    Note 'SKIPPED bridge probe (python or bridge not found)' 'Yellow'
}

# --- 4. verdict ------------------------------------------------------------
Write-Host ""
if (-not $bundleExists) {
    $state = 'NOT INSTALLED'
} elseif (-not $bundleComplete) {
    $state = 'INSTALLED INCOMPLETE'
} elseif (-not $synced) {
    $state = 'INSTALLED BUT STALE'
} elseif (-not $routeReachable -or $probe.Status -eq 404) {
    $state = 'SYNCED - RESTART REQUIRED'
} elseif (-not $runningDigest) {
    $state = 'SYNCED - RUNNING MODULE IS OLDER, RESTART REQUIRED'
} elseif (-not $bundleGen -or $runningDigest -ne $bundleGen.Digest) {
    $state = 'SYNCED - RUNNING MODULE DIFFERS, RESTART REQUIRED'
} elseif (-not $runningHasMaintenance) {
    $state = 'RUNNING MODULE MISSING ros_maintenance, RESTART/REINSTALL REQUIRED'
} else {
    $state = 'ACTIVE'
}

Write-Host "RESULT: $state"
switch ($state) {
    'ACTIVE' {
        Write-Host "The running Harness loaded the installed bundle." -ForegroundColor Green
        Write-Host "Next:"
        Write-Host "  1. ros_doctor (generation line should match the bundle digest)"
        Write-Host "  2. ros_maintenance with no arguments (dry run) - check the report"
        Write-Host "  3. open a demo artifact, preview, then Deliver in the status strip"
        Write-Host "  4. confirm the strip's Details show the short Host bundle value"
    }
    'INSTALLED BUT STALE' {
        Write-Host "The installed bundle is not the source under review." -ForegroundColor Yellow
        Write-Host "Run from $PSScriptRoot :"
        Write-Host "  powershell -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1"
        Write-Host "Then restart DeepSeek Harness and run this check again."
    }
    'SYNCED - RESTART REQUIRED' {
        Write-Host "The bundle is synced, but the Host half is not loaded in this process." -ForegroundColor Yellow
        Write-Host "Restart DeepSeek Harness, then run this check again. Offline tests are not"
        Write-Host "a substitute for this step."
    }
    'SYNCED - RUNNING MODULE IS OLDER, RESTART REQUIRED' {
        Write-Host "The bundle is synced, but the running process predates the generation check." -ForegroundColor Yellow
        Write-Host "The plugin is live, but it is NOT the new build. Restart DeepSeek Harness."
    }
    'SYNCED - RUNNING MODULE DIFFERS, RESTART REQUIRED' {
        Write-Host "The bundle is synced, but the running process loaded different bytes." -ForegroundColor Yellow
        Write-Host "Restart DeepSeek Harness. If this persists, check which path the plugin"
        Write-Host "manager installed and remove any older Rosetta entry."
    }
    'RUNNING MODULE MISSING ros_maintenance, RESTART/REINSTALL REQUIRED' {
        Write-Host "The running generation does not advertise ros_maintenance." -ForegroundColor Yellow
        Write-Host "Restart Harness; if it persists, reinstall the printed plugin-current bundle."
    }
    default {
        Write-Host "The plugin is not installed as this source tree expects." -ForegroundColor Red
        Write-Host "Run setup.ps1, install its printed plugin-current path once with the plugin"
        Write-Host "manager, then restart Harness."
    }
}

if ($state -ne 'ACTIVE') { exit 1 }
exit 0
