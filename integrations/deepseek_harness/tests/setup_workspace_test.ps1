$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $PSScriptRoot
. (Join-Path $here 'workspace_setup.ps1')

$testRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("rosetta-workspace-setup-test-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $testRoot | Out-Null
try {
    $one = Join-Path $testRoot 'project-one'
    $two = Join-Path $testRoot 'project-two'
    New-Item -ItemType Directory -Path $one, $two | Out-Null

    $roots = @(Resolve-RosettaWorkspaceRoots -Workspace @($one, (Join-Path $one '.'), $two))
    if ($roots.Count -ne 2) { throw "Expected duplicate paths to collapse to 2 roots; got $($roots.Count)." }
    if ($roots[0] -ne [System.IO.Path]::GetFullPath($one) -or $roots[1] -ne [System.IO.Path]::GetFullPath($two)) {
        throw "Workspace order or canonical paths were not preserved: $($roots -join '; ')"
    }

    Initialize-RosettaWorkspaceState -WorkspaceRoots $roots
    foreach ($root in $roots) {
        $ignore = Join-Path $root '.rosetta\.gitignore'
        if (-not (Test-Path -LiteralPath $ignore -PathType Leaf)) { throw "Missing ignore file: $ignore" }
        $ignoreText = Get-Content -LiteralPath $ignore -Raw
        if ($ignoreText -notmatch '(?m)^/\*$' -or $ignoreText -notmatch '(?m)^!/\.gitignore$') {
            throw "Ignore rules are incomplete: $ignore"
        }
    }

    $preexistingWorkspace = Join-Path $testRoot 'preexisting-ignore-project'
    $preexistingState = Join-Path $preexistingWorkspace '.rosetta'
    New-Item -ItemType Directory -Path $preexistingState -Force | Out-Null
    $preexistingIgnore = Join-Path $preexistingState '.gitignore'
    Set-Content -LiteralPath $preexistingIgnore -Value 'user-owned ignore rules'
    Initialize-RosettaWorkspaceState -WorkspaceRoots @($preexistingWorkspace)
    if ((Get-Content -LiteralPath $preexistingIgnore -Raw).Trim() -ne 'user-owned ignore rules') {
        throw 'Setup changed an existing .rosetta/.gitignore file.'
    }

    $yaml = @(Get-RosettaWorkspaceYamlLines -WorkspaceRoots $roots) -join "`n"
    if (($yaml -split "`n").Count -ne 3 -or $yaml -notmatch [regex]::Escape($one) -or $yaml -notmatch [regex]::Escape($two)) {
        throw "Generated plugin config does not contain both roots exactly once:`n$yaml"
    }

    $single = @(Resolve-RosettaWorkspaceRoots -Workspace @($one))
    $singleYaml = @(Get-RosettaWorkspaceYamlLines -WorkspaceRoots $single) -join "`n"
    if ($single.Count -ne 1 -or $single[0] -ne [System.IO.Path]::GetFullPath($one) -or $singleYaml -notmatch [regex]::Escape($one)) {
        throw 'The existing single-workspace invocation no longer produces one allowed root.'
    }

    $converterExplicit = Join-Path $testRoot 'sv2v-explicit.exe'
    $converterOnPath = Join-Path $testRoot 'sv2v-path.exe'
    $converterSaved = Join-Path $testRoot 'sv2v-saved.exe'
    $converterPandocPath = Join-Path $testRoot 'pandoc-path.exe'
    $converterPandocSaved = Join-Path $testRoot 'pandoc-saved.exe'
    $converterNushellSaved = Join-Path $testRoot 'nu-saved.exe'
    foreach ($converterFile in @($converterExplicit, $converterOnPath, $converterSaved, $converterPandocPath, $converterPandocSaved, $converterNushellSaved)) {
        Set-Content -LiteralPath $converterFile -Value 'test executable placeholder'
    }
    $converters = Resolve-RosettaConverterSettings `
        -ExplicitPaths @{ sv2v = $converterExplicit } `
        -PathCommands @{ sv2v = $converterOnPath; pandoc = $converterPandocPath } `
        -SavedPaths @{ sv2v = $converterSaved; pandoc = $converterPandocSaved; nu = $converterNushellSaved }
    if ($converters.sv2v -ne (Resolve-Path -LiteralPath $converterExplicit).Path) {
        throw 'An explicit converter path did not override PATH and the saved path.'
    }
    if ($converters.pandoc -ne (Resolve-Path -LiteralPath $converterPandocPath).Path) {
        throw 'A converter found on PATH did not override the previously saved path.'
    }
    if ($converters.nu -ne (Resolve-Path -LiteralPath $converterNushellSaved).Path) {
        throw 'A valid saved converter path was not used as a fallback.'
    }
    $invalidConverterRejected = $false
    try {
        $null = Resolve-RosettaConverterSettings -ExplicitPaths @{ sv2v = (Join-Path $testRoot 'missing-sv2v.exe') }
    } catch { $invalidConverterRejected = $true }
    if (-not $invalidConverterRejected) { throw 'A missing explicit converter path was accepted.' }

    # Keep workspace mutation after the package install and import check. This
    # ensures a failed first-time install cannot leave .rosetta state behind.
    $setupScript = Get-Content -LiteralPath (Join-Path $here 'setup.ps1') -Raw
    $installPosition = $setupScript.IndexOf("Installing the Python package and dependencies")
    $importPosition = $setupScript.IndexOf("import rosetta_studio, compat_layer")
    $workspaceMutationPosition = $setupScript.IndexOf("Initialize-RosettaWorkspaceState -WorkspaceRoots `$workspaceRoots")
    if ($installPosition -lt 0 -or $importPosition -le $installPosition -or $workspaceMutationPosition -le $importPosition) {
        throw 'Workspace state must only be initialized after package installation and import validation.'
    }
    $converterValidationPosition = $setupScript.IndexOf('The explicit $($spec[0]) converter path is not an existing file: $path')
    $environmentCreationPosition = $setupScript.IndexOf('New-Item -ItemType Directory -Force -Path $setupRoot')
    if ($converterValidationPosition -lt 0 -or $environmentCreationPosition -lt 0 -or $converterValidationPosition -gt $environmentCreationPosition) {
        throw 'Explicit converter paths must be validated before setup creates its local Python environment.'
    }
    if ($setupScript -notmatch 'find_spec\(chr\(112\)\+chr\(114\)\+chr\(113\)\+chr\(108\)\+chr\(99\)\)') {
        throw 'PRQL readiness detection must avoid nested quotes that Windows PowerShell 5.1 strips from native arguments.'
    }

    $filePath = Join-Path $testRoot 'not-a-directory.txt'
    Set-Content -LiteralPath $filePath -Value 'file'
    foreach ($invalid in @($filePath, (Join-Path $testRoot 'missing-project'))) {
        $rejected = $false
        try { $null = Resolve-RosettaWorkspaceRoots -Workspace @($invalid) } catch { $rejected = $true }
        if (-not $rejected) { throw "Invalid workspace was accepted: $invalid" }
    }

    # If junction creation is available, prove that setup skips rather than
    # writing through a reparse-point .rosetta directory.
    $linkedWorkspace = Join-Path $testRoot 'linked-project'
    $outsideTarget = Join-Path $testRoot 'outside-target'
    New-Item -ItemType Directory -Path $linkedWorkspace, $outsideTarget | Out-Null
    $junctionCreated = $false
    try {
        New-Item -ItemType Junction -Path (Join-Path $linkedWorkspace '.rosetta') -Target $outsideTarget | Out-Null
        $junctionCreated = $true
    } catch {
        Write-Warning 'Junction creation is unavailable in this environment; the reparse-point test was skipped.'
    }
    if ($junctionCreated) {
        Initialize-RosettaWorkspaceState -WorkspaceRoots @($linkedWorkspace)
        if (Test-Path -LiteralPath (Join-Path $outsideTarget '.gitignore')) {
            throw 'Setup wrote through a reparse-point .rosetta directory.'
        }
    }

    $linkedIgnoreWorkspace = Join-Path $testRoot 'linked-ignore-project'
    $ignoreTarget = Join-Path $testRoot 'ignore-target.txt'
    New-Item -ItemType Directory -Path (Join-Path $linkedIgnoreWorkspace '.rosetta') -Force | Out-Null
    Set-Content -LiteralPath $ignoreTarget -Value 'leave this file unchanged'
    $ignoreLinkCreated = $false
    try {
        New-Item -ItemType SymbolicLink -Path (Join-Path $linkedIgnoreWorkspace '.rosetta\.gitignore') -Target $ignoreTarget | Out-Null
        $ignoreLinkCreated = $true
    } catch {
        Write-Warning 'File symbolic-link creation is unavailable; the .gitignore reparse-point test was skipped.'
    }
    if ($ignoreLinkCreated) {
        Initialize-RosettaWorkspaceState -WorkspaceRoots @($linkedIgnoreWorkspace)
        if ((Get-Content -LiteralPath $ignoreTarget -Raw).Trim() -ne 'leave this file unchanged') {
            throw 'Setup wrote through a reparse-point .gitignore file.'
        }
    }

    Write-Output 'PASS: multiple workspace roots are resolved and written to plugin config'
    Write-Output 'PASS: duplicate canonical paths are de-duplicated while preserving order'
    Write-Output 'PASS: each ordinary workspace gets a safe .rosetta/.gitignore'
    Write-Output 'PASS: existing .rosetta/.gitignore content is preserved'
    Write-Output 'PASS: invalid workspace paths are rejected'
    Write-Output 'PASS: single-workspace invocation remains compatible'
    Write-Output 'PASS: explicit converter paths override PATH and saved settings'
    Write-Output 'PASS: PATH converters override saved settings and saved paths remain fallbacks'
    Write-Output 'PASS: missing explicit converter paths are rejected before environment creation'
    Write-Output 'PASS: failed package installation cannot leave workspace state behind'
    if ($junctionCreated) { Write-Output 'PASS: reparse-point .rosetta is not followed' }
    if ($ignoreLinkCreated) { Write-Output 'PASS: reparse-point .gitignore is not followed' }
} finally {
    $resolvedTestRoot = [System.IO.Path]::GetFullPath($testRoot)
    $tempRoot = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
    if (-not $resolvedTestRoot.StartsWith($tempRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove test directory outside the temp root: $resolvedTestRoot"
    }
    Remove-Item -LiteralPath $resolvedTestRoot -Recurse -Force -ErrorAction SilentlyContinue
}
