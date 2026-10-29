# Rosetta <-> DeepSeek Harness acceptance runner.
#
# Runs every layer of verification that does not require the Harness process to
# restart, streams the output, and writes a timestamped report under
# `acceptance-reports/` so the result can be reviewed afterwards.
#
# Layers:
#   1. tests/setup_workspace_test.ps1 multi-root config and ignore-file safety
#   2. demo.ps1                        standalone: 4 pairs, R/E, refusals, provenance
#   3. tests/repro_typst_growth.py     Tylax round-trip idempotence regression
#   4. tests/*                         installed package, GUI, adapter and projector tests
#   5. tests/test_projector_safety.py  focused shadow projection safety
#   6. tests/run_bridge_acceptance.py  the JSON contract the Host plugin drives
#   7. tests/test_plugin_regressions.py store/render/fence regressions
#   8. tests/test_commit_recovery.py   delivery crash-recovery behaviour
#   9. tests/test_control_plane.py     preview/commit refusal ordering
#  10. tests/test_retention.py         local-state retention and idle drafts
#  11. tests/host_harness_test.mjs     the Host plugin against a fake Cordis ctx
#  12. tests/fence_test.mjs            fence integrity under failure/races
#  13. tests/client_status_test.mjs    status strip rendering and interaction
#  14. tests/action_hints_test.mjs     code-specific recovery advice
#
# Not covered here: live activation requires a Harness restart (see README).
# activation of the plugin inside the running desktop process.
#
# Run all layers:  powershell -NoProfile -ExecutionPolicy Bypass -File run_acceptance.ps1
# Run one layer: add -OnlyLayer setup-workspaces or -OnlyLayer action-hints
param(
    [ValidateSet('setup-workspaces', 'action-hints')]
    [string]$OnlyLayer
)

$ErrorActionPreference = 'Stop'

$Here = $PSScriptRoot

function Run-Layer {
    param([string]$Name, [string]$Exe, [string[]]$Arguments)

    Write-Host ("=" * 72)
    Write-Host "LAYER: $Name" -ForegroundColor Cyan
    Write-Host ("=" * 72)

    $logPath = Join-Path $ReportDir "$Name.log"
    # Native stderr carries progress and warnings, not just failures, so it is
    # merged into the log and the exit code is what decides pass/fail.
    $saved = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $Exe @Arguments 2>&1 | Tee-Object -FilePath $logPath
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $saved
    }

    $script:results += [pscustomobject]@{
        Layer    = $Name
        ExitCode = $code
        Passed   = ($code -eq 0)
        Skipped  = ($code -eq 77)
        Log      = $logPath
    }
    Write-Host ""
}

if ($OnlyLayer -in @('setup-workspaces', 'action-hints')) {
    $results = @()
    $Stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $ReportDir = Join-Path $Here "acceptance-reports\$OnlyLayer-$Stamp"
    New-Item -ItemType Directory -Force -Path $ReportDir | Out-Null
    if ($OnlyLayer -eq 'setup-workspaces') {
        Run-Layer -Name 'setup-workspaces' -Exe 'powershell.exe' -Arguments @(
            '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', (Join-Path $Here 'tests\setup_workspace_test.ps1')
        )
    } else {
        $Node = $null
        if ($env:LOCALAPPDATA) {
            $bundledNode = Join-Path $env:LOCALAPPDATA 'Programs\DeepSeek Harness\resources\runtime\primary-runtime\dependencies\node\bin\node.exe'
            if (Test-Path $bundledNode) { $Node = $bundledNode }
        }
        if (-not $Node) {
            $onPath = Get-Command node -ErrorAction SilentlyContinue
            if ($onPath) { $Node = $onPath.Source }
        }
        if (-not $Node) { throw 'Node.js not found on PATH or in the Harness runtime.' }
        Run-Layer -Name 'action-hints' -Exe $Node -Arguments @(
            (Join-Path $Here 'tests\action_hints_test.mjs')
        )
    }
    if (@($results | Where-Object { -not $_.Passed }).Count -gt 0) {
        throw "The $OnlyLayer layer failed. See $ReportDir"
    }
    Write-Host "PASS: $OnlyLayer only; report: $ReportDir" -ForegroundColor Green
    exit 0
}

# --- locate the standalone distribution root -------------------------------
$Release = $null
$cursor = Get-Item $Here
while ($cursor) {
    if (Test-Path (Join-Path $cursor.FullName 'pyproject.toml')) {
        $Release = $cursor.FullName
        break
    }
    $cursor = $cursor.Parent
}
if (-not $Release) { throw "Could not locate rosetta_release above $Here." }

# Use the same installed package that setup.ps1 configured for the Harness. A
# repository-local .verify is only a fallback: it can outlive source changes,
# so the sync preflight below must prove that it is not stale before tests run.
$PythonCandidates = @()
if ($env:ROSETTA_TEST_PYTHON) { $PythonCandidates += $env:ROSETTA_TEST_PYTHON }
if ($env:LOCALAPPDATA) {
    $PythonCandidates += (Join-Path $env:LOCALAPPDATA 'Rosetta\Harness\venv\Scripts\python.exe')
}
$PythonCandidates += (Join-Path $Release '.verify\Scripts\python.exe')
$Py = $PythonCandidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
if (-not $Py) {
    throw 'No Rosetta test interpreter found. Run integrations/deepseek_harness/setup.ps1 first, or set ROSETTA_TEST_PYTHON to an environment with the package installed.'
}

$SyncCheck = Join-Path $Here 'tests\check_installed_sync.py'
$syncOutput = & $Py $SyncCheck $Release 2>&1
$syncExit = $LASTEXITCODE
$syncOutput | ForEach-Object { Write-Host $_ }
if ($syncExit -ne 0) {
    throw 'The selected Python environment does not contain the package built from this source tree. Run setup.ps1 or set ROSETTA_TEST_PYTHON to a synchronized environment; acceptance stopped before reporting test results.'
}
$env:ROSETTA_TEST_PYTHON = $Py

# --- locate node (bundled with the desktop app, else PATH) -----------------
$Node = $null
$bundledNode = Join-Path $env:LOCALAPPDATA 'Programs\DeepSeek Harness\resources\runtime\primary-runtime\dependencies\node\bin\node.exe'
if (Test-Path $bundledNode) { $Node = $bundledNode }
if (-not $Node) {
    $onPath = Get-Command node -ErrorAction SilentlyContinue
    if ($onPath) { $Node = $onPath.Source }
}
if (-not $Node) { throw 'Node.js not found on PATH or in the Harness runtime.' }

# --- external converters ---------------------------------------------------
$Utf8NoBom = New-Object System.Text.UTF8Encoding $false
$savedConverters = @{}
$converterSettings = Join-Path $env:LOCALAPPDATA 'Rosetta\Harness\converters.json'
if (Test-Path -LiteralPath $converterSettings) {
    try {
        $saved = Get-Content -LiteralPath $converterSettings -Raw | ConvertFrom-Json
        foreach ($key in @('t2l', 'pandoc', 'sv2v', 'typst', 'nu')) {
            $value = $saved.$key
            if ($value -is [string] -and (Test-Path -LiteralPath $value -PathType Leaf)) {
                $savedConverters[$key] = $value
            }
        }
    } catch {
        Write-Warning 'Saved converter paths could not be read; acceptance will use environment variables and PATH.'
    }
}
foreach ($candidate in @(
    @{ Var = 'ROSETTA_TYLAX';  Name = 't2l';  Key = 't2l' },
    @{ Var = 'ROSETTA_PANDOC'; Name = 'pandoc'; Key = 'pandoc' },
    @{ Var = 'ROSETTA_SV2V';   Name = 'sv2v'; Key = 'sv2v' },
    @{ Var = 'ROSETTA_TYPST';  Name = 'typst'; Key = 'typst' },
    @{ Var = 'ROSETTA_NUSHELL'; Name = 'nu'; Key = 'nu' }
)) {
    $fromEnv = [Environment]::GetEnvironmentVariable($candidate.Var)
    if ($fromEnv -and (Test-Path $fromEnv)) {
        Set-Item -Path ("env:" + $candidate.Var) -Value $fromEnv
        continue
    }
    $found = Get-Command $candidate.Name -ErrorAction SilentlyContinue
    if ($found) {
        Set-Item -Path ("env:" + $candidate.Var) -Value $found.Source
    } elseif ($savedConverters.ContainsKey($candidate.Key)) {
        Set-Item -Path ("env:" + $candidate.Var) -Value $savedConverters[$candidate.Key]
    }
}

$Workspace = Join-Path $Here ('.acceptance-w-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force -Path $Workspace | Out-Null

$Stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$ReportDir = Join-Path $Here "acceptance-reports\$Stamp"
New-Item -ItemType Directory -Force -Path $ReportDir | Out-Null

Write-Host "release root : $Release"
Write-Host "python       : $Py"
Write-Host "node         : $Node"
Write-Host "report       : $ReportDir"
Write-Host ""

$results = @()

Run-Layer -Name 'setup-workspaces' -Exe 'powershell.exe' -Arguments @(
    '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', (Join-Path $Here 'tests\setup_workspace_test.ps1')
)

Run-Layer -Name 'demo' -Exe 'powershell.exe' -Arguments @(
    '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', (Join-Path $Here 'demo.ps1'),
    '-Workspace', $Workspace
)

# Reopen/deliver idempotence: a source directive identical to Tylax's injected
# default used to accumulate on every round. Exit 77 is reported as SKIP only
# when Tylax is not installed; the local configured setup runs this check with
# the actual backend and must not silently fall back to regex.
Run-Layer -Name 'typst-idempotence' -Exe $Py -Arguments @(
    (Join-Path $Here 'tests\repro_typst_growth.py'), '--workspace', $Workspace
)

# Package-level tests cover the public API, installed wheel, Studio GUI, and
# adapter regressions. Keep them in the same run so a green plugin suite cannot
# mask a regression in the independently usable compatibility-layer package.
Run-Layer -Name 'package-tests' -Exe $Py -Arguments @(
    '-m', 'unittest', 'discover', '-s', (Join-Path $Release 'tests'), '-v'
)

# The standalone FileProjector API is also part of the published package. A
# failed conversion must not masquerade as a familiar-language file, and a
# forced refresh must never delete unmanaged files from the shadow directory.
Run-Layer -Name 'projector-safety' -Exe $Py -Arguments @(
    (Join-Path $Release 'tests\test_projector_safety.py'), '-v'
)

Run-Layer -Name 'bridge-acceptance' -Exe $Py -Arguments @(
    (Join-Path $Here 'tests\run_bridge_acceptance.py'), '--workspace', $Workspace, '--python', $Py
)

# The P0/P1 regression suite: store concurrency, the write fence's session-state
# rule, the pre-write stability gate, the conservative stability verdict and the
# state-payload minimisation. Each test names the defect it guards.
Run-Layer -Name 'plugin-regressions' -Exe $Py -Arguments @(
    '-m', 'unittest', 'discover', '-s', (Join-Path $Here 'tests'),
    '-p', 'test_plugin_regressions.py', '-v'
)

# Fault injection on the delivery path: a store update that fails *after* the
# artifact is published must report the truth and stay recoverable, while a
# publish that never landed must leave no recovery record behind.
Run-Layer -Name 'commit-recovery' -Exe $Py -Arguments @(
    (Join-Path $Here 'tests\test_commit_recovery.py'), '-v'
)

# Control plane: a failed preview must not leave a committable preview behind, and
# a delivery the adapters report as not compiling must never be written - including
# when the commit runs from a stored preview and no fresh translation happens.
Run-Layer -Name 'control-plane' -Exe $Py -Arguments @(
    (Join-Path $Here 'tests\test_control_plane.py'), '-v'
)

# Local-state retention: the activity log's metadata/off modes, event and
# commit-history ceilings, and the rule that an idle session releases the fence
# while its undelivered draft survives and still refuses a drifted delivery.
Run-Layer -Name 'retention' -Exe $Py -Arguments @(
    (Join-Path $Here 'tests\test_retention.py'), '-v'
)

if ($Node) {
    Write-Host "host-harness-test uses ROSETTA_TEST_PYTHON=$Py"
    Run-Layer -Name 'host-harness' -Exe $Node -Arguments @(
        (Join-Path $Here 'tests\host_harness_test.mjs'), $Workspace
    )

    # Fence integrity: an unknown state must refuse conservatively, a failed read
    # must not silently allow, and a late-returning stale answer must not drop a
    # newer fence. Needs a writable base because one case runs the real bridge.
    Run-Layer -Name 'fence' -Exe $Node -Arguments @(
        (Join-Path $Here 'tests\fence_test.mjs'), $Here
    )
    Run-Layer -Name 'client-status' -Exe $Node -Arguments @(
        (Join-Path $Here 'tests\client_status_test.mjs')
    )
    Run-Layer -Name 'action-hints' -Exe $Node -Arguments @(
        (Join-Path $Here 'tests\action_hints_test.mjs')
    )
} else {
    Write-Host "SKIPPED: host-harness (no node executable found)" -ForegroundColor Yellow
    $results += [pscustomobject]@{
        Layer = 'host-harness'; ExitCode = -1; Passed = $false; Log = '(node not found)'
    }
    $results += [pscustomobject]@{
        Layer = 'fence'; ExitCode = -1; Passed = $false; Log = '(node not found)'
    }
    $results += [pscustomobject]@{
        Layer = 'client-status'; ExitCode = -1; Passed = $false; Log = '(node not found)'
    }
    $results += [pscustomobject]@{
        Layer = 'action-hints'; ExitCode = -1; Passed = $false; Log = '(node not found)'
    }
}

# --- summary ---------------------------------------------------------------
$failed = @($results | Where-Object { -not $_.Passed -and -not $_.Skipped })
$skipped = @($results | Where-Object { $_.Skipped })

$summary = @()
$summary += "Rosetta <-> DeepSeek Harness acceptance report"
$summary += "generated : $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
$summary += "release   : $Release"
$summary += "python    : $Py"
$summary += "node      : $Node"
$summary += "workspace : $Workspace"
$summary += ""
$summary += "layers:"
foreach ($r in $results) {
    $status = if ($r.Skipped) { 'SKIP' } elseif ($r.Passed) { 'PASS' } else { 'FAIL' }
    $summary += ("  [{0}] {1}  (exit {2})" -f $status, $r.Layer, $r.ExitCode)
}
$summary += ""
$summary += "NOT COVERED BY THIS RUN:"
$summary += "  Activation of the plugin inside the running Harness process."
$summary += "  Restart Harness after installing or updating the plugin, then verify:"
$summary += "    - ros_doctor / the ros_* tools are listed by the Tool Inspect provider"
$summary += "    - http://127.0.0.1:19387/rosetta-harness/state returns JSON (404 until then)"
$summary += "    - the Rosetta status strip appears under the composer"
$summary += "    - check_activation.ps1 reports ACTIVE (it compares source, installed bundle"
$summary += "      and the running x-rosetta-generation header; a mere 401 is not enough)"
$summary += ""
$summary += $(
    if ($failed.Count -eq 0 -and $skipped.Count -eq 0) { "RESULT: ALL LAYERS PASSED" }
    elseif ($failed.Count -eq 0) { "RESULT: ALL REQUIRED LAYERS PASSED; $($skipped.Count) OPTIONAL LAYER(S) SKIPPED" }
    else { "RESULT: $($failed.Count) LAYER(S) FAILED: " + (($failed | ForEach-Object { $_.Layer }) -join ', ') }
)
$summary += ""
$summary += "Per-layer logs:"
foreach ($r in $results) { $summary += "  $($r.Layer): $($r.Log)" }

$summaryText = ($summary -join [Environment]::NewLine)
[System.IO.File]::WriteAllText((Join-Path $ReportDir 'SUMMARY.txt'), $summaryText, $Utf8NoBom)

Write-Host $summaryText
Write-Host ""
Write-Host "report written: $(Join-Path $ReportDir 'SUMMARY.txt')"

exit $failed.Count
