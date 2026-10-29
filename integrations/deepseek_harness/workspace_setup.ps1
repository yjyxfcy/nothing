function Resolve-RosettaWorkspaceRoots {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Workspace
    )

    if (-not $Workspace -or @($Workspace | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }).Count -ne $Workspace.Count) {
        throw 'At least one non-empty workspace path is required.'
    }

    $resolvedRoots = [System.Collections.Generic.List[string]]::new()
    $seen = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
    foreach ($requestedPath in $Workspace) {
        try {
            $resolvedInfo = Resolve-Path -LiteralPath $requestedPath -ErrorAction Stop
            if ($resolvedInfo.Provider.Name -ne 'FileSystem') {
                throw 'Workspace must be a filesystem directory.'
            }
            $resolvedPath = $resolvedInfo.ProviderPath
            $item = Get-Item -LiteralPath $resolvedPath -Force -ErrorAction Stop
        } catch {
            throw "Workspace must be an existing directory: $requestedPath"
        }
        if (-not $item.PSIsContainer) {
            throw "Workspace must be an existing directory: $resolvedPath"
        }

        $canonicalPath = [System.IO.Path]::GetFullPath($resolvedPath)
        $pathRoot = [System.IO.Path]::GetPathRoot($canonicalPath)
        if ($canonicalPath.Length -gt $pathRoot.Length) {
            $canonicalPath = $canonicalPath.TrimEnd([char[]]@('\', '/'))
        }
        if ($seen.Add($canonicalPath)) {
            $resolvedRoots.Add($canonicalPath)
        }
    }

    if ($resolvedRoots.Count -eq 0) {
        throw 'At least one existing workspace directory is required.'
    }
    return $resolvedRoots.ToArray()
}

function Initialize-RosettaWorkspaceState {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$WorkspaceRoots
    )

    foreach ($workspaceRoot in $WorkspaceRoots) {
        $stateDirectory = Join-Path $workspaceRoot '.rosetta'
        if (Test-Path -LiteralPath $stateDirectory) {
            $stateItem = Get-Item -LiteralPath $stateDirectory -Force
            if (($stateItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                Write-Warning "Workspace state path is a link; setup will not write through it: $stateDirectory"
                continue
            }
            if (-not $stateItem.PSIsContainer) {
                throw "Workspace state path exists but is not a directory: $stateDirectory"
            }
        } else {
            New-Item -ItemType Directory -Path $stateDirectory | Out-Null
            # Recheck after creation so a concurrently substituted reparse point
            # is never used as the parent of the ignore-file write.
            $stateItem = Get-Item -LiteralPath $stateDirectory -Force
            if (($stateItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                Write-Warning "Workspace state path became a link; setup will not write through it: $stateDirectory"
                continue
            }
        }

        $stateIgnore = Join-Path $stateDirectory '.gitignore'
        $stateIgnoreItem = Get-Item -LiteralPath $stateIgnore -Force -ErrorAction SilentlyContinue
        if ($stateIgnoreItem) {
            if (($stateIgnoreItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                Write-Warning "Local-state ignore path is a link; setup will not write through it: $stateIgnore"
            } else {
                Write-Host "Keeping existing local-state ignore file unchanged: $stateIgnore"
                Write-Host 'Verify that it excludes Rosetta sessions, activity, and recovery data before publishing this workspace.'
            }
            continue
        }

        $ignoreText = "# Rosetta local sessions, activity, and recovery data may contain artifact text.`n/*`n!/.gitignore`n"
        $ignoreBytes = [System.Text.UTF8Encoding]::new($false).GetBytes($ignoreText)
        $ignoreStream = $null
        try {
            $ignoreStream = [System.IO.File]::Open($stateIgnore, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
            $ignoreStream.Write($ignoreBytes, 0, $ignoreBytes.Length)
            Write-Host "Created local-state ignore file: $stateIgnore"
        } catch [System.IO.IOException] {
            if ($ignoreStream) { throw }
            $stateIgnoreItem = Get-Item -LiteralPath $stateIgnore -Force -ErrorAction SilentlyContinue
            if ($stateIgnoreItem) {
                Write-Warning "Local-state ignore path appeared during setup; it was left unchanged: $stateIgnore"
            } else {
                throw
            }
        } finally {
            if ($ignoreStream) { $ignoreStream.Dispose() }
        }
    }
}

function Get-RosettaWorkspaceYamlLines {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$WorkspaceRoots
    )

    $lines = @('        workspaceRoots:')
    foreach ($root in $WorkspaceRoots) {
        $quoted = "'" + $root.Replace("'", "''") + "'"
        $lines += "          - $quoted"
    }
    return $lines
}

function Resolve-RosettaConverterSettings {
    [CmdletBinding()]
    param(
        [hashtable]$ExplicitPaths = @{},
        [hashtable]$PathCommands = @{},
        [hashtable]$SavedPaths = @{}
    )

    $resolved = @{}
    $known = @('t2l', 'pandoc', 'sv2v', 'typst', 'nu')
    $unknown = @($ExplicitPaths.Keys | Where-Object { $_ -notin $known })
    if ($unknown.Count -gt 0) {
        throw "Unknown converter name: $($unknown[0]). Supported names: $($known -join ', ')."
    }

    foreach ($name in $known) {
        $candidate = $null
        if ($ExplicitPaths.ContainsKey($name)) {
            $candidate = [string]$ExplicitPaths[$name]
            if ([string]::IsNullOrWhiteSpace($candidate) -or -not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
                throw "The explicit $name converter path is not an existing file: $candidate"
            }
        } else {
            if ($PathCommands.ContainsKey($name)) {
                $command = $PathCommands[$name]
                if ($command -is [string]) { $candidate = $command }
                elseif ($command -and $command.Source) { $candidate = [string]$command.Source }
            }
            if ((-not $candidate -or -not (Test-Path -LiteralPath $candidate -PathType Leaf)) -and $SavedPaths.ContainsKey($name)) {
                $candidate = [string]$SavedPaths[$name]
            }
        }

        if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            $resolved[$name] = (Resolve-Path -LiteralPath $candidate -ErrorAction Stop).Path
        }
    }
    return $resolved
}
