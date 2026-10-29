param(
    [string[]]$Workspace = @((Get-Location).Path),
    [string]$Python = '',
    # Extra flags for `pip install`, for a machine that must build offline, use a
    # warmed cache, or already has the package dependencies installed. Empty by
    # default, so the normal install path is unchanged.
    [string[]]$PipArgs = @(),
    # Optional converter locations override PATH and previously saved paths.
    [string]$T2lPath = '',
    [string]$PandocPath = '',
    [string]$Sv2vPath = '',
    [string]$TypstPath = '',
    [string]$NushellPath = '',
    # Refresh only the Harness bundle and local workspace/converter settings.
    # The installed Python package must already match src/ exactly.
    [switch]$SkipPackageInstall
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$workspaceSetup = Join-Path $PSScriptRoot 'workspace_setup.ps1'
. $workspaceSetup
$workspaceRoots = @(Resolve-RosettaWorkspaceRoots -Workspace $Workspace)
$workspaceRoot = $workspaceRoots[0]
$setupRoot = Join-Path $env:LOCALAPPDATA 'Rosetta\Harness'
$explicitConverters = @{}
foreach ($spec in @(
    @('t2l', 'T2lPath'),
    @('pandoc', 'PandocPath'),
    @('sv2v', 'Sv2vPath'),
    @('typst', 'TypstPath'),
    @('nu', 'NushellPath')
)) {
    $parameterName = [string]$spec[1]
    if ($PSBoundParameters.ContainsKey($parameterName)) {
        $path = [string]$PSBoundParameters[$parameterName]
        if ([string]::IsNullOrWhiteSpace($path) -or -not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw "The explicit $($spec[0]) converter path is not an existing file: $path"
        }
        $explicitConverters[[string]$spec[0]] = (Resolve-Path -LiteralPath $path -ErrorAction Stop).Path
    }
}
$venv = Join-Path $setupRoot 'venv'
$venvPython = Join-Path $venv 'Scripts\python.exe'
# Stable bundle directory, not a timestamped one. Re-running setup overwrites
# this directory in place, so the profile's installed link keeps resolving to
# the same package and a second timestamped bundle cannot be added by accident.
$bundle = Join-Path $setupRoot 'plugin-current'

if (-not (Test-Path -LiteralPath $venvPython)) {
    New-Item -ItemType Directory -Force -Path $setupRoot | Out-Null
    if ($Python) {
        $basePython = & $Python -c 'import sys; print(sys._base_executable)'
        if ($LASTEXITCODE -ne 0 -or -not $basePython) { throw 'The selected Python interpreter is unavailable.' }
        & $basePython -m venv $venv
    } elseif (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3 -m venv $venv
    } else {
        & python -m venv $venv
    }
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the Python environment.' }
}

$pythonVersion = & $venvPython -c 'import sys; print(sys.version_info[0], sys.version_info[1], sys.version_info[2], sep=chr(46))'
if ($LASTEXITCODE -ne 0 -or -not $pythonVersion) {
    throw "Could not read the selected Python version from: $venvPython"
}
if ([version]$pythonVersion -lt [version]'3.10') {
    throw "Rosetta requires Python 3.10 or newer; the selected interpreter is $pythonVersion. Re-run with -Python <path-to-python-3.10-or-newer>."
}

& $venvPython -c 'import importlib.util, sys; sys.exit(0 if importlib.util.find_spec(chr(112)+chr(105)+chr(112)) else 1)' 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host 'pip is missing from the Rosetta environment; trying Python ensurepip.'
    & $venvPython -m ensurepip --upgrade
    if ($LASTEXITCODE -ne 0) {
        throw "pip is unavailable and ensurepip could not install it in $venvPython. Recreate the venv with a Python build that includes ensurepip."
    }
}

$pipOptions = @($PipArgs | ForEach-Object { [string]$_ -split '\s+' } | Where-Object { $_ })
if ($SkipPackageInstall) {
    Write-Host 'Skipping pip; verifying the installed Python package matches this source tree.'
    & $venvPython (Join-Path $PSScriptRoot 'tests\check_installed_sync.py') $projectRoot
    if ($LASTEXITCODE -ne 0) {
        throw 'The installed Python package is missing or stale. Re-run setup without -SkipPackageInstall to install it before refreshing the plugin bundle.'
    }
} elseif ($pipOptions.Count -eq 0 -and (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host 'Installing Rosetta and dependencies with uv.'
    & uv pip install --python $venvPython $projectRoot
    if ($LASTEXITCODE -ne 0) { throw 'uv installation failed. Check network access and rerun setup, or pass -PipArgs to use pip.' }
} else {
    # Keep the PEP 517 backend in the dedicated venv. Otherwise pip creates a
    # throwaway build environment on every local-source install and an update
    # can unexpectedly require network access just to fetch Hatchling again.
    $hasBuildBackend = $false
    # Inspect pip's plain-text metadata rather than passing a quoted Python
    # one-liner through Windows PowerShell 5.1's legacy native-argument parser.
    # A fresh venv normally reports "Package(s) not found: hatchling" on stderr.
    # With ErrorActionPreference=Stop, PowerShell can promote that expected
    # native stderr to a terminating error before we get a chance to bootstrap
    # the backend. Temporarily make this probe non-terminating and restore the
    # caller's policy immediately afterward.
    $savedErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $hatchlingInfo = & $venvPython -m pip show hatchling 2>$null
        $hatchlingProbeExitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $savedErrorActionPreference
    }
    if ($hatchlingProbeExitCode -eq 0) {
        $versionLine = $hatchlingInfo | Where-Object { $_ -match '^Version:\s*(\d+\.\d+)' } | Select-Object -First 1
        if ($versionLine -and $versionLine -match '^Version:\s*(\d+\.\d+)') {
            $hasBuildBackend = ([version]$Matches[1] -ge [version]'1.25')
        }
    }
    if (-not $hasBuildBackend) {
        $bootstrapOptions = @($pipOptions | Where-Object { $_ -notin @('--no-deps', '--no-build-isolation') })
        Write-Host 'Installing the Hatchling build backend into the Rosetta environment.'
        & $venvPython -m pip install @bootstrapOptions 'hatchling>=1.25'
        if ($LASTEXITCODE -ne 0) {
            throw 'Could not install Hatchling into the Rosetta environment. Check PyPI access, or provide Hatchling and its dependencies in the offline wheel cache before rerunning setup.'
        }
    }
    $pipCommand = @('-m', 'pip', 'install') + $pipOptions + @($projectRoot)
    if ('--no-build-isolation' -notin $pipOptions) {
        $pipCommand = @('-m', 'pip', 'install', '--no-build-isolation') + $pipOptions + @($projectRoot)
    }
    if ($pipOptions.Count -gt 0) { Write-Host "pip options: $($pipOptions -join ' ')" }
    Write-Host 'Installing the Python package and dependencies; the first install needs access to PyPI.'
    & $venvPython @pipCommand
    if ($LASTEXITCODE -ne 0) { throw 'Rosetta package installation failed. Check PyPI connectivity and pip proxy settings, then rerun setup.' }
}
& $venvPython -c 'import rosetta_studio, compat_layer'
if ($LASTEXITCODE -ne 0) { throw 'The installed Rosetta package could not be imported.' }

# Do not touch a user's workspace until package installation/import validation
# has succeeded. A failed first-time pip install should leave no .rosetta state
# directory behind in the project.
Initialize-RosettaWorkspaceState -WorkspaceRoots $workspaceRoots

if (Test-Path -LiteralPath $bundle) {
    $existingBundle = Get-Item -LiteralPath $bundle -Force
    if ($existingBundle.LinkType) {
        throw "The stable bundle path is a link ($($existingBundle.LinkType)): $bundle. Remove it manually and re-run setup."
    }
    if (-not $existingBundle.PSIsContainer) {
        throw "The stable bundle path is not a directory: $bundle"
    }
    Write-Host "Updating existing stable bundle: $bundle"
}
New-Item -ItemType Directory -Force -Path (Join-Path $bundle 'python') | Out-Null
foreach ($file in @('package.json', 'index.js', 'client.js')) {
    Copy-Item -Force -LiteralPath (Join-Path $PSScriptRoot $file) -Destination (Join-Path $bundle $file)
}
Copy-Item -Force -LiteralPath (Join-Path $PSScriptRoot 'python\rosetta_harness_bridge.py') -Destination (Join-Path $bundle 'python\rosetta_harness_bridge.py')

function Quote-Yaml([string]$value) {
    return "'" + $value.Replace("'", "''") + "'"
}
$lines = @(
    '- insert:',
    '    - id: rosetta-compat',
    "      name: '@local/dsh-rosetta-compat'",
    '      config:',
    "        pythonExecutable: $(Quote-Yaml $venvPython)"
)
$lines += Get-RosettaWorkspaceYamlLines -WorkspaceRoots $workspaceRoots
$converters = @{}
$converterSettings = Join-Path $setupRoot 'converters.json'
$savedConverters = @{}
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
        Write-Warning 'Saved converter paths could not be read; setup will detect tools from PATH.'
    }
}
$pathCommands = @{}
foreach ($item in @(
    @('t2l', 't2l'),
    @('pandoc', 'pandoc'),
    @('sv2v', 'sv2v'),
    @('typst', 'typst'),
    @('nu', 'nu')
)) {
    $command = Get-Command $item[1] -ErrorAction SilentlyContinue
    if ($command -and $command.Source) { $pathCommands[$item[0]] = $command.Source }
}
$converters = Resolve-RosettaConverterSettings `
    -ExplicitPaths $explicitConverters `
    -PathCommands $pathCommands `
    -SavedPaths $savedConverters
if ($converters.Count) {
    $lines += '        convert:'
    foreach ($key in @('t2l', 'pandoc', 'sv2v', 'typst', 'nu')) {
        if ($converters.ContainsKey($key)) {
            $lines += "          ${key}: $(Quote-Yaml $converters[$key])"
        }
    }
}
$converterJson = ConvertTo-Json -InputObject $converters -Compress
[System.IO.File]::WriteAllText($converterSettings, $converterJson, [System.Text.UTF8Encoding]::new($false))
$workspaceSettings = Join-Path $setupRoot 'workspace.json'
$workspaceJson = ConvertTo-Json -InputObject @{ workspace = $workspaceRoot } -Compress
[System.IO.File]::WriteAllText($workspaceSettings, $workspaceJson, [System.Text.UTF8Encoding]::new($false))
$patch = Join-Path $bundle 'cordis.patch.yml'
[System.IO.File]::WriteAllLines($patch, $lines, [System.Text.UTF8Encoding]::new($false))

Write-Host "Rosetta package ready: $venvPython"
foreach ($root in $workspaceRoots) { Write-Host "Allowed workspace: $root" }
Write-Host "Plugin bundle (stable): $bundle"
foreach ($tool in @('t2l', 'pandoc', 'sv2v', 'typst', 'nu')) {
    if ($converters.ContainsKey($tool)) {
        $source = 'saved in plugin config'
        if ($explicitConverters.ContainsKey($tool)) { $source = 'explicit setup path' }
        elseif ($pathCommands.ContainsKey($tool)) { $source = 'found on PATH' }
        Write-Host "  $tool : $($converters[$tool]) ($source)"
    } else {
        Write-Host "  $tool : missing (install it if your chosen tool pair needs it)"
    }
}
$hasTylaxBackend = $converters.ContainsKey('t2l')
$hasPandocBackend = $converters.ContainsKey('pandoc')
$hasTypstValidator = $converters.ContainsKey('typst')
$hasSv2vBackend = $converters.ContainsKey('sv2v')
$hasNushellValidator = $converters.ContainsKey('nu')
$null = & $venvPython -c 'import importlib.util, sys; sys.exit(0 if importlib.util.find_spec(chr(112)+chr(114)+chr(113)+chr(108)+chr(99)) else 1)' 2>$null
$hasPrqlCompiler = $LASTEXITCODE -eq 0
Write-Host ''
Write-Host 'Pair readiness (based on this setup environment):'
if ($hasTylaxBackend -or $hasPandocBackend) {
    $typstBackends = @()
    if ($hasTylaxBackend) { $typstBackends += 'Tylax' }
    if ($hasPandocBackend) { $typstBackends += 'Pandoc' }
    Write-Host "  LaTeX / Typst : available; projection backend(s): $($typstBackends -join ', ')"
} else {
    Write-Host '  LaTeX / Typst : available with the built-in subset; install Tylax or Pandoc for broader projection coverage'
}
if ($hasTypstValidator) {
    Write-Host '  Typst compile validation: available'
} else {
    Write-Host '  Typst compile validation: unavailable (translation can run, but compile checks cannot)'
}
if ($hasSv2vBackend) {
    Write-Host '  Verilog / SystemVerilog : available'
} else {
    Write-Host '  Verilog / SystemVerilog : blocked until sv2v is installed or configured'
}
if ($hasPrqlCompiler) {
    Write-Host '  PRQL / SQL : available'
} else {
    Write-Host '  PRQL / SQL : blocked because the Python prqlc dependency is unavailable'
}
if ($hasNushellValidator) {
    Write-Host '  Bash / Nushell : experimental; Nushell validation is available'
} else {
    Write-Host '  Bash / Nushell : experimental; install Nushell to validate generated scripts'
}
Write-Host ''
Write-Host 'Next: in DeepSeek Harness, install this local bundle using plugin_manager the first time:'
Write-Host ('  plugin_manager { action: "install_bundle", target: "' + $bundle + '" }')
Write-Host 'Then restart Harness. Re-running setup later updates the same stable directory in place;'
Write-Host 'no new bundle is created, so just restart Harness to load the updated files.'
Write-Host 'Your existing profile was not modified by this script.'
