param(
    [string[]]$Workspace = @((Get-Location).Path),
    [string]$ProfileDir = '',
    [string]$RepoRoot = '',
    [string]$Python = '',
    [string[]]$PipArgs = @(),
    [string]$T2lPath = '',
    [string]$PandocPath = '',
    [string]$Sv2vPath = '',
    [string]$TypstPath = '',
    [string]$NushellPath = '',
    [switch]$SkipPackageInstall,
    [switch]$SkipSetup,
    [switch]$NoProfileInstall,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

# Windows PowerShell 5.1 may still negotiate TLS 1.0 by default, which GitHub
# rejects. Setting this here is harmless on newer PowerShell versions.
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
} catch {
}

function Write-Step([string]$Message) {
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Resolve-RepositoryRoot {
    if ($RepoRoot) {
        $candidate = (Resolve-Path -LiteralPath $RepoRoot -ErrorAction Stop).Path
        if (-not (Test-Path -LiteralPath (Join-Path $candidate 'integrations\deepseek_harness\setup.ps1') -PathType Leaf)) {
            throw "RepoRoot does not look like the Rosetta repository: $candidate"
        }
        return $candidate
    }

    $candidates = @()
    if ($PSScriptRoot) {
        $candidates += $PSScriptRoot
        $candidates += (Split-Path -Parent $PSScriptRoot)
    }
    $candidates += (Get-Location).Path
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath (Join-Path $candidate 'integrations\deepseek_harness\setup.ps1') -PathType Leaf)) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }

    Write-Step 'Downloading Rosetta from GitHub'
    $tempRoot = Join-Path $env:TEMP ("rosetta-install-" + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Force -Path $tempRoot | Out-Null
    $zip = Join-Path $tempRoot 'rosetta.zip'
    $url = 'https://github.com/yjyxfcy/nothing/archive/refs/heads/main.zip'
    Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $zip
    Expand-Archive -LiteralPath $zip -DestinationPath $tempRoot -Force
    $downloaded = @(Get-ChildItem -LiteralPath $tempRoot -Directory | Where-Object {
        Test-Path -LiteralPath (Join-Path $_.FullName 'integrations\deepseek_harness\setup.ps1') -PathType Leaf
    })
    if ($downloaded.Count -ne 1) {
        throw "The downloaded archive did not contain the expected Rosetta repository layout: $url"
    }
    return $downloaded[0].FullName
}

function Resolve-ProfileDirectory {
    if ($ProfileDir) {
        $resolved = (Resolve-Path -LiteralPath $ProfileDir -ErrorAction Stop).Path
        if (-not (Test-Path -LiteralPath (Join-Path $resolved 'package.json') -PathType Leaf)) {
            throw "ProfileDir does not contain a DSH profile package.json: $resolved"
        }
        return $resolved
    }

    $profilesRoot = Join-Path $env:USERPROFILE '.dsh\profiles'
    $desktop = Join-Path $profilesRoot 'desktop'
    if (Test-Path -LiteralPath (Join-Path $desktop 'package.json') -PathType Leaf) {
        return (Resolve-Path -LiteralPath $desktop).Path
    }
    $found = @(Get-ChildItem -LiteralPath $profilesRoot -Directory -ErrorAction SilentlyContinue | Where-Object {
        Test-Path -LiteralPath (Join-Path $_.FullName 'package.json') -PathType Leaf
    })
    if ($found.Count -eq 1) { return $found[0].FullName }
    throw "Could not identify the DSH profile. Pass -ProfileDir <path-to-profile> (for example C:\Users\<you>\.dsh\profiles\desktop)."
}

function Assert-HarnessCanBeUpdated([switch]$AllowRunning) {
    $running = @(Get-Process -ErrorAction SilentlyContinue | Where-Object {
        $_.ProcessName -like 'DeepSeek*' -or $_.ProcessName -like '*dsh*'
    })
    if ($running.Count -gt 0 -and -not $AllowRunning) {
        $names = ($running | ForEach-Object { $_.ProcessName } | Sort-Object -Unique) -join ', '
        throw "DeepSeek Harness is running ($names). Close it before installing, or rerun with -Force. The plugin only loads after a restart."
    }
}

function Install-BundleIntoProfile([string]$ProfilePath, [string]$BundlePath, [string]$ProfileEditor) {
    $packagePath = Join-Path $ProfilePath 'package.json'
    $lockPath = Join-Path $ProfilePath 'pnpm-lock.yaml'
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $backup = Join-Path $ProfilePath ".plugin-manager\rosetta-install-backup-$stamp"
    New-Item -ItemType Directory -Force -Path $backup | Out-Null
    Copy-Item -LiteralPath $packagePath -Destination (Join-Path $backup 'package.json') -Force
    if (Test-Path -LiteralPath $lockPath) {
        Copy-Item -LiteralPath $lockPath -Destination (Join-Path $backup 'pnpm-lock.yaml') -Force
    }

    $pnpm = Get-Command pnpm -ErrorAction SilentlyContinue
    if (-not $pnpm) {
        throw "pnpm is required to register the plugin in the DSH profile but was not found on PATH. Install it with 'npm install -g pnpm', then rerun this installer, or use the in-app plugin_manager with target '$BundlePath'."
    }
    $node = Get-Command node -ErrorAction SilentlyContinue
    if (-not $node) {
        throw "Node.js is required to update the DSH profile but was not found on PATH. Install Node.js, then rerun this installer."
    }

    try {
        # A small Node helper edits the JSON with the same 2-space formatting as
        # the Harness profile; Windows PowerShell's ConvertTo-Json rewrites the
        # whole file with four-space indentation and creates noisy diffs.
        & $node.Source $ProfileEditor $packagePath $BundlePath
        if (-not $? -or ($LASTEXITCODE -ne 0 -and $null -ne $LASTEXITCODE)) {
            throw "Could not update the DSH profile manifest: $packagePath"
        }

        # Create the directory junction directly. pnpm normally does this, but
        # a plain user account without Developer Mode cannot create symlinks;
        # a junction needs no elevation and keeps the profile's existing tree
        # untouched. pnpm is then asked only to update the lockfile.
        $linkParent = Join-Path $ProfilePath 'node_modules\@local'
        New-Item -ItemType Directory -Force -Path $linkParent | Out-Null
        $linkPath = Join-Path $linkParent 'dsh-rosetta-compat'
        if (Test-Path -LiteralPath $linkPath) {
            $existing = Get-Item -LiteralPath $linkPath -Force
            if ($existing.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
                [System.IO.Directory]::Delete($linkPath, $false)
            } else {
                Remove-Item -LiteralPath $linkPath -Recurse -Force
            }
        }
        New-Item -ItemType Junction -Path $linkPath -Target $BundlePath | Out-Null

        Write-Step "Updating the profile lockfile in $ProfilePath"
        & $pnpm.Source --dir $ProfilePath install --lockfile-only
        if (-not $? -or ($LASTEXITCODE -ne 0 -and $null -ne $LASTEXITCODE)) {
            throw "pnpm install --lockfile-only failed in $ProfilePath (exit code $LASTEXITCODE)."
        }

        $linked = Join-Path $ProfilePath 'node_modules\@local\dsh-rosetta-compat\package.json'
        if (-not (Test-Path -LiteralPath $linked -PathType Leaf)) {
            throw "pnpm finished but the linked package is missing: $linked"
        }
        Write-Host "Registered @local/dsh-rosetta-compat -> $BundlePath"
    } catch {
        Copy-Item -LiteralPath (Join-Path $backup 'package.json') -Destination $packagePath -Force
        if (Test-Path -LiteralPath (Join-Path $backup 'pnpm-lock.yaml')) {
            Copy-Item -LiteralPath (Join-Path $backup 'pnpm-lock.yaml') -Destination $lockPath -Force
        }
        throw
    }
}

$repoRoot = Resolve-RepositoryRoot
$setup = Join-Path $repoRoot 'integrations\deepseek_harness\setup.ps1'
$profileEditor = Join-Path $repoRoot 'integrations\deepseek_harness\update_profile.mjs'
$bundle = Join-Path $env:LOCALAPPDATA 'Rosetta\Harness\plugin-current'

Assert-HarnessCanBeUpdated -AllowRunning:$Force

if (-not $SkipSetup) {
    Write-Step 'Preparing the Rosetta Python environment and Harness bundle'
    $setupParams = @{ Workspace = @($Workspace) }
    if ($Python) { $setupParams.Python = $Python }
    if ($PipArgs.Count -gt 0) { $setupParams.PipArgs = $PipArgs }
    if ($T2lPath) { $setupParams.T2lPath = $T2lPath }
    if ($PandocPath) { $setupParams.PandocPath = $PandocPath }
    if ($Sv2vPath) { $setupParams.Sv2vPath = $Sv2vPath }
    if ($TypstPath) { $setupParams.TypstPath = $TypstPath }
    if ($NushellPath) { $setupParams.NushellPath = $NushellPath }
    if ($SkipPackageInstall) { $setupParams.SkipPackageInstall = $true }
    & $setup @setupParams
    if (-not $? -or ($LASTEXITCODE -ne 0 -and $null -ne $LASTEXITCODE)) {
        throw "Rosetta setup failed (exit code $LASTEXITCODE)."
    }
}

if (-not (Test-Path -LiteralPath (Join-Path $bundle 'package.json') -PathType Leaf)) {
    throw "The Rosetta bundle was not created at $bundle. Run setup.ps1 first, or rerun without -SkipSetup."
}

if ($NoProfileInstall) {
    Write-Host ""
    Write-Host "Bundle ready at: $bundle"
    Write-Host "Register it in DeepSeek Harness with:"
    Write-Host "  plugin_manager { action: `"install_bundle`", target: `"$bundle`" }"
} else {
    $profilePath = Resolve-ProfileDirectory
    Install-BundleIntoProfile -ProfilePath $profilePath -BundlePath $bundle -ProfileEditor $profileEditor
    Write-Host ""
    Write-Host "Rosetta is installed for: $($Workspace -join ', ')"
    Write-Host "Restart DeepSeek Harness, open a chat in that project, and run ros_doctor."
}

Write-Host ""
Write-Host "Machine-local setup lives under $env:LOCALAPPDATA\Rosetta\Harness."
Write-Host "Re-run this installer after updating the repository to refresh the bundle."
