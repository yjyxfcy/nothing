# Minimal, repeatable Rosetta demo runs in one PowerShell session.
#
# This is the command-line twin of the in-Harness demo: it drives the same JSON
# bridge the plugin's Host half uses, so a passing run proves the loop works
# even before the Harness plugin is activated.
#
# Run:  pwsh -NoProfile -File demo.ps1
param(
    [string]$Workspace = (Join-Path $PSScriptRoot ('demo-runs\run-' + [guid]::NewGuid().ToString('N')))
)

$ErrorActionPreference = 'Stop'

# Walk up from this script until the standalone distribution root is found.
$Here = $PSScriptRoot
$Release = $null
$cursor = Get-Item $Here
while ($cursor) {
    if (Test-Path (Join-Path $cursor.FullName 'pyproject.toml')) {
        $Release = $cursor.FullName
        break
    }
    $cursor = $cursor.Parent
}
if (-not $Release) {
    throw "Could not locate the rosetta_release root above $Here (expected pyproject.toml)."
}

$PythonCandidates = @()
if ($env:ROSETTA_TEST_PYTHON) { $PythonCandidates += $env:ROSETTA_TEST_PYTHON }
if ($env:LOCALAPPDATA) {
    $PythonCandidates += (Join-Path $env:LOCALAPPDATA 'Rosetta\Harness\venv\Scripts\python.exe')
}
$PythonCandidates += (Join-Path $Release '.verify\Scripts\python.exe')
$Py = $PythonCandidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
$SyncCheck = Join-Path $Here 'tests\check_installed_sync.py'
$Bridge = Join-Path $Here 'python\rosetta_harness_bridge.py'
$Ws     = [System.IO.Path]::GetFullPath($Workspace)

if (-not $Py) {
    throw 'No Rosetta demo interpreter found. Run setup.ps1 first, or set ROSETTA_TEST_PYTHON to an environment with the package installed.'
}
if (-not (Test-Path -LiteralPath $SyncCheck -PathType Leaf)) {
    throw "Installed-package sync check not found: $SyncCheck"
}
$syncOutput = & $Py $SyncCheck $Release 2>&1
$syncExit = $LASTEXITCODE
$syncOutput | ForEach-Object { Write-Host $_ }
if ($syncExit -ne 0) {
    throw 'The selected Python environment does not contain the package built from this source tree. Run setup.ps1 or set ROSETTA_TEST_PYTHON to a synchronized environment; demo stopped before creating artifacts.'
}

if (Test-Path -LiteralPath $Ws) {
    $existing = @(Get-ChildItem -LiteralPath $Ws -Force)
    if ($existing.Count -gt 0) {
        throw "The demo workspace must be empty to avoid overwriting user files: $Ws"
    }
} else {
    New-Item -ItemType Directory -Force -Path $Ws | Out-Null
}

if (-not (Test-Path $Bridge)) { throw "Bridge not found: $Bridge" }

Write-Host "release root : $Release"
Write-Host "python       : $Py"

foreach ($candidate in @(
    @{ Var = 'ROSETTA_TYLAX';  Name = 't2l' },
    @{ Var = 'ROSETTA_PANDOC'; Name = 'pandoc' },
    @{ Var = 'ROSETTA_SV2V';   Name = 'sv2v' }
)) {
    $fromEnv = [Environment]::GetEnvironmentVariable($candidate.Var)
    if ($fromEnv -and (Test-Path $fromEnv)) {
        Set-Item -Path ("env:" + $candidate.Var) -Value $fromEnv
        continue
    }
    $found = Get-Command $candidate.Name -ErrorAction SilentlyContinue
    if ($found) {
        Set-Item -Path ("env:" + $candidate.Var) -Value $found.Source
    }
}

# Windows PowerShell 5.1's `Set-Content -Encoding UTF8` writes a BOM, and a BOM
# is not valid UTF-8 source text for the target formats. Artifacts are written
# without one, and the pipeline form keeps `@'...'@ | Write-Artifact` readable.
$Utf8NoBom = New-Object System.Text.UTF8Encoding $false
function Write-Artifact {
    param(
        [Parameter(Mandatory = $true, Position = 0)][string]$Path,
        [Parameter(Mandatory = $true, ValueFromPipeline = $true)][string[]]$Text
    )
    begin { $buffer = @() }
    process { $buffer += $Text }
    end { [System.IO.File]::WriteAllText($Path, ($buffer -join [Environment]::NewLine), $Utf8NoBom) }
}

function Invoke-Rosetta {
    param(
        [hashtable]$Command,
        # Name of a converter to hide from the child process, to exercise the
        # missing-converter diagnosis without touching this machine's setup.
        [string]$NoConverter
    )

    # The bridge writes diagnostics to stderr and the JSON result to stdout as
    # its last line. Only stdout is captured: `2>&1` (or Start-Process) would
    # either fold the diagnostics into the pipeline or trip over this
    # environment's duplicated NO_PROXY/no_proxy keys, and
    # `$ErrorActionPreference = 'Stop'` would turn an ordinary refusal into a
    # terminating error.
    $json = $Command | ConvertTo-Json -Compress -Depth 5
    $payload = Join-Path $env:TEMP 'rosetta-demo-command.json'
    # ASCII, not UTF8: Windows PowerShell 5.1's `-Encoding UTF8` writes a BOM.
    # The bridge tolerates it either way, but a clean payload keeps the demo
    # usable as an example.
    Set-Content -Path $payload -Value $json -Encoding ASCII

    # PS 5.1 has no Start-Process -Environment, so the environment is scoped by
    # mutating this process and restoring it: the child inherits a snapshot.
    $savedEnv = $null
    if ($NoConverter) {
        $savedEnv = @{}
        foreach ($var in @('ROSETTA_TYLAX', 'ROSETTA_PANDOC', 'ROSETTA_SV2V', 'ROSETTA_TYPST', 'ROSETTA_NUSHELL')) {
            $savedEnv[$var] = [Environment]::GetEnvironmentVariable($var)
        }
        $savedPath = $env:PATH
        switch ($NoConverter) {
            'sv2v' {
                $env:ROSETTA_SV2V = $null
                Remove-Item Env:ROSETTA_SV2V -ErrorAction SilentlyContinue
                $env:PATH = (($env:PATH -split ';') | Where-Object { $_ -and ($_ -notlike '*\tools') }) -join ';'
            }
            default { throw "Unsupported -NoConverter target: $NoConverter" }
        }
    }

    $saved = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $lines = @(& $Py $Bridge --allow-root $Ws --command-file $payload 2>$null)
    } finally {
        $ErrorActionPreference = $saved
        if ($NoConverter) {
            $env:PATH = $savedPath
            foreach ($var in $savedEnv.Keys) {
                if ($null -eq $savedEnv[$var]) {
                    Remove-Item ("Env:" + $var) -ErrorAction SilentlyContinue
                } else {
                    Set-Item -Path ("Env:" + $var) -Value $savedEnv[$var]
                }
            }
        }
    }

    $lines = @($lines | Where-Object { $_ -and $_.Trim() })
    if ($lines.Count -eq 0) {
        throw "The bridge produced no JSON for command '$($Command.command)'."
    }
    return ($lines[-1] | ConvertFrom-Json)
}

$failures = 0
function Assert($Label, $Condition, $Detail) {
    if ($Condition) {
        Write-Host "[PASS] $Label" -ForegroundColor Green
    } else {
        Write-Host "[FAIL] $Label" -ForegroundColor Red
        if ($Detail) { Write-Host "       $Detail" -ForegroundColor DarkGray }
        $script:failures++
    }
}

Write-Host "`n=== 0. diagnostics ===" -ForegroundColor Cyan
$doctor = Invoke-Rosetta @{ command = 'doctor' }
Assert 'bridge answers doctor' ($doctor.ok -eq $true) ($doctor.error | ConvertTo-Json -Compress)
Assert 'installed package located' ($doctor.imports.rosetta_studio -like '*site-packages*') $doctor.imports.rosetta_studio
Assert 'sv2v available' ($doctor.converters.sv2v.found -eq $true) 'set ROSETTA_SV2V'
Assert 'typst available for independent verification' ($doctor.converters.typst.found -eq $true) 'install typst'

$pairs = Invoke-Rosetta @{ command = 'pairs' }
Assert 'four tool pairs discovered' ($pairs.pairs.Count -eq 4) ($pairs.pairs.name -join ', ')

# ---------------------------------------------------------------------------
Write-Host "`n=== 1. Typst -> LaTeX view -> edit -> preview -> write back -> compile ===" -ForegroundColor Cyan
$typ = Join-Path $Ws 'demo-report.typ'
@'
= Quarterly update

The compatibility layer gives an agent a familiar LaTeX view of this document.

== Status

The prototype is *ready for review*.

#show heading: set text(fill: blue)
'@ | Write-Artifact $typ

$open = Invoke-Rosetta @{ command = 'open'; session = 'demo-typst'; pair = 'LaTeX / Typst'; path = $typ }
Assert 'projected to a LaTeX view' ($open.agentView -like '*\documentclass*') ''
Assert 'Typst-only #show preserved as a marked pass-through block' ($open.agentView -like '*@TYPST-PASSTHROUGH*') ''

$edited = $open.agentView -replace [regex]::Escape('\end{document}'), @'
\section{ Next steps}

Ship the compatibility layer.

\end{document}
'@
Assert 'view edit applied' ($edited -ne $open.agentView) ''

$null = Invoke-Rosetta @{ command = 'write'; session = 'demo-typst'; content = $edited }
$preview = Invoke-Rosetta @{ command = 'preview'; session = 'demo-typst' }
Assert 'preview produces Typst' ($preview.targetContent -like '*Next steps*') ''
Assert 'preview wrote nothing to disk' ((Get-Content $typ -Raw) -notlike '*Next steps*') ''
Assert 'preview keeps the Typst-only directive' ($preview.targetContent -like '*#show heading*') ''
# The verdict is conservative now: only a byte fixed point counts as stable, and
# a fresh Typst artifact is not one yet (the engine injects its preamble first).
Assert 'preview reports the round-trip verdict honestly' (
    @('exact', 'unstable', 'unknown') -contains $preview.roundTrip.status
) ($preview.roundTrip | ConvertTo-Json -Compress)
Assert 'a non-exact verdict names what differs' (
    ($preview.roundTrip.status -eq 'exact') -or [bool]$preview.roundTrip.reason
) ($preview.roundTrip | ConvertTo-Json -Compress)

$commit = Invoke-Rosetta @{ command = 'commit'; session = 'demo-typst' }
$written = Get-Content $typ -Raw
Assert 'delivered to Typst' ($written -like '*Next steps*') ''
Assert '#show directive survived delivery verbatim' ($written -like '*#show heading: set text(fill: blue)*') ''
Assert 'typst compile accepts the delivered file' ($commit.verification.ok -eq $true) ($commit.verification.stderr)

# ---------------------------------------------------------------------------
Write-Host "`n=== 2. Nushell R/E: edit R only, E must pass through unchanged ===" -ForegroundColor Cyan
$nuDir = Join-Path $Ws 'nu'
New-Item -ItemType Directory -Force -Path (Join-Path $nuDir 'data') | Out-Null
@'
GET /index 200
POST /submit 201
GET /docs 200
POST /login 200
GET /status 200
'@ | Write-Artifact (Join-Path $nuDir 'data\access.log')
$nu = Join-Path $nuDir 'demo.nu'
@'
print (open --raw "data/access.log" | lines | where {|l| ($l =~ "GET") } | length)
open --raw data/access.log | lines | parse "{method} {path} {status}" | group-by method
'@ | Write-Artifact $nu

$nuOpen = Invoke-Rosetta @{ command = 'open'; session = 'demo-nu'; pair = 'Bash / Nushell (experimental)'; path = $nu }
Assert 'projected to a Bash view' ($nuOpen.agentView -like 'cat data/access.log*') ''
Assert 'Nushell-only block marked as E' ($nuOpen.agentView -like '*@NU-PASSTHROUGH*') ''

$nuEdited = $nuOpen.agentView -replace 'grep GET', 'grep POST'
$null = Invoke-Rosetta @{ command = 'write'; session = 'demo-nu'; content = $nuEdited }
$nuPreview = Invoke-Rosetta @{ command = 'preview'; session = 'demo-nu' }
Assert 'E block byte-identical in the delivery' ($nuPreview.targetContent -like '*group-by method*') ''
Assert 'R block now filters POST' ($nuPreview.targetContent -like '*=~ "POST"*') ''

$nuCommit = Invoke-Rosetta @{ command = 'commit'; session = 'demo-nu' }
$nuWritten = Get-Content $nu -Raw
Assert 'E block unchanged in the written file' ($nuWritten -like '*group-by method*') ''
Assert 'no pass-through marker leaked into the target' ($nuWritten -notlike '*@NU-PASSTHROUGH*') ''
Assert 'nu runs the delivered file' ($nuCommit.verification.ok -eq $true) ($nuCommit.verification.stderr)
# The script prints the R count first, then the E grouping table. After the edit
# the count must be the POST count (2), and the untouched E block must still
# group both methods.
$nuStdout = [string]$nuCommit.verification.stdout
$nuFirstLine = ($nuStdout -split "`r?`n" | Where-Object { $_.Trim() } | Select-Object -First 1)
Assert 'real execution reports the POST count (2)' ($nuFirstLine.Trim() -eq '2') ("first line=" + $nuFirstLine)
Assert 'E block still groups GET and POST at runtime' (($nuStdout -match 'GET') -and ($nuStdout -match 'POST')) 'grouping table missing a method'

# ---------------------------------------------------------------------------
Write-Host "`n=== 3. SystemVerilog -> Verilog view -> edit -> deliver ===" -ForegroundColor Cyan
$sv = Join-Path $Ws 'demo-counter.sv'
@'
module counter (
    input  logic       clk,
    input  logic       rst,
    output logic [3:0] count
);
    always_ff @(posedge clk) begin
        if (rst) count <= '0;
        else     count <= count + 1'b1;
    end
endmodule
'@ | Write-Artifact $sv

$svOpen = Invoke-Rosetta @{ command = 'open'; session = 'demo-sv'; pair = 'Verilog / SystemVerilog'; path = $sv }
Assert 'projected to a flattened Verilog view' ($svOpen.agentView -like '*always @(posedge clk)*') ''
$svEdited = $svOpen.agentView -replace [regex]::Escape("count <= count + 1'b1;"), "count <= count + 2'd1;"
Assert 'view edit applied' ($svEdited -ne $svOpen.agentView) ''
$null = Invoke-Rosetta @{ command = 'write'; session = 'demo-sv'; content = $svEdited }
$null = Invoke-Rosetta @{ command = 'preview'; session = 'demo-sv' }
$null = Invoke-Rosetta @{ command = 'commit'; session = 'demo-sv' }
Assert 'delivered file is SystemVerilog again' ((Get-Content $sv -Raw) -like '*always_ff*') ''

# ---------------------------------------------------------------------------
Write-Host "`n=== 4. PRQL -> SQL view -> edit -> deliver PRQL ===" -ForegroundColor Cyan
$prql = Join-Path $Ws 'demo-query.prql'
Write-Artifact -Path $prql -Text @'
from employees
select {name, department, salary}
sort {-salary}
take 5
'@

$prqlOpen = Invoke-Rosetta @{ command = 'open'; session = 'demo-prql'; pair = 'PRQL / SQL'; path = $prql }
Assert 'projected to a SQL view' (($prqlOpen.agentView -like '*SELECT*') -and ($prqlOpen.agentView -like '*LIMIT*')) ''

$prqlEdited = $prqlOpen.agentView -replace 'LIMIT\s+5', 'LIMIT 3'
Assert 'view edit applied' ($prqlEdited -ne $prqlOpen.agentView) ''
$null = Invoke-Rosetta @{ command = 'write'; session = 'demo-prql'; content = $prqlEdited }
$prqlPreview = Invoke-Rosetta @{ command = 'preview'; session = 'demo-prql' }
Assert 'delivery preview is PRQL again' (($prqlPreview.targetContent -like '*from employees*') -and ($prqlPreview.targetContent -like '*take 3*')) $prqlPreview.targetContent

$prqlCommit = Invoke-Rosetta @{ command = 'commit'; session = 'demo-prql' }
$prqlWritten = Get-Content $prql -Raw
Assert 'delivered PRQL carries the edit (take 3)' ($prqlWritten -like '*take 3*') $prqlWritten
Assert 'the earlier row limit is gone from the delivered PRQL' ($prqlWritten -notlike '*take 5*') $prqlWritten
Assert 're-projecting the delivered PRQL yields the approved SQL again' (
    (Invoke-Rosetta @{ command = 'open'; session = 'demo-prql-2'; pair = 'PRQL / SQL'; path = $prql }).agentView -like '*LIMIT*3*'
) ''

# ---------------------------------------------------------------------------
Write-Host "`n=== 5. refusals ===" -ForegroundColor Cyan
$outside = Join-Path (Split-Path -Parent $Ws) 'definitely-outside-workspace.typ'
Write-Artifact -Path $outside -Text '= outside'
$escape = Invoke-Rosetta @{ command = 'open'; session = 'x1'; pair = 'LaTeX / Typst'; path = $outside }
Assert 'path outside the workspace refused' ($escape.error.code -eq 'PATH_OUTSIDE_WORKSPACE') ($escape.error.code)

$stale = Join-Path $Ws 'demo-stale.typ'
Write-Artifact -Path $stale -Text @'
= Stale

Original.
'@
$staleOpen = Invoke-Rosetta @{ command = 'open'; session = 'x2'; pair = 'LaTeX / Typst'; path = $stale }
$null = Invoke-Rosetta @{ command = 'write'; session = 'x2'; content = ($staleOpen.agentView + "`n\\section{ Extra}`n`nAdded.`n") }
Write-Artifact -Path $stale -Text @'
#set page(width: 5cm)

= Stale

Changed outside Rosetta.
'@
$staleCommit = Invoke-Rosetta @{ command = 'commit'; session = 'x2' }
Assert 'externally modified target refused' ($staleCommit.error.code -eq 'TARGET_CHANGED') ($staleCommit.error.code)

$missing = Invoke-Rosetta @{ command = 'open'; session = 'x3'; pair = 'LaTeX / Typst'; path = (Join-Path $Ws 'no-such-file.typ') }
Assert 'missing target refused' ($missing.error.code -eq 'TARGET_NOT_FOUND') ($missing.error.code)

$badpair = Invoke-Rosetta @{ command = 'open'; session = 'x4'; pair = 'LaTeX / TypeScript'; path = $typ }
Assert 'unknown pair refused' ($badpair.error.code -eq 'UNKNOWN_PAIR') ($badpair.error.code)

$ext = Invoke-Rosetta @{ command = 'open'; session = 'x5'; pair = 'PRQL / SQL'; path = $typ }
Assert 'extension/pair mismatch refused' ($ext.error.code -eq 'EXTENSION_MISMATCH') ($ext.error.code)

# An out-of-scope construct must be refused, not silently rewritten. `uniq` is
# outside the documented Bash subset, so delivery has to fail explicitly.
$scopeNu = Join-Path $Ws 'demo-scope.nu'
@'
print (open --raw "data/access.log" | lines | where {|l| ($l =~ "GET") } | length)
'@ | Write-Artifact $scopeNu
$scopeOpen = Invoke-Rosetta @{ command = 'open'; session = 'x6'; pair = 'Bash / Nushell (experimental)'; path = $scopeNu }
if ($scopeOpen.ok -eq $true) {
    $scopeDeliver = Invoke-Rosetta @{ command = 'write'; session = 'x6'; content = "cat data.txt | uniq`n" }
    $scopePreview = Invoke-Rosetta @{ command = 'preview'; session = 'x6' }
    Assert 'out-of-scope Bash construct refused explicitly' ($scopePreview.error.code -eq 'DELIVERY_FAILED') ($scopePreview.error.code)
    $null = Invoke-Rosetta @{ command = 'close'; session = 'x6' }
} else {
    Assert 'out-of-scope probe setup' $false $scopeOpen.error.code
}

# An interface-only module has no flat top for sv2v to lower, so the adapter must
# refuse rather than hand back an empty agent view that looks like success.
$iface = Join-Path $Ws 'demo-interface-only.sv'
Write-Artifact -Path $iface -Text @'
interface bus_if(input logic clk);
    logic d;
    modport dut(input clk, output d);
endinterface

module TopModule(bus_if.dut b);
    assign b.d = b.clk;
endmodule
'@
$ifaceOpen = Invoke-Rosetta @{ command = 'open'; session = 'x7'; pair = 'Verilog / SystemVerilog'; path = $iface }
Assert 'interface-only SystemVerilog refused' ($ifaceOpen.error.code -eq 'PROJECTION_FAILED') $ifaceOpen.error.code
Assert 'refusal explains the missing flattening context' (
    (($ifaceOpen.error.errors -join ' ') -like '*empty projection*') -or
    (($ifaceOpen.error.message) -like '*empty projection*')
) ($ifaceOpen.error.errors -join ' | ')

# A missing external converter must be diagnosed, not silently degraded. The
# child inherits an environment with sv2v removed from both the override and PATH.
$noSv2v = Invoke-Rosetta -NoConverter sv2v @{ command = 'open'; session = 'x8'; pair = 'Verilog / SystemVerilog'; path = $sv }
Assert 'SystemVerilog projection without sv2v fails explicitly' ($noSv2v.error.code -eq 'PROJECTION_FAILED') $noSv2v.error.code
Assert 'refusal names the missing binary' ((($noSv2v.error.message) + ' ' + ($noSv2v.error.errors -join ' ')) -like '*sv2v*') $noSv2v.error.message

$noSv2vDoctor = Invoke-Rosetta -NoConverter sv2v @{ command = 'doctor' }
$blockedPairs = @($noSv2vDoctor.pairDiagnostics | Where-Object { $_.pair -like '*Verilog*' })
Assert 'doctor reports the pair as blocked when sv2v is missing' ($blockedPairs.Count -ge 1) ($noSv2vDoctor.pairDiagnostics | ConvertTo-Json -Compress)
Assert 'doctor still lists the other pairs' (@($noSv2vDoctor.converters.PSObject.Properties).Count -ge 5) ''

# ---------------------------------------------------------------------------
Write-Host "`n=== 6. provenance ===" -ForegroundColor Cyan
$auditPayload = Join-Path $env:TEMP 'rosetta-demo-pairs.json'
Set-Content -Path $auditPayload -Value '{"command":"pairs"}' -Encoding ASCII
$saved = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
try {
    $auditLines = @(& $Py $Bridge --allow-root $Ws --command-file $auditPayload --audit-imports 2>$null)
} finally {
    $ErrorActionPreference = $saved
}
$audited = (@($auditLines | Where-Object { $_ -and $_.Trim() })[-1]) | ConvertFrom-Json
Assert 'no module imported from experiments/benchmark/gold/scorer' ($audited.moduleAudit.clean -eq $true) ($audited.moduleAudit.violations | ConvertTo-Json -Compress)
Assert 'audit inspected real imports' ($audited.moduleAudit.moduleCount -gt 50) ("count=" + $audited.moduleAudit.moduleCount)

Remove-Item $outside -Force -ErrorAction SilentlyContinue

Write-Host "`n" + ('=' * 72)
if ($failures -eq 0) {
    Write-Host "ALL CHECKS PASSED" -ForegroundColor Green
} else {
    Write-Host "$failures CHECK(S) FAILED" -ForegroundColor Red
}
Write-Host "demo workspace: $Ws"
exit $failures
